import io
from contextlib import redirect_stdout

import reactivex as rx

from rxplus.opt import redirect_to, stream_print_out


class Collector:
    def __init__(self):
        self.items = []

    def on_next(self, value):
        self.items.append(value)

    def on_error(self, error):
        raise error

    def on_completed(self):
        pass


def test_stream_print_out(capsys):
    out = io.StringIO()
    items = []
    with redirect_stdout(out):
        rx.from_([1, 2]).pipe(stream_print_out("> ")).subscribe(items.append)
    stdout = out.getvalue()
    assert items == [1, 2]
    assert ">1" in stdout.replace(" ", "")
    assert ">2" in stdout.replace(" ", "")


def test_redirect_to():
    target = Collector()
    received = []
    rx.from_([1, 2, 3, 4]).pipe(redirect_to(lambda x: x % 2 == 0, target)).subscribe(
        received.append
    )

    assert received == [1, 3]
    assert target.items == [2, 4]


def test_retry_with_signal_keeps_async_retry_after_sync_error():
    """A first attempt that errors synchronously must not dispose the retry."""
    import threading

    import reactivex as rx
    from reactivex import operators as ops
    from reactivex.disposable import Disposable
    from reactivex.scheduler import NewThreadScheduler

    from rxplus import ErrorRestartSignal, RxException, retry_with_signal

    calls = {"n": 0}

    def subscribe(observer, scheduler=None):
        calls["n"] += 1
        if calls["n"] == 1:
            observer.on_error(RxException(ValueError("first attempt fails")))
            return Disposable()
        return (
            rx.interval(0.02, scheduler=NewThreadScheduler())
            .pipe(ops.take(3))
            .subscribe(observer)
        )

    got: list[object] = []
    done = threading.Event()
    rx.create(subscribe).pipe(retry_with_signal(max_retries=3)).subscribe(
        on_next=got.append, on_completed=done.set, on_error=lambda e: done.set()
    )
    assert done.wait(2.0), "retry subscription was disposed; stream never ended"
    assert [g for g in got if not isinstance(g, ErrorRestartSignal)] == [0, 1, 2]
    assert sum(isinstance(g, ErrorRestartSignal) for g in got) == 1


def test_retry_with_signal_dispose_stops_retry():
    import reactivex as rx

    from rxplus import RxException, retry_with_signal

    subj: rx.subject.Subject[int] = rx.subject.Subject()
    got: list[object] = []
    d = subj.pipe(retry_with_signal(max_retries=5)).subscribe(on_next=got.append)
    subj.on_next(1)
    d.dispose()
    subj.on_next(2)
    assert got == [1]
    subj.on_error(RxException(ValueError("x")))
    assert got == [1]
