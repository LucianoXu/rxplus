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
