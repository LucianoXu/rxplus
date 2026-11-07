"""retry_with_signal when the retry runs on another thread."""

import threading
import time

import reactivex as rx
from reactivex import operators as ops
from reactivex.disposable import Disposable
from reactivex.scheduler import NewThreadScheduler

from rxplus import ErrorRestartSignal, RxException, retry_with_signal


def test_async_retry_while_the_first_subscribe_returns_keeps_the_retry():
    """The first attempt fails on another thread while its subscribe() is
    still returning: the retry's subscription must stay live."""
    calls = {"n": 0}

    def subscribe(observer, scheduler=None):
        calls["n"] += 1
        if calls["n"] == 1:
            threading.Timer(
                0.05, lambda: observer.on_error(RxException(ValueError("first")))
            ).start()
            time.sleep(0.3)  # subscribe() returns after the retry took over
            return Disposable()
        return (
            rx.interval(0.05, scheduler=NewThreadScheduler())
            .pipe(ops.take(3))
            .subscribe(observer)
        )

    got: list[object] = []
    done = threading.Event()
    rx.create(subscribe).pipe(retry_with_signal(max_retries=3)).subscribe(
        on_next=got.append, on_completed=done.set, on_error=lambda e: done.set()
    )
    assert done.wait(3.0), "the live retry subscription was disposed"
    assert [g for g in got if not isinstance(g, ErrorRestartSignal)] == [0, 1, 2]


def test_dispose_during_a_retry_resubscribe_stops_the_late_subscription():
    """The subscriber disposes while the retry is inside source.subscribe():
    the subscription that subscribe() then returns must not keep running."""
    calls = {"n": 0}
    ticks: list[int] = []
    inside = threading.Event()

    def subscribe(observer, scheduler=None):
        calls["n"] += 1
        if calls["n"] == 1:
            threading.Timer(
                0.01, lambda: observer.on_error(RxException(ValueError("first")))
            ).start()
            return Disposable()
        inside.set()
        time.sleep(0.3)  # the subscriber disposes meanwhile
        return rx.interval(0.02, scheduler=NewThreadScheduler()).subscribe(
            on_next=ticks.append
        )

    sub = (
        rx.create(subscribe)
        .pipe(retry_with_signal(max_retries=3))
        .subscribe(on_next=lambda _: None)
    )
    assert inside.wait(2.0)
    sub.dispose()
    time.sleep(0.5)  # the late subscription is created, then must be disposed
    seen = len(ticks)
    time.sleep(0.3)
    assert len(ticks) == seen, "the inner source keeps ticking after dispose"


def test_a_delayed_retry_does_not_block_the_thread_that_delivered_the_error():
    """F12: 0.1 slept on the error's thread (often a WS loop or a timer)."""
    from reactivex.scheduler import HistoricalScheduler

    from rxplus import ErrorRestartSignal, RxException, retry_with_signal

    sched = HistoricalScheduler()
    tries = [0]

    def subscribe(observer, scheduler=None):
        tries[0] += 1
        if tries[0] == 1:
            observer.on_error(RxException(OSError("down"), source="test"))
        else:
            observer.on_next("up")

    out: list[object] = []
    start = time.monotonic()
    rx.create(subscribe).pipe(
        retry_with_signal(delay_s=30.0, scheduler=sched)
    ).subscribe(out.append)
    assert time.monotonic() - start < 1.0  # nothing slept
    assert tries[0] == 1 and isinstance(out[0], ErrorRestartSignal)
    sched.advance_by(30.0)
    assert tries[0] == 2 and out[-1] == "up"


def test_errors_that_are_not_rx_exceptions_are_retried():
    from rxplus import ErrorRestartSignal, RxException, retry_with_signal

    tries = [0]

    def subscribe(observer, scheduler=None):
        tries[0] += 1
        if tries[0] < 3:
            observer.on_error(ValueError("plain"))
        else:
            observer.on_next("ok")

    out: list[object] = []
    rx.create(subscribe).pipe(retry_with_signal(5)).subscribe(out.append)
    signals = [o for o in out if isinstance(o, ErrorRestartSignal)]
    assert len(signals) == 2 and out[-1] == "ok"
    assert isinstance(signals[0].error, RxException)
    assert isinstance(signals[0].error.exception, ValueError)


def test_dispose_cancels_a_retry_waiting_on_the_scheduler():
    from reactivex.scheduler import HistoricalScheduler

    from rxplus import RxException, retry_with_signal

    sched = HistoricalScheduler()
    tries = [0]

    def subscribe(observer, scheduler=None):
        tries[0] += 1
        observer.on_error(RxException(OSError("down"), source="test"))

    sub = (
        rx.create(subscribe)
        .pipe(retry_with_signal(delay_s=1.0, scheduler=sched))
        .subscribe(lambda _: None)
    )
    sub.dispose()
    sched.advance_by(5.0)
    assert tries[0] == 1


def test_a_stale_attempt_whose_teardown_raises_does_not_break_the_retry():
    """Review of #25: wrap the stale/previous dispose() calls."""
    from reactivex.disposable import Disposable

    from rxplus import RxException, retry_with_signal

    tries = [0]

    def boom() -> None:
        raise RuntimeError("teardown failed")

    def subscribe(observer, scheduler=None):
        tries[0] += 1
        if tries[0] == 1:
            observer.on_error(RxException(OSError("down"), source="test"))
            return Disposable()
        observer.on_next("up")
        return Disposable(boom)  # the live attempt's teardown raises

    out: list[object] = []
    sub = rx.create(subscribe).pipe(retry_with_signal(3)).subscribe(out.append)
    assert out[-1] == "up"
    sub.dispose()  # swallowed: never reaches the caller
