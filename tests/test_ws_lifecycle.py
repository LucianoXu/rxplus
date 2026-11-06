"""Thread and loop lifecycle of the WS components on terminal paths."""

import socket
import time
from collections.abc import Callable

from rxplus import TaggedData
from rxplus.ws import (
    RetryPolicy,
    RxWSClient,
    RxWSServer,
    WSConnectionConfig,
    WSConnectionState,
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


def test_client_thread_ends_after_retries_exhausted():
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", _free_port(), "/"),  # nothing listens
        retry_policy=RetryPolicy(max_retries=1, base_delay=0.05, jitter=0.0),
    )
    errors: list[Exception] = []
    client.subscribe(on_error=errors.append)
    assert _wait_for(lambda: bool(errors))
    assert client._thread is not None
    assert _wait_for(lambda: not client._thread.is_alive(), timeout=2.0)


def test_client_after_terminal_error_drops_values_and_completes():
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", _free_port(), "/"),
        retry_policy=RetryPolicy(max_retries=1, base_delay=0.05, jitter=0.0),
        buffer_while_disconnected=True,
    )
    states: list[WSConnectionState] = []
    state_done: list[bool] = []
    client.connection_state.subscribe(
        on_next=states.append, on_completed=lambda: state_done.append(True)
    )
    errors: list[Exception] = []
    client.subscribe(on_error=errors.append)
    assert _wait_for(lambda: bool(errors))
    assert client._thread is not None
    assert _wait_for(lambda: not client._thread.is_alive(), timeout=2.0)
    client.on_next("late")  # must not raise
    client.on_completed()
    assert state_done == [True]
    assert states[-1] == WSConnectionState.CLOSED


def test_server_thread_ends_after_bind_failure():
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        server = RxWSServer(WSConnectionConfig("127.0.0.1", port))
        errors: list[Exception] = []
        server.subscribe(on_error=errors.append)
        assert _wait_for(lambda: bool(errors))
        assert server._thread is not None
        assert _wait_for(lambda: not server._thread.is_alive(), timeout=2.0)
        server.on_next(TaggedData("/", "late"))  # must not raise
        server.on_completed()
