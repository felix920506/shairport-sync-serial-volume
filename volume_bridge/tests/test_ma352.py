"""config/mcintosh-ma352.toml against a simulated McIntosh MA352."""

import asyncio
import re
from pathlib import Path

import pytest

from volume_bridge.amplifiers import create_amplifier
from volume_bridge.amplifiers.base import AmplifierState
from volume_bridge.bridge import BridgeSettings, VolumeBridge
from volume_bridge.config import load_config

from test_amplifiers import wait_for
from test_bridge import FakeSource

CONFIG = Path(__file__).parents[2] / "config" / "mcintosh-ma352.toml"


class SimulatedMA352:
    """Echoes accepted commands, answers queries, pushes status (if enabled)
    when the knob is turned, and rejects everything but power in standby."""

    def __init__(self, level=30, power=True):
        self.level, self.muted, self.power = level, False, power
        self.status_enabled = False
        self.received = []
        self.writers = []

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        self.writers.append(writer)
        buffer = b""
        while chunk := await reader.read(100):
            buffer += chunk
            while (m := re.search(rb"\(([^)]*)\)", buffer)):
                buffer = buffer[m.end():]
                self.received.append(m.group(1).decode())
                writer.write(self._reply(m.group(1).decode()).encode("utf-8") + b"\r\n")

    def _reply(self, cmd):
        name, _, par = cmd.partition(" ")
        if not self.power and name != "PWR":
            return "(ERROR – Invalid Command)"
        if name == "STA":
            self.status_enabled = par == "1" if par else self.status_enabled
            return f"(STA {int(self.status_enabled)})"
        if name == "VOL":
            if par.isdigit() and int(par) <= 100:
                self.level = int(par)
            elif par:
                return "(ERROR – Invalid Parameter)"
            return f"(VOL {self.level})"
        if name == "MUT":
            self.muted = par == "1" if par else self.muted
            return f"(MUT {int(self.muted)})"
        return "(ERROR – Invalid Command)"

    def knob(self, level):
        self.level = level
        if self.status_enabled:
            for w in self.writers:
                w.write(f"(VOL {level})\r\n".encode())

    async def close(self):
        for w in self.writers:
            w.close()
        self.server.close()


@pytest.fixture
async def ma352():
    amp = SimulatedMA352()
    port = await amp.start()
    config = load_config(CONFIG)
    yield amp, config, {**config.amplifier_options, "host": "127.0.0.1", "port": port}
    await amp.close()


async def test_config_loads():
    config = load_config(CONFIG)
    assert config.amplifier_type == "tcp-serial"
    create_amplifier(config.amplifier_type, config.amplifier_options)


async def test_ma352_commands_status_and_knob(ma352):
    sim, config, options = ma352
    amp = create_amplifier(config.amplifier_type, options)
    await amp.start()
    try:
        await wait_for(lambda: amp.state == AmplifierState(30, False))
        assert sim.status_enabled
        assert sim.received[:3] == ["STA 1", "VOL", "MUT"]

        await amp.set_level(55)
        await wait_for(lambda: amp.state == AmplifierState(55, False))
        await amp.set_mute(True)
        await wait_for(lambda: amp.state == AmplifierState(55, True))
        assert sim.received[3:] == ["VOL 55", "MUT 1"]

        sim.knob(42)
        await wait_for(lambda: amp.state.level == 42)
    finally:
        await amp.stop()


async def test_ma352_in_standby_keeps_last_known_state(ma352):
    sim, config, options = ma352
    amp = create_amplifier(config.amplifier_type, options)
    await amp.start()
    try:
        await wait_for(lambda: amp.state == AmplifierState(30, False))
        sim.power = False
        await amp.set_level(80)  # answered with "(ERROR – Invalid Command)"
        await asyncio.sleep(0.1)
        assert amp.state == AmplifierState(30, False)
    finally:
        await amp.stop()


async def test_ma352_with_bridge(ma352):
    sim, config, options = ma352
    amp = create_amplifier(config.amplifier_type, options)
    source = FakeSource(volume=-15.0)
    source.active = False  # no play session until the amp is connected
    bridge = VolumeBridge(source, amp, config.amplifier_scale,
                          BridgeSettings(source_echo_window=0.2, amplifier_echo_window=0.2))
    source.bridge = bridge
    await amp.start()
    try:
        await wait_for(lambda: amp.state is not None)
        source.active = True
        bridge.on_source_active(True)
        await wait_for(lambda: sim.level == 50)          # -15 dB = half way
        await asyncio.sleep(0.3)
        sim.knob(70)
        await wait_for(lambda: bridge.in_sync() and amp.state.level == 70)
        assert source.volume == pytest.approx(-9.0)
    finally:
        bridge.close()
        await amp.stop()
