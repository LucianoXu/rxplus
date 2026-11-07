"""The reacting/1 source: :class:`Reporter` (standard library only).

Threads and their duties (see ``docs/reacting.md``):

- **Callers** (the observed graph's threads) validate, update the registry
  and append a timestamped event under the reporter lock, or update one
  accumulator in O(1). They never close windows, encode JSON, call user
  code or wait for the sink.
- **The clock thread** owns the window clock. In each step it applies the
  callers' events in order to its mirror of the graph, closes every window
  whose end ``T + (k+1)*w`` is at or before the event's ``t`` before
  emitting the event, closes the windows that ended by now, then encodes
  the signals and puts them in the bounded queue. It runs on time even when
  the process is idle and even while the sink is stalled, so ``queue`` and
  Loss bound memory.
- **The writer thread** owns the sink: it opens it (always through the
  sink's backoff when reopening), drains the queue in batches (a batch in
  flight counts as unwritten) and reports a lost connection.

With ``timer=False`` neither thread exists and :meth:`Reporter.poll` performs
one step of each, for deterministic tests.
"""

import atexit
import os
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from typing import Literal, NamedTuple

from ._encode import (
    PROTOCOL,
    Signal,
    check_id,
    check_kind,
    check_label,
    check_reference,
    encode,
)
from ._graph import ElementRec, Graph, LinkRec, SpanRec
from ._window import Accumulator, Totals
from .clock import Clock, SystemClock
from .handles import _CURRENT, Element, Link, LinkKind, Span, _Live
from .sinks import Sink

_NOTIFY_EVENTS = 4096  # wake the clock thread early when this many are pending
_MAX_IDLE = 0.05  # longest sleep of the clock thread, seconds
_MAX_LAG = 64  # windows the clock thread may fall behind before Loss
_MAX_PENDING = 100_000  # window totals it may hold (lag x busy) before Loss
_CHECK_EVERY = 100  # accumulators closed between two checks of the limit

type _Sampler = Callable[[], int]


class _Event(NamedTuple):
    kind: Literal["element", "link", "remove", "span.start", "span.end"]
    t: float
    data: object


class _SpanEnd(NamedTuple):
    span: str
    error: str | None  # None: status ok; "" : error without description


class Reporter:
    """One reacting/1 source. Thread-safe; never blocks the caller.

    Args:
        source: Source name (non-empty, no ``/``).
        sink: Where the session's lines go; one sink per reporter (D16).
        label: Display name of the source, sent in snapshots.
        window: The spec's ``window`` option, in seconds.
        queue: The spec's ``queue`` option: lines created but not yet
            written (including a batch being written) before Loss.
        clock: Time source; :class:`ManualClock` in tests.
        timer: True: a clock thread and a writer thread run in the
            background. False: :meth:`poll` does their work (tests).
    """

    def __init__(
        self,
        source: str,
        sink: Sink,
        *,
        label: str | None = None,
        window: float = 0.1,
        queue: int = 10_000,
        clock: Clock | None = None,
        timer: bool = True,
    ) -> None:
        self._source = check_id(source, "source name")
        self._label = check_label(label, "source label") if label is not None else None
        if not window > 0:
            raise ValueError(f"window must be positive: {window!r}")
        if queue < 1:
            raise ValueError(f"queue must be at least 1: {queue!r}")
        self._sink = sink
        self._queue_limit = queue
        self._batch = max(1, min(64, queue // 4))
        self._clock: Clock = clock or SystemClock()
        self._st = _Live(self._clock.monotonic, self._clock.wall(), float(window))
        self._st.cap = _MAX_PENDING

        # Callers' side, under _lock.
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._registry = Graph()
        self._accs: dict[str, Accumulator] = {}
        self._events: list[_Event] = []
        self._counter = {"e": 0, "l": 0, "s": 0}
        self._want_session = False  # set by the writer: a connection is open
        self._epoch = 0  # connections opened so far (writer)
        self._closing = False
        self._closed = False

        # Clock thread only.
        self._mirror = Graph()
        self._session_open = False
        self._session_epoch = -1
        self._seq = 0
        self._watched: set[str] = set()
        self._pending_events: deque[_Event] = deque()  # events of this step
        self._current: _Event | None = None  # the event being processed
        self._silent = False  # _emit creates (seq) but does not queue: Loss

        # The bounded queue: (line, opens the session, epoch), under _qlock.
        self._qlock = threading.Lock()
        self._qcond = threading.Condition(self._qlock)
        self._q: deque[tuple[str, bool, int]] = deque()
        self._inflight = 0
        self._opening_queued = False
        self._clock_done = False

        self._connected = False  # writer only
        self._threads: list[threading.Thread] = []
        if timer:
            for target, name in (
                (self._clock_loop, "clock"),
                (self._write_loop, "writer"),
            ):
                th = threading.Thread(
                    target=target, name=f"rxplus-reacting-{name}-{source}", daemon=True
                )
                self._threads.append(th)
            for th in self._threads:
                th.start()
        else:
            self.poll()
        _REPORTERS.add(self)

    # ------------------------------------------------------------------ public

    @property
    def source(self) -> str:
        return self._source

    @property
    def live(self) -> bool:
        """Whether a session is open on the sink."""
        return self._st.live

    def element(
        self, label: str, *, kind: str | None = None, parent: Element | None = None
    ) -> Element:
        """Add an element (optionally a child of ``parent``) and return its handle."""
        check_label(label)
        check_kind(kind)
        with self._lock:
            parent_id = None
            if parent is not None:
                if (
                    parent.reporter is not self
                    or parent.id not in self._registry.elements
                ):
                    raise ValueError(
                        "parent is not an existing element of this reporter"
                    )
                parent_id = parent.id
            rec = ElementRec(self._next_id("e"), label, parent_id, kind)
            self._registry.elements[rec.id] = rec
            acc = self._new_acc(rec.id, None, None)
            self._post("element", rec)
        return Element(self, rec.id, label, acc)

    def link(
        self,
        src: Element | str,
        dst: Element | str,
        *,
        kind: LinkKind = "data",
        capacity: int | None = None,
        depth: _Sampler | None = None,
    ) -> Link:
        """Add a link; ``src``/``dst`` are elements or ``"<source>/<id>"``.

        ``depth`` is a sampled backlog, read once per window end on the
        clock thread (outside every lock); it must not block for long.
        """
        if kind not in ("data", "call"):
            raise ValueError(f"link kind must be 'data' or 'call': {kind!r}")
        if capacity is not None and capacity < 0:
            raise ValueError(f"capacity must be non-negative: {capacity!r}")
        with self._lock:
            ends = [self._endpoint(e) for e in (src, dst)]
            rec = LinkRec(self._next_id("l"), ends[0], ends[1], kind)
            self._registry.links[rec.id] = rec
            acc = self._new_acc(rec.id, depth, capacity)
            self._post("link", rec)
        return Link(self, rec.id, kind, acc)

    def remove(self, *handles: Element | Link) -> None:
        """Remove elements and links; removing an element removes its subtree,
        the links touching it and its open spans (spec, Elements)."""
        with self._lock:
            ids = [
                h.id
                for h in handles
                if h.reporter is self and self._registry.exists(h.id)
            ]
            if ids:
                self._registry.remove(ids)
                self._post("remove", ids)

    def poll(self) -> None:
        """``timer=False``: open the sink if needed, run one clock step and
        write what is queued. ``timer=True``: wake the clock thread."""
        if self._threads:
            with self._lock:
                self._cond.notify_all()
            return
        if self._closed:
            return
        if self._connected and _call(self._sink.is_open) is not True:
            self._lost_connection()
        if not self._connected and not self._closing:
            self._try_open()
        self._step()
        while True:
            batch, epoch = self._take_batch(wait=False, limit=None)
            if not batch:
                break
            if not self._write(batch, epoch):
                break
        self._step()

    def close(self, timeout: float = 1.0) -> None:
        """Close the windows that have ended, write what is queued (at most
        ``timeout`` seconds), close the sink. Idempotent."""
        if self._closed:
            return
        if self._threads:
            # One deadline for the whole close (finding N1).
            deadline = time.monotonic() + max(timeout, 0.0)

            def left() -> float:
                return max(deadline - time.monotonic(), 0.0)

            clock, writer = self._threads
            with self._lock:
                self._closing = True
                self._cond.notify_all()
            clock.join(left())
            with self._qcond:
                self._qcond.notify_all()
            writer.join(left())
            if writer.is_alive():
                # The writer is stuck on the network: the sink's close never
                # waits for it (it shuts the socket down), which unblocks it.
                _call(self._sink.close)
                writer.join(min(left(), 0.1))
        else:
            self.poll()
            self._closing = True
        with self._lock:
            self._closed = True
            self._st.live = False
        _call(self._sink.close)

    def __enter__(self) -> "Reporter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ callers' side

    def _next_id(self, prefix: Literal["e", "l", "s"]) -> str:
        self._counter[prefix] += 1
        return f"{prefix}{self._counter[prefix]}"

    def _new_acc(
        self, ident: str, sampler: _Sampler | None, capacity: int | None
    ) -> Accumulator:
        acc = Accumulator(ident, self._st, sampler, capacity)
        if self._st.live:
            acc.start_session(self._st.session)
        self._accs[ident] = acc
        return acc

    def _post(
        self,
        kind: Literal["element", "link", "remove", "span.start", "span.end"],
        data: object,
    ) -> None:
        """Append an event stamped now (caller holds the lock, so events are
        in ``t`` order)."""
        if self._closed:
            return  # nobody will process it (closed, or a forked child)
        self._events.append(_Event(kind, self._st.now(), data))
        if len(self._events) >= _NOTIFY_EVENTS:
            self._cond.notify_all()

    def _endpoint(self, end: Element | str) -> str:
        if isinstance(end, str):
            return check_reference(end, self._source)
        if end.reporter is not self or end.id not in self._registry.elements:
            raise ValueError(
                "link endpoint is not an existing element of this reporter"
            )
        return end.id

    def _start_span(self, element: str, label: str, parent: Span | None) -> Span:
        if parent is None:
            parent = _CURRENT.get()
        with self._lock:
            if element not in self._registry.elements or not self._st.live:
                return Span(self, None)
            sid = self._next_id("s")
            parent_id = None
            # Only a parent of this reporter that is still open (spec).
            if (
                parent is not None
                and parent._reporter is self
                and parent.id in self._registry.spans
            ):
                parent_id = parent.id
            rec = SpanRec(sid, element, label, parent_id, 0.0)
            self._registry.spans[sid] = rec
            self._post("span.start", rec)
        return Span(self, sid)

    def _end_span(self, sid: str, error: BaseException | str | None) -> None:
        # Content rule: never str(exception), which often carries data.
        if isinstance(error, BaseException):
            text: str | None = type(error).__name__
        else:
            text = None if error is None else error
        with self._lock:
            if self._registry.spans.pop(sid, None) is not None:
                self._post("span.end", _SpanEnd(sid, text))

    # ------------------------------------------------------------- clock thread

    def _clock_loop(self) -> None:
        while True:
            self._step()
            with self._lock:
                if self._closing:
                    break
                st = self._st
                # Wake at the next boundary, and at least every _MAX_IDLE so
                # structural signals never wait a whole (long) window.
                wait = _MAX_IDLE
                if self._session_open:
                    nxt = st.T + (st.closed + 1) * st.w
                    wait = min(max(nxt - st.now(), 0.0005), _MAX_IDLE)
                self._cond.wait(wait)
        self._step()
        with self._qcond:
            self._clock_done = True
            self._qcond.notify_all()

    def _step(self) -> None:
        """Apply the callers' events, close ended windows, follow the sink."""
        with self._lock:
            events, self._events = self._events, []
            want = self._want_session and not self._closed
            epoch = self._epoch
            now = self._st.now()  # every later event has t >= now
        if self._session_open and (not want or epoch != self._session_epoch):
            self._end_session()
        self._pending_events = deque(events)
        while self._pending_events:
            ev = self._current = self._pending_events.popleft()
            if self._session_open:
                self._close_until(ev.t)
            if self._current is ev:  # not already applied by a Loss (_shed)
                self._current = None
                self._apply(ev)
        if self._session_open:
            self._close_until(now)
        if not self._session_open and want:
            self._begin_session(now, epoch)

    def _begin_session(self, t: float, epoch: int) -> None:
        """Snapshot with seq 0 at ``t``; windows anchored at ``T = t``."""
        with self._lock:
            st = self._st
            st.session += 1
            st.T = t
            st.closed = 0
            st.windows = {}  # the previous session's counts are dropped whole
            st.pending = 0
            st.saturated = False
            for acc in self._accs.values():
                acc.start_session(st.session)
            st.live = True
        self._watched = {
            i for i in self._mirror.links if (a := self._accs.get(i)) and a.sampler
        }
        self._seq = 0
        self._session_open = True
        self._session_epoch = epoch
        attrs: Signal = {"protocol": PROTOCOL}
        if self._label is not None:
            attrs["label"] = self._label
        attrs.update(self._mirror.snapshot(t, new_session=True))
        self._emit("snapshot", t, attrs, opening=True)

    def _end_session(self) -> None:
        with self._lock:
            self._st.live = False
        self._session_open = False
        with self._qcond:
            self._q.clear()
            self._opening_queued = False

    def _apply(self, ev: _Event) -> None:
        """Update the mirror with one event and emit its signal."""
        m = self._mirror
        if ev.kind == "element":
            rec = ev.data
            assert isinstance(rec, ElementRec)
            m.elements[rec.id] = rec
            self._emit("add", ev.t, {"elements": [rec.signal()]})
        elif ev.kind == "link":
            lrec = ev.data
            assert isinstance(lrec, LinkRec)
            m.links[lrec.id] = lrec
            acc = self._accs.get(lrec.id)
            if acc is not None and acc.sampler is not None:
                self._watched.add(lrec.id)  # sampled at every boundary
            self._emit("add", ev.t, {"links": [lrec.signal()]})
        elif ev.kind == "remove":
            ids = ev.data
            assert isinstance(ids, list)
            removed = m.remove(ids)
            with self._lock:
                for ident in removed:
                    self._accs.pop(ident, None)
            for ident in removed:
                self._watched.discard(ident)
            self._emit("remove", ev.t, {"ids": ids})
        elif ev.kind == "span.start":
            srec = ev.data
            assert isinstance(srec, SpanRec)
            parent = srec.parent if srec.parent in m.spans else None
            m.spans[srec.span] = SpanRec(
                srec.span, srec.element, srec.label, parent, ev.t
            )
            attrs: Signal = {"span": srec.span, "id": srec.element, "label": srec.label}
            if parent is not None:
                attrs["parent"] = parent
            self._emit("span.start", ev.t, attrs)
        else:
            end = ev.data
            assert isinstance(end, _SpanEnd)
            if m.spans.pop(end.span, None) is None:
                return  # ended by a removal: no span.end (spec)
            attrs = {"span": end.span, "status": "ok" if end.error is None else "error"}
            if end.error:
                attrs["error"] = end.error
            self._emit("span.end", ev.t, attrs)

    def _close_until(self, t: float) -> None:
        """Emit the signals of every window that ended at or before ``t``."""
        st = self._st
        end = st.window_of(t)  # windows < end have ended: T + end*w <= t
        k = st.closed
        if end <= k:
            return
        if st.saturated or end - k > _MAX_LAG or self._pending() > _MAX_PENDING:
            # Fallen behind by more than the clock may keep counts for: Loss
            # (findings N3, B).
            self._shed(k)
            return
        # From here on, a late hot-path event for a window < end counts in
        # window `end` (accumulators read st.closed under their lock).
        st.closed = end
        session = st.session
        first = k
        while k < end and st.session == session:
            if k > first:
                lag = st.window_of(st.now()) - k
                if st.saturated or lag > _MAX_LAG or self._pending() > _MAX_PENDING:
                    self._shed(k)
                    return
            if not self._watched:
                # Nothing to report until the next window with counts.
                busy = [i for i in st.windows if i < end]
                nxt = min(busy, default=end)
                if nxt >= end:
                    break
                k = max(k, nxt)
            if not self._close_one(k):
                self._shed(k)
                return
            k += 1

    def _pending(self) -> int:
        """Window totals held now (exact, O(open windows)); also corrects the
        hot path's approximate count."""
        st = self._st
        n = sum(len(w) for w in list(st.windows.values()))
        st.pending = n
        return n

    def _shed(self, k: int) -> None:
        """Loss when the clock thread cannot keep up (findings N3, B).

        O(1) in the counts held: the shared window storage is swapped for an
        empty one and the old one dropped whole; depth state resets lazily
        (epoch). Every window up to the *current* one is given up. To keep
        ``t`` monotone, every change not emitted yet (this step's remaining
        events and any posted since) is applied to the mirror without its
        own signal; the snapshot, taken now, includes them. The ``seq`` gap
        counts the signals not sent.
        """
        st = self._st
        with self._lock:
            more, self._events = self._events, []
            t_now = st.now()  # every later event has t >= t_now
            end = st.window_of(t_now)
            st.closed = max(end, st.closed)  # late events count in `end`
            old, st.windows = st.windows, {}
            st.pending = 0
            st.saturated = False
            st.epoch += 1
        lost = sum(len(w) for w in old.values())  # O(windows), not O(totals)
        del old
        self._silent = True
        try:
            if self._current is not None:
                ev, self._current = self._current, None
                self._apply(ev)
            while self._pending_events:
                self._apply(self._pending_events.popleft())
            for ev in more:
                self._apply(ev)
        finally:
            self._silent = False
        lost += max(end - k, 0) * len(self._watched)  # depth signals not created
        self._watched = {i for i in self._watched if i in self._accs}
        self._seq += max(lost, 1)
        attrs: Signal = {"protocol": PROTOCOL}
        if self._label is not None:
            attrs["label"] = self._label
        attrs.update(self._mirror.snapshot(t_now, new_session=False))
        self._emit("snapshot", t_now, attrs, exempt=True)

    def _close_one(self, k: int) -> bool:
        """Close window ``k``; False when it gave up (pending over the limit)."""
        st = self._st
        t0 = st.T + k * st.w
        t = st.T + (k + 1) * st.w
        session = st.session
        window = st.windows.pop(k, None) or {}
        st.pending -= len(window)
        # Counts a caller recorded into a window that was already closed
        # (it read `closed` just before the clock advanced) count here (R3).
        for j in [j for j in st.windows if j < k]:
            late_window = st.windows.pop(j)
            st.pending -= len(late_window)
            for ident, late in late_window.items():
                window.setdefault(ident, Totals()).merge(late)
        later = [w for i, w in st.windows.items() if i > k]
        for n, ident in enumerate(list(window.keys() | self._watched)):
            if st.session != session:
                return True  # a Loss began a new session: these windows are gone
            if n % _CHECK_EVERY == _CHECK_EVERY - 1 and (
                st.saturated or self._pending() > _MAX_PENDING
            ):
                return False
            acc = self._accs.get(ident)
            if acc is None or not self._mirror.exists(ident):
                self._watched.discard(ident)  # removed: forget it (N5)
                continue
            sample = None
            if acc.sampler is not None:
                try:  # user code: on this thread, outside every lock
                    sample = int(acc.sampler())
                except Exception:
                    sample = None
            with acc.lock():
                tot = window.get(ident)
                later_delta = sum(w[ident].delta for w in later if ident in w)
                d = acc.depth(later_delta, sample)
            if tot is not None:
                if ident in self._mirror.links and (
                    tot.items or tot.dropped or tot.coalesced
                ):
                    flow: Signal = {"link": ident, "t0": t0, "items": tot.items}
                    if tot.sized and tot.items > 0:
                        flow["bytes"] = tot.bytes
                    flow["dropped"] = tot.dropped
                    flow["coalesced"] = tot.coalesced
                    self._emit("flow", t, flow)
                if tot.lat_count:
                    top = tot.lat_max
                    total = min(max(tot.lat_sum, top), tot.lat_count * top)
                    self._emit(
                        "latency",
                        t,
                        {
                            "id": ident,
                            "t0": t0,
                            "count": tot.lat_count,
                            "sum": total,
                            "max": top,
                        },
                    )
            if d.value is not None:
                depth: Signal = {"id": ident, "value": d.value}
                if acc.capacity is not None:
                    depth["capacity"] = acc.capacity
                self._emit("depth", t, depth)
            if d.watched:
                self._watched.add(ident)
            else:
                self._watched.discard(ident)
        return True

    def _emit(
        self,
        typ: str,
        t: float,
        attrs: Signal,
        opening: bool = False,
        exempt: bool = False,
    ) -> None:
        """Create one signal; Loss when the queue overflows (spec, Loss)."""
        if not self._session_open:
            return
        if self._silent:
            self._seq += 1  # created and lost (a Loss in progress)
            return
        signal: Signal = {"source": self._source, "seq": self._seq, "t": t, "type": typ}
        signal.update(attrs)
        self._seq += 1
        line = encode(signal)
        with self._qcond:
            self._q.append((line, opening, self._session_epoch))
            if opening:
                self._opening_queued = True
            # A snapshot that follows a Loss (or opens a session) is never
            # itself discarded; every other line counts strictly, the batch
            # in flight included.
            if exempt or opening or len(self._q) + self._inflight <= self._queue_limit:
                self._qcond.notify()
                return
            restart = self._opening_queued
            self._q.clear()
            self._opening_queued = False
        if restart:
            # The discarded signals include the session's opening snapshot:
            # begin a new session instead (spec, Loss).
            self._begin_session(t, self._session_epoch)
        else:
            attrs = {"protocol": PROTOCOL}
            if self._label is not None:
                attrs["label"] = self._label
            attrs.update(self._mirror.snapshot(t, new_session=False))
            self._emit("snapshot", t, attrs, exempt=True)

    # ------------------------------------------------------------------ writer

    def _try_open(self) -> bool:
        ok = _call(self._sink.open) is True
        with self._lock:
            if self._closing:
                if ok:
                    _call(self._sink.close)  # close() came during the connect
                return False
            if ok:
                self._connected = True
                self._epoch += 1
                self._want_session = True
                self._cond.notify_all()
        return ok

    def _take_batch(self, wait: bool, limit: int | None) -> tuple[list[str], int]:
        with self._qcond:
            if wait and not self._q and not self._clock_done:
                self._qcond.wait(min(self._st.w, 0.25))
            n = len(self._q) if limit is None else min(limit, len(self._q))
            batch: list[str] = []
            epoch = self._epoch
            for _ in range(n):
                line, opening, line_epoch = self._q.popleft()
                if opening:
                    self._opening_queued = False
                if line_epoch == epoch:  # never a line of an earlier connection
                    batch.append(line)
            self._inflight = len(batch)
        return batch, epoch

    def _write(self, batch: list[str], epoch: int) -> bool:
        ok = _call(self._sink.is_open) is True
        if ok:
            try:
                self._sink.write(batch)
            except Exception:
                ok = False
        with self._qcond:
            self._inflight = 0
        if not ok:
            self._lost_connection()
        return ok

    def _lost_connection(self) -> None:
        self._connected = False
        with self._lock:
            self._want_session = False
            self._cond.notify_all()
        with self._qcond:
            self._q.clear()
            self._opening_queued = False

    def _write_loop(self) -> None:
        first = True
        while True:
            with self._lock:
                closing = self._closing
            if not self._connected:
                if closing:
                    return
                if not first:
                    delay = _call(self._sink.retry_delay)
                    if not isinstance(delay, (int, float)):
                        return  # the sink never reopens
                    with self._lock:
                        self._cond.wait_for(lambda: self._closing, timeout=delay)
                        if self._closing:
                            return
                first = False
                self._try_open()
                continue
            if closing:
                with self._qcond:
                    done = self._clock_done and not self._q
                if done:
                    return
            batch, epoch = self._take_batch(wait=True, limit=self._batch)
            if batch:
                self._write(batch, epoch)
            elif _call(self._sink.is_open) is not True:
                self._lost_connection()  # e.g. the observer went away while idle


def _call(fn: Callable[[], object]) -> object:
    """Call a sink method; a sink that raises is treated as failing."""
    try:
        return fn()
    except Exception:
        return None


_REPORTERS: "weakref.WeakSet[Reporter]" = weakref.WeakSet()


@atexit.register
def _close_at_exit() -> None:
    """Close every open reporter at interpreter exit (flushes file sinks)."""
    for reporter in list(_REPORTERS):
        reporter.close(timeout=1.0)


def _disable_after_fork() -> None:
    """In a forked child, every reporter of the parent is disabled.

    The child has no clock or writer thread, and it must not touch the
    parent's connection (closing a shared socket would shut it down for the
    parent too). Its reporters report nothing; a child that wants to report
    creates its own reporter.
    """
    for reporter in list(_REPORTERS):
        # A parent thread may have held these locks at fork time: the child
        # gets fresh ones (finding A), so no call in the child can block.
        reporter._lock = threading.Lock()
        reporter._cond = threading.Condition(reporter._lock)
        reporter._qlock = threading.Lock()
        reporter._qcond = threading.Condition(reporter._qlock)
        reporter._st.live = False
        reporter._closing = True
        reporter._closed = True
        reporter._threads = []
        reporter._events = []
        reporter._accs.clear()
        reporter._q.clear()
    _REPORTERS.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_disable_after_fork)


__all__ = ["LinkKind", "Reporter"]
