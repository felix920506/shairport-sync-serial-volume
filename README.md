# shairport-sync-serial-volume

An AirPlay 2 receiver container ([Shairport Sync](https://github.com/mikebrady/shairport-sync)) that:

1. supports **AirPlay 2 with multi-room** (via NQPTP);
2. passes the decoded audio **straight to an ALSA device**: no volume scaling, mixer, resampler, mixdown or DSP;
3. moves **volume control to an external amplifier**, in both directions: the AirPlay slider sets the amplifier, and changing the volume on the amplifier moves the slider on the iPhone/Mac/Apple TV.

```
 AirPlay source ──audio──▶ shairport-sync ──PCM (unchanged)──▶ ALSA hw:X ──▶ DAC/amp input
      ▲   │                     │  ▲
      │   └─volume──▶ (D-Bus) ──┘  │ SetAirplayVolume
      │                  │         │
      └──volume notify───┘   volume-bridge ◀──▶ amplifier backend ──TCP──▶ serial server ──RS-232──▶ amp
```

## Quick start

```sh
docker compose up --build
```

Edit `docker-compose.yaml` first: set `ALSA_DEVICE` (`aplay -l` on the host lists cards; use `hw:CARD=...`)
and `SPS_NAME`. Host networking and `/dev/snd` are required.

The default config uses the **stub amplifier**, which simulates an amplifier in memory. To act as the
amplifier's front panel:

```sh
nc <docker-host> 7001
set 35        # turn the "knob": the AirPlay slider on your device moves
up / down
mute on
get
```

Container logs show each direction (`airplay volume -12.00 -> amplifier`,
`amplifier level 35 -> airplay volume -19.50`).

## Audio path: what "no processing" means here

`config/shairport-sync.conf` sets:

| Setting | Effect |
| --- | --- |
| `ignore_volume_control = "yes"` | Audio is always at 0 dB. No attenuation, so no dither. The AirPlay volume is still tracked and published. |
| no `mixer_control_name` | The ALSA hardware mixer is never touched. |
| `output_rate/format/channels = "auto"` | Shairport Sync opens the device in the stream's native format (44.1/48 kHz, 16/24/32-bit) when the hardware supports it. |
| `output_device = "hw:…"` | Use `hw:`, not `plughw:`/`default`, so ALSA's plug layer can't convert behind your back. |
| `mixdown = "off"` | Multichannel streams are not folded to stereo. |
| not built with soxr/convolution/loudness | No resampling or DSP code is present. |
| `interpolation = "basic"` | See below. |

Decoding is the only transformation: AirPlay 2 delivers ALAC (lossless) or AAC (in some
modes, e.g. buffered audio from Apple Music), decoded with FFmpeg to PCM.

**Sync correction.** Multi-room playback needs this device's DAC clock to follow the AirPlay timeline.
Shairport Sync corrects drift by occasionally adding or dropping a frame. `basic` does exactly that
and leaves all other samples unchanged. The alternatives (`vernier`, `soxr`) resample whole packets.
If you don't need tight sync, set `disable_synchronization = "yes"` in the `alsa` section; nothing
is ever inserted or dropped then.

## Volume bridge

`volume_bridge/` is a small Python service (asyncio + [dbus-fast](https://github.com/Bluetooth-Devices/dbus-fast)).
It talks to Shairport Sync over a D-Bus system bus that is private to the container:

- **AirPlay → amplifier:** watches `org.gnome.ShairportSync.RemoteControl.AirplayVolume`.
- **Amplifier → AirPlay:** calls `RemoteControl.SetAirplayVolume`. On AirPlay 2 this sends a volume
  notification to the source over the AirPlay 2 event channel. On classic AirPlay it uses DACP.

Shairport Sync is pinned to a commit on its **`development` branch** (see `SHAIRPORT_SYNC_REF` in the
`Dockerfile`), because that branch has two things this project needs:

- AirPlay 2 remote volume control. Upstream labels it *experimental*.
- With `ignore_volume_control`, the source's volume slider stays visible. On `master` it is hidden.

### Mapping

AirPlay's slider is mapped linearly onto `[amplifier.scale] min_level..max_level` and rounded to
`step`. `limit` is a safety ceiling: the bridge never sends a level above it, and the slider at 100%
just means "at the limit". `exponent` > 1 gives finer control at low volume for amplifiers with a
linear/percent scale. Leave it at 1 for dB scales.

With `use_amplifier_mute` and a backend that supports mute, pulling the slider to the bottom (AirPlay
mute) mutes the amplifier and keeps its level. Muting the amplifier mutes the source.

### Loop prevention

Two-way sync is prone to feedback loops. The bridge uses three mechanisms:

- **Common-domain comparison.** Both sides are compared as quantised amplifier levels. A change is
  forwarded only if the other side actually disagrees, so rounding differences never cause traffic.
- **Echo windows.** After the bridge commands one side, reports from that side are ignored for a short
  window (`source_echo_window`, `amplifier_echo_window`). This absorbs Shairport Sync's ramped AirPlay 2
  notifications and amplifiers that report intermediate steps. Each report with a *new* value restarts
  the window, up to `max_echo_window` in total. That way an amp that ramps slowly and reports every step
  can't outlast the window, and an amp that is polled and keeps repeating the same value can't hold it
  open. The amplifier's window closes as soon as it reports the commanded level. When a window closes,
  the bridge checks that side once more. If it ended up elsewhere (the user moved it meanwhile, or the
  device clamped or refused the value), the bridge follows it.
- **Coalescing.** While a send is in flight, only the most recent request is kept, so dragging a slider
  doesn't queue dozens of serial commands.

### Connecting the real amplifier

The built-in `tcp-serial` backend talks to the amplifier through a serial-to-Ethernet adapter, `ser2net` in
raw mode, or a serial multiplexer. The protocol is described in `config/volume-bridge.toml` (see the commented
example there): how messages are framed, command templates, and regexes that extract volume and mute. It
reconnects automatically, can poll, and paces commands.

**Framing.** Not every RS-232 protocol uses lines, so framing (splitting the byte stream into messages) is
separate from parsing:

| `framing` | Messages are... | Example |
| --- | --- | --- |
| `delimited` (default) | ended by any of `receive_terminators` | `MV45\r`, `VOL 10;` |
| `fixed` | exactly `frame_length` bytes; resynchronised after `frame_gap` of silence | 6-byte binary frames |
| `markers` | between `start_marker` and `end_marker`, plus `trailer_length` bytes | `STX V 0x28 ETX checksum` |
| `stream` | not delimited at all; found by scanning for the configured patterns | `MV41MV42MUON` |

With `stream`, a match that might still grow (`VOL\d+` before the last digit has arrived) is accepted after
`frame_gap` seconds of silence. Fixed-width patterns (`MV\d{2}`) don't wait. Bytes that match no pattern
are skipped.

Patterns match the whole message by default (`match = "full"`), which stops fragments from parsing as status
by accident. Use `match = "search"` when the value sits inside a longer message
(`@MAIN:PWR=On,VOL=-30,MUTE=Off`). For binary protocols, set `encoding = "latin-1"` (one character per byte)
and use `level_encoding = "byte"` or `"hex"`, `{level_chr}` in `set_level_command`, and `\u0002`-style escapes
in the TOML strings.

**Status updates from the amplifier.** Many amps send status out of the RS-232 port on their own when
their knob or IR remote is used, often one line per step. Every received line is treated as an
independent status report. The backend never assumes a line is the reply to the command it just sent.
So unsolicited updates simply flow through to the AirPlay slider, and you don't need polling
(`poll_interval = 0`). Messages may arrive split across reads or several to a read, and status that isn't
about volume or mute is ignored.

**Shared port / serial multiplexer.** When several programs share one physical RS-232 port and see each
other's traffic:

- *Other programs' volume changes* show up as amp status and move the AirPlay slider, as they should.
  Replies to *their* queries are status too, and are used.
- *Other programs' commands* are ignored when they can't be status. Copies of our own query commands
  (`MV?`) are skipped, and `ignore_patterns` covers commands that would otherwise look like status. With
  `framing = "stream"`, ignore patterns are also scanned for, so a command is consumed as a unit and not
  mined for status-looking pieces. Unframeable noise is dropped (`max_frame_length`).
- *Our own commands echoed back* by the multiplexer: set `own_commands_echoed = true`. Each command we send
  is run through the same framer, and the resulting message cancels one identical message received within
  `echo_timeout`. That way, if the amp clamps or refuses a
  value, our echoed `MV99` isn't mistaken for the amp being at 99. Leave it `false` if your multiplexer
  doesn't echo to the sender, or the amp's real confirmation could be dropped when it has the same text.
- Each command goes out in a single write and commands are paced (`command_interval`), so we don't
  split a command across another program's traffic or flood the shared line. Keep polling off if the
  amp pushes updates.

On protocols where commands and status are identical text (`MV30` either way), another program's
command is briefly taken as status before the amp's own reply arrives. That's harmless: the amp's reply
corrects it if the amp clamps or refuses the value.

Checksums, handshakes and other protocol details go in a subclass. Override `encode_command`,
`verify_frame`, `parse_message`, `create_framer` (custom framing, a subclass of
`volume_bridge.amplifiers.framing.Framer`) or `open_transport` (e.g. a local `/dev/tty*`). For example, for
`STX <cmd> <value> ETX <checksum>`:

```python
# amplifiers/my_amp.py
from volume_bridge.amplifiers.tcp_serial import TcpSerialAmplifier

class MyAmplifier(TcpSerialAmplifier):
    def encode_command(self, command):
        body = command.encode("latin-1")
        return b"\x02" + body + b"\x03" + bytes([sum(body) & 0xFF])

    def verify_frame(self, frame):           # frame.raw includes markers + trailer
        return frame.raw[-1] == sum(frame.payload) & 0xFF
```
```toml
[amplifier.options]
encoding = "latin-1"
framing = "markers"
start_marker = "\u0002"
end_marker = "\u0003"
trailer_length = 1
set_level_command = "V{level_chr}"
level_pattern = "V(?P<level>.)"
level_encoding = "byte"
```

Or subclass `volume_bridge.amplifiers.base.Amplifier` directly and implement `start`, `stop`,
`set_level` (and optionally `set_mute` with `supports_mute = True`). Call `self.report(AmplifierState(...))`
whenever you learn the amplifier's state, including echoes of your own commands. If the amplifier never
acknowledges commands, report the commanded state yourself.

Then mount the module and point the config at it:

```yaml
    volumes:
      - ./amplifiers:/opt/amplifiers:ro
    environment:
      PYTHONPATH: /opt/amplifiers
```
```toml
[amplifier]
type = "my_amp:MyAmplifier"
```

### Developing the bridge

```sh
cd volume_bridge
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

## Caveats

- **Run one AirPlay 2 receiver per host** unless you configure separate NQPTP interfaces. If the host
  already runs `avahi-daemon` or `nqptp`, set `ENABLE_AVAHI=0` or `ENABLE_NQPTP=0`. With `ENABLE_AVAHI=0`
  you must also make the host's Avahi reachable over D-Bus, so the default (in-container Avahi) is
  simpler.
- **The volume can only be pushed to a connected source.** The bridge pushes amplifier changes while a
  source is connected to this speaker, even if nothing is playing. D-Bus doesn't say whether one is
  connected, so the bridge listens for Shairport Sync's UDP metadata on `127.0.0.1:5555` (`socket_address`
  / `socket_port` in `config/shairport-sync.conf`, `metadata_port` in the bridge config), which announces
  each connect and disconnect. With no source connected the bridge leaves the source alone: Shairport Sync
  would drop the change anyway, and its stored volume is stale. When the next session starts, the source's
  volume wins and the amplifier follows it. This is also what keeps the amp from jumping to Shairport Sync's
  idle default (0 dB = full scale) after a restart. Without the metadata socket the bridge falls back to
  Shairport Sync's "active" state, which begins when play starts and ends `active_state_timeout` (raised to
  15 minutes here) after play stops.
- **In a multi-room group**, a volume change from the amplifier sets *this speaker's* volume in the
  group. The group's master volume on the source adjusts the way it does when you move one speaker's slider.
- AirPlay 2 remote control in Shairport Sync is experimental upstream. If an update breaks it, pin
  `SHAIRPORT_SYNC_REF` back to the commit in this repo.
