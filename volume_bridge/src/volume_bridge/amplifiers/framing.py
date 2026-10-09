"""Splitting a serial byte stream into messages.

RS-232 protocols delimit messages in different ways, so framing is separate
from parsing. A framer is fed raw bytes as they arrive and returns complete
:class:`Frame` objects. Framers that can hold an incomplete message expose it
through :attr:`Framer.pending`. The reader then calls :meth:`Framer.flush` after
``gap`` seconds of silence, to accept or abandon that message.

Built-in framers (config ``framing = ...``):

``delimited``  messages end in a terminator byte (CR, LF, ``;`` ...). The default.
``fixed``      every message is exactly ``frame_length`` bytes.
``markers``    messages start with ``start_marker`` and end with ``end_marker``,
               optionally followed by ``trailer_length`` bytes (e.g. a checksum).
``stream``     no delimiters at all: messages are found by scanning the stream
               for the configured patterns (``MV45MUON`` -> ``MV45``, ``MUON``).
               Bytes that match no pattern are skipped as noise or other
               programs' traffic.

For anything else, subclass :class:`Framer` and override
``TcpSerialAmplifier.create_framer``.
"""

from __future__ import annotations

import abc
import copy
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Frame:
    raw: bytes      # the complete frame as received, including any markers/trailer
    payload: bytes  # the part that patterns are matched against


class Framer(abc.ABC):
    #: Seconds of silence after which :meth:`flush` is called while :attr:`pending`.
    gap: float | None = None

    def __init__(self, max_length: int = 512) -> None:
        self.max_length = max_length
        self.buffer = b""

    @property
    def pending(self) -> bool:
        return bool(self.buffer)

    @abc.abstractmethod
    def feed(self, data: bytes) -> list[Frame]:
        """Add received bytes; return any frames that are now complete."""

    def flush(self) -> list[Frame]:
        """Called after ``gap`` seconds of silence, or at EOF. Default: drop partial data."""
        if self.buffer:
            log.debug("discarding incomplete frame %r", self.buffer)
        self.buffer = b""
        return []

    def reset(self) -> None:
        self.buffer = b""

    def frames_of(self, data: bytes) -> list[Frame]:
        """Frame ``data`` in isolation, e.g. to know how our own sent command
        will look if it is echoed back. Doesn't touch this framer's state."""
        other = copy.copy(self)
        other.buffer = b""
        return other.feed(data) + other.flush_complete()

    def flush_complete(self) -> list[Frame]:
        """Like :meth:`flush`, but for data known to be complete (our own commands)."""
        self.buffer = b""
        return []

    def _limit(self) -> None:
        if len(self.buffer) > self.max_length:
            log.debug("discarding %d bytes of unframed input", len(self.buffer) - self.max_length)
            self.buffer = self.buffer[-self.max_length:]


class DelimitedFramer(Framer):
    def __init__(self, terminators: bytes, max_length: int = 512, gap: float | None = None) -> None:
        super().__init__(max_length)
        if not terminators:
            raise ValueError("terminators must not be empty")
        self._split = re.compile(b"[" + re.escape(terminators) + b"]")
        self.gap = gap

    def feed(self, data: bytes) -> list[Frame]:
        *parts, self.buffer = self._split.split(self.buffer + data)
        frames = [Frame(p, p.strip()) for p in parts if p.strip()]
        if len(self.buffer) > self.max_length:
            log.debug("discarding %d bytes of unterminated input", len(self.buffer))
            self.buffer = b""
        return frames

    def flush_complete(self) -> list[Frame]:
        tail, self.buffer = self.buffer.strip(), b""
        return [Frame(tail, tail)] if tail else []


class FixedLengthFramer(Framer):
    def __init__(self, length: int, gap: float | None = None) -> None:
        super().__init__(max_length=length)
        if length <= 0:
            raise ValueError("frame_length must be positive")
        self.length = length
        # Without a gap, a lost byte would misalign every later frame, so
        # resynchronise on silence by default.
        self.gap = 0.05 if gap is None else gap

    def feed(self, data: bytes) -> list[Frame]:
        self.buffer += data
        frames = []
        while len(self.buffer) >= self.length:
            raw, self.buffer = self.buffer[: self.length], self.buffer[self.length:]
            frames.append(Frame(raw, raw))
        return frames


class MarkerFramer(Framer):
    def __init__(self, start: bytes, end: bytes, trailer_length: int = 0,
                 max_length: int = 512, gap: float | None = None) -> None:
        super().__init__(max_length)
        if not start and not end:
            raise ValueError("at least one of start_marker/end_marker is required")
        self.start, self.end, self.trailer_length = start, end, trailer_length
        self.gap = gap

    def feed(self, data: bytes) -> list[Frame]:
        self.buffer += data
        frames = []
        while True:
            if self.start:
                i = self.buffer.find(self.start)
                if i < 0:
                    # Keep a possible partial start marker at the end.
                    self.buffer = self.buffer[-(len(self.start) - 1):] if len(self.start) > 1 else b""
                    break
                if i:
                    log.debug("skipping %r before start marker", self.buffer[:i])
                self.buffer = self.buffer[i:]
            body_from = len(self.start)
            if self.end:
                j = self.buffer.find(self.end, body_from)
                if j < 0:
                    break
                frame_end = j + len(self.end) + self.trailer_length
                if len(self.buffer) < frame_end:
                    break
                payload = self.buffer[body_from:j]
            else:
                # Start marker only: a frame runs until the next start marker.
                j = self.buffer.find(self.start, body_from)
                if j < 0:
                    break
                frame_end = j
                payload = self.buffer[body_from:j]
            raw, self.buffer = self.buffer[:frame_end], self.buffer[frame_end:]
            frames.append(Frame(raw, payload))
        self._limit()
        return frames

    def flush(self) -> list[Frame]:
        if not self.end and self.buffer.startswith(self.start) and len(self.buffer) > len(self.start):
            # Start-marker-only framing: the last frame ends with silence.
            frames = [Frame(self.buffer, self.buffer[len(self.start):])]
            self.buffer = b""
            return frames
        return super().flush()

    def flush_complete(self) -> list[Frame]:
        return self.flush()


class StreamFramer(Framer):
    """Finds messages by scanning the byte stream for known patterns.

    The earliest match wins (the longest one if several start at the same
    place). A match that runs to the end of the data received so far might
    still grow (``MV4`` before ``5`` arrives, for a pattern like ``MV\\d+``), so it
    is only accepted when more data follows it or after ``gap`` seconds of
    silence. Patterns with a fixed width (``MV\\d{2}``) avoid that wait unless
    the message is the last thing received.
    """

    def __init__(self, patterns: Sequence[re.Pattern[bytes]], max_length: int = 512,
                 gap: float | None = None) -> None:
        super().__init__(max_length)
        if not patterns:
            raise ValueError("stream framing needs at least one pattern")
        self.patterns = list(patterns)
        self.gap = 0.05 if gap is None else gap

    def _next_match(self) -> re.Match[bytes] | None:
        best = None
        for pattern in self.patterns:
            m = pattern.search(self.buffer)
            if m and m.end() > m.start() and (
                best is None or m.start() < best.start()
                or (m.start() == best.start() and m.end() > best.end())
            ):
                best = m
        return best

    def _take(self, final: bool) -> list[Frame]:
        frames = []
        while (m := self._next_match()) is not None:
            if m.end() == len(self.buffer) and not final:
                break  # might still be growing; wait for more data or silence
            if m.start():
                log.debug("skipping %r", self.buffer[: m.start()])
            frames.append(Frame(m.group(0), m.group(0)))
            self.buffer = self.buffer[m.end():]
        if final:
            if self.buffer:
                log.debug("skipping %r", self.buffer)
            self.buffer = b""
        self._limit()
        return frames

    def feed(self, data: bytes) -> list[Frame]:
        self.buffer += data
        return self._take(final=False)

    def flush(self) -> list[Frame]:
        return self._take(final=True)

    def flush_complete(self) -> list[Frame]:
        return self.flush()


def _bytes_option(value: str | None, encoding: str) -> bytes:
    return value.encode(encoding) if value else b""


def create_framer(options: Mapping[str, Any], encoding: str,
                  stream_patterns: Sequence[str]) -> Framer:
    """Build the framer described by the ``framing`` options."""
    kind = options.get("framing", "delimited")
    max_length = int(options.get("max_frame_length", 512))
    gap = options.get("frame_gap")
    gap = None if gap is None else float(gap)
    if kind == "delimited":
        return DelimitedFramer(options.get("receive_terminators", "\r\n").encode(encoding),
                               max_length, gap)
    if kind == "fixed":
        if "frame_length" not in options:
            raise ValueError("framing = 'fixed' requires frame_length")
        return FixedLengthFramer(int(options["frame_length"]), gap)
    if kind == "markers":
        return MarkerFramer(_bytes_option(options.get("start_marker"), encoding),
                            _bytes_option(options.get("end_marker"), encoding),
                            int(options.get("trailer_length", 0)), max_length, gap)
    if kind == "stream":
        return StreamFramer([re.compile(p.encode(encoding)) for p in stream_patterns],
                            max_length, gap)
    raise ValueError(f"unknown framing {kind!r}; use delimited, fixed, markers or stream")
