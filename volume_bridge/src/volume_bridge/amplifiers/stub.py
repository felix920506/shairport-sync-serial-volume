"""An in-memory amplifier for development and testing.

It keeps a level and a mute flag and echoes every command back as a state
report, the way a real amplifier would.

To simulate someone using the amplifier's own controls, set ``control_port``
and connect with e.g. ``nc <host> <port>``. Commands (one per line):

    get            print the current state
    set <level>    set the level as if the knob were turned
    up / down      one step up or down
    mute on|off    toggle mute (if mute is enabled)

Options:
    initial_level  starting level (default 20)
    step           step for up/down (default 1)
    min_level, max_level  clamp range for knob changes (default 0 / 100)
    supports_mute  whether to accept set_mute (default true)
    powered        power state reported to hooks: true, false, or unset (unknown).
                   The commands "PWR 1" / "PWR 0" (send_command) switch it.
    control_host   address for the control port (default 0.0.0.0)
    control_port   TCP port for the simulated front panel (default: disabled)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from .base import Amplifier, AmplifierState

log = logging.getLogger(__name__)


class StubAmplifier(Amplifier):
    def __init__(self, options: Mapping[str, Any]) -> None:
        super().__init__(options)
        self._level = float(self.options.get("initial_level", 20))
        self._muted = False
        self._step = float(self.options.get("step", 1))
        self._min = float(self.options.get("min_level", 0))
        self._max = float(self.options.get("max_level", 100))
        self.supports_mute = bool(self.options.get("supports_mute", True))
        self._server: asyncio.base_events.Server | None = None
        #: Commands received through send_command (for hooks), as (text, raw).
        self.received_commands: list[tuple[str, bool]] = []
        powered = self.options.get("powered")
        self.powered: bool | None = None if powered is None else bool(powered)

    async def start(self) -> None:
        port = self.options.get("control_port")
        if port is not None:
            host = self.options.get("control_host", "0.0.0.0")
            self._server = await asyncio.start_server(self._handle_client, host, int(port))
            log.info("stub amplifier control port listening on %s:%s", host, port)
        log.info("stub amplifier ready at level %g", self._level)
        self._emit()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def set_level(self, level: float) -> None:
        log.info("stub amplifier: set level %g", level)
        self._level = level
        self._emit()

    async def set_mute(self, muted: bool) -> None:
        if not self.supports_mute:
            await super().set_mute(muted)
        log.info("stub amplifier: mute %s", "on" if muted else "off")
        self._muted = muted
        self._emit()

    async def send_command(self, command: str, raw: bool = False) -> None:
        log.info("stub amplifier: command %r%s", command, " (raw)" if raw else "")
        self.received_commands.append((command, raw))
        if command in ("PWR 1", "PWR 0"):
            self.powered = command == "PWR 1"

    async def query_power(self, timeout: float = 2.0) -> bool | None:
        return self.powered

    async def refresh(self) -> None:
        self._emit()

    # -- simulated front panel -------------------------------------------------

    def knob(self, level: float) -> None:
        """Simulate a local volume change on the amplifier."""
        self._level = max(self._min, min(self._max, level))
        log.info("stub amplifier: front panel level %g", self._level)
        self._emit()

    def _emit(self) -> None:
        self.report(AmplifierState(level=self._level, muted=self._muted))

    def _describe(self) -> str:
        return f"level={self._level:g} muted={'on' if self._muted else 'off'}"

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"stub amplifier. commands: get | set <level> | up | down | mute on|off\n")
        try:
            while line := await reader.readline():
                reply = self._command(line.decode(errors="replace").strip())
                writer.write((reply + "\n").encode())
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    def _command(self, text: str) -> str:
        parts = text.split()
        if not parts:
            return self._describe()
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd == "get":
                pass
            elif cmd == "set" and len(args) == 1:
                self.knob(float(args[0]))
            elif cmd == "up":
                self.knob(self._level + self._step)
            elif cmd == "down":
                self.knob(self._level - self._step)
            elif cmd == "mute" and len(args) == 1 and args[0] in ("on", "off") and self.supports_mute:
                self._muted = args[0] == "on"
                self._emit()
            else:
                return f"error: unknown command {text!r}"
        except ValueError:
            return f"error: bad number in {text!r}"
        return self._describe()
