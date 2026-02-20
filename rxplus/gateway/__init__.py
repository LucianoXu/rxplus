"""Gateway layer for reactive peer-to-peer communication.

This package provides generic infrastructure for building protocol-specific
reactive gateways with:
    - Buffer overflow strategies (DropOld, DropNew, BufferK, Disconnect)
    - Stream lifecycle management (INIT → OPEN → ACTIVE → CLOSED)
    - Connection state tracking with HELLO/HELLO_ACK handshake
    - Tagged frame routing for multiplexed connections
    - Symmetric peer-to-peer node architecture

Example:
    >>> from rxplus.gateway import GatewayNode, TaggedFrame
    >>>
    >>> # Create a custom framing class implementing the Framing protocol
    >>> class MyFraming:
    ...     @classmethod
    ...     def encode(cls, obj: "MyFraming") -> bytes: ...
    ...     @classmethod
    ...     def decode(cls, data: bytes) -> "MyFraming": ...
    ...     @classmethod
    ...     def create_hello(cls, node_id: bytes) -> "MyFraming": ...
    ...     @classmethod
    ...     def create_hello_ack(cls, node_id: bytes) -> "MyFraming": ...
    ...     def is_hello(self) -> bool: ...
    ...     def is_hello_ack(self) -> bool: ...
    ...     def get_hello_node_id(self) -> bytes | None: ...
    >>>
    >>> # Use with GatewayNode
    >>> node = GatewayNode[MyFraming](
    ...     host="::", port=8765, framing_cls=MyFraming
    ... )
"""

# Overflow strategies
# Connection management
from .connection import (
    Connection,
    ConnectionState,
    HelloPayload,
)

# Framing abstraction
from .framing import (
    Framing,
    TaggedFrame,
)

# Gateway node
from .node import (
    GatewayNode,
)
from .overflow import (
    BufferK,
    Disconnect,
    DisconnectError,
    DropNew,
    DropOld,
    OverflowPolicy,
    create_overflow_policy,
)

# Stream management
from .stream import (
    OverflowStrategy,
    StreamInfo,
    StreamState,
    StreamTable,
)

__all__ = [
    # Overflow
    "OverflowPolicy",
    "DropOld",
    "DropNew",
    "BufferK",
    "Disconnect",
    "DisconnectError",
    "create_overflow_policy",
    # Stream
    "OverflowStrategy",
    "StreamState",
    "StreamInfo",
    "StreamTable",
    # Connection
    "ConnectionState",
    "Connection",
    "HelloPayload",
    # Framing
    "Framing",
    "TaggedFrame",
    # Node
    "GatewayNode",
]
