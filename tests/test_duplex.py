import reactivex as rx
from reactivex.subject import Subject

from rxplus.duplex import make_duplex, connect_adapter


def test_make_duplex_defaults():
    d = make_duplex()
    assert isinstance(d.sink, Subject)
    assert isinstance(d.stream, Subject)


def test_connect_adapter_propagates():
    a = make_duplex()
    b = make_duplex()

    received_a = []
    received_b = []

    a.sink.subscribe(received_a.append)
    b.sink.subscribe(received_b.append)

    cd = connect_adapter(a, b)

    a.stream.on_next('first')
    b.stream.on_next('second')

    assert received_b == ['first']
    assert received_a == ['second']

    cd.dispose()
