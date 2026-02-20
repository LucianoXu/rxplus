"""Buffer overflow strategies for reactive streams.

This module implements buffer overflow handling strategies for subscribers
that fall behind in broadcast/hot stream mode. Each strategy determines
what happens when a subscriber's buffer reaches capacity.

Strategies:
    - DropOld: Drop oldest items when buffer full (real-time default)
    - DropNew: Drop new items when buffer full
    - BufferK: Fixed-size buffer, raises OverflowError when exceeded
    - Disconnect: Disconnect on overflow

All strategies are thread-safe and can be used concurrently.

Example:
    >>> from rxplus.gateway import DropOld, DropNew, BufferK, Disconnect
    >>>
    >>> # Real-time streaming - keep latest data
    >>> buffer = DropOld(max_size=100)
    >>> dropped = buffer.push(item)  # Returns dropped item or None
    >>>
    >>> # Historical order matters
    >>> buffer = DropNew(max_size=100)
    >>> rejected = buffer.push(item)  # Returns rejected item or None
    >>>
    >>> # Strict - no silent data loss
    >>> buffer = BufferK(k=100)
    >>> buffer.push(item)  # Raises OverflowError when full
    >>>
    >>> # Disconnect on overflow
    >>> buffer = Disconnect(max_size=100)
    >>> buffer.push(item)  # Raises DisconnectError when full
"""

import threading
from abc import ABC, abstractmethod
from collections import deque
from typing import TypeVar

T = TypeVar("T")


class OverflowPolicy[T](ABC):
    """Abstract base class for overflow handling strategies.

    Each strategy must implement thread-safe push, pop, and management
    operations for a bounded buffer.
    """

    @abstractmethod
    def push(self, item: T) -> T | None:
        """Push an item into the buffer.

        Args:
            item: Item to add to buffer

        Returns:
            The dropped item if overflow occurred, None otherwise.
            For BufferK, raises OverflowError instead of returning.
        """
        ...

    @abstractmethod
    def pop(self) -> T | None:
        """Pop the next item from the buffer.

        Returns:
            Next item in FIFO order, or None if buffer is empty.
        """
        ...

    @abstractmethod
    def is_full(self) -> bool:
        """Check if buffer is at capacity.

        Returns:
            True if buffer has reached its limit.
        """
        ...

    @abstractmethod
    def clear(self) -> list[T]:
        """Clear the buffer and return all items.

        Returns:
            List of all items that were in the buffer.
        """
        ...

    @abstractmethod
    def size(self) -> int:
        """Get current number of items in buffer.

        Returns:
            Current buffer size.
        """
        ...

    @abstractmethod
    def capacity(self) -> int:
        """Get maximum buffer capacity.

        Returns:
            Maximum number of items buffer can hold.
        """
        ...


class DropOld(OverflowPolicy[T]):
    """Drop oldest items when buffer full (real-time default).

    This is the most common strategy for real-time streams where
    the latest data is most important. When the buffer is full,
    the oldest item is automatically removed to make room for
    the new item.

    Example:
        >>> buffer = DropOld(max_size=3)
        >>> buffer.push(1)  # None (no overflow)
        >>> buffer.push(2)  # None
        >>> buffer.push(3)  # None (buffer full: [1, 2, 3])
        >>> buffer.push(4)  # Returns 1 (dropped), buffer: [2, 3, 4]
    """

    def __init__(self, max_size: int):
        """Initialize DropOld buffer.

        Args:
            max_size: Maximum number of items to hold

        Raises:
            ValueError: If max_size < 1
        """
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._max_size = max_size
        self._buffer: deque[T] = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def push(self, item: T) -> T | None:
        """Push item, dropping oldest if full.

        Returns:
            The dropped item if buffer was full, None otherwise.
        """
        with self._lock:
            dropped = None
            if len(self._buffer) == self._max_size:
                dropped = self._buffer[0]
            self._buffer.append(item)  # deque with maxlen auto-drops oldest
            return dropped

    def pop(self) -> T | None:
        """Pop oldest item from buffer."""
        with self._lock:
            return self._buffer.popleft() if self._buffer else None

    def is_full(self) -> bool:
        """Check if buffer is at capacity."""
        with self._lock:
            return len(self._buffer) == self._max_size

    def clear(self) -> list[T]:
        """Clear buffer and return all items."""
        with self._lock:
            items = list(self._buffer)
            self._buffer.clear()
            return items

    def size(self) -> int:
        """Get current buffer size."""
        with self._lock:
            return len(self._buffer)

    def capacity(self) -> int:
        """Get buffer capacity."""
        return self._max_size

    def peek(self) -> T | None:
        """Peek at oldest item without removing."""
        with self._lock:
            return self._buffer[0] if self._buffer else None


class DropNew(OverflowPolicy[T]):
    """Drop new items when buffer full.

    This strategy preserves earlier data and rejects new items
    when the buffer is at capacity. Useful when historical order
    matters more than latest updates.

    Example:
        >>> buffer = DropNew(max_size=3)
        >>> buffer.push(1)  # None
        >>> buffer.push(2)  # None
        >>> buffer.push(3)  # None (buffer full: [1, 2, 3])
        >>> buffer.push(4)  # Returns 4 (rejected), buffer unchanged
    """

    def __init__(self, max_size: int):
        """Initialize DropNew buffer.

        Args:
            max_size: Maximum number of items to hold

        Raises:
            ValueError: If max_size < 1
        """
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._max_size = max_size
        self._buffer: deque[T] = deque()
        self._lock = threading.Lock()

    def push(self, item: T) -> T | None:
        """Push item, rejecting if full.

        Returns:
            The new item if buffer was full (item rejected), None otherwise.
        """
        with self._lock:
            if len(self._buffer) >= self._max_size:
                return item  # Reject the new item
            self._buffer.append(item)
            return None

    def pop(self) -> T | None:
        """Pop oldest item from buffer."""
        with self._lock:
            return self._buffer.popleft() if self._buffer else None

    def is_full(self) -> bool:
        """Check if buffer is at capacity."""
        with self._lock:
            return len(self._buffer) >= self._max_size

    def clear(self) -> list[T]:
        """Clear buffer and return all items."""
        with self._lock:
            items = list(self._buffer)
            self._buffer.clear()
            return items

    def size(self) -> int:
        """Get current buffer size."""
        with self._lock:
            return len(self._buffer)

    def capacity(self) -> int:
        """Get buffer capacity."""
        return self._max_size


class BufferK(OverflowPolicy[T]):
    """Fixed-size buffer that raises OverflowError when exceeded.

    This is a strict strategy that signals overflow as an error
    condition rather than silently dropping items. Useful when
    data loss is unacceptable and the application needs to handle
    overflow explicitly.

    Example:
        >>> buffer = BufferK(k=3)
        >>> buffer.push(1)  # None
        >>> buffer.push(2)  # None
        >>> buffer.push(3)  # None (buffer full: [1, 2, 3])
        >>> buffer.push(4)  # Raises OverflowError
    """

    def __init__(self, k: int):
        """Initialize BufferK buffer.

        Args:
            k: Maximum number of items to hold

        Raises:
            ValueError: If k < 1
        """
        if k < 1:
            raise ValueError(f"k must be >= 1, got {k}")
        self._k = k
        self._buffer: deque[T] = deque()
        self._lock = threading.Lock()

    def push(self, item: T) -> T | None:
        """Push item, raising OverflowError if full.

        Returns:
            None if item was added successfully.

        Raises:
            OverflowError: If buffer is at capacity.
        """
        with self._lock:
            if len(self._buffer) >= self._k:
                raise OverflowError(f"Buffer exceeded limit of {self._k}")
            self._buffer.append(item)
            return None

    def pop(self) -> T | None:
        """Pop oldest item from buffer."""
        with self._lock:
            return self._buffer.popleft() if self._buffer else None

    def is_full(self) -> bool:
        """Check if buffer is at capacity."""
        with self._lock:
            return len(self._buffer) >= self._k

    def clear(self) -> list[T]:
        """Clear buffer and return all items."""
        with self._lock:
            items = list(self._buffer)
            self._buffer.clear()
            return items

    def size(self) -> int:
        """Get current buffer size."""
        with self._lock:
            return len(self._buffer)

    def capacity(self) -> int:
        """Get buffer capacity."""
        return self._k


class DisconnectError(Exception):
    """Raised when Disconnect overflow policy signals connection should close."""

    pass


class Disconnect(OverflowPolicy[T]):
    """Disconnect strategy - signals overflow by raising exception.

    This strategy is strict: when the buffer is full, it raises
    a DisconnectError to signal that the connection should be closed.
    The actual connection closing must be handled at the transport layer.

    Example:
        >>> buffer = Disconnect(max_size=3)
        >>> buffer.push(1)  # None
        >>> buffer.push(2)  # None
        >>> buffer.push(3)  # None (buffer full)
        >>> buffer.push(4)  # Raises DisconnectError
    """

    def __init__(self, max_size: int):
        """Initialize Disconnect buffer.

        Args:
            max_size: Maximum number of items before disconnect signal

        Raises:
            ValueError: If max_size < 1
        """
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._max_size = max_size
        self._buffer: deque[T] = deque()
        self._lock = threading.Lock()

    def push(self, item: T) -> T | None:
        """Push item, raising DisconnectError if full.

        Returns:
            None if item was added successfully.

        Raises:
            DisconnectError: If buffer is at capacity (signals disconnect).
        """
        with self._lock:
            if len(self._buffer) >= self._max_size:
                raise DisconnectError(
                    f"Buffer overflow at limit {self._max_size}, disconnect required"
                )
            self._buffer.append(item)
            return None

    def pop(self) -> T | None:
        """Pop oldest item from buffer."""
        with self._lock:
            return self._buffer.popleft() if self._buffer else None

    def is_full(self) -> bool:
        """Check if buffer is at capacity."""
        with self._lock:
            return len(self._buffer) >= self._max_size

    def clear(self) -> list[T]:
        """Clear buffer and return all items."""
        with self._lock:
            items = list(self._buffer)
            self._buffer.clear()
            return items

    def size(self) -> int:
        """Get current buffer size."""
        with self._lock:
            return len(self._buffer)

    def capacity(self) -> int:
        """Get buffer capacity."""
        return self._max_size


def create_overflow_policy(
    strategy: str,
    buffer_limit: int = 100,
) -> OverflowPolicy:
    """Factory function to create overflow policy from string name.

    Args:
        strategy: One of "drop_old", "drop_new", "buffer_k", "disconnect"
        buffer_limit: Maximum buffer size

    Returns:
        Configured OverflowPolicy instance

    Raises:
        ValueError: If strategy is not recognized
    """
    if strategy == "drop_old":
        return DropOld(max_size=buffer_limit)
    elif strategy == "drop_new":
        return DropNew(max_size=buffer_limit)
    elif strategy == "buffer_k":
        return BufferK(k=buffer_limit)
    elif strategy == "disconnect":
        return Disconnect(max_size=buffer_limit)
    else:
        raise ValueError(f"Unknown overflow strategy: {strategy}")
