import pytest

from volume_bridge.mapping import (
    AIRPLAY_MUTE,
    LevelScale,
    airplay_to_position,
    position_to_airplay,
)


def test_airplay_position_endpoints():
    assert airplay_to_position(AIRPLAY_MUTE) == 0.0
    assert airplay_to_position(-30.0) == 0.0
    assert airplay_to_position(0.0) == 1.0
    assert airplay_to_position(-15.0) == pytest.approx(0.5)
    assert position_to_airplay(0.0) == AIRPLAY_MUTE
    assert position_to_airplay(1.0) == 0.0


@pytest.mark.parametrize(
    "scale",
    [
        LevelScale(0, 100, 1),
        LevelScale(0, 80, 1),
        LevelScale(-80, 18, 0.5),
        LevelScale(0, 100, 1, exponent=2.0),
    ],
)
def test_every_level_round_trips(scale):
    """level -> airplay -> level must be stable, or the bridge would ping-pong."""
    steps = int((scale.max_level - scale.min_level) / scale.step)
    for i in range(steps + 1):
        level = scale.quantize(scale.min_level + i * scale.step)
        assert scale.airplay_to_level(scale.level_to_airplay(level)) == level


def test_limit_caps_level():
    scale = LevelScale(0, 100, 1, limit=70)
    assert scale.airplay_to_level(0.0) == 70
    assert scale.airplay_to_level(-15.0) == 50


def test_half_steps():
    scale = LevelScale(-80, 18, 0.5)
    assert scale.airplay_to_level(AIRPLAY_MUTE) == -80
    assert scale.airplay_to_level(0.0) == 18
    assert scale.quantize(-12.26) == -12.5


def test_exponent_gives_finer_low_end():
    linear, curved = LevelScale(0, 100, 1), LevelScale(0, 100, 1, exponent=2.0)
    assert curved.airplay_to_level(-15.0) == 25
    assert linear.airplay_to_level(-15.0) == 50


@pytest.mark.parametrize("kwargs", [
    dict(min_level=10, max_level=10),
    dict(step=0),
    dict(exponent=0),
    dict(limit=200),
])
def test_invalid_scales(kwargs):
    with pytest.raises(ValueError):
        LevelScale(**kwargs)
