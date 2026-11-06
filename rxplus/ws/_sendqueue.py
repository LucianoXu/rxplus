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

    def __init__(self, limit: int | None, overflow: SendOverflow) -> None:
        if limit is not None and limit < 1:
            raise ValueError(f"send_queue must be at least 1 or None: {limit!r}")
        if overflow not in ("drop_old", "drop_new", "disconnect"):
            raise ValueError(f"unknown overflow policy: {overflow!r}")
        self.limit = limit
        self.overflow: SendOverflow = overflow
        self.dropped = 0  # frames discarded by the overflow policy
        self._frames: deque[str | bytes] = deque()
        self._ready = asyncio.Event()
        self._overflowed = False

    def __len__(self) -> int:
        return len(self._frames)

    def put(self, frame: str | bytes) -> bool:
        """Queue a frame; False when the policy says to disconnect."""
        if self.limit is not None and len(self._frames) >= self.limit:
            if self.overflow == "drop_old":
                self._frames.popleft()
                self.dropped += 1
            elif self.overflow == "drop_new":
                self.dropped += 1
                return True
            else:
                self._overflowed = True
                self._ready.set()
                return False
        self._frames.append(frame)
        self._ready.set()
        return True

    async def get(self) -> str | bytes:
        """The next frame; raises :class:`QueueOverflow` after a disconnect
        overflow."""
        while True:
            if self._overflowed:
                raise QueueOverflow(f"send queue exceeded {self.limit} frames")
            if self._frames:
                return self._frames.popleft()
            self._ready.clear()
            await self._ready.wait()

    def reset(self) -> None:
        """Forget the overflow state and the queued frames (on reconnect)."""
        self._frames.clear()
        self._overflowed = False
