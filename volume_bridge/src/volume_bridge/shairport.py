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
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from dbus_fast import BusType, DBusError, Variant
from dbus_fast.aio import MessageBus, ProxyInterface

log = logging.getLogger(__name__)

SERVICE = "org.gnome.ShairportSync"
PATH = "/org/gnome/ShairportSync"
MAIN_INTERFACE = "org.gnome.ShairportSync"
REMOTE_INTERFACE = "org.gnome.ShairportSync.RemoteControl"


class ShairportSync:
    def __init__(self, bus: str = "system", service: str = SERVICE) -> None:
        self.bus_name = bus
        self.service = service
        self.on_volume: Callable[[float], None] = lambda volume: None
        self.on_active: Callable[[bool], None] = lambda active: None
        self._bus: MessageBus | None = None
        self._remote: ProxyInterface | None = None
        self._volume: float | None = None
        self._active = False

    @property
    def volume(self) -> float | None:
        return self._volume

    @property
    def active(self) -> bool:
        return self._active

    async def connect(self, retry_interval: float = 1.0, timeout: float | None = None) -> None:
        """Connect, waiting for Shairport Sync to appear on the bus."""
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

    async def set_volume(self, volume: float) -> None:
        if self._remote is None:
            raise ConnectionError("not connected to Shairport Sync")
        await self._remote.call_set_airplay_volume(volume)

    async def wait_disconnected(self) -> Any:
        if self._bus is not None:
            await self._bus.wait_for_disconnect()

    def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None
