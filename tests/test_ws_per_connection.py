"""RxWSServer per-connection API (0.2, for GatewayNode): received, send_to,
close_connection."""

import socket
import time
from collections.abc import Callable

from rxplus import TaggedData
from rxplus.ws import (
    RxWSClient,
    RxWSServer,
    WSConnectionConfig,
    WSConnectionEvent,
    WSReceived,
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


def test_received_send_to_and_close_connection():
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port))
    opened: list[WSConnectionEvent] = []
    closed: list[WSConnectionEvent] = []
    server.connections.subscribe(
        lambda e: (opened if e.kind == "opened" else closed).append(e)
    )
    received: list[WSReceived] = []
    server.received.subscribe(received.append)
    plain: list[object] = []
    server.subscribe(plain.append)
    a = RxWSClient(WSConnectionConfig("127.0.0.1", port, "/same"))
    b = RxWSClient(WSConnectionConfig("127.0.0.1", port, "/same"))
    got_a: list[object] = []
    got_b: list[object] = []
    a.subscribe(got_a.append)
    b.subscribe(got_b.append)
    try:
        assert _wait_for(lambda: len(opened) == 2 and a._connected and b._connected)
        a.on_next("from a")
        assert _wait_for(lambda: len(received) == 1)
        sender = received[0].connection_id
        assert received[0] == WSReceived(sender, "/same", "from a")
        assert isinstance(plain[0], TaggedData) and plain[0].data == "from a"
        other = next(e.connection_id for e in opened if e.connection_id != sender)

        # Two clients on one path: send_to reaches exactly one of them.
        assert server.send_to(sender, "only a")
        assert _wait_for(lambda: got_a == ["only a"])
        time.sleep(0.2)
        assert got_b == []
        assert server.send_to(other, "only b")
        assert _wait_for(lambda: got_b == ["only b"])

        assert not server.send_to(10_000, "nobody")
        assert server.close_connection(sender)
        assert _wait_for(lambda: [e.connection_id for e in closed] == [sender])
        assert not server.send_to(sender, "gone")
    finally:
        a.on_completed()
        b.on_completed()
        server.on_completed()
