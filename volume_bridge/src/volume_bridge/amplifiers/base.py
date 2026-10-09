"""The interface every amplifier backend implements.

A backend owns the connection to one physical (or simulated) amplifier. It
does two things:

* accepts commands from the bridge: :meth:`Amplifier.set_level` and, if
  :attr:`Amplifier.supports_mute` is true, :meth:`Amplifier.set_mute`;
* optionally, sends arbitrary commands for hooks (:meth:`Amplifier.send_command`,
  e.g. to power the amplifier on) and re-reads the state (:meth:`Amplifier.refresh`);
* reports the amplifier's state whenever it learns of it -- in reply to a
  command, from a poll, or because someone turned the knob -- by calling
  :meth:`Amplifier.report`.

Backends must report *every* state they observe, including the echo of a
level the bridge just set. The bridge decides what is an echo; backends should
not try to filter.

Levels are in the amplifier's own units (see ``mapping.LevelScale``).
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AmplifierState:
    level: float
    muted: bool = False


StateListener = Callable[[AmplifierState], None]


class Amplifier(abc.ABC):
    #: Set to True in backends that implement :meth:`set_mute`.
    supports_mute: bool = False

    def __init__(self, options: Mapping[str, Any]) -> None:
        self.options = dict(options)
        self._listeners: list[StateListener] = []
        self._state: AmplifierState | None = None

    @property
    def state(self) -> AmplifierState | None:
        """Last state reported by the amplifier, or None if not yet known."""
        return self._state

    def add_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    def report(self, state: AmplifierState) -> None:
        """Called by backends whenever they observe the amplifier's state."""
        self._state = state
        for listener in self._listeners:
            try:
                listener(state)
            except Exception:  # a broken listener must not kill the backend's reader
                log.exception("amplifier state listener failed")

    @abc.abstractmethod
    async def start(self) -> None:
        """Connect and start reporting state. Should not block forever."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Disconnect and release resources."""

    @abc.abstractmethod
    async def set_level(self, level: float) -> None:
        """Ask the amplifier to go to ``level`` (already quantised and capped)."""

    async def set_mute(self, muted: bool) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support mute")

    async def send_command(self, command: str, raw: bool = False) -> None:
        """Send a command that isn't about volume (for hooks, e.g. power on).

        ``raw`` sends the text exactly as given instead of wrapping it the way
        the backend wraps its own commands.
        """
        raise NotImplementedError(f"{type(self).__name__} can't send commands")

    async def refresh(self) -> None:
        """Ask the amplifier for its state again, e.g. after waking it. Default: nothing."""
