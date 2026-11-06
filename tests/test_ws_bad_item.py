"""A value that cannot be packaged must not take a WS component down."""

import socket
import time
from collections.abc import Callable

from rxplus import TaggedData
from rxplus.ws import RxWSClient, RxWSServer, WSConnectionConfig


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


def _unpicklable() -> Callable[[], None]:
    return lambda: None


def test_server_survives_unpicklable_value():
    port = _free_port()
    server = RxWSServer(
        WSConnectionConfig("127.0.0.1", port), datatype="object", allow_pickle=True
    )
    errors: list[Exception] = []
    server.subscribe(on_error=errors.append)
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", port, "/a"),
        datatype="object",
        allow_pickle=True,
    )
    received: list[object] = []
    client.subscribe(on_next=received.append)
    try:
        assert _wait_for(lambda: len(server._get_path_channels("/a").queues) == 1)
        server.on_next(TaggedData("/a", _unpicklable()))
        server.on_next(TaggedData("/a", {"ok": 1}))
        assert _wait_for(lambda: received == [{"ok": 1}])
        assert errors == []
    finally:
        client.on_completed()
        server.on_completed()


def test_client_survives_unpicklable_value():
    port = _free_port()
    server = RxWSServer(
        WSConnectionConfig("127.0.0.1", port), datatype="object", allow_pickle=True
    )
    received: list[object] = []
    server.subscribe(on_next=received.append)
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", port, "/a"),
        datatype="object",
        allow_pickle=True,
    )
    errors: list[Exception] = []
    client.subscribe(on_error=errors.append)
    try:
        assert _wait_for(lambda: client._connected)
        client.on_next(_unpicklable())
        client.on_next({"ok": 2})
        assert _wait_for(lambda: len(received) == 1)
        assert isinstance(received[0], TaggedData)
        assert received[0].data == {"ok": 2}
        assert errors == []
    finally:
        client.on_completed()
        server.on_completed()
