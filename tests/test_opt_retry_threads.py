"""retry_with_signal when the retry runs on another thread."""

import threading
import time

import pytest
import reactivex as rx
from reactivex import operators as ops
from reactivex.abc import DisposableBase
from reactivex.disposable import Disposable, SerialDisposable
from reactivex.scheduler import NewThreadScheduler

import rxplus.opt
from rxplus import ErrorRestartSignal, RxException, retry_with_signal


class _SlowFirstStore(SerialDisposable):
    """Widens the window between the generation check and the store."""

    def __init__(self) -> None:
        super().__init__()
        self._stores = 0

    def set_disposable(self, value: DisposableBase) -> None:
        self._stores += 1
        if self._stores == 1:
            time.sleep(0.3)  # the retry on the other thread runs meanwhile
        super().set_disposable(value)

    disposable = property(SerialDisposable.get_disposable, set_disposable)


def test_async_retry_during_store_keeps_live_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rxplus.opt, "SerialDisposable", _SlowFirstStore)
    calls = {"n": 0}

    def subscribe(observer, scheduler=None):
        calls["n"] += 1
        if calls["n"] == 1:
            # Fails from another thread while the outer store is in progress.
            threading.Timer(
                0.1, lambda: observer.on_error(RxException(ValueError("first")))
            ).start()
            return Disposable()
        return (
            rx.interval(0.15, scheduler=NewThreadScheduler())
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
