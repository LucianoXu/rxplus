"""Bounded per-connection send queue (used on a component's loop thread only)."""

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from ..reacting.handles import Link

#: What a full send queue does with one more frame.
#: ``drop_old``: discard the oldest queued frame; ``drop_new``: discard the
#: new one; ``disconnect``: close the connection.
SendOverflow = Literal["drop_old", "drop_new", "disconnect"]


def wire_size(frame: str | bytes) -> int:
    """Bytes of a frame's payload on the wire (text frames are UTF-8)."""
    return len(frame) if isinstance(frame, bytes) else len(frame.encode())


class LiveFlag(Protocol):
    """A reporter's live state (``link._st``), or :data:`OFF`."""

    live: bool


class _Off:
    """Stands in for a reporter's live state while the queue reports
    nowhere: one attribute check per frame either way."""

    live: bool = False


_OFF = _Off()
OFF: LiveFlag = _OFF  # for components: one check per item when unreporting


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
        # Reporting (X4): the link this queue feeds and its reporter's live
        # state (_OFF when unbound: one check per frame), one enter() token
        # per queued frame, always kept aligned with the frames (-1.0 when
        # not live), and the token of the frame taken by get().
        self._link: Link | None = None
        self._st: LiveFlag = _OFF
        self._tokens: deque[float] = deque()
        self._taken = -1.0

    def bind(self, link: "Link | None") -> None:
        """Report on ``link`` (a socket's send link), or stop (``None``).
        The frames already queued enter it now."""
        self._link = link
        self._st = _OFF if link is None else link._st
        self._taken = -1.0
        self._tokens = deque(self._enter() for _ in self._frames)

    def _enter(self) -> float:
        link = self._link
        return link.enter() if self._st.live and link is not None else -1.0

    def sent(self, frame: str | bytes) -> None:
        """The frame taken by :meth:`get` was sent: flow, latency, backlog."""
        link = self._link
        if self._st.live and link is not None:
            link.leave(self._taken, bytes=wire_size(frame))

    def lost(self, taken: bool = False) -> None:
        """A frame was lost: the one taken by :meth:`get` (``taken``), or one
        refused before it was queued."""
        link = self._link
        if self._st.live and link is not None:
            if taken:
                link.leave_dropped(self._taken)
            else:
                link.drop()

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
                self.lost()
                return True
            if policy == "disconnect":
                self._overflowed = True
                self._ready.set()
                return False
            old = self._frames.popleft()  # drop_old
            self._bytes -= len(old)
            self.dropped += 1
            self._taken = self._tokens.popleft()
            self.lost(taken=True)
        self._frames.append(frame)
        self._bytes += size
        self._tokens.append(self._enter())
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
                self._taken = self._tokens.popleft()
                return frame
            self._ready.clear()
            await self._ready.wait()

    def reset(self) -> int:
        """Forget the overflow state and the queued frames; return how many
        frames were discarded."""
        discarded = len(self._frames)
        for token in self._tokens:
            self._taken = token
            self.lost(taken=True)
        self._tokens.clear()
        self._frames.clear()
        self._bytes = 0
        self._overflowed = False
        return discarded
