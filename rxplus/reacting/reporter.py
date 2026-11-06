"""The reacting/1 source: :class:`Reporter` and its handles (standard library only).

See ``docs/reacting.md`` for the model and ``docs/proposals/
2026-11-06-reacting-support.md`` (D16) for the design. In short:

- The reporter always holds the graph state (elements, links, open spans).
- Signals are built only while a session is open on the sink (``live``).
- Windows are anchored at the session's start ``T``; per-window counters
  live in one :class:`~rxplus.reacting._window.Accumulator` per element or
  link. Any structural or span signal first closes the windows that have
  ended, so signals are created in ``t`` order.
- Encoded lines wait in a bounded deque; exceeding ``queue`` discards them
  and queues a snapshot instead (spec, Loss). Nothing ever blocks the caller.
"""

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Literal

from ._encode import (
    PROTOCOL,
    Signal,
    check_id,
    check_kind,
    check_label,
    check_reference,
    encode,
)
from ._window import Accumulator
from .clock import Clock, SystemClock
from .handles import _CURRENT, Element, Link, LinkKind, Span, _Live
from .sinks import Sink

_BATCH = 512  # lines handed to the sink per write


@dataclass(slots=True)
class _ElementRec:
    id: str
    label: str
    parent: str | None
    kind: str | None

    def signal(self) -> Signal:
        d: Signal = {"id": self.id, "label": self.label}
        if self.parent is not None:
            d["parent"] = self.parent
        if self.kind is not None:
            d["kind"] = self.kind
        return d


@dataclass(slots=True)
class _LinkRec:
    id: str
    src: str
    dst: str
    kind: LinkKind

    def signal(self) -> Signal:
        return {"id": self.id, "from": self.src, "to": self.dst, "kind": self.kind}


@dataclass(slots=True)
class _SpanRec:
    span: str
    element: str
    label: str
    parent: str | None  # the parent as sent in the current session
    t0: float


class Reporter:
    """One reacting/1 source. Thread-safe; never blocks the caller.

    Args:
        source: Source name (non-empty, no ``/``).
        sink: Where the session's lines go; one sink per reporter (D16).
        label: Display name of the source, sent in snapshots.
        window: The spec's ``window`` option, in seconds.
        queue: The spec's ``queue`` option: lines created but not yet
            written before Loss.
        clock: Time source; :class:`ManualClock` in tests.
        timer: True: a background thread opens the sink, closes windows
            on time and writes. False: nothing runs in the background;
            :meth:`poll` does all three (deterministic tests).
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
        self._w = float(window)
        self._queue_limit = queue
        self._clock: Clock = clock or SystemClock()
        self._st = _Live(self._clock.monotonic, self._w)

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._elements: dict[str, _ElementRec] = {}
        self._links: dict[str, _LinkRec] = {}
        self._accs: dict[str, Accumulator] = {}
        self._spans: dict[str, _SpanRec] = {}
        self._counter = {"e": 0, "l": 0, "s": 0}

        self._session = 0
        self._t_start = 0.0  # T
        self._mono_start = 0.0  # monotonic time at T
        self._seq = 0
        self._last_t = 0.0
        self._closed_upto = 0  # windows < this index are closed
        self._lines: deque[str] = deque()
        self._connected = False
        self._closing = False
        self._closed = False

        self._thread: threading.Thread | None = None
        if timer:
            self._thread = threading.Thread(
                target=self._run, name=f"rxplus-reacting-{source}", daemon=True
            )
            self._thread.start()
        else:
            self._open_sink()

    # ------------------------------------------------------------------ public

    @property
    def source(self) -> str:
        return self._source

    @property
    def live(self) -> bool:
        """Whether a session is open on the sink."""
        return self._st.live

    def element(
        self, label: str, *, kind: str | None = None, parent: "Element | None" = None
    ) -> "Element":
        """Add an element (optionally a child of ``parent``) and return its handle."""
        check_label(label)
        check_kind(kind)
        with self._lock:
            parent_id = None
            if parent is not None:
                if parent.reporter is not self or parent.id not in self._elements:
                    raise ValueError(
                        "parent is not an existing element of this reporter"
                    )
                parent_id = parent.id
            self._sync()
            rec = _ElementRec(self._next_id("e"), label, parent_id, kind)
            self._elements[rec.id] = rec
            acc = self._accs[rec.id] = Accumulator()
            acc.start_session(self._session, self._closed_upto)
            self._emit_change("add", elements=[rec.signal()])
        return Element(self, rec.id, label, acc)

    def link(
        self,
        src: "Element | str",
        dst: "Element | str",
        *,
        kind: LinkKind = "data",
        capacity: int | None = None,
        depth: Callable[[], int] | None = None,
    ) -> "Link":
        """Add a link; ``src``/``dst`` are elements or ``"<source>/<id>"``.

        ``depth`` is a sampled backlog, read once per window end; it must be
        cheap, must not block and must not call into this reporter.
        """
        if kind not in ("data", "call"):
            raise ValueError(f"link kind must be 'data' or 'call': {kind!r}")
        if capacity is not None and capacity < 0:
            raise ValueError(f"capacity must be non-negative: {capacity!r}")
        with self._lock:
            ends = [self._endpoint(e) for e in (src, dst)]
            self._sync()
            rec = _LinkRec(self._next_id("l"), ends[0], ends[1], kind)
            self._links[rec.id] = rec
            acc = self._accs[rec.id] = Accumulator(depth, capacity)
            acc.start_session(self._session, self._closed_upto)
            self._emit_change("add", links=[rec.signal()])
        return Link(self, rec.id, kind, acc)

    def remove(self, *handles: "Element | Link") -> None:
        """Remove elements and links; removing an element removes its subtree,
        the links touching it and its open spans (spec, Elements)."""
        with self._lock:
            ids = [
                h.id
                for h in handles
                if h.reporter is self
                and (h.id in self._elements or h.id in self._links)
            ]
            if not ids:
                return
            # Windows that ended before the removal still report the removed
            # elements and links: close them before forgetting the counters.
            self._sync()
            for ident in ids:
                self._drop_locked(ident)
            self._emit_change("remove", ids=ids)

    def poll(self) -> None:
        """Close every window that has ended; with ``timer=False`` also open
        the sink if needed and write what is queued."""
        if self._thread is None and not self._closed:
            if self._connected and not self._sink_open():
                with self._lock:
                    self._end_session()
            if not self._connected:
                self._open_sink()
            with self._lock:
                self._close_windows(self._clock.monotonic())
                batch = list(self._lines)
                self._lines.clear()
                session = self._session
            self._write(batch, session)
        else:
            with self._lock:
                self._close_windows(self._clock.monotonic())

    def close(self, timeout: float = 1.0) -> None:
        """End the session: close ended windows, write what is queued (at
        most ``timeout`` seconds), close the sink. Idempotent."""
        if self._closed:
            return
        if self._thread is not None:
            with self._lock:
                self._close_windows(self._clock.monotonic())
                self._closing = True
                self._cond.notify_all()
            self._thread.join(timeout)
        else:
            self.poll()
        with self._lock:
            self._closed = True
            self._end_session()
        try:
            self._sink.close()
        except Exception:
            pass

    def __enter__(self) -> "Reporter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --------------------------------------------------------- graph internals

    def _next_id(self, prefix: Literal["e", "l", "s"]) -> str:
        self._counter[prefix] += 1
        return f"{prefix}{self._counter[prefix]}"

    def _endpoint(self, end: "Element | str") -> str:
        if isinstance(end, str):
            return check_reference(end, self._source)
        if end.reporter is not self or end.id not in self._elements:
            raise ValueError(
                "link endpoint is not an existing element of this reporter"
            )
        return end.id

    def _drop_locked(self, ident: str) -> None:
        """Forget ``ident`` and everything its removal removes (no signal)."""
        if ident in self._links:
            del self._links[ident]
            self._accs.pop(ident, None)
            return
        if ident not in self._elements:
            return
        gone = {ident}
        for rec in self._elements.values():  # insertion order: parents first
            if rec.parent in gone:
                gone.add(rec.id)
        for e in gone:
            del self._elements[e]
            self._accs.pop(e, None)
        for lid in [
            i for i, r in self._links.items() if r.src in gone or r.dst in gone
        ]:
            del self._links[lid]
            self._accs.pop(lid, None)
        for sid in [s for s, r in self._spans.items() if r.element in gone]:
            del self._spans[sid]  # ended by the removal; no span.end (spec)

    # ---------------------------------------------------------------- spans

    def _start_span(self, element: str, label: str, parent: "Span | None") -> "Span":
        check_label(label, "span label")
        if parent is None:
            parent = _CURRENT.get()
        with self._lock:
            if element not in self._elements or not self._st.live:
                return Span(self, None)
            self._sync()
            sid = self._next_id("s")
            parent_id = parent.id if parent is not None else None
            if parent_id not in self._spans:
                parent_id = None  # spec: a parent must be open; otherwise omitted
            t = self._now()[1]
            self._spans[sid] = _SpanRec(sid, element, label, parent_id, t)
            attrs: Signal = {"span": sid, "id": element, "label": label}
            if parent_id is not None:
                attrs["parent"] = parent_id
            self._emit("span.start", t, attrs)
        return Span(self, sid)

    def _end_span(self, sid: str, error: BaseException | str | None) -> None:
        with self._lock:
            if sid not in self._spans:
                return
            self._sync()
            if self._spans.pop(sid, None) is None or not self._st.live:
                return
            t = self._now()[1]
            attrs: Signal = {"span": sid, "status": "ok" if error is None else "error"}
            if error is not None:
                # Content rule: never str(exception), which often carries data.
                text = (
                    type(error).__name__ if isinstance(error, BaseException) else error
                )
                if text:
                    attrs["error"] = text
            self._emit("span.end", t, attrs)

    # ------------------------------------------------------- sessions, signals

    def _now(self) -> tuple[float, float]:
        """(monotonic, t) now; t never below the session's last t."""
        mono = self._clock.monotonic()
        t = self._t_start + (mono - self._mono_start)
        return mono, max(t, self._last_t)

    def _emit(self, typ: str, t: float, attrs: Signal) -> None:
        """Create one signal (caller holds the lock and the session is live)."""
        signal: Signal = {"source": self._source, "seq": self._seq, "t": t, "type": typ}
        signal.update(attrs)
        self._seq += 1
        self._last_t = t
        self._lines.append(encode(signal))
        if len(self._lines) > self._queue_limit:
            # Loss: discard every queued line; the next one written is a
            # snapshot, and the seq gap tells the observer how many were lost.
            self._lines.clear()
            self._emit_snapshot(t, recovery=True)
        self._cond.notify()

    def _sync(self) -> None:
        """Close the windows that have ended (caller holds the lock).

        Every operation that changes the graph or the open spans calls this
        *before* changing them: closing windows may overflow the queue, and
        the recovery snapshot must not already contain the change whose own
        signal follows it.
        """
        self._close_windows(self._clock.monotonic())

    def _emit_change(self, typ: str, **attrs: object) -> None:
        if not self._st.live:
            return
        self._emit(typ, self._now()[1], dict(attrs))

    def _emit_snapshot(self, t: float, recovery: bool) -> None:
        spans = []
        for rec in self._spans.values():  # insertion order: parents first
            if not recovery and rec.parent not in self._spans:
                rec.parent = None  # what this session's snapshot sends
            d: Signal = {"span": rec.span, "id": rec.element, "label": rec.label}
            if rec.parent is not None:
                d["parent"] = rec.parent
            d["t0"] = min(rec.t0, t)
            spans.append(d)
        attrs: Signal = {"protocol": PROTOCOL}
        if self._label is not None:
            attrs["label"] = self._label
        attrs["elements"] = [r.signal() for r in self._elements.values()]
        attrs["links"] = [r.signal() for r in self._links.values()]
        if spans:
            attrs["spans"] = spans
        self._emit("snapshot", t, attrs)

    def _start_session(self) -> None:
        """A connection opened: snapshot with seq 0 (caller holds the lock)."""
        self._session += 1
        self._t_start = self._clock.wall()
        self._mono_start = self._clock.monotonic()
        self._seq = 0
        self._last_t = self._t_start
        self._closed_upto = 0
        self._lines.clear()
        for acc in self._accs.values():
            acc.start_session(self._session)
        st = self._st
        st.session = self._session
        st.mono0 = self._mono_start
        st.live = True
        self._emit_snapshot(self._t_start, recovery=False)

    def _end_session(self) -> None:
        self._st.live = False
        self._connected = False
        self._lines.clear()

    # ------------------------------------------------------------------ windows

    def _close_windows(self, mono: float) -> None:
        """Emit the signals of every window that ended by ``mono`` (locked)."""
        if not self._st.live:
            return
        end = int((mono - self._mono_start) / self._w)  # windows < end have ended
        k = self._closed_upto
        if end <= k:
            return
        accs = list(self._accs.items())
        session = self._session
        while k < end:
            busy = sorted(
                {i for _, a in accs for i in a.pending_windows(session) if i >= k}
            )
            if not any(a.has_backlog(session) for _, a in accs):
                # Nothing to report until the next window with events.
                nxt = busy[0] if busy else end
                if nxt >= end:
                    break
                k = nxt
            self._close_one(k, accs, session)
            k += 1
        self._closed_upto = end
        for _, acc in accs:
            acc.mark_closed(session, end)

    def _close_one(
        self, k: int, accs: list[tuple[str, Accumulator]], session: int
    ) -> None:
        t0 = self._t_start + k * self._w
        t = self._t_start + (k + 1) * self._w
        for ident, acc in accs:
            sample = None
            if acc.sampler is not None:
                try:
                    sample = int(acc.sampler())
                except Exception:
                    sample = 0
            res = acc.close(session, k, sample)
            tot = res.totals
            if tot is not None:
                if ident in self._links and (tot.items or tot.dropped or tot.coalesced):
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
            if res.depth is not None:
                depth: Signal = {"id": ident, "value": res.depth}
                if acc.capacity is not None:
                    depth["capacity"] = acc.capacity
                self._emit("depth", t, depth)

    # ------------------------------------------------------------ the writer

    def _open_sink(self) -> bool:
        try:
            ok = self._sink.open()
        except Exception:
            ok = False
        if ok:
            with self._lock:
                self._connected = True
                self._start_session()
        return ok

    def _sink_open(self) -> bool:
        try:
            return self._sink.is_open()
        except Exception:
            return False

    def _write(self, batch: list[str], session: int) -> None:
        if not batch:
            return
        try:
            self._sink.write(batch)
        except Exception as e:
            # SinkDisconnected, or anything else: the session is over.
            del e
            with self._lock:
                if session == self._session:
                    self._end_session()

    def _run(self) -> None:
        """Writer thread: open the sink, close windows on time, write."""
        while True:
            with self._lock:
                if self._closing and (not self._connected or not self._lines):
                    return
            if not self._connected:
                if self._open_sink():
                    continue
                delay = self._sink.retry_delay()
                with self._lock:
                    if delay is None:
                        self._closing = True
                        return
                    self._cond.wait_for(lambda: self._closing, timeout=delay)
                continue
            if not self._sink_open():
                with self._lock:
                    self._end_session()
                continue
            with self._lock:
                self._close_windows(self._clock.monotonic())
                if not self._lines and not self._closing:
                    nxt = self._mono_start + (self._closed_upto + 1) * self._w
                    self._cond.wait(max(nxt - self._clock.monotonic(), 0.001))
                    self._close_windows(self._clock.monotonic())
                batch = [
                    self._lines.popleft() for _ in range(min(_BATCH, len(self._lines)))
                ]
                session = self._session
            self._write(batch, session)


__all__ = ["LinkKind", "Reporter"]
