from typing import Optional
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