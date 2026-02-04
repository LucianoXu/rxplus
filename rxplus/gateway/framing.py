"""Abstract framing protocol for gateway communication.

This module defines the contract for binary frame encoding/decoding,
allowing different protocols to implement their own framing formats
while reusing the gateway infrastructure.

The Framing protocol specifies:
    - encode/decode: Convert between objects and bytes
    - create_hello/create_hello_ack: Create handshake frames
    - is_hello/is_hello_ack: Detect handshake frames
    - get_hello_node_id: Extract node ID from handshake frames

Example:
    >>> from rxplus.gateway import Framing, TaggedFrame
    >>> 
    >>> # Implement a custom framing class
    >>> class MyFraming:
    ...     @classmethod
    ...     def encode(cls, obj: "MyFraming") -> bytes:
    ...         return obj.to_bytes()
    ...     
    ...     @classmethod
    ...     def decode(cls, data: bytes) -> "MyFraming":
    ...         return cls.from_bytes(data)
    ...     
    ...     @classmethod
    ...     def create_hello(cls, node_id: bytes) -> "MyFraming":
    ...         return cls(type="hello", payload=node_id)
    ...     
    ...     @classmethod
    ...     def create_hello_ack(cls, node_id: bytes) -> "MyFraming":
    ...         return cls(type="hello_ack", payload=node_id)
    ...     
    ...     def is_hello(self) -> bool:
    ...         return self.type == "hello"
    ...     
    ...     def is_hello_ack(self) -> bool:
    ...         return self.type == "hello_ack"
    ...     
    ...     def get_hello_node_id(self) -> bytes | None:
    ...         if self.is_hello() or self.is_hello_ack():
    ...             return self.payload
    ...         return None
    >>> 
    >>> # Use with TaggedFrame
    >>> frame = MyFraming(type="data", payload=b"hello")
    >>> tagged = TaggedFrame(frame=frame, conn_id=conn_id)
"""

from dataclasses import dataclass
from typing import Generic, Optional, Protocol, TypeVar, runtime_checkable


T = TypeVar("T")
F = TypeVar("F", bound="Framing", covariant=True)


@runtime_checkable
class Framing(Protocol):
    """Protocol for binary frame encoding/decoding.
    
    Implementers must provide:
        - encode: Serialize frame to bytes
        - decode: Deserialize bytes to frame
        - create_hello: Create HELLO handshake frame
        - create_hello_ack: Create HELLO_ACK handshake frame
        - is_hello: Check if frame is HELLO
        - is_hello_ack: Check if frame is HELLO_ACK
        - get_hello_node_id: Extract node ID from handshake frame
    """
    
    @classmethod
    def encode(cls, obj: "Framing") -> bytes:
        """Encode frame to binary format.
        
        Args:
            obj: Frame object to encode
            
        Returns:
            Binary-encoded frame
        """
        ...
    
    @classmethod
    def decode(cls, data: bytes) -> "Framing":
        """Decode binary data to frame.
        
        Args:
            data: Binary-encoded frame
            
        Returns:
            Decoded frame object
            
        Raises:
            ValueError: If data is malformed
        """
        ...
    
    @classmethod
    def create_hello(cls, node_id: bytes) -> "Framing":
        """Create a HELLO handshake frame.
        
        Args:
            node_id: 128-bit node identifier (16 bytes)
            
        Returns:
            HELLO frame containing node_id
        """
        ...
    
    @classmethod
    def create_hello_ack(cls, node_id: bytes) -> "Framing":
        """Create a HELLO_ACK handshake frame.
        
        Args:
            node_id: 128-bit node identifier (16 bytes)
            
        Returns:
            HELLO_ACK frame containing node_id
        """
        ...
    
    def is_hello(self) -> bool:
        """Check if this frame is a HELLO handshake frame.
        
        Returns:
            True if this is a HELLO frame
        """
        ...
    
    def is_hello_ack(self) -> bool:
        """Check if this frame is a HELLO_ACK handshake frame.
        
        Returns:
            True if this is a HELLO_ACK frame
        """
        ...
    
    def get_hello_node_id(self) -> Optional[bytes]:
        """Extract node ID from handshake frame.
        
        Returns:
            16-byte node ID if this is a HELLO or HELLO_ACK frame,
            None otherwise
        """
        ...


@dataclass
class TaggedFrame(Generic[T]):
    """Frame with source connection identification.
    
    Wraps a frame with metadata about which connection it came from,
    enabling routing and reply functionality.
    
    Type Parameters:
        T: The frame type (e.g., Envelope for AICP)
    
    Attributes:
        frame: The framed message
        conn_id: Connection ID that sent this frame (16 bytes)
        peer_node_id: Node ID of the sending peer (if handshake complete)
    """
    frame: T
    conn_id: bytes
    peer_node_id: Optional[bytes] = None
    
    def __post_init__(self):
        if len(self.conn_id) != 16:
            raise ValueError(f"conn_id must be 16 bytes, got {len(self.conn_id)}")
