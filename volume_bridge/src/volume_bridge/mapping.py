"""Conversions between AirPlay volume and amplifier volume levels.

AirPlay expresses volume as a value in dB-like units:

* ``-30.0`` .. ``0.0`` -- the slider's range, linear in slider position.
* ``-144.0``           -- muted / slider at the very bottom.

Amplifiers usually expose an integer (or half-step) scale such as ``0..80`` or
``-80.0..+18.0``. Both sides are mapped through a normalised *position* in
``0.0 .. 1.0`` so either side can be swapped without touching the other.
"""

from __future__ import annotations

from dataclasses import dataclass

AIRPLAY_MUTE = -144.0
AIRPLAY_MIN = -30.0
AIRPLAY_MAX = 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def airplay_to_position(volume: float) -> float:
    """AirPlay volume -> slider position in 0.0..1.0 (mute -> 0.0)."""
    if volume <= AIRPLAY_MUTE:
        return 0.0
    return _clamp((volume - AIRPLAY_MIN) / (AIRPLAY_MAX - AIRPLAY_MIN), 0.0, 1.0)


def position_to_airplay(position: float) -> float:
    """Slider position -> AirPlay volume. Position 0.0 maps to mute, like the sources do."""
    position = _clamp(position, 0.0, 1.0)
    if position <= 0.0:
        return AIRPLAY_MUTE
    return AIRPLAY_MIN + position * (AIRPLAY_MAX - AIRPLAY_MIN)


@dataclass(frozen=True)
class LevelScale:
    """Describes an amplifier's volume scale.

    ``min_level``/``max_level`` are the amplifier's own units. ``step`` is the
    smallest increment it accepts. ``limit`` caps the level the bridge will
    ever send (a safety ceiling); it defaults to ``max_level``.

    ``exponent`` shapes the curve: 1.0 is linear in slider position (right for
    amplifiers whose scale is already in dB); values > 1.0 give finer control at
    low volume for amplifiers with a linear/percentage scale.
    """

    min_level: float = 0.0
    max_level: float = 100.0
    step: float = 1.0
    limit: float | None = None
    exponent: float = 1.0

    def __post_init__(self) -> None:
        if self.max_level <= self.min_level:
            raise ValueError("max_level must be greater than min_level")
        if self.step <= 0:
            raise ValueError("step must be positive")
        if self.exponent <= 0:
            raise ValueError("exponent must be positive")
        if self.limit is not None and not self.min_level <= self.limit <= self.max_level:
            raise ValueError("limit must be between min_level and max_level")

    @property
    def ceiling(self) -> float:
        return self.max_level if self.limit is None else self.limit

    def quantize(self, level: float) -> float:
        steps = round((level - self.min_level) / self.step)
        level = self.min_level + steps * self.step
        # Round away float noise (e.g. 0.1 steps) and keep ints as ints-valued floats.
        return _clamp(round(level, 6), self.min_level, self.ceiling)

    def position_to_level(self, position: float) -> float:
        position = _clamp(position, 0.0, 1.0) ** self.exponent
        return self.quantize(self.min_level + position * (self.max_level - self.min_level))

    def level_to_position(self, level: float) -> float:
        fraction = (level - self.min_level) / (self.max_level - self.min_level)
        return _clamp(fraction, 0.0, 1.0) ** (1.0 / self.exponent)

    def airplay_to_level(self, volume: float) -> float:
        return self.position_to_level(airplay_to_position(volume))

    def level_to_airplay(self, level: float) -> float:
        return position_to_airplay(self.level_to_position(level))
