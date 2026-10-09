"""Shairport Sync's D-Bus interface, reduced to what the bridge needs.

* ``org.gnome.ShairportSync.RemoteControl.AirplayVolume`` -- the volume the
  source has set (updated from Shairport Sync's 'pvol' metadata, emits
  PropertiesChanged).
* ``org.gnome.ShairportSync.RemoteControl.SetAirplayVolume(d)`` -- asks the
  *source* to change its volume for this speaker. On AirPlay 2 this sends a
  volume notification over the event channel; on classic AirPlay it uses DACP.
  Shairport Sync then applies the new volume locally, which comes back to us
  as an AirplayVolume change (handled as an echo by the bridge).
* ``org.gnome.ShairportSync.Active`` -- whether a play session is active.

Whether a source is connected isn't on D-Bus (``RemoteControl.Client`` keeps
the last source's address forever), so it comes from Shairport Sync's UDP
metadata: an ``ssnc``/``conn`` message when a source connects and
``ssnc``/``disc`` when it disconnects, each carrying the source's address.
A connected source takes volume changes even while nothing is playing; with
no source connected, SetAirplayVolume is silently dropped.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import struct
from typing import Any, Callable

from dbus_fast import BusType, DBusError, Variant
from dbus_fast.aio import MessageBus, ProxyInterface

log = logging.getLogger(__name__)

SERVICE = "org.gnome.ShairportSync"
PATH = "/org/gnome/ShairportSync"
MAIN_INTERFACE = "org.gnome.ShairportSync"
REMOTE_INTERFACE = "org.gnome.ShairportSync.RemoteControl"


class _MetadataProtocol(asyncio.DatagramProtocol):
    """Receives Shairport Sync's UDP metadata: type (4 bytes), code (4 bytes), data."""

    def __init__(self, on_message: Callable[[bytes, bytes, bytes], None]) -> None:
        self.on_message = on_message

    def datagram_received(self, data: bytes, _addr: Any) -> None:
        if len(data) >= 8:
            self.on_message(data[:4], data[4:8], data[8:])


class ShairportSync:
    def __init__(self, bus: str = "system", service: str = SERVICE, metadata_port: int = 0) -> None:
        self.bus_name = bus
        self.service = service
        self.metadata_port = metadata_port
        self.on_volume: Callable[[float], None] = lambda volume: None
        self.on_active: Callable[[bool], None] = lambda active: None
        self._bus: MessageBus | None = None
        self._remote: ProxyInterface | None = None
        self._volume: float | None = None
        self._active = False
        self._metadata: asyncio.DatagramTransport | None = None
        # None until the first metadata packet: until then, fall back to "active".
        self._connections: collections.Counter[str] | None = None

    @property
    def volume(self) -> float | None:
        return self._volume

    @property
    def active(self) -> bool:
        return self._active

    @property
    def connected(self) -> bool:
        """True while a source is connected (playing or not).

        Without metadata (not configured, or nothing received yet) this is the
        best available approximation: whether a play session is active.
        """
        if self._connections is None:
            return self._active
        return bool(self._connections)

    async def connect(self, retry_interval: float = 1.0, timeout: float | None = None) -> None:
        """Connect, waiting for Shairport Sync to appear on the bus."""
        if self.metadata_port and self._metadata is None:
            # Listen first: Shairport Sync sends "conn" as soon as a source connects.
            self._metadata, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _MetadataProtocol(self._metadata_message),
                local_addr=("127.0.0.1", self.metadata_port))
        self._bus = await self._open_bus()
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            # Shairport Sync takes the bus name before it has exported every
            # interface, so wait until the ones we use are actually there.
            try:
                introspection = await self._bus.introspect(self.service, PATH)
                exported = {i.name for i in introspection.interfaces}
                missing = {MAIN_INTERFACE, REMOTE_INTERFACE} - exported
                reason = f"missing {', '.join(sorted(missing))}" if missing else ""
            except DBusError as e:
                reason = e.type
            if not reason:
                break
            if deadline is not None and loop.time() > deadline:
                raise TimeoutError(f"{self.service} not available: {reason}")
            log.info("waiting for %s on the %s bus (%s)", self.service, self.bus_name, reason)
            await asyncio.sleep(retry_interval)

        proxy = self._bus.get_proxy_object(self.service, PATH, introspection)
        self._remote = proxy.get_interface(REMOTE_INTERFACE)
        main = proxy.get_interface(MAIN_INTERFACE)
        proxy.get_interface("org.freedesktop.DBus.Properties").on_properties_changed(
            self._properties_changed
        )
        self._volume = await self._remote.get_airplay_volume()
        self._active = await main.get_active()
        log.info("connected to Shairport Sync (airplay volume %.2f, %s)",
                 self._volume, "active" if self._active else "idle")

    async def _open_bus(self) -> MessageBus:
        if self.bus_name == "system":
            return await MessageBus(bus_type=BusType.SYSTEM).connect()
        if self.bus_name == "session":
            return await MessageBus(bus_type=BusType.SESSION).connect()
        return await MessageBus(bus_address=self.bus_name).connect()

    def _properties_changed(self, interface: str, changed: dict[str, Variant], _invalidated: list[str]) -> None:
        if interface == REMOTE_INTERFACE and "AirplayVolume" in changed:
            volume = float(changed["AirplayVolume"].value)
            if volume != self._volume:
                self._volume = volume
                log.debug("airplay volume is now %.2f", volume)
                self.on_volume(volume)
        if interface == MAIN_INTERFACE and "Active" in changed:
            self._active = bool(changed["Active"].value)
            log.info("play session %s", "active" if self._active else "ended")
            self.on_active(self._active)

    def _metadata_message(self, type_: bytes, code: bytes, data: bytes) -> None:
        if self._connections is None:
            log.info("receiving Shairport Sync metadata")
            self._connections = collections.Counter()
        log.debug("metadata %s/%s %r", type_.decode("latin-1"), code.decode("latin-1"), data[:64])
        if type_ != b"ssnc" or code not in (b"conn", b"disc"):
            return
        client = data.decode("utf-8", errors="replace")
        if code == b"conn":
            self._connections[client] += 1
            log.info("source %s connected", client)
        elif self._connections[client] > 0:
            self._connections[client] -= 1
            log.info("source %s disconnected", client)
        self._connections += collections.Counter()  # drop zero counts

    async def set_volume(self, volume: float) -> None:
        if self._remote is None:
            raise ConnectionError("not connected to Shairport Sync")
        await self._remote.call_set_airplay_volume(volume)

    async def wait_disconnected(self) -> Any:
        if self._bus is not None:
            await self._bus.wait_for_disconnect()

    def close(self) -> None:
        if self._metadata is not None:
            self._metadata.close()
            self._metadata = None
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None
