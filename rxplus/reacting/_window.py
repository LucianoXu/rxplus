"""Per-window accumulators for flow, latency and depth (standard library only).

One :class:`Accumulator` exists per element or link. The hot path updates
it under its own lock; the reporter closes windows under the reporter lock
and takes each accumulator's lock briefly (lock order: reporter, then
accumulator). Windows are identified by their index ``k`` in the session:
window ``k`` is ``[T + k*w, T + (k+1)*w)``.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass


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
        "_lock",
        "_session",
        "_windows",
        "_closed_upto",
        "_backlog",
        "_last_nonzero",
        "sampler",
        "capacity",
    )

    def __init__(
        self, sampler: Callable[[], int] | None = None, capacity: int | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._session = -1
        self._windows: dict[int, Totals] = {}
        self._closed_upto = 0  # windows < this index are closed
        self._backlog = 0  # counted backlog now
        self._last_nonzero = False  # the depth at the previous boundary was non-zero
        self.sampler = sampler
        self.capacity = capacity

    # ------------------------------------------------------------------ hot path

    def _totals(self, session: int, k: int) -> Totals:
        """Totals of window ``k``; caller holds the lock."""
        if session != self._session:
            self._reset(session)
        if k < self._closed_upto:
            # The window was closed by a concurrent flush after the caller
            # read the clock: the event belongs to the current window.
            k = self._closed_upto
        totals = self._windows.get(k)
        if totals is None:
            totals = self._windows[k] = Totals()
        return totals

    def flow(
        self,
        session: int,
        k: int,
        items: int = 0,
        size: int | None = None,
        dropped: int = 0,
        coalesced: int = 0,
    ) -> None:
        with self._lock:
            t = self._totals(session, k)
            t.items += items
            if size is not None:
                t.bytes += size
                t.sized = True
            t.dropped += dropped
            t.coalesced += coalesced

    def change(self, session: int, k: int, delta: int) -> None:
        """Counted backlog changes by ``delta`` (never below zero)."""
        with self._lock:
            t = self._totals(session, k)
            if self._backlog + delta < 0:
                delta = -self._backlog  # clamp: entered before the session
            self._backlog += delta
            t.delta += delta

    def duration(self, session: int, k: int, seconds: float) -> None:
        with self._lock:
            t = self._totals(session, k)
            t.lat_count += 1
            t.lat_sum += seconds
            if seconds > t.lat_max:
                t.lat_max = seconds

    # -------------------------------------------------------------- reporter side

    def _reset(self, session: int) -> None:
        self._session = session
        self._windows.clear()
        self._closed_upto = 0
        self._backlog = 0
        self._last_nonzero = False

    def start_session(self, session: int, first_window: int = 0) -> None:
        with self._lock:
            self._reset(session)
            self._closed_upto = first_window

    def mark_closed(self, session: int, upto: int) -> None:
        """Windows below ``upto`` are closed (some skipped: nothing to report)."""
        with self._lock:
            if session == self._session and upto > self._closed_upto:
                self._closed_upto = upto

    def pending_windows(self, session: int) -> list[int]:
        """Indices of windows with recorded events (any, unsorted)."""
        with self._lock:
            if session != self._session:
                return []
            return list(self._windows)

    def has_backlog(self, session: int) -> bool:
        """Whether a depth signal may be due (non-zero now or at the last end)."""
        with self._lock:
            if session != self._session:
                return False
            return self._last_nonzero or self._backlog != 0 or self.sampler is not None

    def close(self, session: int, k: int, sample: int | None) -> WindowResult:
        """Close window ``k``: its totals and the depth signal due at its end.

        ``sample`` is the sampled backlog at the boundary (for an
        accumulator with a sampler), read by the caller outside this lock.
        """
        with self._lock:
            if session != self._session:
                self._reset(session)
            totals = self._windows.pop(k, None)
            self._closed_upto = k + 1
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
            return WindowResult(totals, depth)
