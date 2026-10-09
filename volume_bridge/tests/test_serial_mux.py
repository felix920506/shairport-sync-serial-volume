"""The tcp-line backend on a shared serial port (multiplexer) and with an
amplifier that pushes unsolicited status updates."""

import asyncio

import pytest

from volume_bridge.amplifiers.base import AmplifierState
from volume_bridge.amplifiers.tcp_line import TcpLineAmplifier
from volume_bridge.bridge import BridgeSettings, VolumeBridge
from volume_bridge.mapping import LevelScale

from test_amplifiers import wait_for
from test_bridge import FakeSource


class SimulatedAmp:
    """Denon-style amp: "MVnn" sets, "MV?" queries, status "MVnn". Commands and
    status look identical, as on many real amps. Moves one step per
    `step_time` and broadcasts every intermediate level, like an amp with a
    motorised pot. Clamps to `max_level`."""

    def __init__(self, level=40, max_level=98, step_time=0.0):
        self.level = level
        self.max_level = max_level
        self.step_time = step_time
        self.broadcast = None  # set by the mux
        self._target = level
        self._ramp: asyncio.Task | None = None

    def command(self, cmd):
        if cmd == "MV?":
            self.broadcast(f"MV{self.level:02d}")
        elif cmd.startswith("MV") and cmd[2:].isdigit():
            self.move_to(int(cmd[2:]))

    def move_to(self, target):
        self._target = min(target, self.max_level)
        if self._ramp is None or self._ramp.done():
            self._ramp = asyncio.create_task(self._run_ramp())

    async def _run_ramp(self):
        if self.level == self._target:
            self.broadcast(f"MV{self.level:02d}")
        while self.level != self._target:
            await asyncio.sleep(self.step_time)
            self.level += 1 if self._target > self.level else -1
            self.broadcast(f"MV{self.level:02d}")

    def knob(self, target):
        """Front panel / IR remote: the amp pushes status for each step."""
        self.move_to(target)


class SerialMux:
    """One RS-232 port shared by several TCP clients. Bytes from any client
    go to the amp and to every *other* client; with `echo_to_sender`, also
    back to the sender. Amp output goes to all clients."""

    def __init__(self, amp, echo_to_sender=False, terminator=b"\r"):
        self.amp = amp
        self.echo_to_sender = echo_to_sender
        self.terminator = terminator
        self.clients = []
        self.from_clients = []
        amp.broadcast = self._from_amp

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    def _from_amp(self, line):
        self.send_raw(line.encode() + self.terminator)

    def send_raw(self, data, exclude=None):
        for w in self.clients:
            if w is not exclude:
                w.write(data)

    async def _client(self, reader, writer):
        self.clients.append(writer)
        buf = b""
        try:
            while chunk := await reader.read(100):
                self.send_raw(chunk, exclude=None if self.echo_to_sender else writer)
                buf += chunk
                *cmds, buf = buf.split(b"\r")
                for c in cmds:
                    self.from_clients.append(c.decode())
                    self.amp.command(c.decode())
        finally:
            self.clients.remove(writer)

    async def close(self):
        for w in list(self.clients):
            w.close()
        self.server.close()


class OtherProgram:
    """Another client on the multiplexer (home automation, a control panel...)."""

    def __init__(self, port):
        self.port = port
        self.received = []

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        while line := await self.reader.readuntil(b"\r"):
            self.received.append(line.decode().strip())

    async def send(self, cmd):
        self.writer.write(cmd.encode() + b"\r")
        await self.writer.drain()

    def close(self):
        self.task.cancel()
        self.writer.close()


def amp_options(port, **extra):
    return {
        "host": "127.0.0.1",
        "port": port,
        "set_level_command": "MV{level_int:02d}",
        "query_commands": ["MV?"],
        "level_pattern": r"MV(?P<level>\d{2})",
        "command_interval": 0,
        **extra,
    }


@pytest.fixture
async def setup():
    created = []

    async def make(amp=None, echo_to_sender=False, **options):
        amp = amp or SimulatedAmp()
        mux = SerialMux(amp, echo_to_sender=echo_to_sender)
        port = await mux.start()
        client = TcpLineAmplifier(amp_options(port, **options))
        states = []
        client.add_listener(states.append)
        await client.start()
        await wait_for(lambda: client.state is not None)
        created.append((mux, client))
        return amp, mux, client, states

    yield make
    for mux, client in created:
        await client.stop()
        await mux.close()


async def test_unsolicited_status_from_knob_is_reported(setup):
    amp, mux, client, states = await setup(SimulatedAmp(level=40, step_time=0.005))
    amp.knob(45)
    await wait_for(lambda: client.state == AmplifierState(45))
    assert [s.level for s in states] == [40, 41, 42, 43, 44, 45]


async def test_other_programs_traffic(setup):
    amp, mux, client, states = await setup()
    other = OtherProgram(mux.server.sockets[0].getsockname()[1])
    await other.connect()
    try:
        # Another program polls: we see its "MV?" (ignored) and the reply (reported).
        await other.send("MV?")
        await asyncio.sleep(0.05)
        # Another program changes the volume: the amp's status reaches us.
        await other.send("MV33")
        await wait_for(lambda: client.state == AmplifierState(33))
        # We set the volume: the other program sees our command and the result.
        await client.set_level(50)
        await wait_for(lambda: client.state == AmplifierState(50))
        await wait_for(lambda: "MV50" in other.received)
        assert "MV?" in mux.from_clients and "MV33" in mux.from_clients
    finally:
        other.close()


async def test_own_echo_is_not_taken_as_confirmation(setup):
    # The amp clamps 99 -> 98. The mux echoes our "MV99" back to us; that must
    # not be reported as the amplifier being at 99.
    amp, mux, client, states = await setup(
        SimulatedAmp(level=40, max_level=98), echo_to_sender=True, own_commands_echoed=True)
    await client.set_level(99)
    await wait_for(lambda: client.state == AmplifierState(98))
    await asyncio.sleep(0.05)
    assert 99 not in [s.level for s in states]
    assert client.state == AmplifierState(98)


async def test_own_echo_when_command_and_status_are_identical(setup):
    # Echo "MV50" is dropped (it would arrive first, before the ramp); the
    # amp's own status for each step, ending in "MV50", is still reported.
    amp, mux, client, states = await setup(
        SimulatedAmp(level=40), echo_to_sender=True, own_commands_echoed=True)
    states.clear()
    await client.set_level(50)
    await wait_for(lambda: client.state == AmplifierState(50))
    await asyncio.sleep(0.05)
    assert [s.level for s in states] == list(range(41, 51))


async def test_without_echo_suppression_echo_is_harmless(setup):
    amp, mux, client, states = await setup(SimulatedAmp(level=40, max_level=98), echo_to_sender=True)
    await client.set_level(99)
    await wait_for(lambda: client.state == AmplifierState(98))


async def test_ignore_patterns_for_commands_that_look_like_status(setup):
    # A control panel on the same port sends "MVnn!" macros that the level
    # pattern would accept; ignore_patterns keeps them from counting as status.
    amp, mux, client, states = await setup(ignore_patterns=[r"MV\d{2}!"],
                                           level_pattern=r"MV(?P<level>\d{2})!?")
    other = OtherProgram(mux.server.sockets[0].getsockname()[1])
    await other.connect()
    try:
        await other.send("MV10!")  # not understood by the amp; not status
        await asyncio.sleep(0.05)
        assert client.state == AmplifierState(40)
    finally:
        other.close()


async def test_framing_terminators_split_reads_and_noise(setup):
    amp, mux, client, states = await setup()
    states.clear()
    mux.send_raw(b"MV1")
    await asyncio.sleep(0.02)
    mux.send_raw(b"2\r\nMV13\n\nMV14\r")
    await wait_for(lambda: client.state == AmplifierState(14))
    mux.send_raw(b"xxMV77\r")  # interleaved garbage: no partial match
    mux.send_raw(b"\x00" * 2000)  # long unterminated noise is discarded
    mux.send_raw(b"\rMV15\r")
    await wait_for(lambda: client.state == AmplifierState(15))
    assert [s.level for s in states] == [12, 13, 14, 15]


async def test_bridge_with_slow_ramping_amp_does_not_fight_the_source():
    """The amp takes ~0.6 s to ramp and reports every step; the echo window is
    0.1 s. Intermediate steps must not be pushed back to the source."""
    amp = SimulatedAmp(level=20, step_time=0.01)
    mux = SerialMux(amp)
    port = await mux.start()
    client = TcpLineAmplifier(amp_options(port))
    source = FakeSource(volume=-24.0)  # level 20 of 0..100
    bridge = VolumeBridge(source, client, LevelScale(0, 100, 1),
                          BridgeSettings(source_echo_window=0.1, amplifier_echo_window=0.1))
    source.bridge = bridge
    await client.start()
    try:
        await wait_for(lambda: client.state is not None)
        source.user_sets(-6.0)  # -> level 80
        await wait_for(lambda: client.state == AmplifierState(80))
        await asyncio.sleep(0.3)
        assert source.sent == []
        assert source.volume == -6.0

        # Now someone turns the knob on the amp: the slider follows.
        amp.knob(60)
        await wait_for(lambda: source.sent and source.sent[-1] == pytest.approx(-12.0))
        await asyncio.sleep(0.3)
        assert bridge.in_sync()
    finally:
        bridge.close()
        await client.stop()
        await mux.close()


async def test_bridge_with_another_program_changing_volume():
    amp = SimulatedAmp(level=20)
    mux = SerialMux(amp, echo_to_sender=True)
    port = await mux.start()
    client = TcpLineAmplifier(amp_options(port, own_commands_echoed=True))
    source = FakeSource(volume=-24.0)
    bridge = VolumeBridge(source, client, LevelScale(0, 100, 1),
                          BridgeSettings(source_echo_window=0.1, amplifier_echo_window=0.1))
    source.bridge = bridge
    await client.start()
    other = OtherProgram(port)
    await other.connect()
    try:
        await wait_for(lambda: client.state is not None)
        await other.send("MV70")
        await wait_for(lambda: source.sent and source.sent[-1] == pytest.approx(-9.0))
        await asyncio.sleep(0.3)
        assert bridge.in_sync()
        assert mux.from_clients.count("MV70") == 1  # we didn't re-send anything
    finally:
        other.close()
        bridge.close()
        await client.stop()
        await mux.close()
