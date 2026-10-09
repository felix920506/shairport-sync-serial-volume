"""Amplifier reached through a serial-to-network bridge (ser2net, a Moxa/USR
serial server, a serial multiplexer, etc.).

The protocol is described in config: how messages are *framed* in the byte
stream (see :mod:`.framing`: terminated lines, fixed-length frames, start/end
markers, or no delimiters at all), command templates, and regexes that pull
volume and mute out of received messages. For anything stranger (checksums,
handshakes, multi-field binary frames), subclass :class:`TcpSerialAmplifier`
and override the hooks at the bottom (``create_framer``, ``verify_frame``,
``encode_*``, ``parse_message``, ``open_transport``), then point the config at
it with ``type = "your_module:YourClass"``.

Every received message is treated as an independent status report, whether it
answers our query, someone else's query, or is pushed by the amplifier because
its knob or remote was used. Nothing assumes that the next message received is
the reply to the last command sent. That makes the backend safe on a serial
multiplexer, where several programs share one RS-232 port, see each other's
commands, and interleave their traffic. For multiplexers:

* ``ignore_patterns`` drops messages that are other programs' commands but
  would otherwise look like status;
* ``own_commands_echoed`` drops the copy of our own command that a multiplexer
  sends back to us, so it isn't mistaken for the amplifier's confirmation.

Options. Strings may contain any byte via TOML escapes ("\\u0002"); with
``encoding = "latin-1"`` each character is exactly one byte, which suits
binary protocols.

  Connection
    host, port            serial server address (required)
    connect_timeout       seconds (default 5)
    reconnect_max_delay   cap for exponential reconnect backoff (default 30)

  Framing of received data
    framing               "delimited" (default), "fixed", "markers" or "stream"
    receive_terminators   delimited: any of these bytes ends a message (default "\\r\\n")
    frame_length          fixed: bytes per message
    start_marker, end_marker, trailer_length   markers: e.g. "\\u0002", "\\u0003", 1
    frame_gap             seconds of silence that completes/abandons a partial
                          message (default 0.05 for fixed and stream, else off)
    max_frame_length      unframed input beyond this is discarded (default 512)
    encoding              text encoding (default "ascii"; "latin-1" for binary)

  Matching received messages
    level_pattern         regex with a named group "level"
    level_encoding        how the "level" group is written: "decimal" (default),
                          "hex", or "byte" (raw big-endian bytes)
    level_offset          added to the parsed level before reporting, and
                          subtracted before formatting (for "MV00 = -80 dB" style scales)
    mute_pattern          regex with a named group "muted"
    mute_on_values        values of "muted" that mean muted (default ["ON", "1"])
    power_pattern         regex with a named group "power" (for hooks' if_amplifier_off)
    power_on_values       values of "power" that mean on (default ["ON", "1"])
    match                 "full" (default): a pattern must match the whole
                          message; "search": it may match anywhere in it.
                          (With framing = "stream" the patterns *are* the framing.)
    ignore_patterns       regexes for messages to ignore (other programs' commands)

  Commands
    command_prefix, command_suffix   wrapped around every command (default "" and "\\r")
    power_query_command   asks for the power state; the answer must match power_pattern
    set_level_command     template; fields: {level} (float), {level_int} (int),
                          {level_chr} (the level as one character/byte)
    query_commands        list of commands sent on connect and on each poll
    mute_on_command, mute_off_command   enable mute support when both set
    poll_interval         seconds between queries; 0 disables polling (default 0)
    command_interval      minimum seconds between commands (default 0.05)

  Shared ports
    own_commands_echoed   true if the port echoes our own commands back to us,
                          as many multiplexers do (default false). Each command
                          we send then cancels one identical received message
                          seen within echo_timeout. Leave false otherwise, or
                          the amplifier's real confirmation may be dropped.
    echo_timeout          seconds to wait for an echoed copy (default 2)
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import re
from typing import Any, Mapping

from .base import Amplifier, AmplifierState
from .framing import Frame, Framer, create_framer

log = logging.getLogger(__name__)

_LEVEL_DECODERS = {
    "decimal": lambda text, encoding: float(text),
    "hex": lambda text, encoding: float(int(text, 16)),
    "byte": lambda text, encoding: float(int.from_bytes(text.encode(encoding), "big")),
}


class TcpSerialAmplifier(Amplifier):
    def __init__(self, options: Mapping[str, Any]) -> None:
        super().__init__(options)
        o = self.options
        try:
            self.host: str = o["host"]
            self.port: int = int(o["port"])
        except KeyError as e:
            raise ValueError(f"tcp-serial amplifier requires option {e.args[0]!r}") from None
        self.encoding: str = o.get("encoding", "ascii")

        self.level_pattern = re.compile(o["level_pattern"]) if o.get("level_pattern") else None
        self.level_offset = float(o.get("level_offset", 0))
        level_encoding = o.get("level_encoding", "decimal")
        if level_encoding not in _LEVEL_DECODERS:
            raise ValueError(f"level_encoding must be one of {sorted(_LEVEL_DECODERS)}")
        self._decode_level = _LEVEL_DECODERS[level_encoding]
        self.mute_pattern = re.compile(o["mute_pattern"]) if o.get("mute_pattern") else None
        self.mute_on_values = {str(v).upper() for v in o.get("mute_on_values", ["ON", "1"])}
        self.power_pattern = re.compile(o["power_pattern"]) if o.get("power_pattern") else None
        self.power_on_values = {str(v).upper() for v in o.get("power_on_values", ["ON", "1"])}
        self.power_query_command: str | None = o.get("power_query_command")
        self.ignore_patterns = [re.compile(p) for p in o.get("ignore_patterns", [])]
        match = o.get("match", "full")
        if match not in ("full", "search"):
            raise ValueError("match must be 'full' or 'search'")
        self.match_full = match == "full"

        self.command_prefix: str = o.get("command_prefix", "")
        self.command_suffix: str = o.get("command_suffix", "\r")
        self.set_level_command: str | None = o.get("set_level_command")
        self.query_commands: list[str] = list(o.get("query_commands", []))
        self.mute_on_command: str | None = o.get("mute_on_command")
        self.mute_off_command: str | None = o.get("mute_off_command")
        self.supports_mute = bool(self.mute_on_command and self.mute_off_command)
        self.poll_interval = float(o.get("poll_interval", 0))
        self.command_interval = float(o.get("command_interval", 0.05))
        self.connect_timeout = float(o.get("connect_timeout", 5))
        self.reconnect_max_delay = float(o.get("reconnect_max_delay", 30))
        self.own_commands_echoed = bool(o.get("own_commands_echoed", False))
        self.echo_timeout = float(o.get("echo_timeout", 2))

        self.framer = self.create_framer()
        # How our own query commands look when framed, to recognise other
        # programs sending the same queries on a shared port.
        self._query_messages = {
            self._decode(f.payload) for q in self.encode_queries() for f in self.framer.frames_of(q)
        }

        self._writer: asyncio.StreamWriter | None = None
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self._task: asyncio.Task | None = None
        self._early_muted = False
        self._rx_frames: collections.deque[Frame] = collections.deque()
        self._awaiting_echo: collections.deque[tuple[str, float]] = collections.deque()
        self._power_waiters: list[asyncio.Future[bool]] = []

    def _decode(self, data: bytes) -> str:
        return data.decode(self.encoding, errors="replace")

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="tcp-serial-amplifier")

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
            self.framer.reset()
            self._rx_frames.clear()
            self._awaiting_echo.clear()
            self._writer = writer
            log.info("connected to amplifier at %s:%s", self.host, self.port)
            poller = asyncio.create_task(self._poll())
            try:
                await self.on_connected()
                while (frame := await self.read_frame(reader)) is not None:
                    self._handle_frame(frame)
            except (OSError, asyncio.IncompleteReadError) as e:
                log.warning("amplifier connection error: %s", e)
            finally:
                poller.cancel()
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

    async def send_command(self, command: str, raw: bool = False) -> None:
        await self._send_all([command.encode(self.encoding) if raw else self.encode_command(command)])

    async def refresh(self) -> None:
        await self._send_all(self.encode_queries())

    async def query_power(self, timeout: float = 2.0) -> bool | None:
        if not (self.power_pattern and self.power_query_command):
            return None
        waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._power_waiters.append(waiter)
        try:
            await self._send_all([self.encode_command(self.power_query_command)])
            return await asyncio.wait_for(asyncio.shield(waiter), timeout)
        except asyncio.TimeoutError:
            log.warning("amplifier didn't answer the power query within %gs", timeout)
            return None
        finally:
            self._power_waiters.remove(waiter)

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
                    # The echo comes back through the same framer, so expect
                    # exactly the message(s) our command frames into.
                    expires = self._last_send + self.echo_timeout
                    for frame in self.framer.frames_of(payload):
                        self._awaiting_echo.append((self._decode(frame.payload), expires))

    # -- received data ------------------------------------------------------------

    async def read_frame(self, reader: asyncio.StreamReader) -> Frame | None:
        """Return the next received frame, or None on EOF."""
        while not self._rx_frames:
            gap = self.framer.gap if self.framer.pending else None
            try:
                chunk = await asyncio.wait_for(reader.read(4096), gap)
            except asyncio.TimeoutError:
                self._rx_frames.extend(self.framer.flush())  # silence ends a partial message
                continue
            if not chunk:
                self._rx_frames.extend(self.framer.flush())
                if not self._rx_frames:
                    return None
                break
            self._rx_frames.extend(self.framer.feed(chunk))
        return self._rx_frames.popleft()

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

    def _handle_frame(self, frame: Frame) -> None:
        if not self.verify_frame(frame):
            log.debug("amplifier -> %r (failed verification, ignored)", frame.raw)
            return
        message = self._decode(frame.payload)
        if self._is_own_echo(message):
            log.debug("amplifier -> %r (echo of our own command, ignored)", message)
            return
        if self.is_foreign_command(message):
            log.debug("amplifier -> %r (another program's command, ignored)", message)
            return
        log.debug("amplifier -> %r", message)
        if self.power_pattern and (m := self._match(self.power_pattern, message)):
            powered = m.group("power").upper() in self.power_on_values
            for waiter in self._power_waiters:
                if not waiter.done():
                    waiter.set_result(powered)
            return
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

    def _match(self, pattern: re.Pattern[str], message: str) -> re.Match[str] | None:
        return pattern.fullmatch(message) if self.match_full else pattern.search(message)

    # -- protocol hooks (override these for custom protocols) -------------------

    def create_framer(self) -> Framer:
        """Build the framer for received data. Override for custom framing."""
        stream_patterns = [p.pattern for p in (self.level_pattern, self.mute_pattern, self.power_pattern) if p]
        stream_patterns += [p.pattern for p in self.ignore_patterns]
        return create_framer(self.options, self.encoding, stream_patterns)

    def verify_frame(self, frame: Frame) -> bool:
        """Return False to drop a frame, e.g. on a bad checksum in ``frame.raw``."""
        return True

    async def open_transport(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open the byte stream to the amplifier. Override for e.g. a local serial port."""
        return await asyncio.open_connection(self.host, self.port)

    async def on_connected(self) -> None:
        """Called after each (re)connect. Default: ask for the current state."""
        await self._send_all(self.encode_queries())

    def encode_command(self, command: str) -> bytes:
        """Wrap and encode one command. Override to add e.g. a checksum."""
        return (self.command_prefix + command + self.command_suffix).encode(self.encoding)

    def encode_queries(self) -> list[bytes]:
        return [self.encode_command(c) for c in self.query_commands]

    def encode_set_level(self, level: float) -> bytes:
        if not self.set_level_command:
            raise NotImplementedError("set_level_command is not configured")
        raw = level - self.level_offset
        level_int = int(round(raw))
        level_chr = chr(level_int) if 0 <= level_int < 0x110000 else ""
        return self.encode_command(
            self.set_level_command.format(level=raw, level_int=level_int, level_chr=level_chr))

    def encode_set_mute(self, muted: bool) -> bytes:
        command = self.mute_on_command if muted else self.mute_off_command
        assert command is not None
        return self.encode_command(command)

    def is_foreign_command(self, message: str) -> bool:
        """True for messages that are commands (another program's), not status.

        Default: messages matching ``ignore_patterns``, or identical to one of
        our query commands (e.g. another program polling with "MV?").
        """
        return message in self._query_messages or any(
            self._match(p, message) for p in self.ignore_patterns)

    def parse_message(self, message: str) -> dict[str, Any] | None:
        """Turn one received message into ``{"level": float}`` and/or ``{"muted": bool}``."""
        if self.level_pattern and (m := self._match(self.level_pattern, message)):
            return {"level": self._decode_level(m.group("level"), self.encoding) + self.level_offset}
        if self.mute_pattern and (m := self._match(self.mute_pattern, message)):
            return {"muted": m.group("muted").upper() in self.mute_on_values}
        return None
