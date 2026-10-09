import asyncio
import sys

import pytest

from volume_bridge.config import parse_config
from volume_bridge.hooks import Action, HookRunner, parse_hooks
from volume_bridge.shairport import ShairportSync

from test_bridge import SETTINGS, FakeSource, RecordingStub, settle
from volume_bridge.bridge import VolumeBridge
from volume_bridge.mapping import LevelScale


async def make(hooks, source_volume=-15.0):
    source = FakeSource(source_volume)
    amp = RecordingStub()
    bridge = VolumeBridge(source, amp, LevelScale(0, 100, 1), SETTINGS)
    source.bridge = bridge
    await amp.start()
    return source, amp, bridge, HookRunner(parse_hooks(hooks), source, amp, bridge)


def test_parse_hooks():
    hooks = parse_hooks({
        "on_connect": [{"send": "PWR 1"}, {"delay": 2}, {"sync": "source"},
                       {"send": "\u0002ON\u0003", "raw": True}],
        "on_disconnect": [{"shell": "true", "timeout": 5}],
    })
    assert hooks["connect"] == (Action("send", "PWR 1"), Action("delay", 2.0),
                                Action("sync", "source"), Action("send", "\u0002ON\u0003", raw=True))
    assert hooks["disconnect"] == (Action("shell", "true", timeout=5.0),)


@pytest.mark.parametrize("hooks, message", [
    ({"on_wake": []}, "unknown setting"),
    ({"on_connect": {"send": "x"}}, "must be a list"),
    ({"on_connect": ["PWR 1"]}, "must be a table"),
    ({"on_connect": [{"send": "x", "shell": "y"}]}, "exactly one"),
    ({"on_connect": [{}]}, "exactly one"),
    ({"on_connect": [{"send": "x", "timeout": 1}]}, "unknown setting"),
    ({"on_connect": [{"delay": -1}]}, "delay"),
    ({"on_connect": [{"sync": "both"}]}, "sync"),
    ({"on_connect": [{"shell": ["ls"]}]}, "string"),
    ({"on_connect": [{"parallel": []}]}, "parallel"),
    ({"on_connect": [{"if_amplifier_off": {"send": "PWR 1"}}]}, "if_amplifier_off"),
    ({"on_connect": [{"parallel": {"send": "x"}}]}, "parallel"),
    ({"on_connect": [{"parallel": [{"send": "x"}, [{"delay": "soon"}]]}]}, r"parallel\[1\]\[0\]: delay"),
])
def test_parse_hooks_rejects(hooks, message):
    with pytest.raises(ValueError, match=message):
        parse_hooks(hooks)


def test_parse_parallel():
    hooks = parse_hooks({"on_connect": [
        {"parallel": [[{"send": "PWR 1"}, {"delay": 20}, {"sync": "source"}], {"shell": "true"}]},
    ]})
    assert hooks["connect"] == (Action("parallel", (
        (Action("send", "PWR 1"), Action("delay", 20.0), Action("sync", "source")),
        (Action("shell", "true"),),
    )),)


def test_hooks_in_config():
    config = parse_config({"hooks": {"on_session_end": [{"delay": 1}]}})
    assert config.hooks == {"session_end": (Action("delay", 1.0),)}
    assert parse_config({}).hooks == {}


async def test_send_delay_and_order():
    source, amp, bridge, hooks = await make({
        "on_connect": [{"send": "PWR 1"}, {"delay": 0.01}, {"send": "INP 2", "raw": True}],
        "on_disconnect": [{"send": "PWR 0"}],
    })
    hooks.fire("connect", "fe80::1")
    hooks.fire("disconnect", "fe80::1")
    await hooks.idle()
    assert amp.received_commands == [("PWR 1", False), ("INP 2", True), ("PWR 0", False)]


async def test_parallel_branches_run_at_the_same_time(tmp_path):
    marker = tmp_path / "shell-ran"
    source, amp, bridge, hooks = await make({"on_connect": [
        {"parallel": [
            [{"send": "PWR 1"}, {"delay": 0.2}, {"send": "VOL 30"}],
            [{"shell": "exit 1"}, {"shell": f"touch {marker}"}],  # a failure doesn't stop its branch
        ]},
        {"send": "after"},
    ]})
    loop = asyncio.get_running_loop()
    start = loop.time()
    hooks.fire("connect")
    await asyncio.sleep(0.15)
    assert marker.exists()  # didn't wait for the other branch's delay
    assert amp.received_commands == [("PWR 1", False)]
    await hooks.idle()
    assert loop.time() - start < 0.4
    assert amp.received_commands == [("PWR 1", False), ("VOL 30", False), ("after", False)]


async def test_event_without_hooks_does_nothing():
    source, amp, bridge, hooks = await make({"on_connect": [{"send": "PWR 1"}]})
    hooks.fire("session_end")
    await hooks.idle()
    assert amp.received_commands == []


async def test_failing_action_does_not_stop_the_rest(tmp_path):
    source, amp, bridge, hooks = await make({
        "on_connect": [{"shell": "exit 3"}, {"shell": "sleep 5", "timeout": 0.05}, {"send": "PWR 1"}],
    })
    hooks.fire("connect")
    await hooks.idle()
    assert amp.received_commands == [("PWR 1", False)]


async def test_shell_gets_the_event_in_its_environment(tmp_path):
    out = tmp_path / "env"
    source, amp, bridge, hooks = await make(
        {"on_connect": [{"shell": f"env | grep ^VOLUME_BRIDGE_ | sort > {out}"}]}, source_volume=-9.0)
    hooks.fire("connect", "fe80::1")
    await hooks.idle()
    assert out.read_text().splitlines() == [
        "VOLUME_BRIDGE_AIRPLAY_VOLUME=-9.00",
        "VOLUME_BRIDGE_AMPLIFIER_LEVEL=50",
        "VOLUME_BRIDGE_AMPLIFIER_MUTED=0",
        "VOLUME_BRIDGE_EVENT=connect",
        "VOLUME_BRIDGE_SOURCE=fe80::1",
    ]


async def test_sync_source_resends_volume_even_if_amp_seems_in_sync():
    """An amp that was asleep may have ignored the last command although it
    still reports the level we sent."""
    source, amp, bridge, hooks = await make({"on_connect": [{"sync": "source"}]}, source_volume=-15.0)
    amp.knob(50)  # -15 dB = 50: already "in sync"
    await settle(bridge)
    amp.commands.clear()
    hooks.fire("connect")
    await hooks.idle()
    assert amp.commands == [("level", 50)]


async def test_sync_amplifier_pushes_amp_volume_to_source():
    source, amp, bridge, hooks = await make({"on_connect": [{"sync": "amplifier"}]})
    hooks.fire("connect")
    await hooks.idle()
    assert source.sent == [-15.0]  # level 50 (also the source's volume: sent anyway)


async def test_sync_skipped_without_a_source():
    source, amp, bridge, hooks = await make({"on_connect": [{"sync": "source"}]})
    source.connected = False
    hooks.fire("connect")
    await hooks.idle()
    assert amp.commands == []


def test_connection_callback_fires_on_first_connect_and_last_disconnect():
    source = ShairportSync()
    events = []
    source.on_connection = lambda connected, client: events.append((connected, client))
    source._metadata_message(b"ssnc", b"conn", b"a")
    source._metadata_message(b"ssnc", b"conn", b"b")
    source._metadata_message(b"ssnc", b"disc", b"a")
    source._metadata_message(b"ssnc", b"disc", b"b")
    assert events == [(True, "a"), (False, "b")]


async def test_tcp_serial_send_command_and_refresh():
    from volume_bridge.amplifiers.tcp_serial import TcpSerialAmplifier

    received = []

    async def client(reader, writer):
        while data := await reader.read(100):
            received.append(data)

    server = await asyncio.start_server(client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    amp = TcpSerialAmplifier({"host": "127.0.0.1", "port": port, "command_interval": 0,
                              "command_prefix": "(", "command_suffix": ")",
                              "query_commands": ["VOL"]})
    await amp.start()
    try:
        from test_amplifiers import wait_for
        await wait_for(lambda: b"".join(received) == b"(VOL)")
        await amp.send_command("PWR 1")
        await amp.send_command("\x02X\x03", raw=True)
        await amp.refresh()
        await wait_for(lambda: b"".join(received) == b"(VOL)(PWR 1)\x02X\x03(VOL)")
    finally:
        await amp.stop()
        server.close()


async def test_volume_sync_is_held_until_the_hook_syncs():
    """While the amp warms up, neither side follows the other: the slider must
    not snap back to the sleeping amp's stale level, and the amp isn't sent
    commands it would reject. The sync then applies the source's latest volume."""
    source, amp, bridge, hooks = await make(
        {"on_connect": [{"send": "PWR 1"}, {"delay": 0.2}, {"sync": "source"}]}, source_volume=-15.0)
    hooks.fire("connect")
    await asyncio.sleep(0.02)
    source.user_sets(-6.0)  # slider moved during the warm-up
    amp.knob(30)            # e.g. a stale report from the amp
    await asyncio.sleep(0.1)
    assert amp.commands == [] and source.sent == []
    await hooks.idle()
    await settle(bridge)
    assert amp.commands == [("level", 80)]  # -6 dB
    assert source.sent == []
    amp.knob(40)  # back to normal afterwards
    await settle(bridge)
    assert source.sent == [-18.0]  # level 40


async def test_hooks_without_sync_do_not_hold():
    source, amp, bridge, hooks = await make({"on_connect": [{"delay": 0.2}]})
    hooks.fire("connect")
    await asyncio.sleep(0.02)
    source.user_sets(-6.0)
    await settle(bridge)
    assert amp.commands == [("level", 80)]
    await hooks.idle()


async def test_hold_is_released_when_sync_fails():
    source, amp, bridge, hooks = await make({"on_connect": [{"sync": "source"}, {"delay": 0.2}]})

    async def broken_refresh():
        raise ConnectionError("amplifier not connected")

    amp.refresh = broken_refresh
    hooks.fire("connect")
    await asyncio.sleep(0.05)  # sync failed, delay still running
    source.user_sets(-6.0)
    await settle(bridge)
    assert amp.commands == [("level", 80)]
    await hooks.idle()


async def test_hold_lasts_until_the_first_sync_in_parallel_branches():
    source, amp, bridge, hooks = await make({"on_connect": [
        {"parallel": [[{"delay": 0.1}, {"sync": "source"}], {"delay": 0.3}]},
    ]})
    hooks.fire("connect")
    await asyncio.sleep(0.2)  # sync done, the other branch still running
    source.user_sets(-6.0)
    await settle(bridge)
    assert amp.commands[-1] == ("level", 80)
    await hooks.idle()


@pytest.mark.parametrize("powered, runs", [(True, False), (False, True), (None, True)])
async def test_if_amplifier_off(powered, runs):
    source, amp, bridge, hooks = await make({"on_connect": [
        {"if_amplifier_off": [{"send": "PWR 1"}, {"delay": 0.3}]},
        {"send": "after"},
    ]})
    amp.powered = powered
    loop = asyncio.get_running_loop()
    start = loop.time()
    hooks.fire("connect")
    await hooks.idle()
    elapsed = loop.time() - start
    if runs:
        assert amp.received_commands == [("PWR 1", False), ("after", False)]
        assert elapsed >= 0.3
    else:
        assert amp.received_commands == [("after", False)]
        assert elapsed < 0.1


async def test_if_amplifier_off_runs_when_the_query_fails():
    source, amp, bridge, hooks = await make({"on_connect": [{"if_amplifier_off": [{"send": "PWR 1"}]}]})

    async def broken(timeout=2.0):
        raise ConnectionError("amplifier not connected")

    amp.query_power = broken
    hooks.fire("connect")
    await hooks.idle()
    assert amp.received_commands == [("PWR 1", False)]


async def test_already_on_amp_is_synced_without_waiting():
    source, amp, bridge, hooks = await make({"on_connect": [
        {"if_amplifier_off": [{"send": "PWR 1"}, {"delay": 5}]},
        {"sync": "source"},
    ]})
    amp.powered = True
    await settle(bridge)
    source.volume = -6.0  # e.g. the phone's volume when it connects
    amp.commands.clear()
    hooks.fire("connect")
    await asyncio.wait_for(hooks.idle(), 1)
    assert amp.commands == [("level", 80)]


async def test_tcp_serial_query_power():
    from volume_bridge.amplifiers.tcp_serial import TcpSerialAmplifier
    from test_amplifiers import wait_for

    answer = {"text": b"(PWR 0)"}

    async def client(reader, writer):
        while data := await reader.read(100):
            if b"(PWR)" in data and answer["text"]:
                writer.write(answer["text"] + b"\r\n")

    server = await asyncio.start_server(client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    options = {"host": "127.0.0.1", "port": port, "command_interval": 0,
               "framing": "markers", "start_marker": "(", "end_marker": ")",
               "command_prefix": "(", "command_suffix": ")",
               "power_query_command": "PWR", "power_pattern": r"PWR\s*(?P<power>[01])"}
    amp = TcpSerialAmplifier(options)
    unconfigured = TcpSerialAmplifier({"host": "127.0.0.1", "port": port})
    await amp.start()
    try:
        await wait_for(lambda: amp._writer is not None)
        assert await amp.query_power() is False
        answer["text"] = b"(PWR 1)"
        assert await amp.query_power() is True
        answer["text"] = b""
        assert await amp.query_power(timeout=0.1) is None
        assert await unconfigured.query_power() is None
    finally:
        await amp.stop()
        server.close()
