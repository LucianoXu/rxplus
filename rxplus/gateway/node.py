"""Symmetric peer-to-peer gateway node for reactive communication.

This module provides a base class for building protocol-specific nodes with:
    - Bidirectional peer connections
    - HELLO/HELLO_ACK handshake protocol
    - Tagged frame routing

The GatewayNode is generic over the framing type, allowing different
protocols to implement their own binary formats while reusing the
connection management infrastructure.

Example:
    >>> from rxplus.gateway import GatewayNode, TaggedFrame
    >>>
    >>> # Create a node with custom framing
    >>> node = GatewayNode[MyFraming](
    ...     host="::", port=8765, framing_cls=MyFraming
    ... )
    >>>
    >>> # Handle all inbound frames
    >>> node.sink.subscribe(
    ...     on_next=lambda tagged: print(f"From {tagged.peer_node_id}: {tagged.frame}")
    ... )
    >>>
    >>> # Connect to another node
    >>> conn_id = node.connect("192.168.1.100", 8765)
    >>>
    >>> # Send to specific peer
    >>> node.send_to(conn_id, frame)
    >>>
    >>> # Broadcast to all
    >>> node.broadcast(frame)
"""

import threading
import uuid
from typing import TypeVar, cast

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import TracerProvider
from reactivex import Observable
from reactivex import operators as ops
from reactivex.disposable import CompositeDisposable
from reactivex.subject import Subject

from ..duplex import make_duplex
from ..telemetry import OTelLogger
from ..ws import (
    RetryPolicy,
    RxWSClient,
    RxWSServer,
    WSConnectionConfig,
    WSConnectionEvent,
    WSConnectionState,
    WSReceived,
)
from .connection import Connection, GatewayConnectionState
from .framing import Framing, TaggedFrame

F = TypeVar("F", bound=Framing)

_EARLY_LIMIT = 1024  # data frames kept per connection before READY


class GatewayNode[F: Framing]:
    """Symmetric gateway node with HELLO/HELLO_ACK handshake.

    A node can both listen for incoming connections and initiate
    connections to other nodes. All connections are treated uniformly,
    enabling true peer-to-peer communication.

    Type Parameters:
        F: The framing type implementing the Framing protocol

    Attributes:
        node_id: This node's 128-bit identifier
        host: Host address the node listens on
        port: Port the node listens on
        sink: Observable of TaggedFrame from all connections

    Example:
        >>> node = GatewayNode[MyFraming](
        ...     host="::", port=8765, framing_cls=MyFraming
        ... )
        >>>
        >>> # Handle all inbound frames
        >>> node.sink.subscribe(
        ...     on_next=lambda tagged: print(
        ...         f"From {tagged.peer_node_id}: {tagged.frame}"
        ...     )
        ... )
        >>>
        >>> # Connect to another node
        >>> conn_id = node.connect("192.168.1.100", 8765)
        >>>
        >>> # Send to specific peer
        >>> node.send_to(conn_id, frame)
        >>>
        >>> # Broadcast to all
        >>> node.broadcast(frame)
        >>>
        >>> # List connected peers
        >>> print(node.connections)
    """

    def __init__(
        self,
        host: str,
        port: int,
        framing_cls: type[F],
        node_id: bytes | None = None,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 30.0,
        retry_policy: RetryPolicy | None = None,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
        name: str | None = None,
    ):
        """Initialize gateway node.

        Args:
            host: Host to bind to (e.g., "::" for all interfaces)
            port: Port to listen on
            framing_cls: Class implementing the Framing protocol
            node_id: Optional 128-bit node identifier (generated if not provided)
            ping_interval: WebSocket ping interval in seconds
            ping_timeout: WebSocket ping timeout in seconds
            retry_policy: Configurable retry behavior with exponential
                backoff. If None, uses default RetryPolicy with
                base_delay=2.0 and infinite retries.
            tracer_provider: Optional OTel TracerProvider for span instrumentation.
            logger_provider: Optional OTel LoggerProvider for log emission.
                If None, the node logs nothing (0.1: console output).
            name: Optional node name for logging (auto-generated if not provided)
        """
        self.node_id = node_id or uuid.uuid4().bytes
        if len(self.node_id) != 16:
            raise ValueError(f"node_id must be 16 bytes, got {len(self.node_id)}")

        self.host = host
        self.port = port
        self._framing_cls = framing_cls
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._retry_policy = retry_policy or RetryPolicy(base_delay=2.0)

        self._name = name or f"GatewayNode:{self.node_id.hex()[:8]}@{host}:{port}"

        self._tracer_provider = tracer_provider
        self._logger_provider = logger_provider
        # Without a provider the node is silent: a provider with no processor.
        self._otel_logger = OTelLogger(
            (logger_provider or LoggerProvider()).get_logger(
                f"rxplus.gateway.{self._name}"
            ),
            source=self._name,
        )

        # Connection tracking
        self._connections: dict[bytes, Connection] = {}
        self._connections_lock = threading.RLock()

        # Inbound connections, keyed by the server's per-socket connection ID
        self._inbound: dict[int, bytes] = {}

        # Unified sink for all inbound frames
        self._sink: Subject[TaggedFrame[F]] = Subject()

        # Connection event subjects
        self._on_connection: Subject[Connection] = Subject()
        self._on_disconnection: Subject[bytes] = Subject()

        # Disposables for cleanup
        self._disposables = CompositeDisposable()

        # Internal server for accepting inbound connections
        self._server = RxWSServer(
            WSConnectionConfig(host=host, port=port),
            datatype="bytes",
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            name=f"{self._name}:server",
            tracer_provider=self._tracer_provider,
            logger_provider=self._logger_provider,
        )

        self._setup_server_pipeline()

        self._otel_logger.info(
            f"GatewayNode started: {self.node_id.hex()} listening on {host}:{port}"
        )

    def _setup_server_pipeline(self):
        """Wire up inbound connections: one Connection per server socket."""
        self._disposables.add(
            self._server.subscribe(
                on_error=lambda e: self._otel_logger.error(
                    f"Server pipeline error: {e}"  # e.g. the port cannot be bound
                ),
            )
        )
        self._disposables.add(
            self._server.connections.subscribe(
                on_next=self._on_server_connection,
                on_error=lambda e: self._otel_logger.error(f"Server error: {e}"),
            )
        )
        self._disposables.add(
            self._server.received.subscribe(
                on_next=self._on_inbound_data,
                on_error=lambda e: self._otel_logger.error(
                    f"Server pipeline error: {e}"
                ),
            )
        )

    def _on_server_connection(self, event: WSConnectionEvent) -> None:
        """A peer's socket opened (send HELLO) or closed (connection closed)."""
        if event.kind == "opened":
            conn = Connection(
                conn_id=uuid.uuid4().bytes,
                direction="inbound",
                remote_addr=(event.path, 0),  # the path the peer connected on
                state=GatewayConnectionState.HANDSHAKING,
            )
            conn._duplex = make_duplex()
            conn._server_connection = event.connection_id
            with self._connections_lock:
                self._connections[conn.conn_id] = conn
                self._inbound[event.connection_id] = conn.conn_id
            self._otel_logger.info(
                f"Accepted inbound connection: {conn.conn_id.hex()[:16]}"
                f" from {event.path}"
            )
            # Our HELLO goes out in reply to the peer's (see _handle_hello):
            # a HELLO sent now could reach the peer's client before the
            # peer has subscribed to it.
            return
        with self._connections_lock:
            conn_id = self._inbound.get(event.connection_id)
            closed = self._connections.get(conn_id) if conn_id else None
        if closed is not None:
            # The peer's socket is gone: 0.1 kept such connections READY.
            self._finish(closed, "peer socket closed")

    def _finish(self, conn: Connection, reason: str) -> bool:
        """Close a connection once: CLOSED, forgotten, subscriptions disposed.

        ``on_disconnection`` pairs with ``on_connection``: it fires for an
        inbound connection only if it reached READY, and for every outbound
        connection (its id was returned by :meth:`connect`).
        """
        with self._connections_lock:
            if conn.state == GatewayConnectionState.CLOSED:
                return False
            # Under the lock, so _check_handshake_complete cannot emit
            # on_connection after this.
            conn.state = GatewayConnectionState.CLOSED
            self._connections.pop(conn.conn_id, None)
            if conn._server_connection is not None:
                self._inbound.pop(conn._server_connection, None)
            notify = conn.direction == "outbound" or conn._was_ready
            conn._early.clear()
        conn._disposables.dispose()
        self._otel_logger.info(
            f"Connection closed ({reason}): {conn.conn_id.hex()[:16]}"
        )
        if notify:
            self._on_disconnection.on_next(conn.conn_id)
        return True

    def _on_inbound_data(self, received: WSReceived) -> None:
        tagged = self._handle_inbound_data(received)
        if tagged is not None:
            self._sink.on_next(tagged)

    def _handle_inbound_data(self, received: WSReceived) -> TaggedFrame[F] | None:
        """Handle data from one inbound socket."""
        with self._connections_lock:
            conn_id = self._inbound.get(received.connection_id)
            conn = self._connections.get(conn_id) if conn_id else None
        if conn is None:
            self._otel_logger.warning(
                f"Received data for unknown connection {received.connection_id}"
            )
            return None
        data = received.value
        if not isinstance(data, bytes):
            return None

        # Decode frame
        try:
            frame = self._framing_cls.decode(data)
        except Exception as e:
            self._otel_logger.error(f"Frame decode failed: {e}")
            return None

        # Handle handshake events
        typed_frame = cast(F, frame)
        if frame.is_hello():
            return self._handle_hello(conn, typed_frame)
        elif frame.is_hello_ack():
            return self._handle_hello_ack(conn, typed_frame)

        if conn.state != GatewayConnectionState.READY:
            self._hold_early(conn, typed_frame)
            return None

        return TaggedFrame(
            frame=typed_frame,
            conn_id=conn.conn_id,
            peer_node_id=conn.peer_node_id,
        )

    def _hold_early(self, conn: Connection, frame: F) -> None:
        """Keep a data frame that arrived before READY (bounded); 0.1 dropped
        it. A peer's offline buffer reaches a new socket ahead of its HELLO."""
        with self._connections_lock:
            if (
                conn.state == GatewayConnectionState.HANDSHAKING
                and len(conn._early) < _EARLY_LIMIT
            ):
                conn._early.append(frame)
                return
        self._otel_logger.warning(
            f"Dropped a frame on non-ready connection: {conn.conn_id.hex()[:16]}"
        )

    def _send_frame(self, conn: Connection, frame: F | Framing) -> bool:
        """Encode and send one frame on a connection, inbound or outbound."""
        encoded = self._framing_cls.encode(frame)
        if conn.direction == "outbound":
            if conn._ws_client is None:
                return False
            conn._ws_client.on_next(encoded)
            return True
        if conn._server_connection is None:
            return False
        return self._server.send_to(conn._server_connection, encoded)

    def _handle_hello(self, conn: Connection, frame: F) -> TaggedFrame[F] | None:
        """Handle incoming HELLO message."""
        peer_node_id = frame.get_hello_node_id()
        if peer_node_id is None or len(peer_node_id) != 16:
            self._otel_logger.error("Invalid HELLO payload")
            return None

        rehandshake = False
        with self._connections_lock:
            if conn.direction == "inbound" and conn._hello_received:
                # A second HELLO on the same inbound socket: the peer restarted
                # its handshake (a reconnect is a new socket and a new
                # connection). It waits for our HELLO and HELLO_ACK again.
                rehandshake = True
                conn._hello_ack_received = False
                conn.state = GatewayConnectionState.HANDSHAKING
            conn.peer_node_id = peer_node_id
            conn._hello_received = True

        self._otel_logger.debug(
            f"Received HELLO from {peer_node_id.hex()[:16]}"
            f" on conn {conn.conn_id.hex()[:16]}"
        )

        # Inbound: our HELLO answers the peer's first HELLO (and a restarted
        # handshake); then, for every HELLO, the HELLO_ACK.
        if conn.direction == "inbound" and (rehandshake or not conn._hello_sent):
            conn._hello_sent = True
            self._send_frame(conn, self._framing_cls.create_hello(self.node_id))
        self._send_frame(conn, self._framing_cls.create_hello_ack(self.node_id))

        # Check if handshake is complete
        self._check_handshake_complete(conn)
        return None  # Don't forward HELLO to application

    def _handle_hello_ack(self, conn: Connection, frame: F) -> TaggedFrame[F] | None:
        """Handle incoming HELLO_ACK message."""
        peer_node_id = frame.get_hello_node_id()
        if peer_node_id is None or len(peer_node_id) != 16:
            self._otel_logger.error("Invalid HELLO_ACK payload")
            return None

        # Update peer_node_id if not already set
        if conn.peer_node_id is None:
            conn.peer_node_id = peer_node_id

        conn._hello_ack_received = True

        self._otel_logger.debug(
            f"Received HELLO_ACK from {peer_node_id.hex()[:16]}"
            f" on conn {conn.conn_id.hex()[:16]}"
        )

        # Check if handshake is complete
        self._check_handshake_complete(conn)
        return None  # Don't forward HELLO_ACK to application

    def _check_handshake_complete(self, conn: Connection):
        """Check if handshake is complete and transition to READY.

        Thread-safe state transition with lock protection.
        """
        should_emit = False
        early: list[object] = []
        with self._connections_lock:
            if (
                conn._hello_received
                and conn._hello_ack_received
                and conn.state == GatewayConnectionState.HANDSHAKING
            ):
                conn.state = GatewayConnectionState.READY
                conn._was_ready = True
                should_emit = True
                early, conn._early = conn._early, []
                peer = conn.peer_node_id.hex()[:16] if conn.peer_node_id else "unknown"
                self._otel_logger.info(
                    f"Handshake complete: {conn.conn_id.hex()[:16]} ↔ {peer}"
                )
        # Emit event outside lock to avoid potential deadlock
        if should_emit:
            self._on_connection.on_next(conn)
            # Then the frames that arrived before READY, in order (on the
            # thread that delivers this connection's frames).
            for frame in early:
                self._sink.on_next(
                    TaggedFrame(
                        frame=cast(F, frame),
                        conn_id=conn.conn_id,
                        peer_node_id=conn.peer_node_id,
                    )
                )

    def connect(self, host: str, port: int) -> bytes:
        """Initiate outbound connection to another node.

        Creates a new connection to the specified host:port. The connection
        will automatically perform the HELLO/HELLO_ACK handshake.

        Args:
            host: Remote host to connect to
            port: Remote port to connect to

        Returns:
            Connection ID (16 bytes UUID) for this connection
        """
        conn_id = uuid.uuid4().bytes

        # Create connection record
        conn = Connection(
            conn_id=conn_id,
            direction="outbound",
            remote_addr=(host, port),
            state=GatewayConnectionState.CONNECTING,
        )

        # Use unique path based on conn_id to distinguish this connection
        unique_path = f"/{conn_id.hex()}"

        # Create WebSocket client with auto-reconnect
        ws_client = RxWSClient(
            WSConnectionConfig(host=host, port=port, path=unique_path),
            datatype="bytes",
            retry_policy=self._retry_policy,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            name=f"{self._name}:client:{conn_id.hex()[:8]}",
            buffer_while_disconnected=True,  # Buffer messages during reconnect
        )

        conn._ws_client = ws_client
        conn._duplex = make_duplex()

        # Wire up client pipeline (includes connection state subscription)
        self._setup_client_pipeline(conn)

        with self._connections_lock:
            self._connections[conn_id] = conn

        self._otel_logger.info(
            f"Connecting to {host}:{port} (conn_id={conn_id.hex()[:16]})"
        )

        # Note: Handshake (HELLO) will be sent when the WS connection becomes CONNECTED
        # via _handle_ws_connection_state. No need to send here.

        return conn_id

    def _setup_client_pipeline(self, conn: Connection):
        """Wire up pipeline for an outbound connection."""
        ws_client = conn._ws_client
        if ws_client is None:
            return

        def client_failed(error: Exception) -> None:
            # The client ended with an error (e.g. retries exhausted): 0.1
            # left the connection CONNECTING forever, without an event.
            self._finish(conn, f"client error: {type(error).__name__}")

        # Inbound from client: decode and handle
        conn._disposables.add(
            ws_client.pipe(
                ops.map(lambda data: self._handle_outbound_data(conn, data)),  # type: ignore[arg-type]
                ops.filter(lambda x: x is not None),
            ).subscribe(
                on_next=lambda tagged: self._sink.on_next(tagged),  # type: ignore[arg-type]
                on_error=client_failed,
            )
        )

        # Subscribe to connection state changes from RxWSClient
        conn._disposables.add(
            ws_client.connection_state.subscribe(
                on_next=lambda state: self._handle_ws_connection_state(conn, state),
                on_error=lambda e: self._otel_logger.error(
                    f"Connection state error for {conn.conn_id.hex()[:16]}: {e}"
                ),
            )
        )

    def _handle_ws_connection_state(
        self, conn: Connection, ws_state: WSConnectionState
    ):
        """Handle WebSocket connection state changes for outbound connections.

        This bridges the rxplus WebSocket connection state to our gateway connection
        lifecycle. When the underlying WebSocket reconnects, we may need to
        re-run the handshake.

        Args:
            conn: The gateway connection
            ws_state: The new WebSocket connection state from rxplus
        """
        conn_id_short = conn.conn_id.hex()[:16]
        self._otel_logger.debug(
            f"WS state change for {conn_id_short}: {ws_state.value}"
        )

        if ws_state == WSConnectionState.CONNECTED:
            # The handshake state is reset when a socket that was up drops
            # (below), never here: the subscription that delivers this state
            # may replay it after the peer's HELLO has already been handled
            # on the client's loop thread.
            with self._connections_lock:
                if conn.state == GatewayConnectionState.CLOSED:
                    return  # don't revive a closed connection
                conn._ws_up = True
                if conn.state != GatewayConnectionState.READY:
                    conn.state = GatewayConnectionState.HANDSHAKING
            self._otel_logger.info(
                f"WS connected, starting handshake for {conn_id_short}"
            )
            self._send_hello_to_client(conn)
            self._check_handshake_complete(conn)

        elif ws_state == WSConnectionState.DISCONNECTED:
            # A socket that was up dropped: the reconnect starts a fresh
            # handshake. (Auto-reconnect handles the transport.)
            with self._connections_lock:
                if not conn._ws_up:
                    return  # e.g. the initial state, replayed on subscribe
                conn._ws_up = False
                conn._hello_received = False
                conn._hello_ack_received = False
                if conn.state == GatewayConnectionState.READY:
                    self._otel_logger.info(
                        f"WS disconnected, connection {conn_id_short} no longer READY"
                    )
                if conn.state != GatewayConnectionState.CLOSED:
                    conn.state = GatewayConnectionState.HANDSHAKING

        elif ws_state == WSConnectionState.RECONNECTING:
            # Still trying to reconnect
            self._otel_logger.debug(f"WS reconnecting for {conn_id_short}")

        elif ws_state == WSConnectionState.CLOSED:
            # WebSocket closed permanently (max retries or on_completed)
            self._finish(conn, "WS closed permanently")

    def _handle_outbound_data(
        self, conn: Connection, data: bytes
    ) -> TaggedFrame[F] | None:
        """Handle data from an outbound connection."""
        # Decode frame
        try:
            frame = self._framing_cls.decode(data)
        except Exception as e:
            self._otel_logger.error(f"Frame decode failed: {e}")
            return None

        # Handle handshake events
        typed_frame = cast(F, frame)
        if frame.is_hello():
            return self._handle_hello(conn, typed_frame)
        elif frame.is_hello_ack():
            return self._handle_hello_ack(conn, typed_frame)

        if conn.state != GatewayConnectionState.READY:
            self._hold_early(conn, typed_frame)
            return None

        return TaggedFrame(
            frame=typed_frame,
            conn_id=conn.conn_id,
            peer_node_id=conn.peer_node_id,
        )

    def _send_hello_to_client(self, conn: Connection):
        """Send HELLO frame via outbound client."""
        if conn._ws_client is None:
            return

        hello_frame = self._framing_cls.create_hello(self.node_id)
        conn._ws_client.on_next(self._framing_cls.encode(hello_frame))
        self._otel_logger.debug(f"Sent HELLO to {conn.remote_addr}")

    def disconnect(self, conn_id: bytes) -> bool:
        """Close a specific connection.

        Args:
            conn_id: Connection ID to close

        Returns:
            True if connection was found and closed, False otherwise
        """
        with self._connections_lock:
            conn = self._connections.get(conn_id)

        if conn is None:
            self._otel_logger.warning(
                f"Disconnect: connection not found: {conn_id.hex()[:16]}"
            )
            return False

        if not self._finish(conn, "disconnect()"):
            return False
        if conn._ws_client is not None:
            conn._ws_client.on_completed()
        if conn._server_connection is not None:
            self._server.close_connection(conn._server_connection)
        return True

    def send_to(self, conn_id: bytes, frame: F) -> bool:
        """Send frame to a specific connection.

        Args:
            conn_id: Connection ID to send to
            frame: Frame to send

        Returns:
            True if frame was sent, False if connection not found or not ready
        """
        with self._connections_lock:
            conn = self._connections.get(conn_id)

        if conn is None:
            self._otel_logger.warning(
                f"send_to: connection not found: {conn_id.hex()[:16]}"
            )
            return False

        if conn.state != GatewayConnectionState.READY:
            self._otel_logger.warning(
                f"send_to: connection not ready: {conn_id.hex()[:16]}"
                f" (state={conn.state.name})"
            )
            return False

        return self._send_frame(conn, frame)

    def broadcast(self, frame: F) -> int:
        """Send frame to all READY connections.

        Args:
            frame: Frame to broadcast

        Returns:
            Number of connections the frame was sent to
        """
        with self._connections_lock:
            ready = [
                c
                for c in self._connections.values()
                if c.state == GatewayConnectionState.READY
            ]
        return sum(1 for conn in ready if self._send_frame(conn, frame))

    @property
    def sink(self) -> Observable:
        """Observable of TaggedFrame from all connections.

        Subscribe to this to receive all inbound frames tagged with
        their source connection ID.
        """
        return self._sink

    @property
    def on_connection(self) -> Observable:
        """Observable that emits when a connection becomes READY."""
        return self._on_connection

    @property
    def on_disconnection(self) -> Observable:
        """Observable that emits connection ID when a connection closes."""
        return self._on_disconnection

    @property
    def connections(self) -> dict[bytes, Connection]:
        """Dictionary of all connections (conn_id → Connection).

        Note: Returns a shallow copy for thread safety.
        """
        with self._connections_lock:
            return dict(self._connections)

    def get_connection(self, conn_id: bytes) -> Connection | None:
        """Get a specific connection by ID.

        Args:
            conn_id: Connection ID to look up

        Returns:
            Connection if found, None otherwise
        """
        with self._connections_lock:
            return self._connections.get(conn_id)

    def get_ready_connections(self) -> dict[bytes, Connection]:
        """Get all connections in READY state.

        Returns:
            Dictionary of ready connections (conn_id → Connection)
        """
        with self._connections_lock:
            return {
                cid: conn
                for cid, conn in self._connections.items()
                if conn.state == GatewayConnectionState.READY
            }

    def close(self):
        """Shutdown node and all connections.

        Closes all connections and stops listening for new connections.
        """
        self._otel_logger.info(f"GatewayNode shutting down: {self.node_id.hex()}")

        # Close all connections
        with self._connections_lock:
            conn_ids = list(self._connections.keys())

        for conn_id in conn_ids:
            self.disconnect(conn_id)

        # Dispose subscriptions
        self._disposables.dispose()

        # Close server
        self._server.on_completed()

        # Complete subjects
        self._sink.on_completed()
        self._on_connection.on_completed()
        self._on_disconnection.on_completed()

        self._otel_logger.info(f"GatewayNode closed: {self.node_id.hex()}")
