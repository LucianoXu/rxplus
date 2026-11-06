"""Sinks of a reacting/1 source: file, WebSocket, memory (standard library only).

A sink is a transport for encoded lines. The reporter's writer calls
:meth:`Sink.open` until it returns True (each True starts a new session),
then :meth:`Sink.write` with batches of lines; a :class:`SinkDisconnected`
ends the session. Every method of a sink is called from the writer only
(or from ``Reporter.poll`` with ``timer=False``), never from the observed
graph's threads.
"""

import json
import os
import ssl
import threading
from collections.abc import Sequence
from typing import Protocol

from ._websocket import HandshakeError, NotLoopbackError, WSConnection


class SinkDisconnected(Exception):
    """The connection is gone; the session on it has ended."""


class Sink(Protocol):
    def open(self) -> bool:
        """Open a connection. True: a new session starts on it."""
        ...

    def write(self, lines: Sequence[str]) -> None:
        """Write lines (no newline). Raises :class:`SinkDisconnected`."""
        ...

    def retry_delay(self) -> float | None:
        """Seconds to wait before the next :meth:`open`; None: never reopen."""
        ...

    def is_open(self) -> bool:
        """Whether the connection is still up (checked once per window, so an
        idle session ends promptly when the observer goes away)."""
        ...

    def close(self) -> None: ...


class JsonlFileSink:
    """A recording: one session, one signal per line, UTF-8, ``\\n``.

    The file is created at construction (mode ``"x"`` unless ``overwrite``),
    so a path that exists raises :class:`FileExistsError` immediately. One
    reporter per file. A write error ends the session for good.
    """

    def __init__(
        self, path: str | os.PathLike[str], *, overwrite: bool = False
    ) -> None:
        self._file = open(
            path, "w" if overwrite else "x", encoding="utf-8", newline="\n"
        )
        self._opened = False

    def open(self) -> bool:
        if self._opened or self._file.closed:
            return False
        self._opened = True
        return True

    def write(self, lines: Sequence[str]) -> None:
        try:
            self._file.write("".join(f"{line}\n" for line in lines))
            self._file.flush()
        except (OSError, ValueError) as e:
            raise SinkDisconnected(str(e)) from e

    def retry_delay(self) -> float | None:
        return None

    def is_open(self) -> bool:
        return self._opened and not self._file.closed

    def close(self) -> None:
        self._file.close()


class WebSocketSink:
    """Dial an observer (the hub, D9) over WebSocket; one session per connection.

    Transport rules (spec, Transport): no ``Origin`` header; off loopback a
    ``token`` is required and sent as ``Authorization: Bearer <token>``
    (without one the sink refuses to connect); data frames received are
    ignored, control frames handled as RFC 6455 requires. A lost connection
    ends the session; the sink reopens with exponential backoff.
    """

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        open_timeout: float = 5.0,
        backoff: tuple[float, float] = (0.5, 30.0),
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not 0 < backoff[0] <= backoff[1]:
            raise ValueError(f"backoff must satisfy 0 < first <= largest: {backoff!r}")
        self._url = url
        self._token = token
        self._open_timeout = open_timeout
        self._backoff = backoff
        self._delay = backoff[0]
        self._ssl = ssl_context
        self._conn: WSConnection | None = None
        self._refused = False
        self.last_error: str | None = None  # why the last open failed

    def open(self) -> bool:
        if self._refused:
            return False
        try:
            self._conn = WSConnection.connect(
                self._url,
                token=self._token,
                timeout=self._open_timeout,
                ssl_context=self._ssl,
            )
        except NotLoopbackError as e:
            self._refused = True  # a configuration error: do not retry
            self.last_error = str(e)
            return False
        except (OSError, HandshakeError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False
        self._delay = self._backoff[0]
        self.last_error = None
        return True

    def write(self, lines: Sequence[str]) -> None:
        conn = self._conn
        if conn is None or conn.closed:
            raise SinkDisconnected("not connected")
        try:
            for line in lines:
                conn.send_text(line)
        except OSError as e:
            conn.abort()
            raise SinkDisconnected(str(e)) from e

    def is_open(self) -> bool:
        return self._conn is not None and not self._conn.closed

    def retry_delay(self) -> float | None:
        if self._refused:
            return None
        delay = self._delay
        self._delay = min(self._delay * 2, self._backoff[1])
        return delay

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class MemorySink:
    """In-process sink for tests: keeps the lines; can drop or stall."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._connected = True
        self._gate = threading.Event()
        self._gate.set()
        self._lock = threading.Lock()

    def signals(self) -> list[dict[str, object]]:
        with self._lock:
            return [json.loads(line) for line in self.lines]

    def open(self) -> bool:
        return self._connected

    def write(self, lines: Sequence[str]) -> None:
        self._gate.wait()
        if not self._connected:
            raise SinkDisconnected("disconnected")
        with self._lock:
            self.lines.extend(lines)

    def retry_delay(self) -> float | None:
        return 0.01

    def is_open(self) -> bool:
        return self._connected

    def close(self) -> None:
        self._connected = False
        self._gate.set()

    def disconnect(self) -> None:
        """The next write fails and the sink stays closed until :meth:`connect`."""
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def hold(self) -> None:
        """Writes block until :meth:`release` (to exercise Loss)."""
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()
