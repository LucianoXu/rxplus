"""Miscellaneous Rx operators used by ``rxplus``."""

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from attr import dataclass
from opentelemetry.trace import Span, StatusCode
from reactivex import Observable, Observer, create
from reactivex.abc import DisposableBase
from reactivex.disposable import Disposable

from .mechanism import RxException

if TYPE_CHECKING:
    from .reacting import ReportTo
    from .reacting.handles import Span as ReactingSpan


def stream_print_out(prompt: str = "Stream-Print-Out"):
    """
    Print out and forward the data stream. For debug or info output purpose.
    """

    def _stream_print_out(source):

        def subscribe(observer, scheduler=None):
            def on_next(value) -> None:
                print(f"{prompt}{value}")
                observer.on_next(value)

            def on_error(error):
                print(f"Error observed: {error}")
                observer.on_error(error)

            def on_completed():
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
        def subscribe(observer: Observer[T], scheduler=None):
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

        return Observable(subscribe)  # type: ignore[arg-type]

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


def retry_with_signal[T](
    max_retries: int | None = None,
    *,
    delay_s: float | Callable[[int, RxException], float] = 0.0,
    should_retry: Callable[[RxException], bool] | None = None,
    reacting: "ReportTo | None" = None,
    label: str = "retry_with_signal",
) -> Callable[[Observable[T]], Observable[T | ErrorRestartSignal]]:
    """
    Operator: on upstream error, emit a `ErrorRestartSignal` and retry.

    - max_retries: maximum number of retries before surfacing the error.
    - delay_s: fixed delay (seconds) or a function (attempt, error) -> seconds.
    - should_retry: predicate on the underlying Exception to decide retry.
    - reacting: report an operator element (``label``, kind
      ``rxplus.operator``) with one ``attempt`` span per subscription; a
      failed attempt ends with ``error`` and the class of the underlying
      exception. ``None`` installs nothing.
    """
    element = None
    if reacting is not None:
        from .reacting import _component

        element = _component.ComponentReport(reacting, label, "rxplus.operator").element

    def _end(span: "ReactingSpan | None", err: Exception | None = None) -> None:
        if span is not None:
            cause = err.exception if isinstance(err, RxException) else err
            span.end(cause)

    def _op(
        source: Observable[T],
    ) -> Observable[T | ErrorRestartSignal]:
        def _subscribe(
            observer: Observer[T | ErrorRestartSignal],
            scheduler=None,
        ):
            attempts = 0
            disposed = False
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

            def _delay(attempt: int, err: RxException) -> None:
                try:
                    d = delay_s(attempt, err) if callable(delay_s) else float(delay_s)
                except Exception:
                    d = 0.0
                if d and d > 0:
                    try:
                        time.sleep(d)
                    except Exception:
                        pass

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
                    observer.on_completed()

                def _on_error(err: Exception) -> None:
                    _end(span, err)
                    if not isinstance(err, RxException):
                        err = RxException(
                            err,
                            source="retry_with_signal",
                            note="Non-RxException in retry_with_signal",
                        )
                        observer.on_error(err)
                        return

                    nonlocal attempts
                    if should_retry is not None and not should_retry(err):
                        observer.on_error(err)
                        return

                    if max_retries is not None and attempts >= max_retries:
                        observer.on_error(err)
                        return

                    attempts += 1

                    try:
                        observer.on_next(
                            ErrorRestartSignal(error=err, attempts=attempts)
                        )
                    except Exception as e:
                        observer.on_error(
                            RxException(
                                e,
                                source="retry_with_signal",
                                note="Error emitting restart signal",
                            )
                        )
                        return

                    _delay(attempts, err)

                    if not disposed:
                        _subscribe_once()

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
                    subscription.dispose()
                elif previous is not None:
                    previous.dispose()

            _subscribe_once()

            def _dispose() -> None:
                nonlocal disposed
                with generation_lock:
                    disposed = True
                    current, live[0] = live[0], None
                if current is not None:
                    try:
                        current.dispose()
                    except Exception:
                        pass
                _end(attempt_span[0])

            return Disposable(_dispose)

        return create(_subscribe)  # type: ignore[arg-type]

    return _op
