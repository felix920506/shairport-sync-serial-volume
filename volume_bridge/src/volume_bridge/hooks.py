"""Actions run when a source connects/disconnects or a play session starts/ends.

Typical use: wake equipment that went to standby (power the amplifier on over
its serial port, or ask Home Assistant to switch something on) when a phone
connects, and put it back to sleep later.

Each event has a list of actions, run in order:

* ``send``  -- a command through the amplifier backend's connection (the same
  serial port / serial server), wrapped like the backend's own commands, or
  sent byte for byte with ``raw = true`` (e.g. for another device on a shared
  port).
* ``shell`` -- a command run with ``/bin/sh -c``; it gets the event in
  environment variables (see ``_environment``). ``timeout`` (default 30 s).
* ``delay`` -- wait this many seconds (e.g. while the amplifier powers up).
* ``sync``  -- re-read the amplifier's state, then make both sides agree:
  ``"source"`` sends the source's volume to the amplifier, ``"amplifier"``
  sends the amplifier's volume to the source.
* ``parallel`` -- run several branches at the same time and wait for all of
  them. A branch is one action or a list of actions run in order, e.g. power
  the amplifier on and wait for it to warm up while Home Assistant is called.

If an event's actions include a ``sync``, the bridge holds volume sync in both
directions from the start of the event until the first ``sync`` has run: an
amplifier that is still waking up would reject volume commands, and its stale
volume must not be pushed to the source. The sync then sends the latest volume.

A failing action is logged and the next one (in its branch) still runs. Events are handled one
at a time in the order they happen, so a disconnect's actions never overtake
the connect's.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from typing import Any, Mapping

from .amplifiers.base import Amplifier
from .bridge import VolumeBridge, VolumeSource

log = logging.getLogger(__name__)

#: Event name -> the key it is configured under in [hooks].
EVENTS = {
    "connect": "on_connect",
    "disconnect": "on_disconnect",
    "session_start": "on_session_start",
    "session_end": "on_session_end",
}

_ACTIONS = {
    "send": {"raw"},
    "shell": {"timeout"},
    "delay": set(),
    "sync": set(),
    "parallel": set(),
}


@dataclass(frozen=True)
class Action:
    kind: str
    value: Any
    raw: bool = False
    timeout: float = 30.0

    def describe(self) -> str:
        if self.kind == "parallel":
            return f"parallel ({len(self.value)} branches)"
        return f"{self.kind} {self.value!r}"


Hooks = dict[str, tuple[Action, ...]]


def parse_hooks(section: Mapping[str, Any]) -> Hooks:
    """Parse the [hooks] config section into {event: actions}."""
    keys = {v: k for k, v in EVENTS.items()}
    unknown = set(section) - set(keys)
    if unknown:
        raise ValueError(f"unknown setting(s) in [hooks]: {', '.join(sorted(unknown))} "
                         f"(expected {', '.join(EVENTS.values())})")
    hooks: Hooks = {}
    for key, actions in section.items():
        if not isinstance(actions, list):
            raise ValueError(f"[hooks] {key} must be a list of actions")
        hooks[keys[key]] = tuple(_parse_action(a, f"[hooks] {key}[{i}]") for i, a in enumerate(actions))
    return hooks


def _parse_action(data: Any, where: str) -> Action:
    if not isinstance(data, dict):
        raise ValueError(f"{where} must be a table such as {{ send = \"PWR 1\" }}")
    kinds = [k for k in _ACTIONS if k in data]
    if len(kinds) != 1:
        raise ValueError(f"{where} needs exactly one of {', '.join(_ACTIONS)}")
    kind = kinds[0]
    unknown = set(data) - {kind} - _ACTIONS[kind]
    if unknown:
        raise ValueError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))} for {kind}")
    value = data[kind]
    if kind in ("send", "shell") and not isinstance(value, str):
        raise ValueError(f"{where}: {kind} must be a string")
    if kind == "delay":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{where}: delay must be a number of seconds")
        value = float(value)
    if kind == "sync" and value not in ("source", "amplifier"):
        raise ValueError(f'{where}: sync must be "source" or "amplifier"')
    if kind == "parallel":
        if not isinstance(value, list) or not value:
            raise ValueError(f"{where}: parallel must be a list of actions or lists of actions")
        value = tuple(_parse_branch(b, f"{where}.parallel[{i}]") for i, b in enumerate(value))
    return Action(kind, value, raw=bool(data.get("raw", False)),
                  timeout=float(data.get("timeout", 30.0)))


def _parse_branch(data: Any, where: str) -> tuple[Action, ...]:
    if isinstance(data, list):
        return tuple(_parse_action(a, f"{where}[{i}]") for i, a in enumerate(data))
    return (_parse_action(data, where),)


def _contains_sync(actions: tuple[Action, ...]) -> bool:
    return any(a.kind == "sync" or (a.kind == "parallel" and any(_contains_sync(b) for b in a.value))
               for a in actions)


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class HookRunner:
    def __init__(self, hooks: Hooks, source: VolumeSource, amplifier: Amplifier,
                 bridge: VolumeBridge) -> None:
        self.hooks = hooks
        self.source = source
        self.amplifier = amplifier
        self.bridge = bridge
        self._queue: asyncio.Queue[tuple[str, dict[str, str]]] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._holding = False

    def fire(self, event: str, client: str = "") -> None:
        """Queue the actions for ``event`` (no-op if it has none)."""
        if not self.hooks.get(event):
            return
        self._queue.put_nowait((event, self._environment(event, client)))
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="hooks")

    def _environment(self, event: str, client: str) -> dict[str, str]:
        # Captured when the event happens, not when its actions get to run.
        state = self.amplifier.state
        volume = self.source.volume
        return {
            "VOLUME_BRIDGE_EVENT": event,
            "VOLUME_BRIDGE_SOURCE": client,
            "VOLUME_BRIDGE_AIRPLAY_VOLUME": "" if volume is None else f"{volume:.2f}",
            "VOLUME_BRIDGE_AMPLIFIER_LEVEL": "" if state is None else f"{state.level:g}",
            "VOLUME_BRIDGE_AMPLIFIER_MUTED": "" if state is None else str(int(state.muted)),
        }

    async def _worker(self) -> None:
        while not self._queue.empty():
            event, env = self._queue.get_nowait()
            log.info("running %s hooks", event)
            if _contains_sync(self.hooks[event]):
                self.bridge.hold()
                self._holding = True
            try:
                await self._run_sequence(event, self.hooks[event], env)
            finally:
                self._release()
            log.info("%s hooks done", event)

    def _release(self) -> None:
        if self._holding:
            self._holding = False
            self.bridge.release()

    async def _run_sequence(self, event: str, actions: tuple[Action, ...], env: dict[str, str]) -> None:
        for action in actions:
            try:
                await self._run(event, action, env)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("%s hook %s failed: %s", event, action.describe(), e)

    async def _run(self, event: str, action: Action, env: dict[str, str]) -> None:
        log.debug("hook: %s", action.describe())
        if action.kind == "send":
            await self.amplifier.send_command(action.value, raw=action.raw)
        elif action.kind == "delay":
            await asyncio.sleep(action.value)
        elif action.kind == "sync":
            try:
                await self.amplifier.refresh()
                self.bridge.sync(action.value)
                await self.bridge.idle()
            finally:
                self._release()
        elif action.kind == "shell":
            await self._shell(action, env)
        elif action.kind == "parallel":
            await asyncio.gather(*(self._run_sequence(event, branch, env) for branch in action.value))

    async def _shell(self, action: Action, env: dict[str, str]) -> None:
        proc = await asyncio.create_subprocess_shell(
            action.value, env={**os.environ, **env},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)  # own process group, so a timeout kills its children too
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), action.timeout)
        except asyncio.TimeoutError:
            _kill_group(proc)
            await proc.wait()
            raise TimeoutError(f"timed out after {action.timeout:g}s") from None
        except asyncio.CancelledError:
            _kill_group(proc)
            raise
        text = output.decode(errors="replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"exit status {proc.returncode}" + (f": {text[-500:]}" if text else ""))
        if text:
            log.debug("hook output: %s", text[-500:])

    async def idle(self) -> None:
        """Wait until all queued events have been handled (used by tests)."""
        while self._task is not None and not self._task.done():
            await asyncio.shield(self._task)

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
