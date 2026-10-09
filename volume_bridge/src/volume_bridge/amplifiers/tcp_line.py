"""Amplifier reached through a serial-to-network bridge (ser2net, a Moxa/USR
serial server, etc.) speaking a line-based text protocol.

Most RS-232 amplifier protocols look like ``MV45<CR>`` / ``MV?<CR>`` -> ``MV45``,
so the default implementation is driven entirely by templates and regexes in
the config. For anything stranger (binary frames, checksums, handshakes),
subclass :class:`TcpLineAmplifier` and override the ``encode_*`` /
``parse_message`` / ``read_message`` / ``open_transport`` hooks, then point the
config at it with ``type = "your_module:YourClass"``.

Every received message is treated as an independent status report, whether it
answers our query, someone else's query, or is pushed by the amplifier because
its knob or remote was used. Nothing assumes that the next line received is the
reply to the last command sent. That makes the backend safe on a serial
multiplexer, where several programs share one RS-232 port, see each other's
commands, and interleave their traffic. For multiplexers:

* patterns must match a *whole* line (``re.fullmatch``), so fragments and
  other programs' commands don't parse as status by accident;
* ``ignore_patterns`` drops lines that are other programs' commands but would
  otherwise look like status;
* ``own_commands_echoed`` drops the copy of our own command that a multiplexer
  sends back to us, so it isn't mistaken for the amplifier's confirmation.

Options (all strings are Python ``str.format`` templates where noted):

    host, port            serial server address (required)
    line_ending           terminator appended to sent commands (default "\\r")
    receive_terminators   any of these characters ends a received line
                          (default "\\r\\n", so CR, LF and CRLF all work)
    max_line_length       longer unterminated input is discarded as noise (default 512)
    encoding              text encoding (default "ascii")
    set_level_command     template; fields: {level} (float), {level_int} (int)
    query_commands        list of commands sent on connect and on each poll
    level_pattern         regex with a named group "level"
    level_offset          added to the parsed level before reporting, and
                          subtracted before formatting (for "MV00 = -80 dB" style scales)
    mute_on_command, mute_off_command   enable mute support when both set
    mute_pattern          regex with a named group "muted"
    mute_on_values        values of "muted" that mean muted (default ["ON", "1"])
    ignore_patterns       list of regexes; matching lines are ignored (e.g. other
                          programs' commands on a multiplexer)
    own_commands_echoed   true if the port echoes our own commands back to us,
                          as many multiplexers do (default false). Each command
                          we send then cancels one identical received line seen
                          within echo_timeout. Leave false otherwise, or the
                          amplifier's real confirmation may be dropped.
    echo_timeout          seconds to wait for an echoed copy (default 2)
    poll_interval         seconds between queries; 0 disables polling (default 0)
    command_interval      minimum seconds between commands (default 0.05)
    connect_timeout       seconds (default 5)
    reconnect_max_delay   cap for exponential reconnect backoff (default 30)
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import re
from typing import Any, Mapping

from .base import Amplifier, AmplifierState

log = logging.getLogger(__name__)


class TcpLineAmplifier(Amplifier):
    def __init__(self, options: Mapping[str, Any]) -> None:
        super().__init__(options)
        o = self.options
        try:
            self.host: str = o["host"]
            self.port: int = int(o["port"])
        except KeyError as e:
            raise ValueError(f"tcp-line amplifier requires option {e.args[0]!r}") from None
        self.line_ending: str = o.get("line_ending", "\r")
        self.encoding: str = o.get("encoding", "ascii")
        terminators = o.get("receive_terminators", "\r\n").encode(self.encoding)
        if not terminators:
            raise ValueError("receive_terminators must not be empty")
        self._split = re.compile(b"[" + re.escape(terminators) + b"]")
        self.max_line_length = int(o.get("max_line_length", 512))
        self.ignore_patterns = [re.compile(p) for p in o.get("ignore_patterns", [])]
        self.own_commands_echoed = bool(o.get("own_commands_echoed", False))
        self.echo_timeout = float(o.get("echo_timeout", 2))
        self.set_level_command: str | None = o.get("set_level_command")
        self.query_commands: list[str] = list(o.get("query_commands", []))
        self.level_pattern = re.compile(o["level_pattern"]) if o.get("level_pattern") else None
        self.level_offset = float(o.get("level_offset", 0))
        self.mute_on_command: str | None = o.get("mute_on_command")
        self.mute_off_command: str | None = o.get("mute_off_command")
        self.mute_pattern = re.compile(o["mute_pattern"]) if o.get("mute_pattern") else None
        self.mute_on_values = {str(v).upper() for v in o.get("mute_on_values", ["ON", "1"])}
        self.poll_interval = float(o.get("poll_interval", 0))
        self.command_interval = float(o.get("command_interval", 0.05))
        self.connect_timeout = float(o.get("connect_timeout", 5))
        self.reconnect_max_delay = float(o.get("reconnect_max_delay", 30))
        self.supports_mute = bool(self.mute_on_command and self.mute_off_command)

        self._writer: asyncio.StreamWriter | None = None
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._early_muted = False
        self._rx_buffer = b""
        self._rx_lines: collections.deque[str] = collections.deque()
        self._awaiting_echo: collections.deque[tuple[str, float]] = collections.deque()

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="tcp-line-amplifier")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._close()

    async def _run(self) -> None:
        delay = 1.0
        while True:
            try:
                reader, writer = await asyncio.wait_for(self.open_transport(), self.connect_timeout)
            except (OSError, asyncio.TimeoutError) as e:
                log.warning("amplifier %s:%s unreachable (%s); retrying in %.0fs", self.host, self.port, e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.reconnect_max_delay)
                continue
            delay = 1.0
            self._rx_buffer, self._rx_lines = b"", collections.deque()
            self._awaiting_echo.clear()
            self._writer = writer
            self._connected.set()
            log.info("connected to amplifier at %s:%s", self.host, self.port)
            poller = asyncio.create_task(self._poll())
            try:
                await self.on_connected()
                while (message := await self.read_message(reader)) is not None:
                    self._handle_message(message)
            except (OSError, asyncio.IncompleteReadError) as e:
                log.warning("amplifier connection error: %s", e)
            finally:
                poller.cancel()
                self._connected.clear()
                await self._close()
            log.warning("amplifier connection lost; reconnecting")

    async def _poll(self) -> None:
        if self.poll_interval <= 0:
            return
        while True:
            await asyncio.sleep(self.poll_interval)
            await self._send_all(self.encode_queries())

    async def _close(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # -- commands from the bridge ----------------------------------------------

    async def set_level(self, level: float) -> None:
        await self._send_all([self.encode_set_level(level)])

    async def set_mute(self, muted: bool) -> None:
        if not self.supports_mute:
            await super().set_mute(muted)
        await self._send_all([self.encode_set_mute(muted)])

    async def _send_all(self, payloads: list[bytes]) -> None:
        async with self._send_lock:
            for payload in payloads:
                if self._writer is None:
                    raise ConnectionError("amplifier not connected")
                loop = asyncio.get_running_loop()
                wait = self._last_send + self.command_interval - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                log.debug("amplifier <- %r", payload)
                self._writer.write(payload)  # one write per command: never split on a shared port
                await self._writer.drain()
                self._last_send = loop.time()
                if self.own_commands_echoed:
                    text = payload.decode(self.encoding, errors="replace").strip()
                    self._awaiting_echo.append((text, self._last_send + self.echo_timeout))

    def _is_own_echo(self, message: str) -> bool:
        if not self._awaiting_echo:
            return False
        now = asyncio.get_running_loop().time()
        while self._awaiting_echo and self._awaiting_echo[0][1] < now:
            self._awaiting_echo.popleft()
        for i, (text, _) in enumerate(self._awaiting_echo):
            if text == message:
                del self._awaiting_echo[i]
                return True
        return False

    def _handle_message(self, message: str) -> None:
        if self._is_own_echo(message):
            log.debug("amplifier -> %r (echo of our own command, ignored)", message)
            return
        if self.is_foreign_command(message):
            log.debug("amplifier -> %r (another program's command, ignored)", message)
            return
        log.debug("amplifier -> %r", message)
        update = self.parse_message(message)
        if not update:
            return
        known = self.state
        level = update.get("level", known.level if known else None)
        muted = update.get("muted", known.muted if known else self._early_muted)
        if level is None:
            # Mute arrived before any level; hold it until we know the level.
            self._early_muted = muted
            return
        self.report(AmplifierState(level=level, muted=muted))

    # -- protocol hooks (override these for custom protocols) -------------------

    async def open_transport(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open the byte stream to the amplifier. Override for e.g. a local serial port."""
        return await asyncio.open_connection(self.host, self.port)

    async def on_connected(self) -> None:
        """Called after each (re)connect. Default: ask for the current state."""
        await self._send_all(self.encode_queries())

    async def read_message(self, reader: asyncio.StreamReader) -> str | None:
        """Read one message; return None on EOF.

        Lines may end in any of ``receive_terminators``, may arrive split
        across reads or several to a read, and blank lines are skipped.
        """
        while not self._rx_lines:
            chunk = await reader.read(4096)
            if not chunk:
                tail, self._rx_buffer = self._rx_buffer, b""
                text = tail.decode(self.encoding, errors="replace").strip()
                return text or None
            *lines, self._rx_buffer = self._split.split(self._rx_buffer + chunk)
            for raw in lines:
                if text := raw.decode(self.encoding, errors="replace").strip():
                    self._rx_lines.append(text)
            if len(self._rx_buffer) > self.max_line_length:
                log.debug("discarding %d bytes of unterminated input", len(self._rx_buffer))
                self._rx_buffer = b""
        return self._rx_lines.popleft()

    def _frame(self, command: str) -> bytes:
        return (command + self.line_ending).encode(self.encoding)

    def encode_queries(self) -> list[bytes]:
        return [self._frame(c) for c in self.query_commands]

    def encode_set_level(self, level: float) -> bytes:
        if not self.set_level_command:
            raise NotImplementedError("set_level_command is not configured")
        raw = level - self.level_offset
        return self._frame(self.set_level_command.format(level=raw, level_int=int(round(raw))))

    def encode_set_mute(self, muted: bool) -> bytes:
        command = self.mute_on_command if muted else self.mute_off_command
        assert command is not None
        return self._frame(command)

    def is_foreign_command(self, message: str) -> bool:
        """True for lines that are commands (ours or another program's), not status.

        Default: lines matching ``ignore_patterns``, or identical to one of our
        query commands (e.g. another program polling with "MV?").
        """
        return message in self.query_commands or any(p.fullmatch(message) for p in self.ignore_patterns)

    def parse_message(self, message: str) -> dict[str, Any] | None:
        """Turn one received message into ``{"level": float}`` and/or ``{"muted": bool}``."""
        if self.level_pattern and (m := self.level_pattern.fullmatch(message)):
            return {"level": float(m.group("level")) + self.level_offset}
        if self.mute_pattern and (m := self.mute_pattern.fullmatch(message)):
            return {"muted": m.group("muted").upper() in self.mute_on_values}
        return None
