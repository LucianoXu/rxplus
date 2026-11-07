"""Miscellaneous Rx operators used by ``rxplus``."""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opentelemetry.trace import Span, StatusCode
from reactivex import Observable, Observer, create
from reactivex.abc import DisposableBase, ObserverBase, SchedulerBase
from reactivex.disposable import Disposable
from reactivex.scheduler import TimeoutScheduler

from .mechanism import RxException

if TYPE_CHECKING:
    from .reacting import ReportTo
    from .reacting._component import ComponentReport
    from .reacting.handles import Element
    from .reacting.handles import Span as ReactingSpan


def stream_print_out[T](
    prompt: str = "Stream-Print-Out",
) -> Callable[[Observable[T]], Observable[T]]:
    """
    Print out and forward the data stream. For debug or info output purpose.
    """

    def _stream_print_out(source: Observable[T]) -> Observable[T]:

        def subscribe(
            observer: ObserverBase[T], scheduler: SchedulerBase | None = None
        ) -> DisposableBase:
            def on_next(value: T) -> None:
                print(f"{prompt}{value}")
                observer.on_next(value)

            def on_error(error: Exception) -> None:
                print(f"Error observed: {error}")
                observer.on_error(error)

            def on_completed() -> None:
                print(f"{prompt}: Completed.")
                observer.on_completed()

            return source.subscribe(
                on_next=on_next,
                on_error=on_error,
                on_completed=on_completed,
                scheduler=scheduler,
            )

        return Observable(subscribe)

    return _stream_print_out


def redirect_to[T](
    cond: Callable[[T], bool],
    redirect_target: Observer[T] | Callable[[T], object],
) -> Callable[[Observable[T]], Observable[T]]:
    """
    Redirect items to the specified observer (or function),
    and forward other items.
    """

    def _redirect_to(source: Observable[T]) -> Observable[T]:
        def subscribe(
            observer: ObserverBase[T], scheduler: SchedulerBase | None = None
        ) -> DisposableBase:
            # determine the redirection function
            if callable(redirect_target):
                redirect_fun = redirect_target
            else:
                redirect_fun = redirect_target.on_next

            def on_next(value: T) -> None:
                if cond(value):
                    redirect_fun(value)
                else:
                    observer.on_next(value)

            return source.subscribe(
                on_next=on_next,
                on_error=observer.on_error,
                on_completed=observer.on_completed,
                scheduler=scheduler,
            )

        return Observable(subscribe)

    return _redirect_to


@dataclass
class ErrorRestartSignal:
    """
    A signal emitted when a retry is triggered by `retry_with_signal`.
    The underlying error is contained in the `error` attribute.
    """

    error: RxException
    attempts: int

    def __str__(self) -> str:
        return f"ErrorRestartSignal(attempts={self.attempts}, error={self.error})"

    def record_as_span_event(self, span: Span) -> None:
        """Record this signal as an event on the given OTel span."""
        span.add_event(
            "retry",
            attributes={
                "attempt": self.attempts,
                "error.type": type(self.error.exception).__name__,
                "error.message": str(self.error.exception),
                "error.source": self.error.source or "",
                "error.note": self.error.note or "",
            },
        )
        span.set_status(StatusCode.ERROR, f"Retry attempt {self.attempts}")


def _quiet_dispose(d: DisposableBase | None) -> None:
    """Dispose of a subscription that is no longer wanted; an error from a
    stale attempt's teardown must not reach the live one (review of #25)."""
    if d is not None:
        try:
            d.dispose()
        except Exception:
            pass


def retry_with_signal[T](
    max_retries: int | None = None,
    *,
    delay_s: float | Callable[[int, RxException], float] = 0.0,
    should_retry: Callable[[RxException], bool] | None = None,
    scheduler: SchedulerBase | None = None,
    reacting: "ReportTo | None" = None,
    label: str = "retry_with_signal",
) -> Callable[[Observable[T]], Observable[T | ErrorRestartSignal]]:
    """
    Operator: on upstream error, emit a `ErrorRestartSignal` and retry.

    - max_retries: maximum number of retries before surfacing the error.
    - delay_s: fixed delay (seconds) or a function (attempt, error) -> seconds.
    - should_retry: predicate on the error (an ``RxException``; any other
      exception is wrapped in one first) to decide whether to retry. Without
      it every error is retried (0.1: only ``RxException`` was).
    - scheduler: where a delayed retry is scheduled (default
      ``TimeoutScheduler``); 0.1 slept on the thread that delivered the
      error. A retry without delay subscribes again at once.
    - reacting: report an operator element (``label``, kind
      ``rxplus.operator``) with one ``attempt`` span per subscription; a
      failed attempt ends with ``error`` and the class of the underlying
      exception. ``None`` installs nothing.
    """
    # The subscribe function below has its own `scheduler` parameter.
    retry_scheduler = scheduler
    # The operator element exists while at least one subscription is active
    # and is removed (with its spans) when the last one ends.
    report_lock = threading.Lock()
    active = [0]
    report: list[ComponentReport | None] = [None]

    def _acquire() -> "Element | None":
        if reacting is None:
            return None
        from .reacting import _component

        with report_lock:
            current = report[0]
            if current is None:
                current = report[0] = _component.ComponentReport(
                    reacting, label, "rxplus.operator"
                )
            active[0] += 1
            return current.element

    def _release() -> None:
        if reacting is None:
            return
        with report_lock:
            active[0] -= 1
            gone = report[0] if active[0] == 0 else None
            if gone is not None:
                report[0] = None
        if gone is not None:
            gone.remove()

    def _end(span: "ReactingSpan | None", err: Exception | None = None) -> None:
        if span is not None:
            cause = err.exception if isinstance(err, RxException) else err
            span.end(cause)

    def _op(
        source: Observable[T],
    ) -> Observable[T | ErrorRestartSignal]:
        def _subscribe(
            observer: ObserverBase[T | ErrorRestartSignal],
            scheduler: SchedulerBase | None = None,
        ) -> DisposableBase:
            attempts = 0
            disposed = False
            element = _acquire()
            ended = [False]

            def _finish() -> None:
                # Once per subscription: completed, failed or disposed.
                with generation_lock:
                    if ended[0]:
                        return
                    ended[0] = True
                _release()

            def _done() -> None:
                _finish()
                observer.on_completed()

            def _fail(err: Exception) -> None:
                _finish()
                observer.on_error(err)

            # Incremented by every (re)subscription. A retry can start inside
            # source.subscribe() of the previous attempt (synchronous error);
            # the generation tells the outer call that it is no longer current.
            generation = 0
            # Guards `generation` and the store into `sd`: a retry may run on
            # another thread (asynchronous on_error) while the previous
            # attempt's subscribe() is returning, and check-then-store must
            # be atomic or the stale subscription can replace the live one.
            generation_lock = threading.Lock()
            # The live attempt's subscription. Stored and swapped under the
            # lock, but always disposed outside it: a dispose may join the
            # thread that is delivering an error, which may need the lock.
            live: list[DisposableBase | None] = [None]
            # The live attempt's span (reacting=): ended ok on dispose.
            attempt_span: list[ReactingSpan | None] = [None]
            # A retry waiting on the scheduler: cancelled on dispose.
            waiting: list[DisposableBase | None] = [None]

            def _delay(attempt: int, err: RxException) -> float:
                try:
                    d = delay_s(attempt, err) if callable(delay_s) else float(delay_s)
                except Exception:
                    return 0.0
                return d if d > 0 else 0.0

            # Trampoline: a retry without delay that is requested while an
            # attempt is subscribing on this thread (the source failed
            # synchronously) runs after that attempt returns, in a loop, not
            # by recursion (review of #31: ~120 retries ended in a bare
            # RecursionError).
            trampoline = threading.local()

            def _run() -> None:
                if getattr(trampoline, "active", False):
                    trampoline.pending = True
                    return
                trampoline.active = True
                try:
                    trampoline.pending = True
                    while trampoline.pending and not disposed:
                        trampoline.pending = False
                        _subscribe_once()
                finally:
                    trampoline.active = False

            def _retry(delay: float) -> None:
                if delay <= 0:
                    _run()
                    return
                sch = retry_scheduler or TimeoutScheduler()
                waiting[0] = sch.schedule_relative(delay, lambda _sch, _state: _run())
                if disposed:  # disposed while scheduling
                    _quiet_dispose(waiting[0])

            def _subscribe_once() -> None:
                nonlocal generation
                if disposed:
                    return
                with generation_lock:
                    generation += 1
                    my_generation = generation
                span = None if element is None else element.span("attempt")
                attempt_span[0] = span

                def _on_next(v: T) -> None:
                    observer.on_next(v)

                def _on_completed() -> None:
                    _end(span)
                    _done()

                def _on_error(err: Exception) -> None:
                    _end(span, err)
                    if not isinstance(err, RxException):
                        err = RxException(
                            err,
                            source="retry_with_signal",
                            note="Non-RxException in retry_with_signal",
                        )

                    nonlocal attempts
                    if should_retry is not None and not should_retry(err):
                        _fail(err)
                        return

                    if max_retries is not None and attempts >= max_retries:
                        _fail(err)
                        return

                    attempts += 1

                    try:
                        observer.on_next(
                            ErrorRestartSignal(error=err, attempts=attempts)
                        )
                    except Exception as e:
                        _fail(
                            RxException(
                                e,
                                source="retry_with_signal",
                                note="Error emitting restart signal",
                            )
                        )
                        return

                    if not disposed:
                        _retry(_delay(attempts, err))

                subscription = source.subscribe(
                    on_next=_on_next,
                    on_error=_on_error,
                    on_completed=_on_completed,
                    scheduler=scheduler,
                )
                previous: DisposableBase | None = None
                with generation_lock:
                    # Stale: a retry took over while this attempt was
                    # subscribing, or the subscriber disposed meanwhile.
                    stale = disposed or my_generation != generation
                    if not stale:
                        previous, live[0] = live[0], subscription
                if stale:
                    _quiet_dispose(subscription)
                elif previous is not None:
                    _quiet_dispose(previous)

            _run()

            def _dispose() -> None:
                nonlocal disposed
                with generation_lock:
                    disposed = True
                    current, live[0] = live[0], None
                _quiet_dispose(waiting[0])
                _quiet_dispose(current)
                _end(attempt_span[0])
                _finish()

            return Disposable(_dispose)

        return create(_subscribe)

    return _op
