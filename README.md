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

**USB DACs that come and go.** Many USB DACs drop off the bus when switched off or to another input.
`docker-compose.yaml` bind-mounts the host's `/dev/snd` (with a cgroup rule for ALSA devices) instead of
passing `devices: /dev/snd`, which would copy the device nodes once at start. That way a DAC that reappears
is usable right away. Name the card (`hw:CARD=N05XD`), because its number can change when it reappears.
If the DAC is missing when playback starts (some DACs are off the bus while they start up), Shairport Sync
waits up to `ALSA_DEVICE_WAIT` seconds (default 30) for it to appear, then starts playing. That wait comes
from a patch (`patches/shairport-sync/`): upstream fails at once and then crashes on the error. If the DAC
still isn't there, Shairport Sync exits, the container restarts and the AirPlay session ends. If the DAC
disappears during playback, Shairport Sync doesn't exit: `run_this_if_an_unfixable_error_is_detected` in
`config/shairport-sync.conf` points at a handler that only logs, and a second patch closes the dead device so
it's reopened as soon as the DAC is back. Sound returns without reconnecting. On a desktop host,
also keep PipeWire/PulseAudio off the DAC: if they hold it open, Shairport Sync can't use it.

Shairport Sync picks the deepest sample format the device offers. Some USB DACs advertise a 32-bit mode
that doesn't actually work: the DAC sees no signal. `cat /proc/asound/cardN/stream0` lists the formats;
force one that works with e.g. `ALSA_OUTPUT_FORMAT: "S24_3LE"` (24-bit packed) in the container's
environment.

If the build is slow to download packages, pick a nearby mirror from
[mirrors.alpinelinux.org](https://mirrors.alpinelinux.org). The default CDN can be very slow from some
networks. Pass it as a build argument, e.g. in a `docker-compose.override.yaml`:

```yaml
services:
  shairport-sync:
    build:
      args:
        ALPINE_MIRROR: https://mirror.twds.com.tw/alpine
        # The volume bridge's Python packages come from PyPI; a mirror can be set the same way:
        # PIP_INDEX_URL: https://pypi.tuna.tsinghua.edu.cn/simple
```

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
reconnects automatically, can poll, and paces commands. For serial servers in telnet mode instead of raw TCP,
set `protocol = "telnet"`: the connection then goes through [telnetlib3](https://github.com/jquast/telnetlib3),
which handles telnet's escaping and option negotiation in 8-bit binary mode. RFC 2217 line control isn't used,
so set the baud rate on the server.

**Built-in serial multiplexer (optional).** The container includes [ser2net](https://github.com/cminyard/ser2net).
It's off by default; set `ENABLE_SER2NET: "1"` to turn it on. ser2net holds the single connection to the
amplifier, and any number of TCP clients share it: the bridge, and e.g. Home Assistant or a control app.
Everything the amplifier sends goes to every client. What a client sends goes only to the amplifier. Set it up
in `config/ser2net.yaml`. Its *connector* is either a USB-RS232 adapter on this host (pass it in with
`devices:` in `docker-compose.yaml`) or a network serial server that accepts only one client. Its *accepter*
is the port clients connect to: `127.0.0.1:4001` by default, or `0.0.0.0` for other machines. There's no
authentication, so anyone who can reach that port can control the amplifier. Then point the bridge at
`host = "127.0.0.1"`, `port = 4001`. Leave `own_commands_echoed = false`: ser2net doesn't send one client's
commands to the others.

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

Custom backends can also implement `send_command()` and `refresh()` so that hooks can use them (see below).

### Hooks: waking equipment on connect

The `[hooks]` section runs actions when something happens:

| Event | When |
| --- | --- |
| `on_connect` | A source connects to this speaker (the first one, if several do) |
| `on_disconnect` | The last source disconnects |
| `on_session_start` | Playback starts (Shairport Sync's "active" state) |
| `on_session_end` | `active_state_timeout` after playback stops |
| `on_output_lost` | The DAC disappears from the system while audio is playing (not paused) |

Instead of a list of actions, an event can name another event to run the same actions. For example,
`on_output_lost = "on_connect"` runs the power-up procedure again when the DAC drops out during playback.
The bridge checks every 0.5 s whether the ALSA card in `ALSA_DEVICE` (e.g. `hw:CARD=N05XD`) is listed under
`/sys/class/sound` (Docker hides `/proc/asound` inside containers). It runs the actions once per dropout. If the DAC flickers while they're still running, they
aren't queued again. Shairport Sync reopens the DAC once it's back, so sound returns without reconnecting
(see "USB DACs that come and go"). The shell actions get `VOLUME_BRIDGE_EVENT=output_lost`, so a Home
Assistant automation can tell this apart from a connect.

Without a pause, the music that plays while the DAC is gone is skipped, because playback keeps following the
source's clock. Set `pause_while_output_lost = true` under `[shairport]` to pause the source (the phone)
during the dropout instead. The bridge resumes it once the DAC is back, the `on_output_lost` actions have
finished (e.g. an amp warming up) and `output_resume_delay` seconds (default 2) have passed. It resumes only
what it paused: not if the source disconnected, or was played again by hand, in the meantime. If the DAC drops
again before then, the source stays paused. This uses AirPlay remote control, which Shairport Sync marks as
experimental. Some apps (live radio) resume at the live point rather than where they paused.

Each event runs its actions in order, or several at once with `parallel`. If an action fails, the bridge logs
it and runs the next one. Events are handled one at a time, so a disconnect's actions never overtake the
connect's.

| Action | Does |
| --- | --- |
| `{ send = "PWR 1" }` | Sends a command over the amplifier's connection (the same serial port or serial server), wrapped like the backend's own commands. Add `raw = true` to send it byte for byte, e.g. for another device on a shared port. |
| `{ shell = "...", timeout = 30 }` | Runs a command with `/bin/sh -c`, e.g. `curl` to Home Assistant's REST API. It gets `VOLUME_BRIDGE_EVENT`, `VOLUME_BRIDGE_SOURCE` (the source's address), `VOLUME_BRIDGE_AIRPLAY_VOLUME`, `VOLUME_BRIDGE_AMPLIFIER_LEVEL` and `VOLUME_BRIDGE_AMPLIFIER_MUTED` in its environment, plus the container's environment. On timeout, the command and everything it started are killed. |
| `{ delay = 3 }` | Waits, e.g. while equipment powers up. |
| `{ sync = "source" }` | Re-reads the amplifier's state, then sends the source's volume to it, even if they already seem to agree (an amp in standby ignores commands). `"amplifier"` sends the amplifier's volume to the source instead. Until an event's first `sync` has run, the bridge holds volume sync in both directions (see below). |
| `{ parallel = [A, [B, C]] }` | Runs branches at the same time and waits for all of them. A branch is one action, or a list of actions run in order. |
| `{ if_amplifier_off = [A, B] }` | Runs its actions only if the amplifier says it's off, e.g. power on plus a warm-up delay that's skipped when the amp is already on. The backend asks with `power_query_command` and reads the answer with `power_pattern` (see the tcp-serial options). If the power state can't be determined, the actions run anyway. |

For example, when a phone connects, power on a McIntosh MA352 if it's off (it takes 15-20 seconds to warm up;
an amp that's already on gets the phone's volume right away), and at the same time call a Home Assistant webhook (an automation with a Webhook trigger) to switch on the rest
of the system:

```toml
[hooks]
on_connect = [
  { parallel = [
    [ { if_amplifier_off = [ { send = "PWR 1" }, { delay = 20 } ] }, { sync = "source" } ],
    { shell = 'curl -sf -m 10 -X POST -H "Content-Type: application/json" -d "{\"event\": \"$VOLUME_BRIDGE_EVENT\"}" http://192.168.1.10:8123/api/webhook/airplay-living-room' },
  ] },
]
```

While the amp warms up, the bridge holds volume sync. It sends no volume commands that the amp would reject,
and it doesn't push the sleeping amp's stale volume to the phone, so the slider doesn't snap back if you move it.
The `sync` then applies the slider's latest position, and two-way sync resumes. Events without a `sync` don't
hold anything.

Webhooks need no token. Home Assistant's REST API does: pass a long-lived access token in
`docker-compose.yaml` as e.g. `HA_TOKEN`, and send it as `-H "Authorization: Bearer $HA_TOKEN"` to
`/api/services/<domain>/<service>`. `config/mcintosh-ma352.toml` and `config/volume-bridge.toml` have
complete examples.

Longer logic belongs in a script. Mount it into the container (e.g. `./hooks:/opt/hooks:ro`) and call it
with `shell = "/opt/hooks/wake.sh"`. The image has `curl`, `bash` and `python3`. Use IP addresses rather
than `.local` names, because the container doesn't resolve mDNS names. Hook commands run as the
unprivileged `shairport-sync` user.

`on_connect`, `on_disconnect` and `on_output_lost` need the metadata socket described under Caveats. Without
it, only the session events fire.

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
