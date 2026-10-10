"""Telnet (RFC 854) framing for serial servers that speak it instead of raw TCP.

Many serial servers (Moxa, Lantronix, USR, ser2net with a telnet accepter)
offer a "telnet" mode: the serial data is carried in a telnet session, so the
byte 0xFF is escaped and option negotiation (IAC WILL/WONT/DO/DONT, IAC SB ...
IAC SE) is mixed into the stream. :func:`open_telnet` wraps a TCP connection so
the rest of the backend sees only the serial bytes.

Negotiation: we ask for BINARY (8-bit clean data, no CR NUL padding) and
SUPPRESS-GO-AHEAD in both directions and refuse every other option, including
the server echoing our commands back (ECHO) and RFC 2217 COM port control (the
serial line settings stay as configured on the server). Subnegotiations are
dropped. If the server refuses BINARY, the NVT rules apply: a CR we send is
followed by NUL, and NUL after a received CR is removed.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
BINARY, SGA = 0, 3
#: Options we're willing to enable on both sides.
_SUPPORTED = {BINARY, SGA}
_NAMES = {DONT: "DONT", DO: "DO", WONT: "WONT", WILL: "WILL"}


class TelnetProtocol:
    """Telnet state for one connection: strips telnet commands from received
    data, answers negotiation, and escapes data we send."""

    def __init__(self, send_raw) -> None:
        self._send_raw = send_raw          # writes bytes to the socket as is
        self.local: set[int] = set()       # options we have enabled (we WILL)
        self.remote: set[int] = set()      # options the server has enabled (it WILL)
        self._requested: set[tuple[int, int]] = set()  # (verb, option) we sent, awaiting a reply
        self._state = "data"
        self._verb = 0
        self._last_cr = False

    def start(self) -> None:
        """Offer/request the options we want."""
        for option in sorted(_SUPPORTED):
            self._request(WILL, option)
            self._request(DO, option)

    def _request(self, verb: int, option: int) -> None:
        self._requested.add((verb, option))
        self._send(verb, option)

    def _send(self, verb: int, option: int) -> None:
        log.debug("telnet <- IAC %s %d", _NAMES[verb], option)
        self._send_raw(bytes((IAC, verb, option)))

    # -- received data ----------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        """Return the serial data in ``data``, handling any telnet commands."""
        out = bytearray()
        for byte in data:
            if self._state == "data":
                if byte == IAC:
                    self._state = "iac"
                elif byte == 0 and self._last_cr and BINARY not in self.remote:
                    self._last_cr = False  # NVT: CR NUL is a bare CR
                else:
                    out.append(byte)
                    self._last_cr = byte == 13
            elif self._state == "iac":
                if byte == IAC:
                    out.append(IAC)  # escaped 0xFF
                    self._last_cr = False
                    self._state = "data"
                elif byte in (WILL, WONT, DO, DONT):
                    self._verb = byte
                    self._state = "option"
                elif byte == SB:
                    self._state = "sb"
                else:
                    self._state = "data"  # NOP, GA, etc.: nothing to do
            elif self._state == "option":
                self._negotiate(self._verb, byte)
                self._state = "data"
            elif self._state == "sb":
                if byte == IAC:
                    self._state = "sb_iac"
            elif self._state == "sb_iac":
                self._state = "data" if byte == SE else "sb"
        return bytes(out)

    def _negotiate(self, verb: int, option: int) -> None:
        log.debug("telnet -> IAC %s %d", _NAMES[verb], option)
        if verb in (WILL, WONT):
            enabled, ours = self.remote, (DO, option)
            agree, refuse = DO, DONT
        else:
            enabled, ours = self.local, (WILL, option)
            agree, refuse = WILL, WONT
        answered = ours in self._requested
        self._requested.discard(ours)
        if verb in (WILL, DO):
            if option in _SUPPORTED:
                if option not in enabled:
                    enabled.add(option)
                    if not answered:
                        self._send(agree, option)
            elif not answered:
                self._send(refuse, option)
        else:  # WONT / DONT: always acknowledged once, never argued with
            if option in enabled:
                enabled.discard(option)
                if not answered:
                    self._send(refuse, option)

    # -- data we send -------------------------------------------------------------

    def encode(self, data: bytes) -> bytes:
        data = data.replace(b"\xff", b"\xff\xff")
        if BINARY not in self.local:
            data = data.replace(b"\r", b"\r\0")
        return data


class TelnetReader:
    """The parts of :class:`asyncio.StreamReader` the backend uses."""

    def __init__(self, reader: asyncio.StreamReader, protocol: TelnetProtocol) -> None:
        self._reader, self._protocol = reader, protocol

    async def read(self, n: int = -1) -> bytes:
        # A chunk may be nothing but negotiation; b"" must only mean EOF.
        while chunk := await self._reader.read(n):
            if data := self._protocol.feed(chunk):
                return data
        return b""


class TelnetWriter:
    """The parts of :class:`asyncio.StreamWriter` the backend uses."""

    def __init__(self, writer: asyncio.StreamWriter, protocol: TelnetProtocol) -> None:
        self._writer, self._protocol = writer, protocol

    def write(self, data: bytes) -> None:
        self._writer.write(self._protocol.encode(data))

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()


async def open_telnet(host: str, port: int) -> tuple[TelnetReader, TelnetWriter]:
    reader, writer = await asyncio.open_connection(host, port)
    protocol = TelnetProtocol(writer.write)
    protocol.start()
    return TelnetReader(reader, protocol), TelnetWriter(writer, protocol)
