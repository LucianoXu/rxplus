"""Helpers for building bidirectional Rx adapters."""

from dataclasses import dataclass
from typing import Callable, Optional

import reactivex as rx
from reactivex import Observable, Observer
from reactivex.disposable import CompositeDisposable
from reactivex.subject import Subject


@dataclass(frozen=True)
class Duplex:
    sink: Subject  # inbound
    stream: Subject  # outbound


def make_duplex(
    sink: Optional[Subject] = None, stream: Optional[Subject] = None
) -> Duplex:
    if sink is None:
        sink = Subject()  # inbound
    if stream is None:
        stream = Subject()  # outbound

    return Duplex(sink, stream)


def get_sink_stream(adapter: Duplex | Subject):
    if isinstance(adapter, Duplex):
        return adapter.sink, adapter.stream
    else:
        return adapter, adapter


def connect_adapter(
    adapterA: Duplex | Subject,
    adapterB: Duplex | Subject,
    A_to_B_pipeline: Optional[tuple[Callable[[Observable], Observable], ...]] = None,
    B_to_A_pipeline: Optional[tuple[Callable[[Observable], Observable], ...]] = None,
) -> CompositeDisposable:
    """
    Connect two duplex adapters or subjects together.
    The data from adapterA will be sent to adapterB, and vice versa.
    If `A_to_B_pipeline` is provided, it will be applied to the stream of adapterA before sending to adapterB. Same for `B_to_A_pipeline`.

    Return a `CompositeDisposable` that can be used to manage the subscriptions.
    To disconnect the adapters, call `dispose()` on the returned `CompositeDisposable`.
    """

    sinkA, streamA = get_sink_stream(adapterA)
    sinkB, streamB = get_sink_stream(adapterB)

    if A_to_B_pipeline is not None:
        streamA = streamA.pipe(*A_to_B_pipeline)

    if B_to_A_pipeline is not None:
        streamB = streamB.pipe(*B_to_A_pipeline)

    # Create a composite disposable to manage the subscriptions
    cd = CompositeDisposable()

    # connect the sink of A to the stream of B
    cd.add(
        streamA.subscribe(
            on_next=sinkB.on_next,
            on_error=sinkB.on_error,
            on_completed=sinkB.on_completed,
        )
    )

    # connect the sink of B to the stream of A
    cd.add(
        streamB.subscribe(
            on_next=sinkA.on_next,
            on_error=sinkA.on_error,
            on_completed=sinkA.on_completed,
        )
    )

    return cd
