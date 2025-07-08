from typing import Optional
import reactivex as rx
from reactivex.disposable import CompositeDisposable
from reactivex.subject import Subject
from reactivex import Observable, Observer
from dataclasses import dataclass

@dataclass(frozen=True)
class Duplex:
    sink:   Subject      # inbound
    stream: Subject      # outbound

def make_duplex(sink: Optional[Subject] = None, stream: Optional[Subject] = None) -> Duplex:
    if sink is None:
        sink = Subject()               # inbound
    if stream is None:
        stream = Subject()             # outbound

    return Duplex(sink, stream)

def get_sink_stream(adapter: Duplex|Subject):
    if isinstance(adapter, Duplex):
        return adapter.sink, adapter.stream
    else:
        return adapter, adapter


def connect_adapter(
    adapterA: Duplex|Subject,
    adapterB: Duplex|Subject,
    ) -> CompositeDisposable:
    """
    Connect two duplex adapters or subjects together.

    Return a `CompositeDisposable` that can be used to manage the subscriptions. 
    To disconnect the adapters, call `dispose()` on the returned `CompositeDisposable`.
    """

    sinkA, streamA = get_sink_stream(adapterA)
    sinkB, streamB = get_sink_stream(adapterB)

    # Create a composite disposable to manage the subscriptions
    cd = CompositeDisposable()

    # connect the sink of A to the stream of B
    cd.add(
        streamA.subscribe(
            on_next=sinkB.on_next,
            on_error=sinkB.on_error,
            on_completed=sinkB.on_completed
        )
    )

    # connect the sink of B to the stream of A
    cd.add(
        streamB.subscribe(
            on_next=sinkA.on_next,
            on_error=sinkA.on_error,
            on_completed=sinkA.on_completed
        )
    )

    return cd