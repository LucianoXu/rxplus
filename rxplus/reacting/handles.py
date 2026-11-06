"""Handles of a reporter's elements, links and spans (standard library only).

Hot-path methods start with one check of ``_Live.live``: while no session
is open they cost one attribute load and one branch (spec: at most one
check per item when no observer is connected).
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Literal

from ._window import Accumulator

if TYPE_CHECKING:
    from .reporter import Reporter

LinkKind = Literal["data", "call"]


class _Live:
    """What the hot path reads: one attribute check while not live."""

    __slots__ = ("live", "session", "mono0", "inv_w", "monotonic")

    def __init__(self, monotonic: Callable[[], float], window: float) -> None:
        self.live = False
        self.session = 0
        self.mono0 = 0.0
        self.inv_w = 1.0 / window
        self.monotonic = monotonic

    def window(self, mono: float) -> int:
        return int((mono - self.mono0) * self.inv_w)


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
        mono = st.monotonic()
        self._acc.change(st.session, st.window(mono), 1)
        return mono

    def end(self, token: float) -> None:
        """The item is finished: backlog -1 and one latency duration."""
        st = self._st
        if not st.live:
            return
        mono = st.monotonic()
        k = st.window(mono)
        if token >= st.mono0:
            self._acc.change(st.session, k, -1)
            self._acc.duration(st.session, k, mono - token)

    def span(self, label: str, *, parent: Span | None = None) -> Span:
        """Start a span of work on this element (``current_span()`` is the
        default parent)."""
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
        self._acc.flow(st.session, st.window(st.monotonic()), items=n, size=bytes)

    def drop(self, n: int = 1) -> None:
        st = self._st
        if not st.live:
            return
        self._acc.flow(st.session, st.window(st.monotonic()), dropped=n)

    def coalesce(self, n: int = 1) -> None:
        st = self._st
        if not st.live:
            return
        self._acc.flow(st.session, st.window(st.monotonic()), coalesced=n)

    def enter(self) -> float:
        """An item entered the link: backlog +1. Returns a token for leave."""
        st = self._st
        if not st.live:
            return -1.0
        mono = st.monotonic()
        self._acc.change(st.session, st.window(mono), 1)
        return mono

    def leave(self, token: float, *, bytes: int | None = None) -> None:
        """The item that entered with ``token`` was delivered."""
        st = self._st
        if not st.live:
            return
        mono = st.monotonic()
        k = st.window(mono)
        self._acc.flow(st.session, k, items=1, size=bytes)
        if token >= st.mono0:  # entered during this session
            self._acc.change(st.session, k, -1)
            self._acc.duration(st.session, k, mono - token)

    def leave_dropped(self, token: float) -> None:
        """The item that entered with ``token`` was dropped."""
        st = self._st
        if not st.live:
            return
        k = st.window(st.monotonic())
        self._acc.flow(st.session, k, dropped=1)
        if token >= st.mono0:
            self._acc.change(st.session, k, -1)

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
        mono = st.monotonic()
        k = st.window(mono)
        # A call counts in the window in which it was made (spec, Flow).
        link._acc.flow(st.session, k, items=1)
        link._acc.change(st.session, k, 1)
        self._token = mono

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        link = self._link
        st = link._st
        if not st.live or self._token < st.mono0:
            return
        mono = st.monotonic()
        k = st.window(mono)
        link._acc.change(st.session, k, -1)
        link._acc.duration(st.session, k, mono - self._token)


class Span:
    """An open span; a context manager that ends it (``error`` on exception)."""

    __slots__ = ("_reporter", "id", "_token")

    def __init__(self, reporter: Reporter, ident: str | None) -> None:
        self._reporter = reporter
        self.id = ident  # None: not reported (no session when it started)
        self._token: contextvars.Token[Span | None] | None = None

    def end(self, error: BaseException | str | None = None) -> None:
        """End the span; ``error`` marks it failed (only an exception's class
        name is sent)."""
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
