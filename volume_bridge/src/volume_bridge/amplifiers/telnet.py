"""Telnet transport for serial servers in "telnet mode" (Moxa, Lantronix, USR,
ser2net with a telnet accepter), using telnetlib3.

telnetlib3 handles the telnet side: escaping 0xFF, option negotiation and
subnegotiations. With ``encoding=False`` and ``force_binary=True`` its reader
and writer carry raw bytes, so the rest of the backend sees only the serial
data, as with a raw TCP connection. RFC 2217 COM port control isn't used: the
serial line settings stay as configured on the server.
"""

from __future__ import annotations

import asyncio

import telnetlib3


async def open_telnet(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await telnetlib3.open_connection(
        host, port,
        client_factory=telnetlib3.TelnetClient,  # never the interactive terminal client
        encoding=False, force_binary=True,
        # Let the server finish negotiating: some drop serial data sent before that.
        connect_minwait=0.5, connect_maxwait=2.0)
    return reader, writer  # type: ignore[return-value]  # same read/write/drain/close API
