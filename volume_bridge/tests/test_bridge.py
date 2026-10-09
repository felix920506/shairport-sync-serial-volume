import asyncio

import pytest

from volume_bridge.amplifiers.stub import StubAmplifier
from volume_bridge.bridge import BridgeSettings, VolumeBridge
from volume_bridge.mapping import AIRPLAY_MIN, AIRPLAY_MUTE, LevelScale

WINDOW = 0.05
SETTINGS = BridgeSettings(source_echo_window=WINDOW, amplifier_echo_window=WINDOW)


class FakeSource:
    """Mimics Shairport Sync: set_volume ramps notifications, then reports the
    final volume back as an AirplayVolume change (an echo)."""

    def __init__(self, volume=-15.0):
        self.volume = volume
        self.active = True
        self.sent = []
        self.bridge = None
        self.fail = False

    async def set_volume(self, volume):
        if self.fail:
            raise ConnectionError("no session")
        self.sent.append(volume)
        start = self.volume
        for v in (start + (volume - start) / 3, start + 2 * (volume - start) / 3, volume):
            await asyncio.sleep(0.001)
            self.user_sets(v)

    def user_sets(self, volume):
        self.volume = volume
        if self.bridge:
            self.bridge.on_source_volume(volume)


class RecordingStub(StubAmplifier):
    def __init__(self, options=None):
        super().__init__(options or {"initial_level": 50})
        self.commands = []
        self.fail = False

    async def set_level(self, level):
        if self.fail:
            raise ConnectionError("amplifier offline")
        self.commands.append(("level", level))
        await super().set_level(level)

    async def set_mute(self, muted):
        self.commands.append(("mute", muted))
        await super().set_mute(muted)


async def make(scale=LevelScale(0, 100, 1), source_volume=-15.0, settings=SETTINGS, amp_options=None,
               active=True):
    source = FakeSource(source_volume)
    source.active = active
    amp = RecordingStub(amp_options)
    bridge = VolumeBridge(source, amp, scale, settings)
    source.bridge = bridge
    await amp.start()
    return source, amp, bridge


async def settle(bridge):
    await bridge.idle()
    await asyncio.sleep(WINDOW * 3)
    await bridge.idle()


async def test_in_sync_at_start_sends_nothing():
    source, amp, bridge = await make()  # -15 dB == 50%
    await settle(bridge)
    assert amp.commands == [] and source.sent == []
    assert bridge.in_sync()


async def test_airplay_change_sets_amplifier_without_echo_back():
    source, amp, bridge = await make()
    source.user_sets(-6.0)
    await settle(bridge)
    assert amp.commands == [("level", 80)]
    assert source.sent == []
    assert bridge.in_sync()


async def test_amplifier_knob_updates_airplay_and_ignores_ramp_echo():
    source, amp, bridge = await make()
    amp.knob(20)
    await settle(bridge)
    assert source.sent == [pytest.approx(-24.0)]
    assert amp.commands == []  # ramp echoes from the source didn't bounce back
    assert bridge.in_sync()


async def test_slider_drag_is_coalesced():
    source, amp, bridge = await make()
    for v in [-14.0, -13.0, -12.0, -11.0, -10.0, -9.0]:
        source.user_sets(v)
    await settle(bridge)
    assert amp.commands[-1] == ("level", 70)
    assert len(amp.commands) <= 2
    assert source.sent == []


async def test_knob_turned_during_echo_window_is_reported_after_it():
    source, amp, bridge = await make()
    source.user_sets(-6.0)
    await bridge.idle()
    amp.knob(30)  # within the amplifier echo window -> deferred, not lost
    assert source.sent == []
    await settle(bridge)
    assert source.sent == [pytest.approx(-21.0)]
    assert bridge.in_sync()


async def test_slider_moved_during_echo_window_wins_after_it():
    source, amp, bridge = await make()
    amp.knob(20)
    await bridge.idle()
    source.user_sets(-3.0)  # inside the source echo window
    await settle(bridge)
    assert amp.commands == [("level", 90)]
    assert bridge.in_sync()


async def test_limit_does_not_cause_ping_pong():
    source, amp, bridge = await make(scale=LevelScale(0, 100, 1, limit=70))
    source.user_sets(0.0)
    await settle(bridge)
    assert amp.commands == [("level", 70)]
    assert source.sent == []  # slider stays at the top, amp stays at the cap


async def test_airplay_mute_mutes_amplifier_and_unmute_restores():
    source, amp, bridge = await make()
    source.user_sets(AIRPLAY_MUTE)
    await settle(bridge)
    assert amp.commands == [("mute", True)]
    assert amp.state.level == 50
    source.user_sets(-12.0)
    await settle(bridge)
    assert amp.commands[1:] == [("mute", False), ("level", 60)]
    assert source.sent == []


async def test_amplifier_mute_mutes_airplay():
    source, amp, bridge = await make()
    await amp.set_mute(True)  # e.g. mute button on the front panel
    amp.commands.clear()
    await settle(bridge)
    assert source.sent == [AIRPLAY_MUTE]
    assert amp.commands == []


async def test_amplifier_at_minimum_moves_slider_to_bottom_without_muting():
    source, amp, bridge = await make()
    amp.knob(0)
    await settle(bridge)
    assert source.sent == [AIRPLAY_MIN]
    assert amp.commands == []


async def test_without_mute_support_airplay_mute_goes_to_min_level():
    source, amp, bridge = await make(amp_options={"initial_level": 50, "supports_mute": False})
    source.user_sets(AIRPLAY_MUTE)
    await settle(bridge)
    assert amp.commands == [("level", 0)]
    assert source.sent == []


async def test_failed_amplifier_command_does_not_revert_slider():
    source, amp, bridge = await make()
    amp.fail = True
    source.user_sets(-6.0)
    await settle(bridge)
    assert source.sent == []


async def test_failed_source_push_does_not_move_amplifier():
    source, amp, bridge = await make()
    source.fail = True
    amp.knob(20)
    await settle(bridge)
    assert amp.commands == []


async def test_session_start_brings_amplifier_to_source_volume():
    source, amp, bridge = await make()
    source.volume = -3.0  # changed while we weren't told (e.g. same value re-sent)
    bridge.on_source_active(True)
    await settle(bridge)
    assert amp.commands == [("level", 90)]


async def test_nothing_pushed_to_source_before_its_volume_is_known():
    source, amp, bridge = await make(source_volume=None)
    amp.knob(20)
    await settle(bridge)
    assert source.sent == []


async def test_idle_source_never_drives_the_amplifier():
    """Regression: at startup Shairport Sync reports a default 0 dB while idle.
    The amplifier must not be sent to full volume."""
    source, amp, bridge = await make(source_volume=0.0, active=False)
    amp.knob(20)
    await settle(bridge)
    assert source.sent == []
    assert amp.commands == []


async def test_knob_while_idle_is_overridden_by_source_at_session_start():
    source, amp, bridge = await make()
    source.active = False
    amp.knob(70)
    await settle(bridge)
    assert source.sent == []
    source.active = True
    bridge.on_source_active(True)
    await settle(bridge)
    assert amp.commands == [("level", 50)]


async def test_airplay_change_event_is_followed_even_if_not_active():
    source, amp, bridge = await make()
    source.active = False
    source.user_sets(-6.0)
    await settle(bridge)
    assert amp.commands == [("level", 80)]


class SilentStub(RecordingStub):
    """Accepts commands without reporting (the report comes later, from the test)."""

    async def set_level(self, level):
        self.commands.append(("level", level))


async def test_repeated_identical_reports_do_not_hold_the_window_open():
    # A polling amp that refused our command keeps reporting its old level.
    source, amp, bridge = await make(amp_options={"initial_level": 50})
    amp.__class__ = SilentStub
    source.user_sets(-6.0)
    for _ in range(int(WINDOW * 4 / 0.01)):
        amp.report(amp.state)  # same state every 10 ms
        await asyncio.sleep(0.01)
    assert source.sent == [pytest.approx(-15.0)]  # slider corrected to the amp's real level


async def test_echo_window_is_capped():
    settings = BridgeSettings(source_echo_window=WINDOW, amplifier_echo_window=WINDOW,
                              max_echo_window=WINDOW * 3)
    source, amp, bridge = await make(settings=settings)
    amp.__class__ = SilentStub
    source.user_sets(-6.0)
    loop = asyncio.get_running_loop()
    start, level = loop.time(), 50
    while not source.sent and loop.time() - start < 2:
        level = 51 if level == 50 else 50  # a chatty, changing amp
        amp.report(type(amp.state)(level))
        await asyncio.sleep(0.01)
    assert source.sent, "window never closed"
    assert loop.time() - start < WINDOW * 3 + 0.1


async def test_window_closes_early_when_amp_reaches_target():
    settings = BridgeSettings(source_echo_window=WINDOW, amplifier_echo_window=10.0)
    source, amp, bridge = await make(settings=settings)
    amp.__class__ = SilentStub
    source.user_sets(-6.0)
    await bridge.idle()
    amp.report(type(amp.state)(80))  # the amp confirms the target
    amp.report(type(amp.state)(81))  # and then someone nudges the knob
    await settle(bridge)
    assert source.sent == [pytest.approx(-5.7)]


class SlowSource(FakeSource):
    async def set_volume(self, volume):
        self.sent.append(volume)
        await asyncio.sleep(0.05)
        self.user_sets(volume)


async def test_report_matching_stale_source_value_during_push_is_not_lost():
    """Amp streams 68 then 70 while the push for 68 is in flight. The source
    still shows its old -9.0 (= 70), but it is about to become 68, so 70 must
    still be sent afterwards."""
    source = SlowSource(volume=-9.0)  # level 70
    amp = RecordingStub({"initial_level": 70})
    bridge = VolumeBridge(source, amp, LevelScale(0, 100, 1), SETTINGS)
    source.bridge = bridge
    await amp.start()
    amp.knob(68)
    await asyncio.sleep(0.01)  # push for 68 now in flight
    amp.knob(70)
    await settle(bridge)
    await asyncio.sleep(0.1)
    assert source.volume == pytest.approx(-9.0)
    assert bridge.in_sync()


class SlowAmp(RecordingStub):
    async def set_level(self, level):
        self.commands.append(("level", level))
        await asyncio.sleep(0.05)
        self.knob(level)


async def test_slider_matching_stale_amp_value_during_command_is_not_lost():
    source = FakeSource(volume=-15.0)  # level 50
    amp = SlowAmp({"initial_level": 50})
    bridge = VolumeBridge(source, amp, LevelScale(0, 100, 1), SETTINGS)
    source.bridge = bridge
    await amp.start()
    source.user_sets(-12.0)  # 60, command in flight
    await asyncio.sleep(0.01)
    source.user_sets(-15.0)  # back to 50: equals the amp's stale level
    await settle(bridge)
    await asyncio.sleep(0.1)
    assert amp.state.level == 50
    assert bridge.in_sync()
