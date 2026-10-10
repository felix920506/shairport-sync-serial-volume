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
from typing import Any, Callable

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
        self.recovery: OutputRecovery | None = None  # set by the caller, for cleanup
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


class OutputRecovery:
    """What to do when the DAC disappears while audio is playing: fire the
    output_lost hooks (e.g. power it up again) and, with ``pause``, pause the
    source until the DAC is back, so nothing is skipped meanwhile.

    It resumes only what it paused: not if the source has disconnected, or
    started playing again on its own, in the meantime. Before resuming it waits
    for the output_lost hooks to finish (an amplifier warming up) and then
    ``resume_delay`` seconds, so the DAC has settled.
    """

    def __init__(self, card: str, source: Any, hooks: Any, pause: bool = False,
                 resume_delay: float = 2.0) -> None:
        self.card = card
        self.source = source
        self.hooks = hooks
        self.pause = pause
        self.resume_delay = resume_delay
        #: The sources connected when we paused; None when we haven't paused.
        self._paused: frozenset[str] | None = None
        self._resuming = False
        self.present = True
        self._task: asyncio.Task | None = None

    def on_output_change(self, present: bool) -> None:
        self.present = present
        if present:
            log.info("output device %s is back", self.card)
            if self._paused is not None:
                self._resuming = True
                self._start(self._resume())
            return
        if self._resuming:
            self._task.cancel()  # gone again before we resumed: stay paused
            self._resuming = False
            log.info("output device %s is gone again: not resuming yet", self.card)
            return
        if not self.source.playing:
            log.info("output device %s is gone (not playing: no action)", self.card)
            return
        if self.hooks.pending("output_lost"):
            log.info("output device %s is gone again (output_lost hooks still running)", self.card)
        else:
            log.info("output device %s is gone while playing", self.card)
            self.hooks.fire("output_lost")
        if self.pause:
            self._start(self._pause())

    def on_playing(self, playing: bool) -> None:
        if playing and self._paused is not None and not self._resuming:
            log.info("source resumed on its own: not resuming it when the output device is back")
            self._paused = None

    def _start(self, coro: Any) -> None:
        if self._task is not None:
            self._task.cancel()
        self._task = asyncio.create_task(coro, name="output-recovery")

    async def _pause(self) -> None:
        clients = self.source.clients
        try:
            await self.source.pause()
        except Exception as e:
            log.warning("couldn't pause the source: %s", e)
            return
        self._paused = clients
        log.info("paused the source until output device %s is back", self.card)
        if self.present:  # it came back while we were pausing
            self._resuming = True
            self._task = asyncio.create_task(self._resume(), name="output-recovery")

    async def _resume(self) -> None:
        try:
            while self.hooks.pending("output_lost"):
                await asyncio.sleep(0.2)
            await asyncio.sleep(self.resume_delay)
            paused, self._paused = self._paused, None
            if not paused or not (paused & self.source.clients):
                log.info("not resuming: the source that was paused has disconnected")
            elif self.source.playing:
                log.info("not resuming: the source is already playing")
            else:
                await self.source.play()
                log.info("resumed the source")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("couldn't resume the source: %s", e)
        finally:
            self._resuming = False

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
