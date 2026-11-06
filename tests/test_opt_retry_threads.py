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
