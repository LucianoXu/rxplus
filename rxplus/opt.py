"""Miscellaneous Rx operators used by ``rxplus``."""

import time
from collections.abc import Callable
from typing import Any

from attr import dataclass
from opentelemetry.trace import Span, StatusCode
from reactivex import Observable, Observer, create
from reactivex.disposable import Disposable, SerialDisposable

from .mechanism import RxException


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


def redirect_to(cond: Callable[[Any], bool], redirect_target: Observer | Callable):
    """
    Redirect items to the specified observer (or function),
    and forward other items.
    """

    def _redirect_to(source):
        def subscribe(observer, scheduler=None):

            # determine the redirection function
            if callable(redirect_target):
                redirect_fun = redirect_target
            else:
                redirect_fun = redirect_target.on_next

            def on_next(value: Any) -> None:
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


def retry_with_signal(
    max_retries: int | None = None,
    *,
    delay_s: float | Callable[[int, RxException], float] = 0.0,
    should_retry: Callable[[RxException], bool] | None = None,
):
    """
    Operator: on upstream error, emit a `ErrorRestartSignal` and retry.

    - max_retries: maximum number of retries before surfacing the error.
    - delay_s: fixed delay (seconds) or a function (attempt, error) -> seconds.
    - should_retry: predicate on the underlying Exception to decide retry.
    """

    def _op(source: Observable[Any]) -> Observable[Any]:
        def _subscribe(observer, scheduler=None):
            attempts = 0
            disposed = False
            sd = SerialDisposable()

            def _delay(attempt: int, err: RxException):
                try:
                    d = delay_s(attempt, err) if callable(delay_s) else float(delay_s)
                except Exception:
                    d = 0.0
                if d and d > 0:
                    try:
                        time.sleep(d)
                    except Exception:
                        pass

            def _subscribe_once():
                if disposed:
                    return

                def _on_next(v: Any):
                    observer.on_next(v)

                def _on_error(err: Exception):
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

                sd.disposable = source.subscribe(
                    on_next=_on_next,
                    on_error=_on_error,
                    on_completed=observer.on_completed,
                    scheduler=scheduler,
                )

            _subscribe_once()

            def _dispose():
                nonlocal disposed
                disposed = True
                try:
                    if sd.disposable is not None:
                        sd.disposable.dispose()
                except Exception:
                    pass

            return Disposable(_dispose)

        return create(_subscribe)

    return _op
