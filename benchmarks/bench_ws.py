"""Baseline benchmarks for rxplus hot paths (audit 2026-11-06).

Run: python benchmarks/bench_ws.py [--n N] [--repeat R]

One line per benchmark: median throughput, median cost per item and, for the
WS and gateway paths, the time the producing thread spends per on_next.
"""

import argparse
import socket
import statistics
import threading
import time
from collections.abc import Callable

from reactivex import Observable
from reactivex import operators as ops
from reactivex.subject import Subject

from rxplus import TaggedData
from rxplus.gateway import GatewayNode
from rxplus.telemetry import configure_telemetry
from rxplus.ws import RxWSClient, RxWSServer, WSConnectionConfig
from rxplus.ws.datatypes import WSDatatypeName

PAYLOAD = "x" * 16
Pipeline = tuple[Callable[[Observable[int]], Observable[int]], ...]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(cond: Callable[[], bool], timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.005)
    raise TimeoutError("condition not reached")


class _Counter:
    """Counts items and signals when ``n`` have arrived."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.count = 0
        self.done = threading.Event()

    def __call__(self, _item: object) -> None:
        self.count += 1
        if self.count == self.n:
            self.done.set()


def _report(
    name: str, n: int, secs: list[float], caller: list[float] | None = None
) -> None:
    med = statistics.median(secs)
    line = f"{name:<44} {n / med:>12,.0f} items/s  {med / n * 1e6:>8.2f} us/item"
    if caller:
        line += f"  caller {statistics.median(caller) / n * 1e6:.2f} us/item"
    print(line, flush=True)


def _rx_once(n: int, pipeline: Pipeline) -> float:
    subject: Subject[int] = Subject()
    counter = _Counter(n)
    subject.pipe(*pipeline).subscribe(counter)
    start = time.perf_counter()
    for i in range(n):
        subject.on_next(i)
    elapsed = time.perf_counter() - start
    assert counter.count == n
    return elapsed


def bench_rx(n: int, repeat: int) -> None:
    cases: list[tuple[str, Pipeline]] = [
        ("rx Subject -> subscriber", ()),
        (
            "rx Subject -> map -> filter -> subscriber",
            (ops.map(lambda x: x), ops.filter(lambda x: True)),
        ),
        (
            "rx Subject -> do_action(noop) -> subscriber",
            (ops.do_action(lambda x: None),),
        ),
    ]
    for label, pipeline in cases:
        _report(label, n, [_rx_once(n, pipeline) for _ in range(repeat)])


def _ws_once(
    n: int, datatype: WSDatatypeName, server_to_client: bool
) -> tuple[float, float]:
    payload: object = PAYLOAD if datatype == "string" else {"k": PAYLOAD}
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port), datatype=datatype)
    time.sleep(0.2)
    client = RxWSClient(WSConnectionConfig("127.0.0.1", port, "/b"), datatype=datatype)
    try:
        _wait_for(lambda: client._connected)
        _wait_for(lambda: len(server._get_path_channels("/b").queues) == 1)
        counter = _Counter(n)
        if server_to_client:
            client.subscribe(counter)
            start = time.perf_counter()
            for _ in range(n):
                server.on_next(TaggedData("/b", payload))
        else:
            server.subscribe(counter)
            start = time.perf_counter()
            for _ in range(n):
                client.on_next(payload)
        caller = time.perf_counter() - start
        assert counter.done.wait(120), f"received {counter.count}/{n}"
        return time.perf_counter() - start, caller
    finally:
        client.on_completed()
        server.on_completed()


def bench_ws(n: int, repeat: int, datatype: WSDatatypeName) -> None:
    for server_to_client, label in (
        (True, "server->client"),
        (False, "client->server"),
    ):
        runs = [_ws_once(n, datatype, server_to_client) for _ in range(repeat)]
        _report(
            f"ws {datatype} {label} (16 B)",
            n,
            [r[0] for r in runs],
            [r[1] for r in runs],
        )


class Frame:
    """Minimal framing: one type byte followed by the payload."""

    def __init__(self, kind: int, payload: bytes) -> None:
        self.kind = kind
        self.payload = payload

    @classmethod
    def encode(cls, obj: "Frame") -> bytes:
        return bytes([obj.kind]) + obj.payload

    @classmethod
    def decode(cls, data: bytes) -> "Frame":
        return cls(data[0], data[1:])

    @classmethod
    def create_hello(cls, node_id: bytes) -> "Frame":
        return cls(1, node_id)

    @classmethod
    def create_hello_ack(cls, node_id: bytes) -> "Frame":
        return cls(2, node_id)

    def is_hello(self) -> bool:
        return self.kind == 1

    def is_hello_ack(self) -> bool:
        return self.kind == 2

    def get_hello_node_id(self) -> bytes | None:
        return self.payload if self.kind in (1, 2) else None


def _gateway_once(n: int, outbound: bool) -> tuple[float, float]:
    _, quiet = configure_telemetry()  # no exporter: silent
    a = GatewayNode("127.0.0.1", _free_port(), framing_cls=Frame, logger_provider=quiet)
    b = GatewayNode("127.0.0.1", _free_port(), framing_cls=Frame, logger_provider=quiet)
    try:
        time.sleep(0.2)
        cid = a.connect("127.0.0.1", b.port)
        _wait_for(lambda: cid in a.get_ready_connections())
        _wait_for(lambda: bool(b.get_ready_connections()))
        bcid = next(iter(b.get_ready_connections()))
        counter = _Counter(n)
        frame = Frame(9, b"x" * 16)
        sender, receiver, target = (a, b, cid) if outbound else (b, a, bcid)
        receiver.sink.subscribe(counter)
        start = time.perf_counter()
        for _ in range(n):
            sender.send_to(target, frame)
        caller = time.perf_counter() - start
        assert counter.done.wait(120), f"received {counter.count}/{n}"
        return time.perf_counter() - start, caller
    finally:
        a.close()
        b.close()


def bench_gateway(n: int, repeat: int) -> None:
    for outbound, label in ((True, "outbound"), (False, "inbound")):
        runs = [_gateway_once(n, outbound) for _ in range(repeat)]
        _report(
            f"gateway {label} send_to (17 B frame)",
            n,
            [r[0] for r in runs],
            [r[1] for r in runs],
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50_000)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    bench_rx(args.n * 4, args.repeat)
    bench_ws(args.n, args.repeat, "string")
    bench_ws(args.n, args.repeat, "object")
    bench_gateway(args.n, args.repeat)


if __name__ == "__main__":
    main()
