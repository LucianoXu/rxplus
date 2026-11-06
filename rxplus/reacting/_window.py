"""Per-window accumulators for flow, latency and depth (standard library only).

One :class:`Accumulator` exists per element or link. Hot-path calls update
it under its own lock in O(1) and never wait for anything else; only the
reporter's clock thread closes windows. Windows are identified by their
index ``k`` in the session: window ``k`` is ``[T + k*w, T + (k+1)*w)``.

An accumulator with events in an open window puts its ID once on the
reporter's ``busy`` deque, so closing a window costs O(busy accumulators),
not O(all accumulators).
"""

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class _Closed(Protocol):
    """What an accumulator reads from the reporter: windows below
    ``closed`` are closed (set by the clock thread before it closes them)."""

    closed: int


@dataclass(slots=True)
class Totals:
    """What happened on one element or link during one window."""

    items: int = 0
    bytes: int = 0
    sized: bool = False  # at least one item had a size
    dropped: int = 0
    coalesced: int = 0
    lat_count: int = 0
    lat_sum: float = 0.0
    lat_max: float = 0.0
    delta: int = 0  # net change of the counted backlog during the window


@dataclass(slots=True)
class WindowResult:
    """The signals' content for one closed window of one accumulator."""

    totals: Totals | None
    depth: int | None  # backlog at the window's end; None: emit no depth


class Accumulator:
    """Counters of one element or link, per window of the current session."""

    __slots__ = (
        "ident",
        "_busy",
        "_flagged",
        "_lock",
        "_session",
        "_windows",
        "_shared",
        "_backlog",
        "_last_nonzero",
        "sampler",
        "capacity",
    )

    def __init__(
        self,
        ident: str,
        busy: deque[str],
        shared: _Closed,
        sampler: Callable[[], int] | None = None,
        capacity: int | None = None,
    ) -> None:
        self.ident = ident
        self._busy = busy
        self._flagged = False
        self._lock = threading.Lock()
        self._session = -1  # no session: hot-path calls are ignored
        self._windows: dict[int, Totals] = {}
        self._shared = shared
        self._backlog = 0  # counted backlog now
        self._last_nonzero = False  # the depth at the previous boundary was non-zero
        self.sampler = sampler
        self.capacity = capacity

    # ------------------------------------------------------------------ hot path

    def record(
        self,
        session: int,
        k: int,
        items: int = 0,
        size: int | None = None,
        dropped: int = 0,
        coalesced: int = 0,
        delta: int = 0,
        duration: float | None = None,
    ) -> None:
        """One hot-path event in window ``k`` of ``session``: O(1)."""
        with self._lock:
            if session != self._session:
                return  # from a session that ended: ignore it
            closed = self._shared.closed
            if k < closed:
                # The clock thread closed this window after the caller read
                # the clock: the event counts in the current window (R3).
                k = closed
            t = self._windows.get(k)
            if t is None:
                t = self._windows[k] = Totals()
            t.items += items
            if size is not None:
                t.bytes += size
                t.sized = True
            t.dropped += dropped
            t.coalesced += coalesced
            if delta:
                if self._backlog + delta < 0:
                    delta = -self._backlog  # clamp: entered before the session
                self._backlog += delta
                t.delta += delta
            if duration is not None:
                t.lat_count += 1
                t.lat_sum += duration
                if duration > t.lat_max:
                    t.lat_max = duration
            if not self._flagged:
                self._flagged = True
                self._busy.append(self.ident)  # deque.append is thread-safe

    # ------------------------------------------------------- clock thread only

    def start_session(self, session: int) -> None:
        with self._lock:
            self._session = session
            self._windows.clear()
            self._backlog = 0
            self._last_nonzero = False
            self._flagged = False

    def next_window(self) -> int | None:
        """The lowest window index with recorded events, if any."""
        with self._lock:
            return min(self._windows) if self._windows else None

    def watched(self) -> bool:
        """Whether a depth signal may be due at every boundary."""
        with self._lock:
            return self._last_nonzero or self._backlog != 0 or self.sampler is not None

    def close(self, k: int, sample: int | None) -> WindowResult:
        """Close window ``k``: its totals and the depth signal due at its end.

        ``sample`` is the sampled backlog at the boundary (for an
        accumulator with a sampler), read by the caller outside any lock.
        """
        with self._lock:
            totals = self._windows.pop(k, None)
            if sample is not None:
                backlog = sample
            else:
                # Backlog at this boundary: now, minus what changed in the
                # windows after k that already recorded events.
                later = sum(t.delta for i, t in self._windows.items() if i > k)
                backlog = self._backlog - later
            if self.capacity is not None and backlog > self.capacity:
                backlog = self.capacity
            backlog = max(backlog, 0)
            if backlog != 0:
                depth: int | None = backlog
            elif self._last_nonzero:
                depth = 0
            else:
                depth = None
            self._last_nonzero = backlog != 0
            if not self._windows:
                self._flagged = False  # the next event puts it on busy again
            return WindowResult(totals, depth)
