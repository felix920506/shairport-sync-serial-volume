"""Watches whether the DAC that Shairport Sync plays to is present.

USB DACs drop off the bus when they're switched off, go to standby or switch
to another input. The kernel lists each sound card in sysfs as
/sys/class/sound/cardN, with its name in cardN/id ("N05XD"), so the watcher
polls for the card named in the ALSA device string. (/proc/asound would say
the same, but Docker hides it inside containers.)
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SYSFS = Path("/sys/class/sound")


def alsa_card(device: str) -> str | None:
    """The card in an ALSA device string such as "hw:CARD=N05XD", "plughw:1,0"
    or "hw:N05XD" (a name or a number), or None if it names no card ("default")."""
    m = re.search(r"CARD=([^,]+)", device) or re.match(r"^\w+:([^,=]+)(?:,|$)", device)
    return m.group(1).strip('"') if m else None


def card_present(card: str, sysfs: Path = SYSFS) -> bool:
    if card.isdigit():
        return (sysfs / f"card{card}").exists()
    for path in sysfs.glob("card*/id"):  # the number can change when the card reappears
        try:
            if path.read_text().strip() == card:
                return True
        except OSError:
            pass  # removed while we looked
    return False


class OutputWatcher:
    def __init__(self, card: str, on_change: Callable[[bool], None], interval: float = 0.5,
                 sysfs: Path = SYSFS) -> None:
        self.card = card
        self.on_change = on_change
        self.interval = interval
        self.sysfs = sysfs
        self.present = card_present(card, sysfs)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        log.info("watching output device %s (%s)", self.card,
                 "present" if self.present else "not present")
        self._task = asyncio.create_task(self._run(), name="output-watcher")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            present = card_present(self.card, self.sysfs)
            if present != self.present:
                self.present = present
                self.on_change(present)

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
