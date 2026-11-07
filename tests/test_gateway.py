"""GatewayNode handshake over real local WebSocket connections."""

import socket
import threading
import time
from collections.abc import Callable

from rxplus.gateway import GatewayConnectionState, GatewayNode
from rxplus.telemetry import configure_telemetry
from rxplus.ws import RetryPolicy


class _Frame:
    """Minimal framing: one type byte followed by the payload."""

    def __init__(self, kind: int, payload: bytes) -> None:
        self.kind = kind
        self.payload = payload

    @classmethod
    def encode(cls, obj: "_Frame") -> bytes:
        return bytes([obj.kind]) + obj.payload

    @classmethod
    def decode(cls, data: bytes) -> "_Frame":
        return cls(data[0], data[1:])

    @classmethod
    def create_hello(cls, node_id: bytes) -> "_Frame":
        return cls(1, node_id)

    @classmethod
    def create_hello_ack(cls, node_id: bytes) -> "_Frame":
        return cls(2, node_id)

    def is_hello(self) -> bool:
        return self.kind == 1

    def is_hello_ack(self) -> bool:
        return self.kind == 2

    def get_hello_node_id(self) -> bytes | None:
        return self.payload if self.kind in (1, 2) else None


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


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _node() -> GatewayNode[_Frame]:
    _, quiet = configure_telemetry()  # no exporter: silent
    node = GatewayNode(
        "127.0.0.1", _free_port(), framing_cls=_Frame, logger_provider=quiet
    )
    # The server binds asynchronously; dialling earlier is refused and the
    # client's first retry would be 2 s later.
    assert _wait_for(lambda: _listening(node.port))
    return node


def test_handshake_completes_and_frames_flow():
    a, b = _node(), _node()
    try:
        cid = a.connect("127.0.0.1", b.port)
        assert _wait_for(lambda: cid in a.get_ready_connections())
        assert _wait_for(lambda: bool(b.get_ready_connections()))
        got: list[bytes] = []
        b.sink.subscribe(lambda tagged: got.append(tagged.frame.payload))
        assert a.send_to(cid, _Frame(9, b"hi"))
        assert _wait_for(lambda: got == [b"hi"])
    finally:
        a.close()
        b.close()


def test_handshake_completes_again_after_reconnect():
    a, b = _node(), _node()
    try:
        ready_events: list[bytes] = []
        a.on_connection.subscribe(lambda conn: ready_events.append(conn.conn_id))
        cid = a.connect("127.0.0.1", b.port)
        assert _wait_for(lambda: ready_events == [cid])
        assert _wait_for(lambda: bool(b.get_ready_connections()))

        # Drop the TCP connection from B's side; A's client reconnects on the
        # same path and restarts the handshake.
        server = b._server
        loop = server._loop
        assert loop is not None
        for channels in list(server.path_channels.values()):
            for ws in list(channels.channels):
                loop.call_soon_threadsafe(lambda w=ws: loop.create_task(w.close()))
        # A becomes READY a second time once the new handshake completes.
        assert _wait_for(lambda: ready_events == [cid, cid], timeout=5.0), (
            a.get_connection(cid)
        )
        conn = a.get_connection(cid)
        assert conn is not None and conn.state == GatewayConnectionState.READY
        got: list[bytes] = []
        b.sink.subscribe(lambda tagged: got.append(tagged.frame.payload))
        assert a.send_to(cid, _Frame(9, b"again"))
        assert _wait_for(lambda: got == [b"again"])
    finally:
        a.close()
        b.close()


def test_inbound_connection_closes_when_the_peer_goes_away():
    """0.1 kept a dead inbound peer READY forever (audit F8)."""
    a, b = _node(), _node()
    try:
        disconnected: list[bytes] = []
        b.on_disconnection.subscribe(disconnected.append)
        a.connect("127.0.0.1", b.port)
        assert _wait_for(lambda: bool(b.get_ready_connections()))
        (inbound,) = b.get_ready_connections()
        a.close()
        assert _wait_for(lambda: disconnected == [inbound], timeout=5.0)
        assert inbound not in b.connections
        assert b.broadcast(_Frame(9, b"x")) == 0
    finally:
        b.close()


def test_two_peers_on_the_same_path_are_two_connections():
    """0.1 keyed inbound connections by path and merged such peers."""
    import asyncio
    import threading

    import websockets

    b = _node()
    got: list[tuple[bytes, bytes]] = []
    b.sink.subscribe(lambda tagged: got.append((tagged.conn_id, tagged.frame.payload)))
    try:

        async def peer(node_id: bytes, payload: bytes) -> None:
            async with websockets.connect(f"ws://127.0.0.1:{b.port}/same") as ws:
                await ws.send(_Frame.encode(_Frame.create_hello(node_id)))
                hello = _Frame.decode(await ws.recv())  # B answers with HELLO
                assert hello.is_hello()
                ack = _Frame.decode(await ws.recv())  # ... then HELLO_ACK
                assert ack.is_hello_ack()
                await ws.send(_Frame.encode(_Frame.create_hello_ack(node_id)))
                await asyncio.sleep(0.2)  # let B reach READY
                await ws.send(_Frame.encode(_Frame(9, payload)))
                await asyncio.sleep(0.5)

        def run(node_id: bytes, payload: bytes) -> None:
            asyncio.run(peer(node_id, payload))

        threads = [
            threading.Thread(target=run, args=(bytes([i]) * 16, f"p{i}".encode()))
            for i in (1, 2)
        ]
        for t in threads:
            t.start()
        assert _wait_for(lambda: len(got) == 2)
        for t in threads:
            t.join()
        assert {payload for _, payload in got} == {b"p1", b"p2"}
        assert len({conn_id for conn_id, _ in got}) == 2
    finally:
        b.close()


def test_node_without_logger_provider_is_silent(capfd):
    """0.1 installed a console exporter when no provider was given."""
    node = GatewayNode("127.0.0.1", _free_port(), framing_cls=_Frame)
    node.close()
    out, err = capfd.readouterr()
    assert "GatewayNode" not in out + err


def test_frames_buffered_across_an_outbound_reconnect_arrive():
    """Review of #26: the client's offline buffer reaches the new socket
    ahead of HELLO, and B dropped it as data on a non-ready connection."""
    a, b = _node(), _node()
    try:
        got: list[bytes] = []
        b.sink.subscribe(lambda tagged: got.append(tagged.frame.payload))
        cid = a.connect("127.0.0.1", b.port)
        assert _wait_for(lambda: bool(b.get_ready_connections()))
        assert _wait_for(lambda: cid in a.get_ready_connections())
        client = a._connections[cid]._ws_client
        assert client is not None and client._loop is not None
        sent: list[int] = []
        done = threading.Event()

        def drop_then_send() -> None:
            # On the client's loop: the socket dies, and before the client
            # notices, the application sends; these frames are buffered
            # while offline and flushed into the next socket, after HELLO.
            client.ws.transport.abort()
            for i in range(200):
                if a.send_to(cid, _Frame(9, str(i).encode())):
                    sent.append(i)
            done.set()

        client._loop.call_soon_threadsafe(drop_then_send)
        assert done.wait(5.0)
        assert sent == list(range(200))
        assert _wait_for(lambda: len(got) == 200, timeout=10.0), len(got)
        assert got == [str(i).encode() for i in range(200)]  # in order
    finally:
        a.close()
        b.close()


def test_data_between_hello_and_ready_is_delivered_after_on_connection():
    """A reconnecting peer flushes its offline buffer right after its HELLO;
    data before HELLO is dropped, as in 0.1 (review of #26)."""
    import asyncio

    import websockets

    b = _node()
    events: list[str] = []
    b.on_connection.subscribe(lambda conn: events.append("connection"))
    b.sink.subscribe(lambda tagged: events.append(tagged.frame.payload.decode()))
    try:

        async def peer() -> None:
            async with websockets.connect(f"ws://127.0.0.1:{b.port}/p") as ws:
                await ws.send(_Frame.encode(_Frame(9, b"before hello")))
                await ws.send(_Frame.encode(_Frame.create_hello(b"\x01" * 16)))
                await ws.send(_Frame.encode(_Frame(9, b"early")))
                assert _Frame.decode(await ws.recv()).is_hello()
                assert _Frame.decode(await ws.recv()).is_hello_ack()
                await ws.send(_Frame.encode(_Frame.create_hello_ack(b"\x01" * 16)))
                await ws.send(_Frame.encode(_Frame(9, b"late")))
                await asyncio.sleep(0.3)

        asyncio.run(peer())
        assert _wait_for(lambda: len(events) == 3)
        time.sleep(0.1)
        assert events == ["connection", "early", "late"]
    finally:
        b.close()


def test_a_peer_that_never_sends_hello_pins_no_memory():
    """Review of #26 (p8): 1024 frames were held per socket before READY,
    with no byte bound, even without a HELLO."""
    import asyncio

    import websockets

    b = _node()
    try:

        async def peer() -> int:
            async with websockets.connect(f"ws://127.0.0.1:{b.port}/x") as ws:
                for _ in range(300):
                    await ws.send(_Frame.encode(_Frame(9, b"\0" * 100_000)))
                await asyncio.sleep(0.5)
                return sum(len(c._early) for c in list(b._connections.values()))

        assert asyncio.run(peer()) == 0
    finally:
        b.close()


def test_the_handshake_buffer_is_bounded_in_bytes_and_closes_the_socket():
    import asyncio

    import websockets

    _, quiet = configure_telemetry()
    b = GatewayNode(
        "127.0.0.1",
        _free_port(),
        framing_cls=_Frame,
        logger_provider=quiet,
        handshake_buffer_bytes=10_000,
    )
    assert _wait_for(lambda: _listening(b.port))
    peaks: list[int] = []
    try:

        async def peer() -> bool:
            async with websockets.connect(f"ws://127.0.0.1:{b.port}/x") as ws:
                await ws.send(_Frame.encode(_Frame.create_hello(b"\x02" * 16)))
                try:
                    for _ in range(50):  # never HELLO_ACK: never READY
                        await ws.send(_Frame.encode(_Frame(9, b"\0" * 999)))
                        peaks.append(
                            sum(c._early_bytes for c in list(b._connections.values()))
                        )
                        await asyncio.sleep(0.005)
                    await asyncio.wait_for(ws.wait_closed(), 5.0)
                except websockets.ConnectionClosed:
                    pass
                return ws.state is websockets.protocol.State.CLOSED

        assert asyncio.run(peer())  # B closed the socket
        assert max(peaks) <= 10_000
        assert _wait_for(lambda: not b.connections)
    finally:
        b.close()


def test_an_inbound_socket_that_never_handshakes_emits_no_events():
    """on_disconnection pairs with on_connection (review of #26)."""
    import asyncio

    import websockets

    b = _node()
    connected: list[bytes] = []
    disconnected: list[bytes] = []
    b.on_connection.subscribe(lambda conn: connected.append(conn.conn_id))
    b.on_disconnection.subscribe(disconnected.append)
    try:

        async def peer() -> None:
            async with websockets.connect(f"ws://127.0.0.1:{b.port}/p") as ws:
                await ws.send(_Frame.encode(_Frame(9, b"never ready")))
                await asyncio.sleep(0.2)

        asyncio.run(peer())
        assert _wait_for(lambda: not b.connections)
        time.sleep(0.2)
        assert connected == [] and disconnected == []
    finally:
        b.close()


def test_an_outbound_client_that_exhausts_its_retries_is_closed():
    """0.1 left it CONNECTING in the table forever, without an event."""
    _, quiet = configure_telemetry()
    a = GatewayNode(
        "127.0.0.1",
        _free_port(),
        framing_cls=_Frame,
        logger_provider=quiet,
        retry_policy=RetryPolicy(max_retries=1, base_delay=0.05),
    )
    try:
        disconnected: list[bytes] = []
        a.on_disconnection.subscribe(disconnected.append)
        cid = a.connect("127.0.0.1", _free_port())  # nobody listens
        assert _wait_for(lambda: disconnected == [cid], timeout=10.0)
        assert a.get_connection(cid) is None
    finally:
        a.close()


def test_disconnect_closes_once_with_one_event():
    a, b = _node(), _node()
    try:
        disconnected: list[bytes] = []
        a.on_disconnection.subscribe(disconnected.append)
        cid = a.connect("127.0.0.1", b.port)
        assert _wait_for(lambda: cid in a.get_ready_connections())
        conn = a.get_connection(cid)
        assert conn is not None
        assert a.disconnect(cid)
        assert not a.disconnect(cid)
        assert conn.state == GatewayConnectionState.CLOSED
        assert a.get_connection(cid) is None
        time.sleep(0.2)
        assert disconnected == [cid]
    finally:
        a.close()
        b.close()


def test_a_server_that_cannot_bind_logs_an_error():
    """Review of #26: the bind failure went unreported."""
    from opentelemetry.sdk._logs.export import (
        InMemoryLogRecordExporter,
        SimpleLogRecordProcessor,
    )

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        exporter = InMemoryLogRecordExporter()
        _, provider = configure_telemetry()
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
        node = GatewayNode(
            "127.0.0.1", port, framing_cls=_Frame, logger_provider=provider
        )
        try:

            def errors() -> list[str]:
                return [
                    str(r.log_record.body)
                    for r in exporter.get_finished_logs()
                    if r.log_record.severity_text == "ERROR"
                ]

            assert _wait_for(
                lambda: any("Server pipeline error" in e for e in errors())
            ), errors()
        finally:
            node.close()
