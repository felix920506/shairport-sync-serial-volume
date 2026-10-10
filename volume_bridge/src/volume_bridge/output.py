"""Watches whether the DAC that Shairport Sync plays to is present.

USB DACs drop off the bus when they're switched off, go to standby or switch
to another input. The kernel lists each sound card under /proc/asound (by
number, "card1", and by name, "N05XD"), so the watcher polls for the card
named in the ALSA device string.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


def alsa_card_path(device: str, proc: Path = Path("/proc/asound")) -> Path | None:
    """The /proc entry for the card in an ALSA device string such as
    "hw:CARD=N05XD", "plughw:1,0" or "hw:N05XD", or None if it names no card
    (e.g. "default")."""
    m = re.search(r"CARD=([^,]+)", device) or re.match(r"^\w+:([^,=]+)(?:,|$)", device)
    if not m:
        return None
    card = m.group(1).strip('"')
    return proc / (f"card{card}" if card.isdigit() else card)


class OutputWatcher:
    def __init__(self, path: Path, on_change: Callable[[bool], None], interval: float = 0.5) -> None:
        self.path = path
        self.on_change = on_change
        self.interval = interval
        self.present = path.exists()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        log.info("watching output device %s (%s)", self.path.name,
                 "present" if self.present else "not present")
        self._task = asyncio.create_task(self._run(), name="output-watcher")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            present = self.path.exists()
            if present != self.present:
                self.present = present
                self.on_change(present)

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
