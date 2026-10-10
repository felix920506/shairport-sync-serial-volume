import asyncio
import re

import pytest

from volume_bridge.amplifiers import create_amplifier
from volume_bridge.amplifiers.base import AmplifierState
from volume_bridge.amplifiers.telnet import (BINARY, DO, DONT, IAC, SB, SE, SGA, WILL, WONT,
                                             TelnetProtocol)
from volume_bridge.config import load_config

from test_amplifiers import wait_for
from test_ma352 import CONFIG

ECHO, COM_PORT = 1, 44


def make():
    sent = []
    protocol = TelnetProtocol(sent.append)
    return protocol, sent


def test_strips_commands_and_unescapes():
    protocol, sent = make()
    data = (b"(VOL" + bytes((IAC, 241)) + b" 5" + bytes((IAC, IAC)) + b")"
            + bytes((IAC, SB, COM_PORT, 1, IAC, IAC, 0, IAC, SE)) + b"\r\n")
    assert protocol.feed(data) == b"(VOL 5\xff)\r\n"
    assert sent == []


def test_commands_split_across_chunks():
    protocol, sent = make()
    out = b"".join(protocol.feed(bytes((b,))) for b in b"a" + bytes((IAC, WILL, ECHO, IAC, SB, 1, IAC, SE)) + b"b")
    assert out == b"ab"
    assert sent == [bytes((IAC, DONT, ECHO))]


def test_negotiation():
    protocol, sent = make()
    protocol.start()
    assert set(sent) == {bytes((IAC, v, o)) for v in (WILL, DO) for o in (BINARY, SGA)}
    sent.clear()
    # Replies to our requests are not answered again.
    protocol.feed(bytes((IAC, DO, BINARY, IAC, WILL, BINARY, IAC, DONT, SGA, IAC, WILL, SGA)))
    assert sent == []
    assert protocol.local == {BINARY} and protocol.remote == {BINARY, SGA}
    # Unsupported options are refused; repeats of enabled ones are ignored.
    protocol.feed(bytes((IAC, DO, COM_PORT, IAC, WILL, ECHO, IAC, WILL, BINARY)))
    assert sent == [bytes((IAC, WONT, COM_PORT)), bytes((IAC, DONT, ECHO))]
    sent.clear()
    # Disabling an enabled option is acknowledged.
    protocol.feed(bytes((IAC, WONT, BINARY)))
    assert sent == [bytes((IAC, DONT, BINARY))] and protocol.remote == {SGA}


def test_encoding_binary_and_nvt():
    protocol, _ = make()
    assert protocol.encode(b"\xff(VOL 5)\r") == b"\xff\xff(VOL 5)\r\0"   # NVT: CR NUL
    assert protocol.feed(b"(A)\r\0\n") == b"(A)\r\n"
    protocol.feed(bytes((IAC, DO, BINARY, IAC, WILL, BINARY)))
    assert protocol.encode(b"\xff(VOL 5)\r") == b"\xff\xff(VOL 5)\r"
    assert protocol.feed(b"\r\0") == b"\r\0"


class TelnetMA352:
    """A minimal MA352 behind a serial server in telnet mode: it negotiates,
    sprinkles telnet commands into its replies, and checks our escaping."""

    def __init__(self):
        self.received = bytearray()
        self.level = 30

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        self.writer = writer
        writer.write(bytes((IAC, DO, ECHO, IAC, WILL, BINARY, IAC, DO, BINARY, IAC, WILL, SGA)))
        while chunk := await reader.read(100):
            self.received += chunk
            for command in self.commands():
                name, _, par = command.partition(" ")
                if name == "VOL" and par:
                    self.level = int(par)
                reply = {"VOL": f"(VOL {self.level})", "MUT": "(MUT 0)", "STA": "(STA 1)"}.get(name)
                if reply:
                    writer.write(bytes((IAC, 241)) + reply.encode() + bytes((IAC, SB, COM_PORT, 1, IAC, SE)) + b"\r\n")

    def commands(self):
        # The client only sends 3-byte negotiation commands and "(...)" messages.
        text = re.sub(rb"\xff[\xfb-\xfe].", b"", bytes(self.received), flags=re.S)
        if (m := re.search(rb"\([^)]*$", text)) and len(self.received) > 0:
            self.received = bytearray(m.group())
        else:
            self.received.clear()
        return [c.decode() for c in re.findall(rb"\(([^)]*)\)", text)]

    async def close(self):
        self.server.close()


@pytest.fixture
async def telnet_amp():
    sim = TelnetMA352()
    port = await sim.start()
    config = load_config(CONFIG)
    amp = create_amplifier(config.amplifier_type,
                           {**config.amplifier_options, "host": "127.0.0.1", "port": port,
                            "protocol": "telnet"})
    await amp.start()
    yield sim, amp
    await amp.stop()
    await sim.close()


async def test_tcp_serial_over_telnet(telnet_amp):
    sim, amp = telnet_amp
    await wait_for(lambda: amp.state == AmplifierState(30, False))
    await amp.set_level(55)
    await wait_for(lambda: sim.level == 55 and amp.state == AmplifierState(55, False))


def test_protocol_option_validated():
    config = load_config(CONFIG)
    with pytest.raises(ValueError, match="protocol"):
        create_amplifier(config.amplifier_type, {**config.amplifier_options, "protocol": "ssh"})
