"""Helpers for building bidirectional Rx adapters."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from reactivex import Observable
from reactivex.disposable import CompositeDisposable
from reactivex.subject import Subject


@dataclass(frozen=True)
class Duplex[TIn, TOut]:
    """Two subjects: ``sink`` takes inbound items, ``stream`` emits outbound
    ones (0.2: generic in both directions)."""

    sink: Subject[TIn]  # inbound
    stream: Subject[TOut]  # outbound


def make_duplex[TIn, TOut](
    sink: Subject[TIn] | None = None, stream: Subject[TOut] | None = None
) -> Duplex[TIn, TOut]:
    return Duplex(
        sink if sink is not None else Subject(),
        stream if stream is not None else Subject(),
    )


type _Adapter = Duplex[Any, Any] | Subject[Any]
type _Pipeline = tuple[Callable[[Observable[Any]], Observable[Any]], ...]


def get_sink_stream(adapter: _Adapter) -> tuple[Subject[Any], Subject[Any]]:
    if isinstance(adapter, Duplex):
        return adapter.sink, adapter.stream
    return adapter, adapter


def connect_adapter(
    adapterA: _Adapter,
    adapterB: _Adapter,
    A_to_B_pipeline: _Pipeline | None = None,
    B_to_A_pipeline: _Pipeline | None = None,
) -> CompositeDisposable:
    """
    Connect two duplex adapters or subjects together.
    The data from adapterA will be sent to adapterB, and vice versa.
    If `A_to_B_pipeline` is provided, it will be applied to the stream
    of adapterA before sending to adapterB. Same for `B_to_A_pipeline`.

    Return a `CompositeDisposable` that can be used to manage the subscriptions.
    To disconnect the adapters, call `dispose()` on the returned `CompositeDisposable`.
    """

    sinkA, subjectA = get_sink_stream(adapterA)
    sinkB, subjectB = get_sink_stream(adapterB)
    streamA: Observable[Any] = subjectA
    streamB: Observable[Any] = subjectB

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
