"""Stream lifecycle management for reactive gateways.

This module implements stream lifecycle management with:
    - State machine: INIT → OPEN → ACTIVE → CLOSED
    - Per-stream metadata tracking (content_type, overflow policy)
    - Sequence number management (sending and validation)
    - Gap detection for broadcast mode

State transitions:
    INIT → OPEN: Stream opened by publisher
    OPEN → ACTIVE: Subscriber joined with overflow policy
    ACTIVE → CLOSED: Stream completed/errored/cancelled
    OPEN → CLOSED: Stream closed before any subscribers

All operations are thread-safe via internal locking.

Example:
    >>> from rxplus.gateway import StreamTable, StreamState
    >>> 
    >>> table = StreamTable()
    >>> info = table.open_stream(stream_id, content_type="text/plain")
    >>> info = table.subscribe(stream_id, overflow="drop_old", buffer_limit=50)
    >>> seq = table.next_seq(stream_id)  # Get seq for sending
    >>> has_gap = table.validate_seq(stream_id, received_seq)  # Check received
    >>> table.close_stream(stream_id)
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, Literal, Optional
import threading
import logging

# Type alias for overflow strategies
OverflowStrategy = Literal["drop_old", "drop_new", "buffer_k", "disconnect"]

logger = logging.getLogger("rxplus.gateway.stream")


class StreamState(Enum):
    """Stream lifecycle states.
    
    State transitions:
        INIT → OPEN: Stream opened by publisher
        OPEN → ACTIVE: Subscriber joined with overflow policy
        ACTIVE → CLOSED: Stream completed/errored/cancelled
        OPEN → CLOSED: Stream closed before any subscribers
        
    Terminal state: CLOSED (no transitions out)
    """
    INIT = auto()     # Initial state (not yet opened)
    OPEN = auto()     # Opened by publisher, awaiting subscribers
    ACTIVE = auto()   # Has active subscribers
    CLOSED = auto()   # Terminal state


@dataclass
class StreamInfo:
    """Per-stream metadata and state.
    
    Attributes:
        stream_id: 128-bit stream identifier (16 bytes)
        state: Current lifecycle state
        content_type: MIME type of stream content
        delivery_mode: Delivery semantics ("broadcast" for hot streams)
        local_seq: Next sequence number to assign (for sending)
        expected_seq: Next sequence number expected (for receiving)
        overflow_strategy: Subscriber's overflow handling strategy
        buffer_limit: Maximum buffer size for overflow
        gap_count: Number of detected sequence gaps
        created_at: Timestamp when stream was created (microseconds)
    """
    stream_id: bytes
    state: StreamState = StreamState.INIT
    content_type: str = "application/octet-stream"
    delivery_mode: str = "broadcast"
    local_seq: int = 0
    expected_seq: int = 0
    overflow_strategy: OverflowStrategy = "drop_old"
    buffer_limit: int = 100
    gap_count: int = 0
    created_at: int = 0
    
    def __post_init__(self):
        if len(self.stream_id) != 16:
            raise ValueError(f"stream_id must be 16 bytes, got {len(self.stream_id)}")


class StreamTable:
    """Thread-safe stream state management.
    
    Manages multiple streams indexed by stream_id. Provides atomic
    operations for stream lifecycle events and sequence number management.
    
    Example:
        >>> table = StreamTable()
        >>> info = table.open_stream(stream_id, content_type="text/plain")
        >>> info = table.subscribe(stream_id, overflow="drop_old", buffer_limit=50)
        >>> seq = table.next_seq(stream_id)  # Get seq for sending
        >>> has_gap = table.validate_seq(stream_id, received_seq)  # Check received
        >>> table.close_stream(stream_id)
    """
    
    def __init__(self):
        """Initialize empty stream table."""
        self._streams: Dict[bytes, StreamInfo] = {}
        self._lock = threading.RLock()  # Reentrant for nested calls
    
    def open_stream(
        self,
        stream_id: bytes,
        content_type: str = "application/octet-stream",
        delivery_mode: str = "broadcast",
        created_at: int = 0,
    ) -> StreamInfo:
        """Create or reopen a stream.
        
        Args:
            stream_id: 128-bit stream identifier
            content_type: MIME type of stream content
            delivery_mode: Delivery semantics (default: "broadcast")
            created_at: Creation timestamp in microseconds (optional)
            
        Returns:
            StreamInfo for the opened stream
            
        Raises:
            ValueError: If stream exists and is not CLOSED
        """
        with self._lock:
            if stream_id in self._streams:
                info = self._streams[stream_id]
                if info.state != StreamState.CLOSED:
                    raise ValueError(
                        f"Stream {stream_id.hex()} already open (state={info.state.name})"
                    )
            
            info = StreamInfo(
                stream_id=stream_id,
                content_type=content_type,
                delivery_mode=delivery_mode,
                created_at=created_at,
            )
            info.state = StreamState.OPEN
            self._streams[stream_id] = info
            
            logger.info(
                f"Stream opened: {stream_id.hex()[:16]}..., "
                f"content_type={content_type}, delivery_mode={delivery_mode}"
            )
            return info
    
    def subscribe(
        self,
        stream_id: bytes,
        overflow: OverflowStrategy = "drop_old",
        buffer_limit: int = 100,
    ) -> StreamInfo:
        """Subscribe to a stream with overflow policy.
        
        Transitions stream from OPEN to ACTIVE state.
        
        Args:
            stream_id: 128-bit stream identifier
            overflow: Overflow strategy when subscriber falls behind
            buffer_limit: Maximum buffer size
            
        Returns:
            Updated StreamInfo
            
        Raises:
            KeyError: If stream does not exist
            ValueError: If stream is not in OPEN or ACTIVE state
        """
        with self._lock:
            info = self._get_or_raise(stream_id)
            
            if info.state not in (StreamState.OPEN, StreamState.ACTIVE):
                raise ValueError(
                    f"Cannot subscribe to stream in state {info.state.name}"
                )
            
            info.overflow_strategy = overflow
            info.buffer_limit = buffer_limit
            info.state = StreamState.ACTIVE
            
            logger.info(
                f"Stream subscribed: {stream_id.hex()[:16]}..., "
                f"overflow={overflow}, buffer_limit={buffer_limit}"
            )
            return info
    
    def next_seq(self, stream_id: bytes) -> int:
        """Get and increment sequence number for sending.
        
        Thread-safe atomic get-and-increment for outbound messages.
        
        Args:
            stream_id: 128-bit stream identifier
            
        Returns:
            Sequence number to use for next message
            
        Raises:
            KeyError: If stream does not exist
        """
        with self._lock:
            info = self._get_or_raise(stream_id)
            seq = info.local_seq
            info.local_seq += 1
            return seq
    
    def validate_seq(self, stream_id: bytes, received_seq: int) -> bool:
        """Validate received sequence number, detect gaps.
        
        In broadcast mode, gaps are allowed (indicates dropped messages
        due to overflow or network issues). This method tracks gaps
        for diagnostics.
        
        Args:
            stream_id: 128-bit stream identifier
            received_seq: Sequence number from received message
            
        Returns:
            True if a gap was detected, False if sequential
            
        Raises:
            KeyError: If stream does not exist
        """
        with self._lock:
            info = self._get_or_raise(stream_id)
            has_gap = received_seq != info.expected_seq
            
            if has_gap:
                info.gap_count += 1
                gap_size = received_seq - info.expected_seq
                logger.warning(
                    f"Seq gap detected: stream={stream_id.hex()[:16]}..., "
                    f"expected={info.expected_seq}, got={received_seq}, gap={gap_size}"
                )
            
            # Always advance to received + 1 (broadcast mode accepts gaps)
            info.expected_seq = received_seq + 1
            return has_gap
    
    def close_stream(self, stream_id: bytes) -> None:
        """Mark stream as closed.
        
        Terminal state - no further operations allowed on this stream
        until it is reopened.
        
        Args:
            stream_id: 128-bit stream identifier
            
        Raises:
            KeyError: If stream does not exist
        """
        with self._lock:
            info = self._get_or_raise(stream_id)
            previous_state = info.state
            info.state = StreamState.CLOSED
            
            logger.info(
                f"Stream closed: {stream_id.hex()[:16]}..., "
                f"previous_state={previous_state.name}, gap_count={info.gap_count}"
            )
    
    def get_stream(self, stream_id: bytes) -> Optional[StreamInfo]:
        """Get stream info if exists.
        
        Args:
            stream_id: 128-bit stream identifier
            
        Returns:
            StreamInfo if stream exists, None otherwise
        """
        with self._lock:
            return self._streams.get(stream_id)
    
    def has_stream(self, stream_id: bytes) -> bool:
        """Check if stream exists.
        
        Args:
            stream_id: 128-bit stream identifier
            
        Returns:
            True if stream exists (in any state)
        """
        with self._lock:
            return stream_id in self._streams
    
    def list_streams(self, state: Optional[StreamState] = None) -> list[bytes]:
        """List all stream IDs, optionally filtered by state.
        
        Args:
            state: Filter to streams in this state (None = all streams)
            
        Returns:
            List of stream IDs
        """
        with self._lock:
            if state is None:
                return list(self._streams.keys())
            return [
                sid for sid, info in self._streams.items()
                if info.state == state
            ]
    
    def active_count(self) -> int:
        """Count active streams.
        
        Returns:
            Number of streams in ACTIVE state
        """
        with self._lock:
            return sum(
                1 for info in self._streams.values()
                if info.state == StreamState.ACTIVE
            )
    
    def remove_stream(self, stream_id: bytes) -> Optional[StreamInfo]:
        """Remove a closed stream from the table.
        
        Can only remove streams in CLOSED state. Use this to clean up
        old streams and free memory.
        
        Args:
            stream_id: 128-bit stream identifier
            
        Returns:
            Removed StreamInfo, or None if not found/not closed
        """
        with self._lock:
            info = self._streams.get(stream_id)
            if info is None:
                return None
            if info.state != StreamState.CLOSED:
                logger.warning(
                    f"Cannot remove stream {stream_id.hex()[:16]}... "
                    f"in state {info.state.name}"
                )
                return None
            return self._streams.pop(stream_id, None)
    
    def _get_or_raise(self, stream_id: bytes) -> StreamInfo:
        """Get stream info or raise KeyError.
        
        Internal helper - caller must hold lock.
        """
        info = self._streams.get(stream_id)
        if info is None:
            raise KeyError(f"Unknown stream: {stream_id.hex()}")
        return info
