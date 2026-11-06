"""Bounded per-connection send queue (used on a component's loop thread only)."""

import asyncio
from collections import deque
from typing import Literal

#: What a full send queue does with one more frame.
#: ``drop_old``: discard the oldest queued frame; ``drop_new``: discard the
#: new one; ``disconnect``: close the connection.
SendOverflow = Literal["drop_old", "drop_new", "disconnect"]


class QueueOverflow(Exception):
    """The queue overflowed under the ``disconnect`` policy."""


class SendQueue:
    """Frames waiting for one connection; not thread-safe (loop thread only).

    ``limit=None`` means unbounded (the 0.1 behaviour).
    """

    def __init__(
        self,
        limit: int | None,
        overflow: SendOverflow,
        limit_bytes: int | None = None,
    ) -> None:
        if limit is not None and limit < 1:
            raise ValueError(f"send_queue must be at least 1 or None: {limit!r}")
        if limit_bytes is not None and limit_bytes < 1:
            raise ValueError(
                f"send_queue_bytes must be at least 1 or None: {limit_bytes!r}"
            )
        if overflow not in ("drop_old", "drop_new", "disconnect"):
            raise ValueError(f"unknown overflow policy: {overflow!r}")
        self.limit = limit
        self.limit_bytes = limit_bytes
        self.overflow: SendOverflow = overflow
        self._bytes = 0
        self.dropped = 0  # frames discarded by the overflow policy
        self._frames: deque[str | bytes] = deque()
        self._ready = asyncio.Event()
        self._overflowed = False

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def overflowed(self) -> bool:
        """The ``disconnect`` policy fired (until :meth:`reset`)."""
        return self._overflowed

    def _full(self, size: int) -> bool:
        if self.limit is not None and len(self._frames) >= self.limit:
            return True
        return (
            self.limit_bytes is not None
            and bool(self._frames)
            and self._bytes + size > self.limit_bytes
        )

    def put(self, frame: str | bytes, overflow: SendOverflow | None = None) -> bool:
        """Queue a frame; False when the policy says to disconnect.

        ``overflow`` overrides the queue's policy for this frame (a client
        buffering while offline uses ``"drop_old"``).
        """
        policy = overflow or self.overflow
        size = len(frame)
        while self._full(size):
            if policy == "drop_new":
                self.dropped += 1
                return True
            if policy == "disconnect":
                self._overflowed = True
                self._ready.set()
                return False
            old = self._frames.popleft()  # drop_old
            self._bytes -= len(old)
            self.dropped += 1
        self._frames.append(frame)
        self._bytes += size
        self._ready.set()
        return True

    async def get(self) -> str | bytes:
        """The next frame; raises :class:`QueueOverflow` after a disconnect
        overflow."""
        while True:
            if self._overflowed:
                raise QueueOverflow(f"send queue exceeded {self.limit} frames")
            if self._frames:
                frame = self._frames.popleft()
                self._bytes -= len(frame)
                return frame
            self._ready.clear()
            await self._ready.wait()

    def reset(self) -> int:
        """Forget the overflow state and the queued frames; return how many
        frames were discarded."""
        discarded = len(self._frames)
        self._frames.clear()
        self._bytes = 0
        self._overflowed = False
        return discarded
