import asyncio

import pytest

from volume_bridge.amplifiers import create_amplifier, load_amplifier_class
from volume_bridge.amplifiers.base import AmplifierState
from volume_bridge.amplifiers.stub import StubAmplifier
from volume_bridge.amplifiers.tcp_line import TcpLineAmplifier
from volume_bridge.config import parse_config


class FakeSerialServer:
    """A Denon-ish amplifier behind a serial-to-TCP adapter."""

    def __init__(self, level=40):
        self.level = level
        self.muted = False
        self.received = []
        self.writers = []

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def handle(self, reader, writer):
        self.writers.append(writer)
        while line := await reader.readuntil(b"\r"):
            cmd = line.decode().strip()
            self.received.append(cmd)
            if cmd.startswith("MV") and cmd != "MV?":
                self.level = int(cmd[2:])
            elif cmd in ("MUON", "MUOFF"):
                self.muted = cmd == "MUON"
            if cmd.startswith("MU"):
                writer.write(f"MU{'ON' if self.muted else 'OFF'}\r".encode())
            else:
                writer.write(f"MV{self.level:02d}\r".encode())
            await writer.drain()

    async def front_panel(self, level):
        self.level = level
        for w in self.writers:
            w.write(f"MV{level:02d}\r".encode())
            await w.drain()

    async def close(self):
        for w in self.writers:
            w.close()
        self.server.close()


async def wait_for(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_tcp_line_amplifier_round_trip():
    server = FakeSerialServer(level=40)
    port = await server.start()
    amp = TcpLineAmplifier({
        "host": "127.0.0.1",
        "port": port,
        "set_level_command": "MV{level_int:02d}",
        "query_commands": ["MV?", "MU?"],
        "level_pattern": r"^MV(?P<level>\d+)$",
        "mute_on_command": "MUON",
        "mute_off_command": "MUOFF",
        "mute_pattern": r"^MU(?P<muted>ON|OFF)$",
        "command_interval": 0,
    })
    states = []
    amp.add_listener(states.append)
    await amp.start()
    try:
        await wait_for(lambda: amp.state == AmplifierState(40, False))
        await amp.set_level(55)
        await wait_for(lambda: amp.state == AmplifierState(55, False))
        await amp.set_mute(True)
        await wait_for(lambda: amp.state == AmplifierState(55, True))
        await server.front_panel(12)
        await wait_for(lambda: amp.state == AmplifierState(12, True))
        assert server.received[:3] == ["MV?", "MU?", "MV55"]
    finally:
        await amp.stop()
        await server.close()


async def test_tcp_line_reconnects():
    server = FakeSerialServer(level=10)
    port = await server.start()
    amp = TcpLineAmplifier({"host": "127.0.0.1", "port": port, "query_commands": ["MV?"],
                            "level_pattern": r"^MV(?P<level>\d+)$"})
    await amp.start()
    try:
        await wait_for(lambda: amp.state is not None)
        for w in server.writers:
            w.close()
        server.level = 33
        await wait_for(lambda: amp.state.level == 33, timeout=5)
    finally:
        await amp.stop()
        await server.close()


async def test_tcp_line_level_offset():
    amp = TcpLineAmplifier({"host": "x", "port": 1, "set_level_command": "V{level_int}",
                            "level_pattern": r"^V(?P<level>\d+)$", "level_offset": -80})
    assert amp.encode_set_level(-30) == b"V50\r"
    assert amp.parse_message("V50") == {"level": -30.0}


async def test_tcp_line_requires_host():
    with pytest.raises(ValueError, match="host"):
        TcpLineAmplifier({"port": 1})


async def test_stub_control_port():
    amp = StubAmplifier({"initial_level": 5, "control_port": 0})
    await amp.start()
    port = amp._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await reader.readline()
    writer.write(b"set 42\nup\nmute on\n")
    replies = [await reader.readline() for _ in range(3)]
    assert replies[-1].strip() == b"level=43 muted=on"
    assert amp.state == AmplifierState(43, True)
    writer.close()
    await amp.stop()


def test_registry_builtin_and_module_reference():
    assert load_amplifier_class("stub") is StubAmplifier
    assert load_amplifier_class("volume_bridge.amplifiers.tcp_line:TcpLineAmplifier") is TcpLineAmplifier
    assert isinstance(create_amplifier("stub", {}), StubAmplifier)
    with pytest.raises(ValueError):
        load_amplifier_class("nope")
    with pytest.raises(TypeError):
        load_amplifier_class("volume_bridge.mapping:LevelScale")


def test_config_rejects_typos():
    with pytest.raises(ValueError, match="echo_windw"):
        parse_config({"bridge": {"echo_windw": 1}})
    with pytest.raises(ValueError, match="maxlevel"):
        parse_config({"amplifier": {"scale": {"maxlevel": 1}}})
