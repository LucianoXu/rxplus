import reactivex as rx

from rxplus.logging import LogItem, log_filter, drop_log, log_redirect_to


class Collector:
    def __init__(self):
        self.items = []

    def on_next(self, value):
        self.items.append(value)

    def on_error(self, error):
        raise error

    def on_completed(self):
        pass


def test_log_filter():
    logs = [LogItem('a', 'INFO'), LogItem('b', 'DEBUG'), 'x']
    collected = []
    rx.from_(logs).pipe(log_filter({'DEBUG'})).subscribe(collected.append)

    assert len(collected) == 1
    log = collected[0]
    assert isinstance(log, LogItem)
    assert log.level == 'DEBUG'
    assert log.msg == 'b'


def test_drop_log():
    items = [LogItem('a'), 'keep']
    collected = []
    rx.from_(items).pipe(drop_log()).subscribe(collected.append)

    assert collected == ['keep']


def test_log_redirect_to():
    target = Collector()
    output = []
    source = [LogItem('x', 'INFO'), LogItem('y', 'DEBUG'), 1]
    rx.from_(source).pipe(log_redirect_to(target, {'INFO'})).subscribe(output.append)

    assert output == [1]
    assert len(target.items) == 1
    assert isinstance(target.items[0], LogItem)
    assert target.items[0].msg == 'x'
