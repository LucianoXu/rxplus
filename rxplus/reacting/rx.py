"""Probe operators for reactivex pipelines, and :class:`ContextScheduler`.

Every probe takes a handle or ``None``; with ``None`` it is the identity
operator and installs nothing, so ``rr.count(link if reporter else None)``
costs nothing without a reporter. With a handle, each item costs one check
while no session is open (spec: at most one check per item without an
observer).

Probes never change items, their order or their timing, and never catch
exceptions: an exception raised downstream propagates unchanged (a
``span`` probe ends its span with ``error`` first).

This is the only module of :mod:`rxplus.reacting` that imports reactivex.
"""

from __future__ import annotations

import contextvars
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, cast, overload

import reactivex
from reactivex import Observable, abc
from reactivex.scheduler.periodicscheduler import PeriodicScheduler

from .handles import Element, Link

type Operator[T] = Callable[[Observable[T]], Observable[T]]


class Probe(Protocol):
    """An operator that passes items of any type through unchanged."""

    def __call__[T](self, source: Observable[T], /) -> Observable[T]: ...


#: Tokens an ``enter`` probe keeps for its ``leave`` probe, per link. A hop
#: that drops items without ``Link.leave_dropped`` would otherwise grow it
#: without bound; beyond this the oldest token is forgotten.
MAX_IN_FLIGHT = 1 << 16


def _identity[T](source: Observable[T]) -> Observable[T]:
    return source


def _tap[T](
    source: Observable[T],
    on_next: Callable[[abc.ObserverBase[T], T], None],
) -> Observable[T]:
    """An observable that calls ``on_next(observer, value)`` per item and
    forwards errors and completion unchanged."""

    def subscribe(
        observer: abc.ObserverBase[T], scheduler: abc.SchedulerBase | None = None
    ) -> abc.DisposableBase:
        return source.subscribe(
            lambda value: on_next(observer, value),
            observer.on_error,
            observer.on_completed,
            scheduler=scheduler,
        )

    return Observable(subscribe)


@overload
def count(link: Link | None) -> Probe: ...
@overload
def count[T](link: Link | None, *, size: Callable[[T], int]) -> Operator[T]: ...
def count[T](
    link: Link | None, *, size: Callable[[T], int] | None = None
) -> Probe | Operator[T]:
    """Each item passing is delivered on ``link`` (flow; bytes if ``size``)."""
    if link is None:
        return _identity
    st = link._st

    def op(source: Observable[T]) -> Observable[T]:
        def on_next(observer: abc.ObserverBase[T], value: T) -> None:
            if st.live:
                link.deliver(1, bytes=None if size is None else size(value))
            observer.on_next(value)

        return _tap(source, on_next)

    return op if size is not None else cast(Probe, op)


def _tokens(link: Link) -> deque[float]:
    tokens = link._tokens
    if tokens is None:
        tokens = link._tokens = deque(maxlen=MAX_IN_FLIGHT)
    return tokens


def enter(link: Link | None) -> Probe:
    """Each item passing enters ``link``: backlog +1. Pair it with
    :func:`leave` on the far side of an asynchronous hop (``observe_on``, a
    queue); they assume the hop keeps FIFO order. A token is kept for every
    item, with or without a session (``-1`` without), so the pairs stay
    aligned across a session gap."""
    if link is None:
        return _identity
    tokens = _tokens(link)

    def op[T](source: Observable[T]) -> Observable[T]:
        def on_next(observer: abc.ObserverBase[T], value: T) -> None:
            tokens.append(link.enter())  # -1.0 without a session: one check
            observer.on_next(value)

        return _tap(source, on_next)

    return op


@overload
def leave(link: Link | None) -> Probe: ...
@overload
def leave[T](link: Link | None, *, size: Callable[[T], int]) -> Operator[T]: ...
def leave[T](
    link: Link | None, *, size: Callable[[T], int] | None = None
) -> Probe | Operator[T]:
    """Each item passing leaves ``link``, entered by :func:`enter`: it is
    delivered (flow; bytes if ``size``), backlog -1, and its time in the hop
    counts as latency. An item that entered before the session counts as
    flow only."""
    if link is None:
        return _identity
    st = link._st
    tokens = _tokens(link)

    def op(source: Observable[T]) -> Observable[T]:
        def on_next(observer: abc.ObserverBase[T], value: T) -> None:
            # Popped with or without a session, so the pairs stay aligned.
            token = tokens.popleft() if tokens else None
            if st.live:
                nbytes = None if size is None else size(value)
                if token is None:  # did not pass `enter` (or was forgotten)
                    link.deliver(1, bytes=nbytes)
                else:
                    link.leave(token, bytes=nbytes)
            observer.on_next(value)

        return _tap(source, on_next)

    return op if size is not None else cast(Probe, op)


def work(element: Element | None) -> Probe:
    """Time until the downstream ``on_next`` returns, per item: latency and
    in-flight backlog of ``element``."""
    if element is None:
        return _identity
    st = element._st

    def op[T](source: Observable[T]) -> Observable[T]:
        def on_next(observer: abc.ObserverBase[T], value: T) -> None:
            if not st.live:
                observer.on_next(value)
                return
            token = element.begin()
            try:
                observer.on_next(value)
            finally:
                element.end(token)

        return _tap(source, on_next)

    return op


def span(element: Element | None, label: str) -> Probe:
    """One span per item around the downstream ``on_next`` (for coarse
    streams). It is the current span for that call, so spans started further
    down the same synchronous chain are its children; it ends with ``error``
    (the exception's class name) when downstream raises."""
    if element is None:
        return _identity
    st = element._st
    from ._encode import check_label

    check_label(label, "span label")

    def op[T](source: Observable[T]) -> Observable[T]:
        def on_next(observer: abc.ObserverBase[T], value: T) -> None:
            if not st.live:
                observer.on_next(value)
                return
            with element.span(label):
                observer.on_next(value)

        return _tap(source, on_next)

    return op


class ContextScheduler(PeriodicScheduler):
    """Wraps a scheduler so that each action runs in a copy of the context
    taken when it was scheduled. Use it in ``observe_on`` or
    ``subscribe_on`` to keep span parents (:func:`current_span`) across a
    thread hop; without it, a span on the far side has no parent. It is a
    periodic scheduler too (``interval``, ``timer``), built on the inner
    scheduler's ``schedule_relative``."""

    def __init__(self, inner: abc.SchedulerBase) -> None:
        super().__init__()
        self._inner = inner

    def _wrap(self, action: abc.ScheduledAction[Any]) -> abc.ScheduledAction[Any]:
        ctx = contextvars.copy_context()

        def run(_: abc.SchedulerBase, state: Any) -> abc.DisposableBase | None:
            # `self`, not the inner scheduler: what the action schedules in
            # turn keeps its context too.
            return ctx.run(action, self, state)

        return run

    @property
    def now(self) -> datetime:
        return self._inner.now

    def schedule(
        self, action: abc.ScheduledAction[Any], state: Any = None
    ) -> abc.DisposableBase:
        return self._inner.schedule(self._wrap(action), state)

    def schedule_relative(
        self,
        duetime: reactivex.typing.RelativeTime,
        action: abc.ScheduledAction[Any],
        state: Any = None,
    ) -> abc.DisposableBase:
        return self._inner.schedule_relative(duetime, self._wrap(action), state)

    def schedule_absolute(
        self,
        duetime: reactivex.typing.AbsoluteTime,
        action: abc.ScheduledAction[Any],
        state: Any = None,
    ) -> abc.DisposableBase:
        return self._inner.schedule_absolute(duetime, self._wrap(action), state)


__all__ = [
    "MAX_IN_FLIGHT",
    "ContextScheduler",
    "Operator",
    "Probe",
    "count",
    "enter",
    "leave",
    "span",
    "work",
]
