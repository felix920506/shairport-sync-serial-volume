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
  notifications and amplifiers that report intermediate steps. When the window closes, the bridge checks
  that side once more. If it ended up elsewhere (the user moved it meanwhile, or the device clamped
  or refused the value), the bridge follows it.
- **Coalescing.** While a send is in flight, only the most recent request is kept, so dragging a slider
  doesn't queue dozens of serial commands.

### Connecting the real amplifier

The built-in `tcp-line` backend fits typical RS-232 text protocols (`MV45\r`, `MV?\r` → `MV45\r`)
reached through a serial-to-Ethernet adapter or `ser2net` in raw mode. It is driven by templates and
regexes in `config/volume-bridge.toml`; see the commented example there. It reconnects automatically,
can poll, and paces commands.

For anything else (binary frames, checksums, login handshakes, a local `/dev/tty*`), write a backend:

```python
# amplifiers/my_amp.py
from volume_bridge.amplifiers.tcp_line import TcpLineAmplifier

class MyAmplifier(TcpLineAmplifier):
    def encode_set_level(self, level):
        body = bytes([0x02, 0x10, int(level)])
        return body + bytes([sum(body) & 0xFF])

    async def read_message(self, reader):
        frame = await reader.readexactly(4)
        return frame.hex()

    def parse_message(self, message):
        frame = bytes.fromhex(message)
        if frame[1] == 0x10:
            return {"level": frame[2]}
        return None
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
- **The volume can only be pushed to a source during a session.** If the amplifier changes while nothing
  is playing, the source sends its own volume when it next connects, and the amplifier follows it.
- **In a multi-room group**, a volume change from the amplifier sets *this speaker's* volume in the
  group. The group's master volume on the source adjusts the way it does when you move one speaker's slider.
- AirPlay 2 remote control in Shairport Sync is experimental upstream. If an update breaks it, pin
  `SHAIRPORT_SYNC_REF` back to the commit in this repo.
