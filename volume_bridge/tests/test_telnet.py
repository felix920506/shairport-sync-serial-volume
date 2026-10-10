import asyncio
import re

import pytest

from volume_bridge.amplifiers import create_amplifier
from volume_bridge.amplifiers.base import AmplifierState
from volume_bridge.config import load_config

from test_amplifiers import wait_for
from test_ma352 import CONFIG

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
BINARY, ECHO, SGA, COM_PORT = 0, 1, 3, 44


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
        # Strip the client's negotiation and subnegotiations, leaving "(...)" messages.
        text = re.sub(rb"\xff\xfa.*?\xff\xf0|\xff[\xfb-\xfe].", b"", bytes(self.received), flags=re.S)
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
