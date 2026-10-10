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
    """What to do when the DAC is missing while audio is playing: fire the
    output_lost hooks (e.g. power it up again) and, with ``pause``, pause the
    source, so nothing is skipped meanwhile. With ``resume`` too, it resumes the
    source once the DAC is back.

    "Missing while playing" is either the DAC disappearing during playback, or
    playback starting while it's gone. In the second case, with ``pause``, the
    source is held (paused) and always resumed once the DAC is ready, whatever
    ``resume`` says: pressing play once is enough. One recovery attempt is made
    per absence:
    once the hooks have run (or on_connect hooks ran while the DAC was missing),
    starting playback again doesn't rerun them until the DAC has come back or
    the last source has disconnected.

    It resumes only what it paused: not if the source has disconnected, or
    started playing again on its own, in the meantime. Before resuming it waits
    for the output_lost and connect hooks to finish (an amplifier warming up) and then
    ``resume_delay`` seconds, so the DAC has settled.
    """

    def __init__(self, card: str, source: Any, hooks: Any, pause: bool = False,
                 resume: bool = False, resume_delay: float = 2.0, pause_check: float = 1.5) -> None:
        self.card = card
        self.source = source
        self.hooks = hooks
        self.pause = pause
        self.resume = resume
        self.resume_delay = resume_delay
        #: Seconds to wait for the source to report that it paused before asking again.
        self.pause_check = pause_check
        #: The sources connected when we paused; None when we haven't paused.
        self._paused: frozenset[str] | None = None
        #: Resume what we paused once the DAC is back, even without ``resume``
        #: (it was paused as playback started).
        self._resume_on_back = False
        #: Shairport Sync reported the pause (else ``source.playing`` can't be trusted).
        self._pause_confirmed = True
        self._resuming = False
        #: A recovery attempt was made for the current absence of the DAC.
        self._attempted = False
        self.present = True
        self._task: asyncio.Task | None = None

    def on_output_change(self, present: bool) -> None:
        self.present = present
        if present:
            self._attempted = False
            log.info("output device %s is back", self.card)
            if self._paused is not None:
                self._back_while_paused()
            return
        if self._resuming:
            self._task.cancel()  # gone again before we resumed: stay paused
            self._resuming = False
            log.info("output device %s is gone again: not resuming yet", self.card)
            return
        if not self.source.playing:
            log.info("output device %s is gone (not playing: recovery when playback starts)", self.card)
            return
        if self.hooks.pending("output_lost"):
            log.info("output device %s is gone again (output_lost hooks still running)", self.card)
        else:
            log.info("output device %s is gone while playing", self.card)
            self._fire()
        if self.pause:
            self._start(self._pause())

    def on_playing(self, playing: bool) -> None:
        if not playing:
            return
        if self._paused is not None and not self._resuming:
            self._paused = None  # played by hand: handled below if the DAC is still missing
            if self.present:
                log.info("source resumed on its own: not resuming it when the output device is back")
        if self.present:
            return
        if self._attempted or self.hooks.pending("output_lost"):
            log.info("playback started while output device %s is missing; still waiting for it "
                     "after the last recovery attempt", self.card)
        else:
            log.info("playback started while output device %s is missing", self.card)
            self._fire()
        if self.pause:
            # Hold the source until the DAC is ready rather than skip the start.
            self._start(self._pause(resume_when_back=True))

    def on_connection(self, connected: bool) -> None:
        if connected:
            # on_connect hooks (e.g. a power-up) running while the DAC is missing
            # are this absence's recovery attempt.
            if not self.present and self.hooks.hooks.get("connect"):
                self._attempted = True
        else:
            self._attempted = False  # the next session gets a fresh attempt

    def _fire(self) -> None:
        self._attempted = True
        self.hooks.fire("output_lost")

    def _start(self, coro: Any) -> None:
        if self._task is not None:
            self._task.cancel()
        self._task = asyncio.create_task(coro, name="output-recovery")

    async def _pause(self, resume_when_back: bool = False) -> None:
        clients = self.source.clients
        # A source that has only just connected may not take remote commands yet:
        # check that it paused (Shairport Sync reports it), and ask again if not.
        # Pausing twice is harmless. Without a confirmation, assume it paused
        # anyway (it may have before it ever reported playing), so it's resumed.
        self._pause_confirmed = False
        for attempt in range(3):
            try:
                await self.source.pause()
            except Exception as e:
                log.warning("couldn't pause the source: %s", e)
                return
            for _ in range(int(self.pause_check / 0.05)):
                if not self.source.playing:
                    self._pause_confirmed = True
                    break
                await asyncio.sleep(0.05)
            if self._pause_confirmed:
                break
            log.debug("the source hasn't confirmed the pause yet; asking again")
        else:
            log.info("the source didn't confirm the pause; assuming it paused")
        self._paused = clients
        self._resume_on_back = resume_when_back
        log.info("paused the source while output device %s is gone%s", self.card,
                 " (resuming when it's ready)" if resume_when_back else "")
        if self.present:  # it came back while we were pausing
            self._back_while_paused()

    def _back_while_paused(self) -> None:
        if not (self.resume or self._resume_on_back):
            log.info("output device %s is back; the source stays paused "
                     "(resume_when_output_back is off)", self.card)
            self._paused = None
            return
        # Only ever scheduled once the pause has finished: no task to cancel.
        self._resuming = True
        self._task = asyncio.create_task(self._resume(), name="output-recovery")

    async def _resume(self) -> None:
        try:
            # The DAC may be back before the amplifier has warmed up.
            while self.hooks.pending("output_lost") or self.hooks.pending("connect"):
                await asyncio.sleep(0.2)
            await asyncio.sleep(self.resume_delay)
            paused, self._paused = self._paused, None
            if not paused or not (paused & self.source.clients):
                log.info("not resuming: the source that was paused has disconnected")
            elif self.source.playing and self._pause_confirmed:
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
