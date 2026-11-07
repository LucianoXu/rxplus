"""Per-window counts of flow, latency and depth (standard library only).

Window counts live in one structure shared by the reporter:
``shared.windows[k][ident]`` is the :class:`Totals` of element or link
``ident`` in window ``k`` of the session (window ``k`` is
``[T + k*w, T + (k+1)*w)``). So:

- closing window ``k`` pops ``windows[k]``: its keys are exactly the busy
  elements and links, no separate bookkeeping;
- a Loss replaces ``windows`` by an empty dict in O(1); the old counts are
  dropped as a whole;
- the number of totals held is ``sum(len(d) for d in windows.values())``.

One :class:`Accumulator` per element or link holds what outlives a window:
the counted backlog and the depth state. Hot-path calls take only its lock,
in O(1).
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


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

    def merge(self, other: "Totals") -> None:
        self.items += other.items
        self.bytes += other.bytes
        self.sized = self.sized or other.sized
        self.dropped += other.dropped
        self.coalesced += other.coalesced
        self.lat_count += other.lat_count
        self.lat_sum += other.lat_sum
        self.lat_max = max(self.lat_max, other.lat_max)
        self.delta += other.delta


class Shared(Protocol):
    """What accumulators share with their reporter.

    ``closed``: windows below it are closed (set by the clock thread before
    it closes them). ``windows``: the open windows' totals. ``epoch``:
    incremented by a Loss; the depth state of every accumulator resets.
    """

    closed: int
    windows: dict[int, dict[str, Totals]]
    epoch: int
    pending: int  # totals held (approximate: updated without a common lock)
    cap: int  # the hard limit on `pending`
    saturated: bool  # a total was refused at the cap: the clock must Loss


@dataclass(slots=True)
class Depth:
    """The depth signal due at a window end, and whether one may be due next."""

    value: int | None  # backlog at the window's end; None: emit no depth
    watched: bool


class Accumulator:
    """Backlog and depth state of one element or link; records hot-path events."""

    __slots__ = (
        "ident",
        "_lock",
        "_session",
        "_shared",
        "_backlog",
        "_last_nonzero",
        "_epoch",
        "sampler",
        "capacity",
    )

    def __init__(
        self,
        ident: str,
        shared: Shared,
        sampler: Callable[[], int] | None = None,
        capacity: int | None = None,
    ) -> None:
        self.ident = ident
        self._lock = threading.Lock()
        self._session = -1  # no session: hot-path calls are ignored
        self._shared = shared
        self._backlog = 0  # counted backlog now
        self._last_nonzero = False  # the depth at the previous boundary was non-zero
        self._epoch = shared.epoch
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
            shared = self._shared
            closed = shared.closed
            if k < closed:
                # The clock thread closed this window after the caller read
                # the clock: the event counts in the current window (R3).
                k = closed
            if delta:
                if self._backlog + delta < 0:
                    delta = -self._backlog  # clamp: entered before the session
                self._backlog += delta  # the backlog is kept even at the cap
            windows = shared.windows
            window = windows.get(k)
            if window is None:
                window = windows.setdefault(k, {})  # atomic on a dict
            t = window.get(self.ident)
            if t is None:
                # A new total: once per element/link per window, never per
                # item. At the cap the count is not kept and the clock thread
                # turns the gap into a Loss (finding B): memory stays bounded
                # however the threads are scheduled.
                if shared.pending >= shared.cap:
                    shared.saturated = True
                    return
                shared.pending += 1
                t = window.setdefault(self.ident, Totals())
            t.items += items
            if size is not None:
                t.bytes += size
                t.sized = True
            t.dropped += dropped
            t.coalesced += coalesced
            if delta:
                t.delta += delta
            if duration is not None:
                t.lat_count += 1
                t.lat_sum += duration
                if duration > t.lat_max:
                    t.lat_max = duration

    # ------------------------------------------------------- clock thread only

    def start_session(self, session: int) -> None:
        with self._lock:
            self._session = session
            self._backlog = 0
            self._last_nonzero = False
            self._epoch = self._shared.epoch

    def lock(self) -> threading.Lock:
        """The lock under which the clock thread reads this id's totals."""
        return self._lock

    def depth(self, later_delta: int, sample: int | None) -> Depth:
        """The depth due at a window end (caller holds :meth:`lock`).

        ``later_delta`` is the backlog change recorded in later windows;
        ``sample`` the sampled backlog, for an accumulator with a sampler.
        """
        if self._epoch != self._shared.epoch:
            self._epoch = self._shared.epoch  # a Loss reset every backlog
            self._last_nonzero = False
        backlog = sample if sample is not None else self._backlog - later_delta
        if self.capacity is not None and backlog > self.capacity:
            backlog = self.capacity
        backlog = max(backlog, 0)
        if backlog != 0:
            value: int | None = backlog
        elif self._last_nonzero:
            value = 0
        else:
            value = None
        self._last_nonzero = backlog != 0
        watched = self._last_nonzero or self._backlog != 0 or self.sampler is not None
        return Depth(value, watched)

    # ------------------------------------------------------------------- tests

    def pending(self) -> int:
        """Windows holding counts of this id."""
        return sum(1 for w in list(self._shared.windows.values()) if self.ident in w)

    def next_window(self) -> int | None:
        """The lowest window index holding counts of this id, if any."""
        found = [k for k, w in list(self._shared.windows.items()) if self.ident in w]
        return min(found, default=None)
