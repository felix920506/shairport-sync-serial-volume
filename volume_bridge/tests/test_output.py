import shutil

import pytest

from volume_bridge.__main__ import create_output_watcher
from volume_bridge.config import parse_config
from volume_bridge.hooks import parse_hooks
from volume_bridge.output import OutputWatcher, alsa_card, card_present
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


def test_no_watcher_without_a_card(caplog):
    assert create_output_watcher("default", None, None) is None
    assert "on_output_lost is disabled" in caplog.text
