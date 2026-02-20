"""Connection management for reactive gateways.

This module provides connection abstractions for peer-to-peer reactive
communication:
    - ConnectionState: Connection lifecycle states
    - Connection: Base connection dataclass
    - HelloPayload: Handshake payload for HELLO/HELLO_ACK messages

Connection states:
    CONNECTING → HANDSHAKING: Transport connected (outbound only)
    HANDSHAKING → READY: HELLO/HELLO_ACK exchange complete
    READY → CLOSED: Connection terminated
    HANDSHAKING → CLOSED: Handshake failed or connection dropped
    CONNECTING → CLOSED: Connection failed (outbound only)

Example:
    >>> from rxplus.gateway import Connection, ConnectionState, HelloPayload
    >>>
    >>> # Create connection record
    >>> conn = Connection(
    ...     conn_id=uuid.uuid4().bytes,
    ...     direction="outbound",
    ...     remote_addr=("192.168.1.100", 8765),
    ... )
    >>>
    >>> # Update state after handshake
    >>> conn.state = ConnectionState.READY
    >>> conn.peer_node_id = received_node_id
"""

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Literal

from reactivex.disposable import CompositeDisposable

from .stream import StreamTable


class ConnectionState(Enum):
    """Connection lifecycle states.

    State transitions:
        CONNECTING → HANDSHAKING: Transport connected (outbound only)
        HANDSHAKING → READY: HELLO/HELLO_ACK exchange complete
        READY → CLOSED: Connection terminated
        HANDSHAKING → CLOSED: Handshake failed or connection dropped
        CONNECTING → CLOSED: Connection failed (outbound only)
    """

    CONNECTING = auto()  # Transport connecting (outbound only)
    HANDSHAKING = auto()  # HELLO exchange in progress
    READY = auto()  # Handshake complete, ready for traffic
    CLOSED = auto()  # Connection terminated


@dataclass
class Connection:
    """Base connection abstraction for gateway peers.

    Represents a single connection to a remote node, regardless of
    whether it was initiated by us (outbound) or by the remote (inbound).

    Attributes:
        conn_id: 128-bit connection identifier (16 bytes UUID)
        direction: Whether this is an inbound or outbound connection
        remote_addr: Remote host and port tuple
        state: Current connection state
        peer_node_id: Remote node's ID (set after handshake completes)
        streams: Per-connection stream table
        created_at: Timestamp when connection was created
    """

    conn_id: bytes
    direction: Literal["inbound", "outbound"]
    remote_addr: tuple[str, int]
    state: ConnectionState = ConnectionState.CONNECTING
    peer_node_id: bytes | None = None
    streams: StreamTable = field(default_factory=StreamTable)
    created_at: float = field(default_factory=time.time)

    # Internal fields (not part of public API)
    _duplex: Any | None = field(default=None, repr=False)
    _ws_client: Any | None = field(default=None, repr=False)
    _disposables: CompositeDisposable = field(
        default_factory=CompositeDisposable, repr=False
    )
    _hello_received: bool = field(default=False, repr=False)
    _hello_ack_received: bool = field(default=False, repr=False)

    def __post_init__(self):
        if len(self.conn_id) != 16:
            raise ValueError(f"conn_id must be 16 bytes, got {len(self.conn_id)}")


@dataclass
class HelloPayload:
    """Handshake payload for HELLO/HELLO_ACK messages.

    Contains the node identity and protocol version for handshake exchange.
    This is a protocol-agnostic representation that can be serialized
    using any framing format.

    Attributes:
        node_id: 128-bit node identifier (16 bytes UUID)
        protocol_version: Protocol version (default: 1)
    """

    node_id: bytes
    protocol_version: int = 1

    def __post_init__(self):
        if len(self.node_id) != 16:
            raise ValueError(f"node_id must be 16 bytes, got {len(self.node_id)}")
