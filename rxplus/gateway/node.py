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
from ..telemetry import OTelLogger, get_default_providers
from ..utils import TaggedData
from ..ws import (
    RetryPolicy,
    RxWSClient,
    RxWSServer,
    WSConnectionConfig,
    WSConnectionState,
)
from .connection import Connection, ConnectionState
from .framing import Framing, TaggedFrame

F = TypeVar("F", bound=Framing)


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
                If None, uses default providers with console output.
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

        # Auto-configure default providers if not provided
        if logger_provider is None:
            tracer_provider, logger_provider = get_default_providers("rxplus.gateway")

        self._tracer_provider = tracer_provider
        self._logger_provider = logger_provider
        self._otel_logger = OTelLogger(
            logger_provider.get_logger(f"rxplus.gateway.{self._name}"),
            source=self._name,
        )

        # Connection tracking
        self._connections: dict[bytes, Connection] = {}
        self._connections_lock = threading.RLock()

        # Track path → conn_id mapping for inbound connections
        self._inbound_path_to_conn: dict[str, bytes] = {}

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
        """Wire up pipeline for handling inbound connections."""
        # Inbound: WS binary (TaggedData) → decode → handle
        self._disposables.add(
            self._server.pipe(
                ops.map(self._handle_inbound_data),
                ops.filter(lambda x: x is not None),
            ).subscribe(
                on_next=lambda tagged: self._sink.on_next(tagged),  # type: ignore[arg-type]
                on_error=lambda e: self._otel_logger.error(
                    f"Server pipeline error: {e}"
                ),
            )
        )

    def _handle_inbound_data(self, tagged: TaggedData) -> TaggedFrame[F] | None:
        """Handle data from server (inbound connections)."""
        path = tagged.tag
        data = tagged.data

        # Get or create connection for this path
        with self._connections_lock:
            if path not in self._inbound_path_to_conn:
                # New inbound connection
                conn_id = uuid.uuid4().bytes
                self._inbound_path_to_conn[path] = conn_id

                conn = Connection(
                    conn_id=conn_id,
                    direction="inbound",
                    remote_addr=(path, 0),  # Path is used as identifier for inbound
                    state=ConnectionState.HANDSHAKING,
                )
                conn._duplex = make_duplex()
                self._connections[conn_id] = conn

                self._otel_logger.info(
                    f"Accepted inbound connection: {conn_id.hex()[:16]} from {path}"
                )

                # Send HELLO to new connection
                self._send_hello_to_path(path)

            conn_id = self._inbound_path_to_conn[path]
            conn = self._connections.get(conn_id)  # type: ignore[assignment]

        if conn is None:
            self._otel_logger.warning(
                f"Received data for unknown connection path: {path}"
            )
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

        # Only forward non-handshake events if connection is READY
        if conn.state != ConnectionState.READY:
            self._otel_logger.warning(
                f"Received data on non-ready connection: {conn_id.hex()[:16]}"
            )
            return None

        return TaggedFrame(
            frame=typed_frame,
            conn_id=conn_id,
            peer_node_id=conn.peer_node_id,
        )

    def _send_hello_to_path(self, path: str):
        """Send HELLO frame to an inbound connection by path."""
        hello_frame = self._framing_cls.create_hello(self.node_id)
        self._server.on_next(TaggedData(path, self._framing_cls.encode(hello_frame)))
        self._otel_logger.debug(f"Sent HELLO to path {path}")

    def _send_hello_ack_to_path(self, path: str):
        """Send HELLO_ACK frame to an inbound connection by path."""
        hello_ack_frame = self._framing_cls.create_hello_ack(self.node_id)
        self._server.on_next(
            TaggedData(path, self._framing_cls.encode(hello_ack_frame))
        )
        self._otel_logger.debug(f"Sent HELLO_ACK to path {path}")

    def _handle_hello(self, conn: Connection, frame: F) -> TaggedFrame[F] | None:
        """Handle incoming HELLO message."""
        peer_node_id = frame.get_hello_node_id()
        if peer_node_id is None or len(peer_node_id) != 16:
            self._otel_logger.error("Invalid HELLO payload")
            return None

        conn.peer_node_id = peer_node_id
        conn._hello_received = True

        self._otel_logger.debug(
            f"Received HELLO from {peer_node_id.hex()[:16]}"
            f" on conn {conn.conn_id.hex()[:16]}"
        )

        # Send HELLO_ACK
        if conn.direction == "inbound":
            # Find path for this connection (copy under lock to avoid race)
            path = self._find_path_for_conn(conn.conn_id)
            if path:
                self._send_hello_ack_to_path(path)
        else:
            # Outbound: send via client
            self._send_hello_ack_to_client(conn)

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
        with self._connections_lock:
            if (
                conn._hello_received
                and conn._hello_ack_received
                and conn.state == ConnectionState.HANDSHAKING
            ):
                conn.state = ConnectionState.READY
                should_emit = True
                peer = conn.peer_node_id.hex()[:16] if conn.peer_node_id else "unknown"
                self._otel_logger.info(
                    f"Handshake complete: {conn.conn_id.hex()[:16]} ↔ {peer}"
                )
        # Emit event outside lock to avoid potential deadlock
        if should_emit:
            self._on_connection.on_next(conn)

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
            state=ConnectionState.CONNECTING,
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

        # Inbound from client: decode and handle
        conn._disposables.add(
            ws_client.pipe(
                ops.map(lambda data: self._handle_outbound_data(conn, data)),  # type: ignore[arg-type]
                ops.filter(lambda x: x is not None),
            ).subscribe(
                on_next=lambda tagged: self._sink.on_next(tagged),  # type: ignore[arg-type]
                on_error=lambda e: self._otel_logger.error(
                    f"Client pipeline error for {conn.conn_id.hex()[:16]}: {e}"
                ),
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
            # WebSocket (re)connected - need to send HELLO
            # Reset handshake state for fresh handshake
            if conn.state == ConnectionState.CLOSED:
                # Don't revive a closed connection
                return

            # On reconnection, reset handshake flags and re-send HELLO
            conn._hello_received = False
            conn._hello_ack_received = False
            conn.state = ConnectionState.HANDSHAKING
            self._otel_logger.info(
                f"WS connected, starting handshake for {conn_id_short}"
            )
            self._send_hello_to_client(conn)

        elif ws_state == WSConnectionState.DISCONNECTED:
            # WebSocket disconnected - mark connection as not ready
            # but don't close it (auto-reconnect will handle it)
            if conn.state == ConnectionState.READY:
                self._otel_logger.info(
                    f"WS disconnected, connection {conn_id_short} no longer READY"
                )
                # Move back to HANDSHAKING (will re-handshake on reconnect)
                conn.state = ConnectionState.HANDSHAKING

        elif ws_state == WSConnectionState.RECONNECTING:
            # Still trying to reconnect
            self._otel_logger.debug(f"WS reconnecting for {conn_id_short}")

        elif ws_state == WSConnectionState.CLOSED:
            # WebSocket closed permanently (max retries or on_completed)
            if conn.state != ConnectionState.CLOSED:
                self._otel_logger.info(f"WS closed permanently for {conn_id_short}")
                conn.state = ConnectionState.CLOSED
                self._on_disconnection.on_next(conn.conn_id)

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

        # Only forward non-handshake events if connection is READY
        if conn.state != ConnectionState.READY:
            self._otel_logger.warning(
                "Received data on non-ready outbound connection: "
                f"{conn.conn_id.hex()[:16]}"
            )
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

    def _send_hello_ack_to_client(self, conn: Connection):
        """Send HELLO_ACK frame via outbound client."""
        if conn._ws_client is None:
            return

        hello_ack_frame = self._framing_cls.create_hello_ack(self.node_id)
        conn._ws_client.on_next(self._framing_cls.encode(hello_ack_frame))
        self._otel_logger.debug(f"Sent HELLO_ACK to {conn.remote_addr}")

    def _find_path_for_conn(self, conn_id: bytes) -> str | None:
        """Find the WebSocket path for an inbound connection.

        Thread-safe lookup of path → conn_id mapping.

        Args:
            conn_id: Connection ID to look up

        Returns:
            Path string if found, None otherwise
        """
        with self._connections_lock:
            for path, cid in self._inbound_path_to_conn.items():
                if cid == conn_id:
                    return path
        return None

    def disconnect(self, conn_id: bytes) -> bool:
        """Close a specific connection.

        Args:
            conn_id: Connection ID to close

        Returns:
            True if connection was found and closed, False otherwise
        """
        with self._connections_lock:
            conn = self._connections.pop(conn_id, None)

            # Also remove from inbound path mapping if applicable
            path_to_remove = None
            for path, cid in self._inbound_path_to_conn.items():
                if cid == conn_id:
                    path_to_remove = path
                    break
            if path_to_remove:
                del self._inbound_path_to_conn[path_to_remove]

        if conn is None:
            self._otel_logger.warning(
                f"Disconnect: connection not found: {conn_id.hex()[:16]}"
            )
            return False

        # Close the connection
        conn.state = ConnectionState.CLOSED
        conn._disposables.dispose()

        if conn._ws_client is not None:
            conn._ws_client.on_completed()

        self._otel_logger.info(f"Connection closed: {conn_id.hex()[:16]}")
        self._on_disconnection.on_next(conn_id)

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

        if conn.state != ConnectionState.READY:
            self._otel_logger.warning(
                f"send_to: connection not ready: {conn_id.hex()[:16]}"
                f" (state={conn.state.name})"
            )
            return False

        encoded = self._framing_cls.encode(frame)

        if conn.direction == "outbound":
            # Send via client
            if conn._ws_client is not None:
                conn._ws_client.on_next(encoded)
                return True
        else:
            # Send via server (need to find path)
            with self._connections_lock:
                for path, cid in self._inbound_path_to_conn.items():
                    if cid == conn_id:
                        self._server.on_next(TaggedData(path, encoded))
                        return True

        return False

    def broadcast(self, frame: F) -> int:
        """Send frame to all READY connections.

        Args:
            frame: Frame to broadcast

        Returns:
            Number of connections the frame was sent to
        """
        encoded = self._framing_cls.encode(frame)
        sent_count = 0

        with self._connections_lock:
            connections = list(self._connections.values())

        for conn in connections:
            if conn.state != ConnectionState.READY:
                continue

            if conn.direction == "outbound":
                if conn._ws_client is not None:
                    conn._ws_client.on_next(encoded)
                    sent_count += 1
            else:
                # Find path for inbound connection
                with self._connections_lock:
                    for path, cid in self._inbound_path_to_conn.items():
                        if cid == conn.conn_id:
                            self._server.on_next(TaggedData(path, encoded))
                            sent_count += 1
                            break

        return sent_count

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
                if conn.state == ConnectionState.READY
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
