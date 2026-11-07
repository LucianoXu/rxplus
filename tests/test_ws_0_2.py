"""WS behaviour of the 0.2 batch: codecs on the wire, limits, error semantics."""

import asyncio
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import websockets

from rxplus import TaggedData
from rxplus.ws import (
    JsonCodec,
    RetryPolicy,
    RxWSClient,
    RxWSClientGroup,
    RxWSServer,
    WSConnectionConfig,
    WSConnectionEvent,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _clients(server: RxWSServer, path: str) -> int:
    return len(server._get_path_channels(path).queues)


@dataclass
class Reading:
    sensor: str
    value: float


def test_json_codec_round_trip_between_server_and_client():
    codec = JsonCodec()
    codec.register(Reading)
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port), datatype=codec)
    got: list[object] = []
    server.subscribe(on_next=got.append)
    client = RxWSClient(WSConnectionConfig("127.0.0.1", port, "/r"), datatype=codec)
    echoed: list[object] = []
    client.subscribe(on_next=echoed.append)
    try:
        assert _wait_for(lambda: client._connected and _clients(server, "/r") == 1)
        client.on_next(Reading("t1", 21.5))
        assert _wait_for(lambda: len(got) == 1)
        assert isinstance(got[0], TaggedData) and got[0].data == Reading("t1", 21.5)
        server.on_next(TaggedData("/r", Reading("t2", -1.0)))
        assert _wait_for(lambda: echoed == [Reading("t2", -1.0)])
    finally:
        client.on_completed()
        server.on_completed()


def test_undecodable_and_unencodable_values_are_dropped():
    codec = JsonCodec()
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port), datatype=codec)
    got: list[object] = []
    errors: list[Exception] = []
    server.subscribe(on_next=got.append, on_error=errors.append)
    client = RxWSClient(WSConnectionConfig("127.0.0.1", port, "/r"), datatype="string")
    try:
        assert _wait_for(lambda: client._connected)
        client.on_next('{"$type":"os.system","fields":{}}')  # refused by the codec
        client.on_next('{"ok": 1}')
        assert _wait_for(lambda: len(got) == 1)
        assert got[0].data == {"ok": 1}
        server.on_next(TaggedData("/r", object()))  # cannot be encoded: dropped
        server.on_next("not tagged")  # type: ignore[arg-type]  # dropped
        assert errors == []
    finally:
        client.on_completed()
        server.on_completed()


def test_max_size_closes_oversized_messages():
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port), max_size=1024)
    got: list[object] = []
    server.subscribe(on_next=got.append)
    time.sleep(0.2)
    closed = threading.Event()

    async def main() -> None:
        async with websockets.connect(f"ws://127.0.0.1:{port}/big") as ws:
            await ws.send("x" * 4096)
            try:
                await asyncio.wait_for(ws.recv(), 3)
            except websockets.exceptions.ConnectionClosed as e:
                if e.rcvd is not None and e.rcvd.code == 1009:
                    closed.set()

    asyncio.run(main())
    server.on_completed()
    assert closed.is_set() and got == []


def _stalled_client(port: int, path: str, seconds: float) -> threading.Event:
    """A raw client that connects and never reads."""
    ready = threading.Event()

    async def main() -> None:
        async with websockets.connect(f"ws://127.0.0.1:{port}{path}", max_queue=1):
            ready.set()
            await asyncio.sleep(seconds)

    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
    assert ready.wait(5)
    return ready


def test_send_queue_is_bounded_for_a_client_that_does_not_read():
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port), send_queue=100)
    time.sleep(0.2)
    _stalled_client(port, "/slow", 4)
    assert _wait_for(lambda: _clients(server, "/slow") == 1)
    for _ in range(200_000):
        server.on_next(TaggedData("/slow", "x" * 1024))
    time.sleep(0.5)
    (client,) = server._get_path_channels("/slow").get_queues_snapshot()
    queue = client.queue  # type: ignore[attr-defined]
    assert len(queue) <= 100 and queue.dropped > 0
    server.on_completed()


def test_disconnect_policy_closes_the_slow_client():
    port = _free_port()
    server = RxWSServer(
        WSConnectionConfig("127.0.0.1", port), send_queue=10, overflow="disconnect"
    )
    events: list[WSConnectionEvent] = []
    server.connections.subscribe(on_next=events.append)
    time.sleep(0.2)
    _stalled_client(port, "/slow", 4)
    assert _wait_for(lambda: _clients(server, "/slow") == 1)
    for _ in range(200_000):
        server.on_next(TaggedData("/slow", "x" * 1024))
        if _clients(server, "/slow") == 0:
            break
    assert _wait_for(lambda: _clients(server, "/slow") == 0)
    server.on_completed()
    assert [e.kind for e in events] == ["opened", "closed"]
    assert events[0].connection_id == events[1].connection_id
    assert events[0].path == "/slow"


def test_a_raising_subscriber_does_not_end_the_server():
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port))
    seen: list[object] = []

    def flaky(value: object) -> None:
        seen.append(value)
        if len(seen) == 1:
            raise RuntimeError("subscriber bug")

    server.subscribe(on_next=flaky)
    client = RxWSClient(WSConnectionConfig("127.0.0.1", port, "/a"))
    try:
        assert _wait_for(lambda: client._connected)
        client.on_next("one")
        client.on_next("two")
        assert _wait_for(lambda: len(seen) == 2)
    finally:
        client.on_completed()
        server.on_completed()


def test_upstream_on_error_shuts_down_and_forwards_the_error():
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port))
    client = RxWSClient(WSConnectionConfig("127.0.0.1", port, "/a"))
    server_errors: list[Exception] = []
    client_errors: list[Exception] = []
    server.subscribe(on_error=server_errors.append)
    client.subscribe(on_error=client_errors.append)
    assert _wait_for(lambda: client._connected)
    boom = ValueError("upstream failed")
    client.on_error(boom)
    server.on_error(boom)
    assert client_errors == [boom] and server_errors == [boom]
    assert client._thread is not None and server._thread is not None
    assert _wait_for(lambda: not client._thread.is_alive())
    assert _wait_for(lambda: not server._thread.is_alive())


def test_group_survives_a_failing_child():
    group = RxWSClientGroup(
        WSConnectionConfig("127.0.0.1", _free_port()),  # nothing listens
        retry_policy=RetryPolicy(max_retries=1, base_delay=0.05, jitter=0.0),
    )
    failures: list[tuple[str, Exception]] = []
    group.child_errors.subscribe(on_next=failures.append)
    group_errors: list[Exception] = []
    group.subscribe(on_error=group_errors.append)
    group.open_path("/a")
    assert _wait_for(lambda: len(failures) == 1)
    assert failures[0][0] == "/a" and "/a" not in group._clients
    assert group_errors == []
    group.open_path("/a")  # a fresh child for the same path
    assert "/a" in group._clients
    group.on_completed()


def test_shutdown_with_a_stalled_client_leaves_no_pending_task():
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port), send_queue=10)
    time.sleep(0.2)
    _stalled_client(port, "/slow", 8)
    assert _wait_for(lambda: _clients(server, "/slow") == 1)
    for _ in range(50_000):
        server.on_next(TaggedData("/slow", "x" * 1024))
    time.sleep(0.3)
    server.on_completed()
    loop = server._loop
    assert loop is not None and server._thread is not None
    assert _wait_for(lambda: not server._thread.is_alive())
    # Tasks still pending here would be destroyed with the closed loop.
    assert [t for t in asyncio.all_tasks(loop) if not t.done()] == []


# --- review of #24 -----------------------------------------------------------


def test_a_raising_connections_subscriber_does_not_kill_the_connection():
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port))

    def boom(_event: WSConnectionEvent) -> None:
        raise RuntimeError("subscriber bug")

    server.connections.subscribe(on_next=boom)
    got: list[object] = []
    server.subscribe(on_next=got.append)
    client = RxWSClient(WSConnectionConfig("127.0.0.1", port, "/a"))
    try:
        assert _wait_for(lambda: client._connected)
        client.on_next("hello")
        client.on_next("again")
        assert _wait_for(lambda: [g.data for g in got] == ["hello", "again"])
    finally:
        client.on_completed()
        server.on_completed()


def test_pickle_codecs_are_refused_however_supplied():
    import pytest

    from rxplus.ws import PickleNotAllowedError, WSObject, wsdt_factory

    cfg = WSConnectionConfig("127.0.0.1", _free_port())
    with pytest.raises(PickleNotAllowedError):
        RxWSServer(cfg, datatype=WSObject())
    with pytest.raises(PickleNotAllowedError):
        RxWSClient(cfg, datatype=wsdt_factory("object"))
    group = RxWSClientGroup(cfg, datatype=lambda p: wsdt_factory("object"))
    group.on_next(TaggedData("/x", 1))  # refused path: logged, not raised
    assert group.dropped == 1
    group.on_completed()


def test_group_drops_values_that_are_not_tagged():
    group = RxWSClientGroup(WSConnectionConfig("127.0.0.1", _free_port()))
    group.on_next("not tagged")  # type: ignore[arg-type]
    assert group.dropped == 1 and group._clients == {}
    group.on_completed()
    group.on_next(TaggedData("/after", 1))  # ignored after the end
    assert group._clients == {}


def test_server_counts_dropped_items():
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port))
    server.on_next("not tagged")  # type: ignore[arg-type]
    server.on_next(TaggedData("/a", 123))  # "string" codec: not a str
    assert server.dropped == 2
    server.on_completed()


def test_disconnect_overflow_logs_once_per_connection():
    port = _free_port()
    server = RxWSServer(
        WSConnectionConfig("127.0.0.1", port), send_queue=16, overflow="disconnect"
    )
    warnings: list[str] = []
    real_log = server._log
    server._log = lambda body, level="INFO": (  # type: ignore[method-assign]
        warnings.append(body) if level == "WARN" else None,
        real_log(body, level),
    )
    time.sleep(0.2)
    _stalled_client(port, "/slow", 6)
    assert _wait_for(lambda: _clients(server, "/slow") == 1)
    for _ in range(3000):
        server.on_next(TaggedData("/slow", "x" * 65536))
    assert _wait_for(lambda: _clients(server, "/slow") == 0)
    server.on_completed()
    assert sum("overflowed" in w for w in warnings) == 1
    assert server.dropped > 0


def test_offline_buffer_drops_oldest_even_with_disconnect_policy():
    port = _free_port()
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", port, "/b"),
        buffer_while_disconnected=True,
        send_queue=4,
        overflow="disconnect",
        retry_policy=RetryPolicy(base_delay=0.2, max_delay=0.2, jitter=0.0),
    )
    time.sleep(0.3)
    for i in range(10):
        client.on_next(f"m{i}")  # offline: the buffer keeps the newest four
    time.sleep(0.2)
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port))
    received: list[object] = []
    events: list[str] = []
    server.subscribe(on_next=lambda td: received.append(td.data))
    server.connections.subscribe(on_next=lambda e: events.append(e.kind))
    try:
        assert _wait_for(lambda: len(received) == 4, timeout=5.0)
        client.on_next("after")
        assert _wait_for(lambda: len(received) == 5)
        assert received == ["m6", "m7", "m8", "m9", "after"]
        assert events == ["opened"]  # the reconnection was not dropped
        assert client.dropped == 6
    finally:
        client.on_completed()
        server.on_completed()


def test_send_queue_byte_bound():
    from rxplus.ws._sendqueue import SendQueue

    queue = SendQueue(None, "drop_old", limit_bytes=10)
    for frame in (b"aaaa", b"bbbb", b"cccc"):  # 12 bytes > 10
        queue.put(frame)
    assert len(queue) == 2 and queue.dropped == 1
    assert queue.put(b"x" * 50)  # larger than the bound alone: kept, others go
    assert len(queue) == 1


def test_reconnection_after_a_disconnect_overflow_starts_clean():
    # Review of #24 (item 9): the sender ended on ConnectionClosed while
    # blocked in send(), the overflow flag survived and dropped the next
    # connection right after it opened.
    from rxplus.ws._sendqueue import SendQueue

    port = _free_port()
    connections: list[int] = []
    received: list[tuple[int, bytes]] = []
    ready = threading.Event()

    async def handler(ws) -> None:
        n = len(connections) + 1
        connections.append(n)
        if n == 1:
            await asyncio.sleep(1000)  # stalls: never reads
        async for message in ws:
            received.append((n, message))

    async def main() -> None:
        async with websockets.serve(
            handler, "127.0.0.1", port, max_queue=1, max_size=None
        ):
            ready.set()
            await asyncio.Future()

    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
    assert ready.wait(5)
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", port, "/c"),
        datatype="bytes",
        send_queue=4,
        overflow="disconnect",
        retry_policy=RetryPolicy(base_delay=0.2, max_delay=0.2, jitter=0.0),
        ping_interval=None,
    )
    try:
        assert _wait_for(lambda: client._connected)
        for _ in range(300):
            client.on_next(b"x" * 262144)
        assert _wait_for(lambda: len(connections) >= 2, timeout=5.0)
        assert _wait_for(lambda: client._connected)
        time.sleep(0.6)  # a stale overflow flag dropped it within ~0.2 s
        for k in range(3):
            client.on_next(b"after%d" % k)
        assert _wait_for(lambda: len([r for r in received if r[0] >= 2]) >= 3)
        assert len(connections) == 2
        assert client.dropped > 0
    finally:
        client.on_completed()
    assert isinstance(SendQueue(1, "drop_old").reset(), int)


def test_values_sent_while_offline_without_buffering_are_counted_as_dropped():
    """0.2 review: with buffer_while_disconnected=False they were dropped
    silently, so the client's totals came up short."""
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", _free_port(), "/x"),  # nobody listens
        retry_policy=RetryPolicy(base_delay=10.0),
    )
    try:
        for i in range(5):
            client.on_next(str(i))
        assert client.dropped == 5
    finally:
        client.on_completed()
