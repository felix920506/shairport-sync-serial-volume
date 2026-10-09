"""Two-way volume synchronisation between an AirPlay source and an amplifier.

The hard part of two-way sync is not the mapping, it's avoiding feedback
loops. Three mechanisms work together:

1. **Compare in a common domain.** Before forwarding a change, the bridge
   checks whether the other side already agrees, by comparing both sides as
   quantised amplifier levels. Rounding differences (e.g. -17.4 vs -17.43 dB)
   never trigger a send.

2. **Echo windows.** After sending a volume to one side, reports *from* that
   side are ignored for a short window. Sources ramp volume notifications and
   amplifiers often stream a status update for every step as they ramp; those
   echoes are not user actions. Each report with a *new* value extends the
   window (up to a cap), so a slow ramp can't outlive it, and the amplifier's
   window closes early once it reports the commanded target. When the window
   closes, the bridge re-checks that side once and follows it if it ended up
   somewhere else (a user moved it meanwhile, or the device refused/clamped
   the value).

3. **Latest-value senders.** Bursts (dragging a slider, spinning a knob) are
   coalesced: while a send is in flight only the most recent request is kept.

The source's volume only means something while a play session is active.
Outside a session Shairport Sync reports a stale/default value and can't pass
a volume on to any source, so the bridge neither pushes to the source nor
follows its *stored* value then. Actual volume change events are always
followed, since only a source (or the bridge itself) produces them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, Protocol, TypeVar

from .amplifiers.base import Amplifier, AmplifierState
from .mapping import AIRPLAY_MIN, AIRPLAY_MUTE, LevelScale

log = logging.getLogger(__name__)

T = TypeVar("T")

_MUTED = "muted"


class VolumeSource(Protocol):
    """The AirPlay side, as seen by the bridge (see ``shairport.ShairportSync``)."""

    @property
    def volume(self) -> float | None: ...

    @property
    def active(self) -> bool: ...

    async def set_volume(self, volume: float) -> None: ...


@dataclass(frozen=True)
class BridgeSettings:
    #: Seconds to ignore AirPlay volume reports after pushing a volume to the source.
    source_echo_window: float = 1.5
    #: Seconds to ignore amplifier reports after commanding the amplifier.
    amplifier_echo_window: float = 1.0
    #: Each report with a new value restarts the echo window, but a window never
    #: stays open longer than this in total (covers slow ramps without letting a
    #: chatty device suppress reports forever).
    max_echo_window: float = 10.0
    #: Map AirPlay mute (-144) to the amplifier's mute instead of its minimum level,
    #: and report amplifier mute back as AirPlay mute. Only if the backend supports mute.
    use_amplifier_mute: bool = True


class _LatestValueSender(Generic[T]):
    """Runs ``send`` for requested values one at a time, skipping superseded ones."""

    def __init__(self, send: Callable[[T], Awaitable[None]]) -> None:
        self._send = send
        self._value: T | None = None
        self._has_value = False
        self._task: asyncio.Task | None = None

    def request(self, value: T) -> None:
        self._value, self._has_value = value, True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while self._has_value:
            value, self._has_value = self._value, False
            await self._send(value)  # type: ignore[arg-type]

    @property
    def busy(self) -> bool:
        """True while a send is in flight or queued."""
        return self._has_value or (self._task is not None and not self._task.done())

    async def idle(self) -> None:
        while self._task is not None and not self._task.done():
            await asyncio.shield(self._task)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()


class _EchoWindow:
    def __init__(self, duration: float, max_duration: float, on_close: Callable[[], None]) -> None:
        self.duration = duration
        self.max_duration = max(duration, max_duration)
        self._on_close = on_close
        self._handle: asyncio.TimerHandle | None = None
        self._deadline = 0.0

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def open(self) -> None:
        self.cancel()
        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + self.max_duration
        self._handle = loop.call_later(self.duration, self._close)

    def extend(self) -> None:
        """Restart the timer (if open), without passing the hard deadline."""
        if self._handle is None:
            return
        loop = asyncio.get_running_loop()
        self._handle.cancel()
        delay = min(self.duration, max(0.0, self._deadline - loop.time()))
        self._handle = loop.call_later(delay, self._close)

    def close_now(self) -> None:
        """Close immediately, running the close handler."""
        if self._handle is not None:
            self._handle.cancel()
            self._close()

    def _close(self) -> None:
        self._handle = None
        self._on_close()

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None


class VolumeBridge:
    def __init__(
        self,
        source: VolumeSource,
        amplifier: Amplifier,
        scale: LevelScale,
        settings: BridgeSettings = BridgeSettings(),
    ) -> None:
        self.source = source
        self.amplifier = amplifier
        self.scale = scale
        self.settings = settings
        self.use_amp_mute = settings.use_amplifier_mute and amplifier.supports_mute

        self._to_amp = _LatestValueSender(self._send_to_amp)
        self._to_source = _LatestValueSender(self._send_to_source)
        self._amp_window = _EchoWindow(
            settings.amplifier_echo_window, settings.max_echo_window, self._amp_window_closed)
        self._source_window = _EchoWindow(
            settings.source_echo_window, settings.max_echo_window, self._source_window_closed)
        self._last_source_report: float | None = None
        self._last_amp_report: AmplifierState | None = None
        # What the amplifier was last commanded to, as a comparison key.
        self._amp_target: float | str | None = None
        self._amp_send_failed = False
        self._source_send_failed = False

        amplifier.add_listener(self.on_amplifier_state)

    # -- comparison in a common domain -----------------------------------------

    def _source_key(self, volume: float) -> float | str:
        if self.use_amp_mute and volume <= AIRPLAY_MUTE:
            return _MUTED
        return self.scale.airplay_to_level(volume)

    def _amp_key(self, state: AmplifierState) -> float | str:
        if self.use_amp_mute and state.muted:
            return _MUTED
        return state.level

    def _source_target(self, state: AmplifierState) -> float:
        """The AirPlay volume that represents an amplifier state."""
        if self.use_amp_mute and state.muted:
            return AIRPLAY_MUTE
        volume = self.scale.level_to_airplay(state.level)
        if self.use_amp_mute and volume <= AIRPLAY_MUTE:
            # Amp at its minimum but not muted: put the slider at the bottom
            # without asking the source to mute (which would mute the amp).
            return AIRPLAY_MIN
        return volume

    def in_sync(self) -> bool | None:
        volume, state = self.source.volume, self.amplifier.state
        if volume is None or state is None:
            return None
        return self._source_key(volume) == self._amp_key(state)

    # -- events -------------------------------------------------------------------

    def on_source_volume(self, volume: float) -> None:
        """The AirPlay volume changed (user moved a slider on a source)."""
        changed, self._last_source_report = volume != self._last_source_report, volume
        if self._source_window.is_open:
            log.debug("airplay volume %.2f ignored (echo window)", volume)
            if changed:
                self._source_window.extend()
            return
        self._follow_source(volume)

    def on_source_active(self, active: bool) -> None:
        """A play session started or ended. On start, make the amplifier match the source."""
        if active and self.source.volume is not None and not self._source_window.is_open:
            self._follow_source(self.source.volume)

    def on_amplifier_state(self, state: AmplifierState) -> None:
        """The amplifier reported its state (echo, poll, status broadcast, or the knob)."""
        changed, self._last_amp_report = state != self._last_amp_report, state
        if self._amp_window.is_open:
            if self._amp_key(state) == self._amp_target and not self._to_amp.busy:
                log.debug("amplifier reached target %s", state)
                self._amp_window.close_now()
            else:
                log.debug("amplifier state %s ignored (echo window)", state)
                if changed:
                    self._amp_window.extend()
            return
        self._follow_amplifier(state)

    def _follow_source(self, volume: float) -> None:
        state = self.amplifier.state
        if state is not None and self._amp_key(state) == self._source_key(volume):
            return
        log.info("airplay volume %.2f -> amplifier", volume)
        self._to_amp.request(volume)

    def _follow_amplifier(self, state: AmplifierState) -> None:
        volume = self.source.volume
        if volume is None or not self.source.active:
            # No session: there's no source to tell. When a session starts,
            # the amplifier is brought to the source's volume.
            return
        if self._source_key(volume) == self._amp_key(state):
            return
        target = self._source_target(state)
        log.info("amplifier level %g%s -> airplay volume %.2f",
                 state.level, " (muted)" if state.muted else "", target)
        self._to_source.request(target)

    def _amp_window_closed(self) -> None:
        # We commanded the amplifier; if it didn't end up where the source is
        # (clamped, refused, or turned by hand meanwhile), the amp wins.
        state = self.amplifier.state
        if state is not None and not self._amp_send_failed:
            self._follow_amplifier(state)

    def _source_window_closed(self) -> None:
        # We pushed a volume to the source; if a user moved the slider
        # meanwhile, the source wins.
        volume = self.source.volume
        if volume is not None and self.source.active and not self._source_send_failed:
            self._follow_source(volume)

    # -- senders ----------------------------------------------------------------

    async def _send_to_amp(self, volume: float) -> None:
        self._amp_window.open()
        self._amp_send_failed = False
        state = self.amplifier.state
        self._amp_target = self._source_key(volume)
        try:
            if self.use_amp_mute and volume <= AIRPLAY_MUTE:
                if state is None or not state.muted:
                    await self.amplifier.set_mute(True)
                return
            level = self.scale.airplay_to_level(volume)
            if self.use_amp_mute and state is not None and state.muted:
                await self.amplifier.set_mute(False)
            if state is None or state.level != level:
                await self.amplifier.set_level(level)
        except Exception as e:
            self._amp_send_failed = True
            log.warning("failed to set amplifier volume: %s", e)

    async def _send_to_source(self, volume: float) -> None:
        self._source_window.open()
        self._source_send_failed = False
        try:
            await self.source.set_volume(volume)
        except Exception as e:
            self._source_send_failed = True
            log.warning("failed to set airplay volume: %s", e)

    async def idle(self) -> None:
        """Wait until no sends are in flight (used by tests)."""
        await self._to_amp.idle()
        await self._to_source.idle()

    def close(self) -> None:
        self._to_amp.cancel()
        self._to_source.cancel()
        self._amp_window.cancel()
        self._source_window.cancel()
