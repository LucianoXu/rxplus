"""A bounded, thread-safe FIFO buffer with an overflow strategy.

0.2 replaces the four 0.1 classes (``DropOld``, ``DropNew``, ``BufferK``,
``Disconnect``), which were the same locked deque four times, with one
:class:`BoundedBuffer` (audit F21).

Strategies when the buffer is full:

- ``"drop_old"``: drop the oldest item to make room (``push`` returns it);
- ``"drop_new"``: refuse the new item (``push`` returns it);
- ``"buffer_k"``: raise :class:`OverflowError`;
- ``"disconnect"``: raise :class:`DisconnectError` (the caller closes the
  connection).

Example:
    >>> buf = BoundedBuffer[int](3, "drop_old")
    >>> [buf.push(i) for i in range(4)]
    [None, None, None, 0]
    >>> buf.pop()
    1
"""

import threading
from collections import deque
from typing import Literal

#: Overflow strategies of :class:`BoundedBuffer`.
OverflowStrategy = Literal["drop_old", "drop_new", "buffer_k", "disconnect"]


class DisconnectError(Exception):
    """Raised by a ``"disconnect"`` buffer: the connection should be closed."""


class BoundedBuffer[T]:
    """A FIFO of at most ``limit`` items; full: apply ``strategy``. Thread-safe."""

    def __init__(self, limit: int, strategy: OverflowStrategy = "drop_old") -> None:
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if strategy not in ("drop_old", "drop_new", "buffer_k", "disconnect"):
            raise ValueError(f"Unknown overflow strategy: {strategy}")
        self._limit = limit
        self._strategy: OverflowStrategy = strategy
        self._items: deque[T] = deque()
        self._lock = threading.Lock()

    @property
    def strategy(self) -> OverflowStrategy:
        return self._strategy

    def push(self, item: T) -> T | None:
        """Add ``item``; returns the item dropped by the strategy, if any.

        Raises:
            OverflowError: full, strategy ``"buffer_k"``.
            DisconnectError: full, strategy ``"disconnect"``.
        """
        with self._lock:
            if len(self._items) < self._limit:
                self._items.append(item)
                return None
            if self._strategy == "drop_old":
                dropped = self._items.popleft()
                self._items.append(item)
                return dropped
            if self._strategy == "drop_new":
                return item
            if self._strategy == "buffer_k":
                raise OverflowError(f"Buffer exceeded limit of {self._limit}")
            raise DisconnectError(
                f"Buffer overflow at limit {self._limit}, disconnect required"
            )

    def pop(self) -> T:
        """Remove and return the oldest item.

        Raises:
            IndexError: the buffer is empty (0.1 returned ``None``, which was
                ambiguous for buffers holding ``None``).
        """
        with self._lock:
            if not self._items:
                raise IndexError("pop from an empty BoundedBuffer")
            return self._items.popleft()

    def peek(self) -> T:
        """The oldest item, not removed. Raises ``IndexError`` when empty."""
        with self._lock:
            if not self._items:
                raise IndexError("peek into an empty BoundedBuffer")
            return self._items[0]

    def is_full(self) -> bool:
        with self._lock:
            return len(self._items) >= self._limit

    def clear(self) -> list[T]:
        """Remove and return all items, oldest first."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    def size(self) -> int:
        with self._lock:
            return len(self._items)

    def __len__(self) -> int:
        return self.size()

    def capacity(self) -> int:
        return self._limit


def create_overflow_policy(
    strategy: OverflowStrategy, buffer_limit: int = 100
) -> BoundedBuffer[object]:
    """A :class:`BoundedBuffer` for ``strategy`` (kept for 0.1 callers)."""
    return BoundedBuffer(buffer_limit, strategy)
