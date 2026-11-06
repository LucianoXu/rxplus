"""GatewayNode handshake over real local WebSocket connections."""

import socket
import time
from collections.abc import Callable

from rxplus.gateway import GatewayConnectionState, GatewayNode
from rxplus.telemetry import configure_telemetry


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
