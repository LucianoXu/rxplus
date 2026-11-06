"""GatewayNode handshake over real local WebSocket connections."""

import socket
import time
from collections.abc import Callable

from rxplus.gateway import ConnectionState, GatewayNode
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


def _node() -> GatewayNode[_Frame]:
    _, quiet = configure_telemetry()  # no exporter: silent
    return GatewayNode(
        "127.0.0.1", _free_port(), framing_cls=_Frame, logger_provider=quiet
    )


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
        assert conn is not None and conn.state == ConnectionState.READY
        got: list[bytes] = []
        b.sink.subscribe(lambda tagged: got.append(tagged.frame.payload))
        assert a.send_to(cid, _Frame(9, b"again"))
        assert _wait_for(lambda: got == [b"again"])
    finally:
        a.close()
        b.close()
