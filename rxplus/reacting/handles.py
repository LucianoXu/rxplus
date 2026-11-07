"""Handles of a reporter's elements, links and spans (standard library only).

Hot-path methods read the clock once and update one accumulator in O(1).
While no session is open they cost one attribute load and one branch (spec:
at most one check per item when no observer is connected). They never close
windows, encode signals or wait for the sink: the reporter's clock thread
does that.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Literal

from ._encode import check_label, check_text
from ._window import Accumulator, Totals

if TYPE_CHECKING:
    from .reporter import Reporter

LinkKind = Literal["data", "call"]


class _Live:
    """The shared time base and session that the hot path reads.

    ``t = base_t + (monotonic() - base_m)`` is the reporter's clock in Unix
    seconds; it never decreases. Window ``k`` of the session that began at
    ``T`` holds the times ``T + k*w <= t < T + (k+1)*w``.
    """

    __slots__ = (
        "live",
        "session",
        "T",
        "closed",
        "windows",
        "epoch",
        "pending",
        "cap",
        "saturated",
        "w",
        "inv_w",
        "base_t",
        "base_m",
        "monotonic",
    )

    def __init__(
        self, monotonic: Callable[[], float], base_t: float, window: float
    ) -> None:
        self.live = False
        self.session = -1
        self.T = 0.0
        self.closed = 0  # windows of the session below this index are closed
        # windows[k][ident]: the totals of element/link ident in window k
        self.windows: dict[int, dict[str, Totals]] = {}
        self.epoch = 0  # incremented by a Loss: every depth state resets
        self.pending = 0  # totals held in `windows` (approximate)
        self.cap = 100_000  # hard limit on `pending` (the reporter sets it)
        self.saturated = False  # a total was refused at the cap
        self.w = window
        self.inv_w = 1.0 / window
        self.base_t = base_t
        self.base_m = monotonic()
        self.monotonic = monotonic

    def now(self) -> float:
        return self.base_t + (self.monotonic() - self.base_m)

    def window_of(self, t: float) -> int:
        """Index of the window that contains ``t`` (decided in t-space)."""
        k = int((t - self.T) * self.inv_w)
        if self.T + (k + 1) * self.w <= t:
            k += 1
        elif k > 0 and self.T + k * self.w > t:
            k -= 1
        return max(k, 0)


_CURRENT: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "rxplus_reacting_span", default=None
)


def current_span() -> Span | None:
    """The span whose block (or ``rx.span`` operator) is running, if any."""
    return _CURRENT.get()


class Element:
    """Handle of an element. Hot-path methods cost one check while not live."""

    __slots__ = ("reporter", "id", "label", "_acc", "_st")

    def __init__(
        self, reporter: Reporter, ident: str, label: str, acc: Accumulator
    ) -> None:
        self.reporter = reporter
        self.id = ident
        self.label = label
        self._acc = acc
        self._st = reporter._st

    def child(self, label: str, *, kind: str | None = None) -> Element:
        return self.reporter.element(label, kind=kind, parent=self)

    def begin(self) -> float:
        """An item was received: backlog +1. Returns the token for :meth:`end`."""
        st = self._st
        if not st.live:
            return -1.0
        t = st.now()
        self._acc.record(st.session, st.window_of(t), delta=1)
        return t

    def end(self, token: float) -> None:
        """The item is finished: backlog -1 and one latency duration."""
        st = self._st
        if not st.live:
            return
        if token >= st.T:  # began during this session
            t = st.now()
            self._acc.record(
                st.session, st.window_of(t), delta=-1, duration=max(t - token, 0.0)
            )

    def span(self, label: str, *, parent: Span | None = None) -> Span:
        """Start a span of work on this element (``current_span()`` of this
        reporter is the default parent). Without a session it is not
        reported, but ``label`` is validated all the same."""
        check_label(label, "span label")
        if not self._st.live:
            return Span(self.reporter, None)
        return self.reporter._start_span(self.id, label, parent)

    def remove(self) -> None:
        self.reporter.remove(self)


class Link:
    """Handle of a link. Hot-path methods cost one check while not live."""

    __slots__ = ("reporter", "id", "kind", "_acc", "_st")

    def __init__(
        self, reporter: Reporter, ident: str, kind: LinkKind, acc: Accumulator
    ) -> None:
        self.reporter = reporter
        self.id = ident
        self.kind = kind
        self._acc = acc
        self._st = reporter._st

    def deliver(self, n: int = 1, *, bytes: int | None = None) -> None:
        """``n`` items were delivered at ``to`` (``bytes``: their total size)."""
        st = self._st
        if not st.live:
            return
        self._acc.record(st.session, st.window_of(st.now()), items=n, size=bytes)

    def drop(self, n: int = 1) -> None:
        st = self._st
        if not st.live:
            return
        self._acc.record(st.session, st.window_of(st.now()), dropped=n)

    def coalesce(self, n: int = 1) -> None:
        st = self._st
        if not st.live:
            return
        self._acc.record(st.session, st.window_of(st.now()), coalesced=n)

    def enter(self) -> float:
        """An item entered the link: backlog +1. Returns a token for leave."""
        st = self._st
        if not st.live:
            return -1.0
        t = st.now()
        self._acc.record(st.session, st.window_of(t), delta=1)
        return t

    def leave(self, token: float, *, bytes: int | None = None) -> None:
        """The item that entered with ``token`` was delivered."""
        st = self._st
        if not st.live:
            return
        t = st.now()
        if token >= st.T:  # entered during this session
            self._acc.record(
                st.session,
                st.window_of(t),
                items=1,
                size=bytes,
                delta=-1,
                duration=max(t - token, 0.0),
            )
        else:
            self._acc.record(st.session, st.window_of(t), items=1, size=bytes)

    def leave_dropped(self, token: float) -> None:
        """The item that entered with ``token`` was dropped."""
        st = self._st
        if not st.live:
            return
        delta = -1 if token >= st.T else 0
        self._acc.record(st.session, st.window_of(st.now()), dropped=1, delta=delta)

    def call(self) -> _Call:
        """Context manager for one call on a ``call`` link."""
        if self.kind != "call":
            raise ValueError(f"link {self.id} is not a call link")
        return _Call(self)

    def remove(self) -> None:
        self.reporter.remove(self)


class _Call:
    __slots__ = ("_link", "_token")

    def __init__(self, link: Link) -> None:
        self._link = link
        self._token = -1.0

    def __enter__(self) -> None:
        link = self._link
        st = link._st
        if not st.live:
            return
        t = st.now()
        # A call counts in the window in which it was made (spec, Flow).
        link._acc.record(st.session, st.window_of(t), items=1, delta=1)
        self._token = t

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        link = self._link
        st = link._st
        if not st.live or self._token < st.T:
            return
        t = st.now()
        link._acc.record(
            st.session, st.window_of(t), delta=-1, duration=max(t - self._token, 0.0)
        )


class Span:
    """An open span; a context manager that ends it (``error`` on exception)."""

    __slots__ = ("_reporter", "id", "_token")

    def __init__(self, reporter: Reporter, ident: str | None) -> None:
        self._reporter = reporter
        self.id = ident  # None: not reported (no session when it started)
        self._token: contextvars.Token[Span | None] | None = None

    def end(self, error: BaseException | str | None = None) -> None:
        """End the span; ``error`` marks it failed (only an exception's class
        name is sent; a string must be free of data content)."""
        if isinstance(error, str):
            check_text(error, "span error")
        if self.id is not None:
            self._reporter._end_span(self.id, error)
            self.id = None

    def __enter__(self) -> Span:
        self._token = _CURRENT.set(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._token is not None:
            _CURRENT.reset(self._token)
            self._token = None
        self.end(exc)
