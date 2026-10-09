import asyncio
import re

import pytest

from volume_bridge.amplifiers.base import AmplifierState
from volume_bridge.amplifiers.framing import (
    DelimitedFramer,
    FixedLengthFramer,
    MarkerFramer,
    StreamFramer,
    create_framer,
)
from volume_bridge.amplifiers.tcp_serial import TcpSerialAmplifier

from test_amplifiers import wait_for


def payloads(frames):
    return [f.payload for f in frames]


def feed_bytewise(framer, data):
    out = []
    for i in range(len(data)):
        out += framer.feed(data[i:i + 1])
    return out


# -- delimited ---------------------------------------------------------------------

def test_delimited_mixed_terminators_and_splits():
    f = DelimitedFramer(b"\r\n")
    assert payloads(f.feed(b"MV1")) == []
    assert payloads(f.feed(b"2\r\nMV13\n\nMV14\r")) == [b"MV12", b"MV13", b"MV14"]


def test_delimited_custom_terminator():
    f = DelimitedFramer(b";")
    assert payloads(f.feed(b"VOL 10;MUTE OFF;")) == [b"VOL 10", b"MUTE OFF"]


# -- fixed --------------------------------------------------------------------------

def test_fixed_length():
    f = FixedLengthFramer(4)
    assert payloads(feed_bytewise(f, b"\x01\x02\x03\x04\x05\x06\x07\x08\x09")) == [
        b"\x01\x02\x03\x04", b"\x05\x06\x07\x08"]
    assert f.pending
    assert f.flush() == []  # silence: the stray byte is dropped, so frames resync
    assert payloads(f.feed(b"ABCD")) == [b"ABCD"]


# -- markers ------------------------------------------------------------------------

def test_markers_with_trailer_and_noise():
    f = MarkerFramer(b"\x02", b"\x03", trailer_length=1)
    frames = feed_bytewise(f, b"junk\x02V\x28\x03\xaa\x02M\x01\x03\xbb")
    assert payloads(frames) == [b"V\x28", b"M\x01"]
    assert frames[0].raw == b"\x02V\x28\x03\xaa"


def test_markers_multibyte_start_split_across_reads():
    f = MarkerFramer(b"@@", b"#")
    assert f.feed(b"xx@") == []
    assert payloads(f.feed(b"@VOL=5#")) == [b"VOL=5"]


def test_markers_start_only_ends_at_next_start_or_silence():
    f = MarkerFramer(b"!", b"")
    assert payloads(f.feed(b"!1VOL20!1MUT0")) == [b"1VOL20"]
    assert payloads(f.flush()) == [b"1MUT0"]


def test_markers_end_only():
    f = MarkerFramer(b"", b"\xff")
    assert payloads(f.feed(b"\x10\x20\xff\x11\x00\xff")) == [b"\x10\x20", b"\x11\x00"]


# -- stream (no delimiters) ---------------------------------------------------------

def stream(*patterns, gap=None):
    return StreamFramer([re.compile(p) for p in patterns], gap=gap)


def test_stream_back_to_back_messages():
    f = stream(rb"MV\d{2}", rb"MU(ON|OFF)")
    assert payloads(f.feed(b"MV45MUONMV4")) == [b"MV45", b"MUON"]
    assert payloads(f.feed(b"6")) == []  # MV46 could still grow: wait
    assert payloads(f.feed(b"MUOFF")) == [b"MV46"]
    assert payloads(f.flush()) == [b"MUOFF"]


def test_stream_skips_unknown_bytes_and_other_traffic():
    f = stream(rb"MV\d{2}")
    assert payloads(f.feed(b"PWON\x00SI?MV33Z2ONMV34X")) == [b"MV33", b"MV34"]


def test_stream_variable_width_waits_for_silence():
    f = stream(rb"VOL\d+")
    assert payloads(feed_bytewise(f, b"VOL1")) == []
    assert payloads(f.feed(b"00")) == []
    assert payloads(f.flush()) == [b"VOL100"]


def test_stream_prefers_earliest_then_longest():
    f = stream(rb"V\d", rb"V\d\d")
    assert payloads(f.feed(b"V12x")) == [b"V12"]


def test_stream_bounds_buffer():
    f = StreamFramer([re.compile(rb"MV\d{2}")], max_length=16)
    f.feed(b"x" * 100)
    assert len(f.buffer) <= 16


def test_frames_of_does_not_disturb_state():
    f = DelimitedFramer(b"\r")
    f.feed(b"MV4")
    assert payloads(f.frames_of(b"MV50\r")) == [b"MV50"]
    assert payloads(f.feed(b"5\r")) == [b"MV45"]


@pytest.mark.parametrize("options, error", [
    ({"framing": "fixed"}, "frame_length"),
    ({"framing": "markers"}, "marker"),
    ({"framing": "bogus"}, "unknown framing"),
])
def test_create_framer_errors(options, error):
    with pytest.raises(ValueError, match=error):
        create_framer(options, "ascii", [])


# -- end to end through TcpSerialAmplifier ------------------------------------------

class RawPort:
    """A serial server: captures what clients send; lets the test push raw
    bytes; optionally echoes clients' bytes back (a multiplexer)."""

    def __init__(self, echo=False):
        self.echo = echo
        self.writers = []
        self.received = b""

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        self.writers.append(writer)
        while chunk := await reader.read(100):
            self.received += chunk
            if self.echo:
                writer.write(chunk)

    def push(self, data):
        for w in self.writers:
            w.write(data)

    async def close(self):
        for w in self.writers:
            w.close()
        self.server.close()


@pytest.fixture
async def port():
    ports = []

    async def make(**kw):
        p = RawPort(**kw)
        ports.append((p, await p.start()))
        return ports[-1]

    yield make
    for p, _ in ports:
        await p.close()


async def start_amp(port_number, **options):
    amp = TcpSerialAmplifier({"host": "127.0.0.1", "port": port_number, "command_interval": 0,
                              **options})
    states = []
    amp.add_listener(states.append)
    await amp.start()
    return amp, states


async def test_undelimited_stream_protocol(port):
    raw, number = await port()
    amp, states = await start_amp(
        number, framing="stream", command_suffix="",
        set_level_command="MV{level_int:02d}",
        level_pattern=r"MV(?P<level>\d{2})", mute_pattern=r"MU(?P<muted>ON|OFF)",
        mute_on_command="MUON", mute_off_command="MUOFF")
    try:
        await wait_for(lambda: raw.writers)
        raw.push(b"MV41MV42MV43MUON")  # knob spun, then mute, no delimiters at all
        await wait_for(lambda: amp.state == AmplifierState(43, True))
        assert [s.level for s in states] == [41, 42, 43, 43]
        await amp.set_level(50)
        await wait_for(lambda: raw.received.endswith(b"MV50"))
    finally:
        await amp.stop()


async def test_undelimited_stream_with_own_echo(port):
    raw, number = await port(echo=True)
    amp, states = await start_amp(
        number, framing="stream", command_suffix="", own_commands_echoed=True,
        set_level_command="MV{level_int:02d}", level_pattern=r"MV(?P<level>\d{2})")
    try:
        await wait_for(lambda: raw.writers)
        raw.push(b"MV40")
        await wait_for(lambda: amp.state == AmplifierState(40))
        await amp.set_level(99)          # echoed straight back as "MV99"...
        await asyncio.sleep(0.05)
        raw.push(b"MV98")                # ...and the amp clamps to 98
        await wait_for(lambda: amp.state == AmplifierState(98))
        assert 99 not in [s.level for s in states]
    finally:
        await amp.stop()


class ChecksummedAmp(TcpSerialAmplifier):
    """STX <cmd> <value byte> ETX <checksum = sum of cmd+value & 0xFF>."""

    def encode_command(self, command):
        body = command.encode("latin-1")
        return b"\x02" + body + b"\x03" + bytes([sum(body) & 0xFF])

    def verify_frame(self, frame):
        return frame.raw[-1] == sum(frame.payload) & 0xFF


def checksummed(body: bytes) -> bytes:
    return b"\x02" + body + b"\x03" + bytes([sum(body) & 0xFF])


async def test_binary_markers_with_checksum(port):
    raw, number = await port()
    amp = ChecksummedAmp({
        "host": "127.0.0.1", "port": number, "command_interval": 0, "encoding": "latin-1",
        "framing": "markers", "start_marker": "\x02", "end_marker": "\x03", "trailer_length": 1,
        "set_level_command": "V{level_chr}",
        "level_pattern": "V(?P<level>.)", "level_encoding": "byte",
        "mute_pattern": "M(?P<muted>[\x00\x01])", "mute_on_values": ["\x01"],
    })
    await amp.start()
    try:
        await wait_for(lambda: raw.writers)
        raw.push(b"\xff\xfe" + checksummed(b"V\x28"))         # noise, then level 40
        await wait_for(lambda: amp.state == AmplifierState(40))
        raw.push(b"\x02V\x50\x03\x00")                         # bad checksum: dropped
        raw.push(checksummed(b"M\x01"))
        await wait_for(lambda: amp.state == AmplifierState(40, True))
        await amp.set_level(200)
        await wait_for(lambda: raw.received.endswith(checksummed(b"V\xc8")))
    finally:
        await amp.stop()


async def test_search_match_mode(port):
    raw, number = await port()
    amp, states = await start_amp(
        number, framing="delimited", match="search",
        level_pattern=r"VOL=(?P<level>-?\d+)")
    try:
        await wait_for(lambda: raw.writers)
        raw.push(b"@MAIN:PWR=On,VOL=-30,MUTE=Off\r")
        await wait_for(lambda: amp.state == AmplifierState(-30))
    finally:
        await amp.stop()


async def test_hex_level_encoding(port):
    raw, number = await port()
    amp, states = await start_amp(number, level_pattern=r"VL(?P<level>[0-9A-F]{2})",
                                  level_encoding="hex")
    try:
        await wait_for(lambda: raw.writers)
        raw.push(b"VL3C\r")
        await wait_for(lambda: amp.state == AmplifierState(60))
    finally:
        await amp.stop()
