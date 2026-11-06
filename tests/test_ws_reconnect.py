"""Reconnection behaviour of RxWSClient against real local servers."""

import asyncio
import socket
import threading
import time
from collections.abc import Awaitable, Callable

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.http11 import Request, Response

from rxplus.ws import RetryPolicy, RxWSClient, WSConnectionConfig

Handler = Callable[[ServerConnection], Awaitable[None]]
ProcessRequest = Callable[[ServerConnection, Request], Awaitable[Response | None]]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(
    handler: Handler,
    seconds: float,
    process_request: ProcessRequest | None = None,
) -> int:
    """Run a websockets server on a background thread for ``seconds``."""
    port = _free_port()
    ready = threading.Event()

    async def main() -> None:
        async with websockets.serve(
            handler, "127.0.0.1", port, process_request=process_request
        ):
            ready.set()
            await asyncio.sleep(seconds)

    threading.Thread(
        target=lambda: asyncio.run(main()), daemon=True, name="test-ws-server"
    ).start()
    assert ready.wait(5)
    return port


def _wait_for(cond: Callable[[], bool], timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_handshake_slower_than_base_delay_still_connects():
    """The connect timeout is open_timeout, not RetryPolicy.base_delay."""

    async def slow_handshake(conn: ServerConnection, request: Request) -> None:
        await asyncio.sleep(0.6)

    async def hold(ws: ServerConnection) -> None:
        await ws.wait_closed()

    port = _serve(hold, 5.0, process_request=slow_handshake)
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", port, "/"),
        retry_policy=RetryPolicy(base_delay=0.1),
    )
    try:
        assert _wait_for(lambda: client._connected, timeout=3.0)
    finally:
        client.on_completed()


def test_accept_then_close_is_retried_with_backoff():
    """A server that closes at once must not be reconnected in a tight loop."""
    accepts: list[float] = []

    async def close_at_once(ws: ServerConnection) -> None:
        accepts.append(time.monotonic())
        await ws.close()

    port = _serve(close_at_once, 3.0)
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", port, "/"),
        retry_policy=RetryPolicy(base_delay=0.2, jitter=0.0, stable_after=1.0),
    )
    time.sleep(1.5)
    client.on_completed()
    # Attempts at about 0, 0.2, 0.6 and 1.4 s. Before the fix: thousands.
    assert 2 <= len(accepts) <= 6, len(accepts)


def test_stable_connection_reconnects_at_once():
    """A drop after a stable connection reconnects without waiting."""
    accepts: list[float] = []
    closes: list[float] = []

    async def close_later(ws: ServerConnection) -> None:
        accepts.append(time.monotonic())
        await asyncio.sleep(0.5)
        closes.append(time.monotonic())
        await ws.close()

    port = _serve(close_later, 3.0)
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", port, "/"),
        retry_policy=RetryPolicy(base_delay=2.0, jitter=0.0, stable_after=0.3),
    )
    try:
        assert _wait_for(lambda: len(accepts) >= 2, timeout=2.0)
    finally:
        client.on_completed()
    # base_delay is 2 s, so a reconnect within 0.5 s means no backoff was used.
    assert accepts[1] - closes[0] < 0.5


def test_retry_policy_stable_after_default():
    assert RetryPolicy().stable_after == 1.0
