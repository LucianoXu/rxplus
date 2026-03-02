"""OTel tracing operators for reactive pipelines.

Provides two Rx operator factories that integrate OpenTelemetry spans into
observable pipelines:

- ``traced_operator``: Wraps the entire subscription lifetime in a single span.
  Suitable for finite observables (data loaders, batch pipelines).
- ``traced_batch_operator``: Creates one child span per ``on_next`` emission.
  Suitable for infinite/long-running streams where a subscription-lifetime span
  would never end (e.g. WebSocket listeners, real-time streaming).

Both operators are no-ops when ``tracer`` is ``None``, preserving backward
compatibility with code paths where telemetry is not configured.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from opentelemetry import context, trace
from opentelemetry.trace import StatusCode, Tracer
from reactivex import Observable, create
from reactivex.abc import ObserverBase, SchedulerBase
from reactivex.disposable import CompositeDisposable, Disposable

T = TypeVar("T")


def traced_operator(
    tracer: Tracer | None,
    span_name: str,
    **span_attrs: str | int | float | bool,
) -> Callable[[Observable[T]], Observable[T]]:
    """Rx operator wrapping a finite subscription lifetime in an OTel span.

    Suitable for finite observables (data loaders, batch pipelines).
    For infinite/long-running streams, use traced_batch_operator() instead.

    Thread safety: OTel context is captured at subscribe time. Each callback
    (on_next, on_completed, on_error) calls context.attach(ctx)/context.detach()
    explicitly, because RxPY schedulers may fire callbacks on different threads
    (e.g. NewThreadScheduler, ThreadPoolScheduler).

    Disposal safety: Returns a CompositeDisposable that ends the span on
    disposal to prevent span leaks when subscriptions are disposed before
    on_completed or on_error fires.

    Args:
        tracer: OTel Tracer (API interface), or None for no-op passthrough.
        span_name: Name for the created span.
        **span_attrs: Optional initial span attributes (str/int/float/bool values).

    Returns:
        Rx operator function (Observable -> Observable).
    """

    def _op(source: Observable[T]) -> Observable[T]:
        if tracer is None:
            return source

        def _subscribe(
            observer: ObserverBase[T],
            scheduler: SchedulerBase | None = None,
        ) -> CompositeDisposable:
            span = tracer.start_span(span_name, attributes=span_attrs or None)
            ctx = trace.set_span_in_context(span)
            item_count = 0
            span_ended = False

            def _end_span(
                status: StatusCode, description: str | None = None
            ) -> None:
                nonlocal span_ended
                # Guard against double-end: on_completed/on_error AND disposal
                # may both try to end the span in race conditions.
                if span_ended:
                    return
                span_ended = True
                span.set_attribute("items.emitted", item_count)
                span.set_status(status, description)
                span.end()

            def on_next(value: T) -> None:
                nonlocal item_count
                # Attach context per callback: thread affinity is not guaranteed
                # across RxPY scheduler boundaries.
                token = context.attach(ctx)
                try:
                    item_count += 1
                    observer.on_next(value)
                finally:
                    context.detach(token)

            def on_error(err: Exception) -> None:
                token = context.attach(ctx)
                try:
                    span.record_exception(err)
                    _end_span(StatusCode.ERROR, str(err))
                    observer.on_error(err)
                finally:
                    context.detach(token)

            def on_completed() -> None:
                token = context.attach(ctx)
                try:
                    _end_span(StatusCode.OK)
                    observer.on_completed()
                finally:
                    context.detach(token)

            def on_dispose() -> None:
                # End span on disposal to prevent leaks when the subscription
                # is cancelled before on_completed/on_error fires.
                _end_span(StatusCode.OK, "disposed")

            sub = source.subscribe(
                on_next=on_next,
                on_error=on_error,
                on_completed=on_completed,
                scheduler=scheduler,
            )
            return CompositeDisposable(sub, Disposable(on_dispose))

        return create(_subscribe)

    return _op


def traced_batch_operator(
    tracer: Tracer | None,
    span_name: str,
    **span_attrs: str | int | float | bool,
) -> Callable[[Observable[T]], Observable[T]]:
    """Rx operator that creates a new span per on_next emission.

    For infinite/long-running streams where a single subscription-scoped
    span would never end (e.g. WebSocket listeners, real-time candlestick
    streaming). Each emission gets a short-lived child span with an
    auto-incrementing emission_index attribute.

    Args:
        tracer: OTel Tracer (API interface), or None for no-op passthrough.
        span_name: Name for each per-emission span.
        **span_attrs: Optional span attributes applied to every emission span.

    Returns:
        Rx operator function (Observable -> Observable).
    """

    def _op(source: Observable[T]) -> Observable[T]:
        if tracer is None:
            return source

        def _subscribe(
            observer: ObserverBase[T],
            scheduler: SchedulerBase | None = None,
        ) -> Disposable:
            emission_index = 0

            def on_next(value: T) -> None:
                nonlocal emission_index
                # emission_index relies on the Rx serialization contract: on_next
                # calls for a given observer are never concurrent (per Rx grammar),
                # so no lock is needed here.
                attrs: dict[str, str | int | float | bool] = {
                    **span_attrs,
                    "emission_index": emission_index,
                }
                span = tracer.start_span(span_name, attributes=attrs)
                ctx = trace.set_span_in_context(span)
                token = context.attach(ctx)
                try:
                    observer.on_next(value)
                    span.set_status(StatusCode.OK)
                except Exception as e:
                    span.set_status(StatusCode.ERROR, str(e))
                    span.record_exception(e)
                    raise
                finally:
                    span.end()
                    context.detach(token)
                    emission_index += 1

            return source.subscribe(
                on_next=on_next,
                on_error=observer.on_error,
                on_completed=observer.on_completed,
                scheduler=scheduler,
            )

        return create(_subscribe)

    return _op


__all__ = ["traced_operator", "traced_batch_operator"]
