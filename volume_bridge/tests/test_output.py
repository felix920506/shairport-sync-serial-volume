import asyncio
import shutil

import pytest

from volume_bridge.__main__ import create_output_watcher
from volume_bridge.config import parse_config
from volume_bridge.hooks import parse_hooks
from volume_bridge.output import OutputRecovery, OutputWatcher, alsa_card, card_present
from volume_bridge.shairport import ShairportSync

from test_amplifiers import wait_for
from test_hooks import make

@pytest.mark.parametrize("device, card", [
    ("hw:CARD=N05XD", "N05XD"),
    ("hw:CARD=N05XD,DEV=0", "N05XD"),
    ("plughw:CARD=\"N05XD\"", "N05XD"),
    ("hw:N05XD", "N05XD"),
    ("hw:1", "1"),
    ("plughw:1,0", "1"),
    ("default", None),
    ("", None),
])
def test_alsa_card(device, card):
    assert alsa_card(device) == card


def add_card(sysfs, number, name):
    (sysfs / f"card{number}").mkdir()
    (sysfs / f"card{number}" / "id").write_text(name + "\n")


def test_card_present_by_name_or_number(tmp_path):
    add_card(tmp_path, 0, "PCH")
    assert not card_present("N05XD", tmp_path) and not card_present("1", tmp_path)
    add_card(tmp_path, 3, "N05XD")  # numbers change when a card reappears
    assert card_present("N05XD", tmp_path) and card_present("3", tmp_path)
    assert card_present("PCH", tmp_path) and card_present("0", tmp_path)


def test_output_lost_alias():
    hooks = parse_hooks({"on_connect": [{"send": "PWR 1"}], "on_output_lost": "on_connect"})
    assert hooks["output_lost"] == hooks["connect"]
    with pytest.raises(ValueError, match="not an event"):
        parse_hooks({"on_output_lost": "on_connect"})
    with pytest.raises(ValueError, match="not an event"):
        parse_hooks({"on_connect": [], "on_output_lost": "connect"})


def test_alsa_device_defaults_to_the_environment(monkeypatch):
    monkeypatch.setenv("ALSA_DEVICE", "hw:CARD=N05XD")
    assert parse_config({}).alsa_device == "hw:CARD=N05XD"
    assert parse_config({"shairport": {"alsa_device": "hw:1"}}).alsa_device == "hw:1"


def test_playing_follows_play_state_metadata():
    source = ShairportSync()
    message = source._metadata_message
    message(b"ssnc", b"conn", b"a")
    assert not source.playing
    message(b"ssnc", b"pres", b"")
    assert source.playing
    message(b"ssnc", b"pfls", b"1234")  # a skip: still playing
    assert source.playing
    message(b"ssnc", b"paus", b"")
    assert not source.playing
    message(b"ssnc", b"prsm", b"")
    assert source.playing
    message(b"ssnc", b"pend", b"")
    message(b"ssnc", b"pbeg", b"")  # AirPlay 2: the stream is set up, not yet playing
    assert not source.playing
    message(b"ssnc", b"pffr", b"1/2")  # first frame received
    assert source.playing
    message(b"ssnc", b"disc", b"a")
    assert not source.playing


async def test_output_watcher(tmp_path):
    add_card(tmp_path, 1, "N05XD")
    changes = []
    watcher = OutputWatcher("N05XD", changes.append, interval=0.01, sysfs=tmp_path)
    watcher.start()
    try:
        shutil.rmtree(tmp_path / "card1")
        await wait_for(lambda: changes == [False])
        add_card(tmp_path, 2, "N05XD")
        await wait_for(lambda: changes == [False, True])
    finally:
        watcher.close()


@pytest.mark.parametrize("playing, fires", [(True, True), (False, False)])
async def test_output_lost_fires_only_while_playing(playing, fires):
    source, amp, bridge, hooks = await make({"on_output_lost": [{"send": "PWR 1"}]})
    source.playing = playing
    watcher = create_output_watcher("hw:CARD=N05XD", source, hooks)
    watcher.on_change(False)
    await hooks.idle()
    assert amp.received_commands == ([("PWR 1", False)] if fires else [])


async def test_output_lost_is_not_queued_twice():
    source, amp, bridge, hooks = await make({"on_output_lost": [{"send": "PWR 1"}, {"delay": 0.05}]})
    source.playing = True
    watcher = create_output_watcher("hw:CARD=N05XD", source, hooks)
    watcher.on_change(False)
    watcher.on_change(True)
    watcher.on_change(False)  # flickers while it powers up
    await hooks.idle()
    assert amp.received_commands == [("PWR 1", False)]
    watcher.on_change(True)
    watcher.on_change(False)  # a later drop runs it again
    await hooks.idle()
    assert amp.received_commands == [("PWR 1", False)] * 2


async def test_no_watcher_without_a_card(caplog):
    _, _, _, hooks = await make({"on_output_lost": [{"send": "PWR 1"}]})
    assert create_output_watcher("default", None, hooks) is None
    assert "on_output_lost / pause_while_output_lost are disabled" in caplog.text


class PausableSource:
    """Shairport Sync as far as OutputRecovery sees it."""

    def __init__(self, clients=("phone",)):
        self.playing = True
        self.clients = frozenset(clients)
        self.commands = []

    async def pause(self):
        self.commands.append("pause")
        self.playing = False

    async def play(self):
        self.commands.append("play")
        self.playing = True

    async def drop_session(self):
        self.commands.append("drop")
        self.clients = frozenset()


async def make_recovery(hooks=None, **kwargs):
    _, amp, _, runner = await make(hooks or {})
    source = PausableSource()
    kwargs = {"resume": True, **kwargs}
    return source, amp, runner, OutputRecovery("N05XD", source, runner, pause=True, resume_delay=0.01, **kwargs)


async def test_pause_while_lost_and_resume_when_back():
    source, amp, hooks, recovery = await make_recovery({"on_output_lost": [{"send": "PWR 1"}]})
    recovery.on_output_change(False)
    await wait_for(lambda: source.commands == ["pause"])
    recovery.on_output_change(True)
    await wait_for(lambda: source.commands == ["pause", "play"])
    assert amp.received_commands == [("PWR 1", False)]


async def test_resume_waits_for_the_output_lost_hooks():
    source, amp, hooks, recovery = await make_recovery({"on_output_lost": [{"delay": 0.2}]})
    recovery.on_output_change(False)
    await wait_for(lambda: source.commands == ["pause"])
    recovery.on_output_change(True)  # back while the amp is still "warming up"
    await asyncio.sleep(0.1)
    assert source.commands == ["pause"]
    await wait_for(lambda: source.commands == ["pause", "play"])


async def test_no_resume_if_the_source_left():
    source, _, _, recovery = await make_recovery()
    recovery.on_output_change(False)
    await wait_for(lambda: source.commands == ["pause"])
    source.clients = frozenset({"another phone"})
    recovery.on_output_change(True)
    await asyncio.sleep(0.1)
    assert source.commands == ["pause"]


async def test_no_resume_if_played_by_hand_after_the_dac_is_back():
    source, _, _, recovery = await make_recovery(resume=False)
    recovery.on_output_change(False)
    await wait_for(lambda: source.commands == ["pause"])
    recovery.on_output_change(True)
    source.playing = True
    recovery.on_playing(True)  # the user pressed play once it was back
    await asyncio.sleep(0.1)
    assert source.commands == ["pause"]


async def test_gone_again_before_resuming_stays_paused():
    source, _, _, recovery = await make_recovery()
    recovery.resume_delay = 0.1
    recovery.on_output_change(False)
    await wait_for(lambda: source.commands == ["pause"])
    recovery.on_output_change(True)
    recovery.on_output_change(False)  # flickers
    await asyncio.sleep(0.2)
    assert source.commands == ["pause"]
    recovery.on_output_change(True)
    await wait_for(lambda: source.commands == ["pause", "play"])


async def test_back_while_pausing_still_resumes():
    source, _, _, recovery = await make_recovery()
    recovery.on_output_change(False)
    recovery.on_output_change(True)  # before the pause task has run
    await wait_for(lambda: source.commands == ["pause", "play"])


async def test_not_playing_does_nothing():
    source, _, _, recovery = await make_recovery()
    source.playing = False
    recovery.on_output_change(False)
    recovery.on_output_change(True)
    await asyncio.sleep(0.1)
    assert source.commands == []


async def test_pause_without_resume():
    source, _, _, recovery = await make_recovery(resume=False)
    recovery.on_output_change(False)
    await wait_for(lambda: source.commands == ["pause"])
    recovery.on_output_change(True)
    await asyncio.sleep(0.1)
    assert source.commands == ["pause"]


def test_pause_options_in_config():
    config = parse_config({})
    assert config.pause_while_output_lost and not config.resume_when_output_back
    config = parse_config({"shairport": {"pause_while_output_lost": False, "resume_when_output_back": True,
                                         "output_resume_delay": 5}})
    assert not config.pause_while_output_lost and config.resume_when_output_back
    assert config.output_resume_delay == 5.0


async def test_recovery_when_playback_starts_with_the_dac_missing():
    source, amp, hooks, recovery = await make_recovery({"on_output_lost": [{"send": "PWR 1"}]}, resume=False)
    source.playing = False
    recovery.on_output_change(False)  # dropped while not playing: nothing yet
    await hooks.idle()
    assert amp.received_commands == [] and source.commands == []
    source.playing = True
    recovery.on_playing(True)
    await wait_for(lambda: source.commands == ["pause"])  # held...
    await hooks.idle()
    assert amp.received_commands == [("PWR 1", False)]
    recovery.on_output_change(True)
    await wait_for(lambda: source.commands == ["pause", "play"])  # ...until the DAC is ready, without resume


async def test_one_recovery_attempt_per_absence():
    source, amp, hooks, recovery = await make_recovery({"on_output_lost": [{"send": "PWR 1"}]}, resume=False)
    recovery.on_output_change(False)  # dropped while playing: attempt, pause
    await wait_for(lambda: source.commands == ["pause"])
    await hooks.idle()
    source.playing = True
    recovery.on_playing(True)  # played again by hand, DAC still missing: hold it, no new attempt
    await wait_for(lambda: source.commands == ["pause", "pause"])
    await hooks.idle()
    assert amp.received_commands == [("PWR 1", False)]

    recovery.on_output_change(True)  # back: resumes the held source; the next absence gets a new attempt
    await wait_for(lambda: source.commands == ["pause", "pause", "play"])
    source.playing = False
    recovery.on_output_change(False)
    source.playing = True
    recovery.on_playing(True)
    await hooks.idle()
    assert amp.received_commands == [("PWR 1", False)] * 2


async def test_on_connect_while_the_dac_is_missing_is_the_attempt():
    hooks_config = {"on_connect": [{"send": "PWR 1"}], "on_output_lost": "on_connect"}
    source, amp, hooks, recovery = await make_recovery(hooks_config, resume=False)
    source.playing = False
    recovery.on_output_change(False)
    recovery.on_connection(True)
    hooks.fire("connect")
    source.playing = True
    recovery.on_playing(True)
    await wait_for(lambda: source.commands == ["pause"])  # held, but no second attempt
    await hooks.idle()
    assert amp.received_commands == [("PWR 1", False)]

    recovery.on_connection(False)  # a new session gets a new attempt
    recovery.on_playing(True)
    await hooks.idle()
    assert amp.received_commands == [("PWR 1", False)] * 2


async def test_resume_waits_for_the_connect_hooks():
    source, _, hooks, recovery = await make_recovery({"on_connect": [{"delay": 0.2}]}, resume=False)
    source.playing = False
    recovery.on_output_change(False)
    recovery.on_connection(True)
    hooks.fire("connect")  # e.g. the amplifier warming up
    source.playing = True
    recovery.on_playing(True)
    await wait_for(lambda: source.commands == ["pause"])
    recovery.on_output_change(True)  # the DAC is ready before the amplifier
    await asyncio.sleep(0.1)
    assert source.commands == ["pause"]
    await wait_for(lambda: source.commands == ["pause", "play"])


class SlowToPauseSource(PausableSource):
    """Ignores the first pause, like a phone that has only just connected."""

    async def pause(self):
        self.commands.append("pause")
        if self.commands.count("pause") > 1:
            self.playing = False


async def test_pause_is_retried_until_the_source_pauses():
    _, _, _, hooks = await make({})
    source = SlowToPauseSource()
    recovery = OutputRecovery("N05XD", source, hooks, pause=True, resume_delay=0.01, pause_check=0.05)
    recovery.present = False  # the DAC isn't there yet...
    recovery.on_connection(True)
    recovery.on_playing(True)  # ...when a source connects that is already playing
    await wait_for(lambda: source.commands == ["pause", "pause"] and not source.playing)
    recovery.on_output_change(True)
    await wait_for(lambda: source.commands == ["pause", "pause", "play"])


class NeverConfirmsPauseSource(PausableSource):
    """Pauses, but Shairport Sync never reports it (it paused before it started)."""

    async def pause(self):
        self.commands.append("pause")


async def test_unconfirmed_pause_is_still_resumed():
    _, _, _, hooks = await make({})
    source = NeverConfirmsPauseSource()
    recovery = OutputRecovery("N05XD", source, hooks, pause=True, resume_delay=0.01, pause_check=0.02)
    recovery.present = False
    recovery.on_playing(True)
    await wait_for(lambda: source.commands == ["pause"] * 3)
    recovery.on_output_change(True)
    await wait_for(lambda: source.commands == ["pause"] * 3 + ["play"])


async def test_session_dropped_if_the_dac_does_not_come_back():
    source, _, _, recovery = await make_recovery({"on_output_lost": [{"send": "PWR 1"}]}, give_up_after=0.1)
    recovery.on_output_change(False)  # unplugged while playing
    await wait_for(lambda: source.commands == ["pause", "drop"])


async def test_session_not_dropped_if_the_dac_comes_back():
    source, _, _, recovery = await make_recovery({"on_output_lost": [{"send": "PWR 1"}]}, give_up_after=0.1)
    recovery.on_output_change(False)
    await wait_for(lambda: source.commands == ["pause"])
    recovery.on_output_change(True)
    await asyncio.sleep(0.2)
    assert "drop" not in source.commands


async def test_session_dropped_if_the_dac_is_missing_after_connecting():
    source, _, hooks, recovery = await make_recovery({"on_connect": [{"send": "PWR 1"}]}, give_up_after=0.1)
    recovery.present = False
    source.playing = False
    recovery.on_connection(True)  # the on_connect power-up is the attempt
    await wait_for(lambda: source.commands == ["drop"])

    source, _, hooks, recovery = await make_recovery({"on_connect": [{"send": "PWR 1"}]}, give_up_after=0.1)
    recovery.present = False
    source.playing = False
    recovery.on_connection(True)
    recovery.on_connection(False)  # left before the timeout
    await asyncio.sleep(0.2)
    assert source.commands == []


def test_recovery_timeout_in_config():
    assert parse_config({}).output_recovery_timeout == 60.0
    assert parse_config({"shairport": {"output_recovery_timeout": 0}}).output_recovery_timeout == 0.0
