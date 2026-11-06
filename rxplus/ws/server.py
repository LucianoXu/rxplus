"""WebSocket server for bi-directional ReactiveX communication.

Provides WSChannels for managing per-path client connections and RxWSServer
which runs a websockets server on a background thread.

Error semantics (0.2): a problem with one item (not ``TaggedData``, cannot be
encoded, cannot be decoded, a subscriber raising) is logged at ERROR and the
item is dropped; a problem with one connection (its send queue overflowing
under ``overflow="disconnect"``, an unexpected exception) closes that
connection only. Only lifecycle failures (the port cannot be bound) reach
``on_error``.
"""

import asyncio
import itertools
import socket
import threading
from dataclasses import dataclass
from typing import Any, Literal

import websockets
from opentelemetry._logs import LoggerProvider
from opentelemetry.trace import Tracer, TracerProvider
from reactivex import Observable, Subject
from websockets import ClientConnection, Server, ServerConnection

from ..mechanism import RxException
from ..utils import TaggedData, get_short_error_info
from ._otel_mixin import OTelLoggingMixin
from ._sendqueue import QueueOverflow, SendOverflow, SendQueue
from .codec import WSCodec
from .datatypes import (
    DatatypeSpec,
    WSConnectionConfig,
    WSDatatypeName,
    codec_for,
    resolve_codecs,
)

DEFAULT_MAX_SIZE = 1 << 20  # 1 MiB: largest incoming message by default


def _ws_path(ws: ClientConnection | ServerConnection) -> str:
    """Return the request path of a WebSocket connection.

    Supports both async and sync connection objects by probing common
    attributes. Falls back to empty string when unavailable.
    """
    req = getattr(ws, "request", None)
    if req is not None:
        return getattr(req, "path", "")
    path = getattr(ws, "path", None)
    if isinstance(path, str):
        return path
    return ""


@dataclass(frozen=True, slots=True)
class WSConnectionEvent:
    """A client connection of an :class:`RxWSServer` opened or closed.

    ``connection_id`` is unique per server for its lifetime; ``path`` is the
    request path the client connected on.
    """

    kind: Literal["opened", "closed"]
    connection_id: int
    path: str


@dataclass(frozen=True, slots=True)
class WSReceived:
    """A value received by an :class:`RxWSServer`, with its connection."""

    connection_id: int
    path: str
    value: object


class WSChannels:
    """Manages the websocket channels of the same path.

    Thread-safe access to channels and queues via internal lock.
    """

    def __init__(
        self,
        datatype: WSDatatypeName | WSCodec[object] = "string",
        *,
        allow_pickle: bool = False,
    ):
        self.adapter: WSCodec[object] = codec_for(datatype, allow_pickle=allow_pickle)
        self._lock = threading.Lock()
        self.channels: set[object] = set()
        self.queues: set[object] = set()

    def add_client(self, websocket: object, queue: object) -> None:
        """Thread-safe registration of a client."""
        with self._lock:
            self.channels.add(websocket)
            self.queues.add(queue)

    def remove_client(self, websocket: object, queue: object) -> None:
        """Thread-safe removal of a client."""
        with self._lock:
            self.channels.discard(websocket)
            self.queues.discard(queue)

    def get_queues_snapshot(self) -> list[object]:
        """Return a snapshot of queues for iteration."""
        with self._lock:
            return list(self.queues)


class _Client:
    """A connected client: its socket, send queue and identity."""

    __slots__ = ("websocket", "queue", "connection_id", "desc")

    def __init__(
        self, websocket: ServerConnection, queue: SendQueue, cid: int, desc: str
    ) -> None:
        self.websocket = websocket
        self.queue = queue
        self.connection_id = cid
        self.desc = desc


class RxWSServer(Subject, OTelLoggingMixin):
    """WebSocket server for bi-directional communication between ReactiveX components.

    The server handles clients on any request path. Received values are
    emitted as ``TaggedData(path, value)``; ``on_next(TaggedData(path,
    value))`` sends ``value`` to every client on ``path``.

    Args:
        config: Host and port to listen on (``path`` is ignored).
        datatype: ``"string"``, ``"bytes"``, ``"object"`` (pickle, needs
            ``allow_pickle``), a :class:`~rxplus.ws.codec.WSCodec` such as
            :class:`~rxplus.ws.codec.JsonCodec`, or a function of the path
            returning one of these.
        allow_pickle: Allow ``"object"``. Unpickling runs the peer's code:
            trusted links only.
        max_size: Largest incoming message in bytes (None: unlimited).
        send_queue: Frames queued per client before ``overflow`` applies
            (None: unbounded). It counts frames, not bytes: the worst case
            per client is ``send_queue`` times your largest frame.
        send_queue_bytes: Optional bound on the queued bytes per client;
            ``overflow`` applies when either bound is reached.
        overflow: ``"drop_old"``, ``"drop_new"`` or ``"disconnect"``.

    ``connections`` emits a :class:`WSConnectionEvent` when a client
    connects or disconnects. The server shuts down on ``on_completed`` and
    on ``on_error`` (which it then forwards to its subscribers).

    Telemetry is optional -- pass tracer_provider and/or logger_provider to enable
    OTel instrumentation. Without providers, the component operates silently.
    """

    def __init__(
        self,
        config: WSConnectionConfig,
        datatype: DatatypeSpec = "string",
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 30.0,
        name: str | None = None,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
        *,
        allow_pickle: bool = False,
        max_size: int | None = DEFAULT_MAX_SIZE,
        send_queue: int | None = 1024,
        overflow: SendOverflow = "drop_old",
        send_queue_bytes: int | None = None,
    ):
        """Initialize the WebSocket server and start listening."""
        super().__init__()
        self.host = config.host
        self.port = config.port

        # Source name for logging/tracing
        self._name = name if name else f"RxWSServer:{self.host}:{self.port}"

        # OTel instrumentation (optional)
        self._tracer: Tracer | None = (
            tracer_provider.get_tracer(f"rxplus.{self._name}")
            if tracer_provider
            else None
        )
        self._logger = (
            logger_provider.get_logger(f"rxplus.{self._name}")
            if logger_provider
            else None
        )

        # Codec per path; a fixed "object" without allow_pickle fails here.
        self._codec_for_path = resolve_codecs(datatype, allow_pickle=allow_pickle)
        SendQueue(send_queue, overflow, send_queue_bytes)  # validate now
        self._send_queue = send_queue
        self._send_queue_bytes = send_queue_bytes
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._overflow: SendOverflow = overflow
        self._max_size = max_size

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        # Thread-safe access to path_channels
        self._path_channels_lock = threading.Lock()
        self.path_channels: dict[str, WSChannels] = {}

        self._connection_ids = itertools.count(1)
        self._connections: Subject[WSConnectionEvent] = Subject()
        self._received: Subject[WSReceived] = Subject()
        # connection_id -> (client, its path's channels); written on the loop
        # thread, read by send_to from any thread.
        self._clients_lock = threading.Lock()
        self._clients: dict[int, tuple[_Client, WSChannels]] = {}

        # Private asyncio loop in a background thread
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._stop_flag = threading.Event()

        self.serve: Server | None = None

        self._start_server_thread()

    @property
    def connections(self) -> Observable[WSConnectionEvent]:
        """Client connections opening and closing (emitted on the loop thread)."""
        return self._connections

    @property
    def received(self) -> Observable[WSReceived]:
        """Every received value with the connection it came from (the main
        stream carries ``TaggedData(path, value)`` without it)."""
        return self._received

    def send_to(self, connection_id: int, value: object) -> bool:
        """Send ``value`` to one client connection only.

        Returns False if the connection is unknown or closed, or the value
        cannot be encoded (logged and dropped, as for ``on_next``).
        """
        with self._clients_lock:
            entry = self._clients.get(connection_id)
        loop = self._loop
        if entry is None or loop is None or loop.is_closed():
            return False
        client, ws_channels = entry
        try:
            frame = ws_channels.adapter.encode(value)
        except Exception as e:
            self._drop(f"a value for connection {connection_id}", e)
            return False
        try:
            loop.call_soon_threadsafe(self._enqueue_one, client, frame)
        except RuntimeError:
            return False
        return True

    def close_connection(self, connection_id: int) -> bool:
        """Close one client connection; False if it is unknown."""
        with self._clients_lock:
            entry = self._clients.get(connection_id)
        loop = self._loop
        if entry is None or loop is None or loop.is_closed():
            return False
        websocket = entry[0].websocket
        try:
            loop.call_soon_threadsafe(lambda: loop.create_task(websocket.close()))
        except RuntimeError:
            return False
        return True

    def _enqueue_one(self, client: "_Client", frame: str | bytes) -> None:
        """Loop thread: queue a frame for one client (see _put)."""
        self._put(client, frame)

    def _get_path_channels(self, path: str) -> WSChannels:
        """Get the WSChannels instance for the given path.

        If the path does not exist, create a new WSChannels instance.
        Thread-safe via _path_channels_lock. Raises if the path's datatype
        is refused (``"object"`` without ``allow_pickle``).
        """
        with self._path_channels_lock:
            if path not in self.path_channels:
                codec = self._codec_for_path(path)
                # The codec was checked against allow_pickle in resolve_codecs.
                self.path_channels[path] = WSChannels(codec, allow_pickle=True)
            return self.path_channels[path]

    @property
    def dropped(self) -> int:
        """Items dropped so far: per-item failures (not ``TaggedData``,
        cannot be encoded or decoded, a subscriber raising) plus frames
        discarded by send-queue overflow. A reporter (X4) reports the same
        counts as ``dropped`` on the component's links."""
        with self._dropped_lock:
            return self._dropped

    def _count_dropped(self, n: int = 1) -> None:
        with self._dropped_lock:
            self._dropped += n

    def _drop(self, what: str, error: Exception | None = None) -> None:
        self._count_dropped()
        detail = f": {get_short_error_info(error)}" if error is not None else ""
        self._log(f"Dropped {what}{detail}", "ERROR")

    def _emit_event(self, event: WSConnectionEvent) -> None:
        """Emit a connection event; a subscriber that raises is logged and
        never affects the connection."""
        try:
            self._connections.on_next(event)
        except Exception as e:
            self._log(
                f"A connections subscriber raised on {event.kind}"
                f" {event.connection_id}: {get_short_error_info(e)}",
                "ERROR",
            )

    def on_next(self, value: TaggedData) -> None:
        """Encode once and queue the frame for every client on the path.

        A value that is not ``TaggedData`` or cannot be encoded is logged and
        dropped (0.1: ``on_error`` ended the server).
        """
        if not isinstance(value, TaggedData):
            self._drop(f"a {type(value).__name__}: RxWSServer expects TaggedData")
            return
        try:
            ws_channels = self._get_path_channels(value.tag)
            frame = ws_channels.adapter.encode(value.data)
        except Exception as e:
            self._drop(f"a value for path {value.tag!r}", e)
            return

        if not self._loop_ready.wait(timeout=5.0):
            self._drop("a value: the server event loop did not start")
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return  # the server has ended; there is no client to send to
        try:
            loop.call_soon_threadsafe(self._enqueue, ws_channels, frame)
        except RuntimeError:
            return  # the loop closed meanwhile

    def _enqueue(self, ws_channels: WSChannels, frame: str | bytes) -> None:
        """Loop thread: queue a frame for every client of a path."""
        for client in ws_channels.get_queues_snapshot():
            assert isinstance(client, _Client)
            self._put(client, frame)

    def _put(self, client: "_Client", frame: str | bytes) -> None:
        """Loop thread: queue one frame for one client, applying its overflow
        policy; drops are counted and logged (the first, every thousandth,
        and a disconnect once)."""
        queue = client.queue
        if queue.overflowed:
            self._count_dropped()  # disconnecting: the frame is lost
            return
        before = queue.dropped
        if not queue.put(frame):
            self._count_dropped()
            self._log(  # once per connection
                f"Send queue of {client.desc} overflowed"
                f" ({queue.limit} frames); disconnecting",
                "WARN",
            )
        elif queue.dropped != before:
            self._count_dropped(queue.dropped - before)
            if before == 0 or queue.dropped // 1000 != before // 1000:
                self._log(
                    f"Send queue of {client.desc} full; {queue.dropped}"
                    f" frames dropped so far ({queue.overflow})",
                    "WARN",
                )

    def on_error(self, error: Exception) -> None:
        """Shut the server down, then forward the error to subscribers."""
        self._shutdown_server(error)

    def on_completed(self) -> None:
        """Complete the server after closing all connections."""
        self._shutdown_server(None)

    async def handle_client(self, websocket: ServerConnection) -> None:
        """Serve a connected WebSocket client until the link closes."""
        path = _ws_path(websocket)
        remote_desc = f"[{websocket.remote_address} on path {path}]"
        try:
            ws_channels = self._get_path_channels(path)
        except Exception as e:
            self._log(f"Refused {remote_desc}: {get_short_error_info(e)}", "ERROR")
            await websocket.close(1011, "datatype refused")
            return

        cid = next(self._connection_ids)
        client = _Client(
            websocket,
            SendQueue(self._send_queue, self._overflow, self._send_queue_bytes),
            cid,
            remote_desc,
        )
        ws_channels.add_client(websocket, client)
        with self._clients_lock:
            self._clients[cid] = (client, ws_channels)
        try:
            self._log(f"Client established from {remote_desc}", "INFO")
            self._emit_event(WSConnectionEvent("opened", cid, path))
            sender_task = asyncio.create_task(self._server_sender(client))
            receiver_task = asyncio.create_task(
                self._server_receiver(
                    websocket, path, ws_channels.adapter, remote_desc, cid
                )
            )
            done, pending = await asyncio.wait(
                {sender_task, receiver_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()  # surface unexpected exceptions below
        except asyncio.CancelledError:
            self._log(f"Client {remote_desc} connection cancelled.", "INFO")
            raise
        except Exception as e:
            # One connection's failure closes that connection only.
            self._log(
                f"Closing {remote_desc} after an unexpected error:"
                f" {get_short_error_info(e)}",
                "ERROR",
            )
        finally:
            # Unregister first: no more frames for it. A peer that does not
            # read cannot finish the closing handshake, so bound the close.
            ws_channels.remove_client(websocket, client)
            with self._clients_lock:
                self._clients.pop(cid, None)
            try:
                await asyncio.wait_for(websocket.close(), timeout=2.0)
            except TimeoutError:
                websocket.transport.abort()
            self._log(f"Client {remote_desc} resources released.", "INFO")
            self._emit_event(WSConnectionEvent("closed", cid, path))

    async def _server_sender(self, client: _Client) -> None:
        while True:
            try:
                frame = await client.queue.get()
            except QueueOverflow:
                return  # overflow="disconnect": end this connection
            try:
                await client.websocket.send(frame)
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                self._log(
                    f"Failed to send to client {client.desc},"
                    f" connection broken: {get_short_error_info(e)}",
                    "WARN",
                )
                return
            except websockets.exceptions.ConnectionClosed as e:
                self._log(
                    f"Failed to send to client {client.desc},"
                    f" connection closed: {get_short_error_info(e)}",
                    "WARN",
                )
                return

    async def _server_receiver(
        self,
        websocket: ServerConnection,
        path: str,
        adapter: WSCodec[object],
        remote_desc: str,
        cid: int,
    ) -> None:
        while True:
            try:
                data = await websocket.recv()
            except websockets.exceptions.ConnectionClosedOK:
                self._log(f"Client {remote_desc} disconnected gracefully.", "INFO")
                return
            except websockets.exceptions.ConnectionClosedError as e:
                self._log(
                    f"Client {remote_desc} disconnected"
                    f" with error: {get_short_error_info(e)}.",
                    "WARN",
                )
                return
            except OSError as e:
                self._log(f"Network error: {get_short_error_info(e)}", "WARN")
                return

            try:
                payload = adapter.decode(data)
            except Exception as e:
                self._drop(f"a message from {remote_desc} that did not decode", e)
                continue
            # A subscriber that raises is its problem, not the connection's;
            # each stream is protected separately.
            try:
                super().on_next(TaggedData(path, payload))
            except Exception as e:
                # The item is counted as dropped.
                self._drop(f"a message from {remote_desc}: a subscriber raised", e)
            try:
                self._received.on_next(WSReceived(cid, path, payload))
            except Exception as e:
                self._drop(f"a message from {remote_desc}: a subscriber raised", e)

    def _make_dual_stack_socket(self) -> socket.socket | None:
        """Create a dual-stack IPv6 socket if the host is an IPv6 wildcard.

        Returns a bound, listening socket with ``IPV6_V6ONLY=0`` so that both
        IPv4 and IPv6 clients can connect.  Returns ``None`` when the host is
        not an IPv6 wildcard address.
        """
        if self.host not in ("::", "::0"):
            return None

        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Accept both IPv4 and IPv6 connections on the same socket.
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind((self.host, self.port))
        sock.listen(100)
        sock.setblocking(False)
        return sock

    async def start_server(self) -> None:
        """Create the asyncio WebSocket server on the private loop."""
        try:
            dual_sock = self._make_dual_stack_socket()
            serve_kwargs: dict[str, Any] = dict(
                ping_interval=self.ping_interval,
                ping_timeout=self.ping_timeout,
                max_size=self._max_size,
                process_request=self._process_request,
            )
            if dual_sock is not None:
                serve_kwargs["sock"] = dual_sock
                self.serve = await websockets.serve(
                    self.handle_client,
                    **serve_kwargs,
                )
            else:
                self.serve = await websockets.serve(
                    self.handle_client,
                    self.host,
                    self.port,
                    **serve_kwargs,
                )
            self._log(f"WebSocket server started on {self.host}:{self.port}", "INFO")
        except OSError as e:
            self._log(
                f"FATAL: Failed to bind {self.host}:{self.port} -- {e}. "
                f"Another process may already be listening on this port.",
                "ERROR",
            )
            # Propagate as an observable error so the owner is notified.
            rx_exception = RxException(
                e,
                source=self._name,
                note=f"Port {self.port} bind failed",
            )
            super().on_error(rx_exception)
            # Nothing is served: release the loop thread.
            asyncio.get_running_loop().stop()
        except asyncio.CancelledError:
            self._log("WebSocket server task cancelled.", "INFO")
            raise

    async def _process_request(
        self, connection: ClientConnection, request: websockets.http11.Request
    ) -> websockets.http11.Response | None:
        """Handle HTTP request before WebSocket upgrade.

        This catches invalid requests (like regular HTTP) that cannot be
        upgraded to WebSocket connections. Returns an HTTP response tuple
        for invalid requests, or None to proceed with WebSocket upgrade.

        Args:
            connection: The WebSocket connection object
            request: The HTTP Request object with headers
        """
        # Access headers from the request object
        headers = request.headers
        path = request.path

        # Check for valid WebSocket upgrade request
        connection_header = headers.get("Connection", "")
        upgrade_header = headers.get("Upgrade", "")

        # WS upgrade requires "Upgrade" in Connection and "websocket" in Upgrade
        if (
            "upgrade" not in connection_header.lower()
            or upgrade_header.lower() != "websocket"
        ):
            self._log(
                f"Rejected non-WebSocket request on {path}: "
                f"Connection={connection_header!r}, Upgrade={upgrade_header!r}",
                "WARN",
            )
            # Return HTTP response to reject the request
            from websockets.http11 import Response

            return Response(
                426,
                "Upgrade Required",
                websockets.Headers([("Content-Type", "text/plain")]),
                b"WebSocket upgrade required. This is a WebSocket endpoint.\n",
            )

        # Proceed with WebSocket handshake
        return None

    # ---------------- threaded event loop plumbing ---------------- #
    def _run_server_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._loop_ready.set()
        asyncio.set_event_loop(loop)
        # start the server
        loop.create_task(self.start_server())
        try:
            loop.run_forever()
        finally:
            try:
                if self.serve is not None:
                    # Close all connections immediately if supported
                    self.serve.close(True)
                    self.serve = None
            except Exception:
                pass
            loop.close()

    def _start_server_thread(self) -> None:
        self._thread = threading.Thread(target=self._run_server_loop, daemon=True)
        self._thread.start()

    def _shutdown_server(self, error: Exception | None) -> None:
        """Stop serving, then complete (or, with ``error``, error) the server."""
        self._log("Closing...", "INFO")
        try:
            loop_alive = (
                self._loop is not None
                and self._thread is not None
                and self._thread.is_alive()
            )
            if loop_alive:

                async def _async_close() -> None:
                    try:
                        if self.serve is not None:
                            self.serve.close(True)
                            # Wait for the close task to complete
                            if self.serve.close_task is not None:
                                try:
                                    await asyncio.wait_for(
                                        self.serve.close_task, timeout=2.0
                                    )
                                except TimeoutError:
                                    pass
                            self.serve = None
                        # Peers that did not finish the closing handshake
                        # (e.g. stalled readers) would leave their handler
                        # tasks pending and destroyed with the loop: abort
                        # their sockets, then cancel and await the handlers.
                        for channels in list(self.path_channels.values()):
                            for ws in list(channels.channels):
                                transport = getattr(ws, "transport", None)
                                if transport is not None:
                                    transport.abort()
                        current = asyncio.current_task()
                        others = [t for t in asyncio.all_tasks() if t is not current]
                        for task in others:
                            task.cancel()
                        if others:
                            await asyncio.wait(others, timeout=1.0)
                    finally:
                        asyncio.get_running_loop().stop()

                assert self._loop is not None
                try:
                    self._loop.call_soon_threadsafe(
                        lambda: self._loop.create_task(_async_close())  # type: ignore[union-attr]
                    )
                except RuntimeError:
                    pass  # the loop ended on its own meanwhile

            if (
                self._thread is not None
                and self._thread is not threading.current_thread()
            ):
                self._thread.join(timeout=3.0)

            self._log("Closed.", "INFO")
            self._connections.on_completed()
            self._received.on_completed()
            if error is None:
                super().on_completed()
            else:
                super().on_error(error)
        except Exception as e:
            rx_exception = RxException(e, source=self._name, note="RxWSServer shutdown")
            super().on_error(rx_exception)
