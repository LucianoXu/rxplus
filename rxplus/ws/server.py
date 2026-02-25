"""WebSocket server for bi-directional ReactiveX communication.

Provides WSChannels for managing per-path client connections and RxWSServer
which runs a websockets server on a background thread.
"""

import asyncio
import socket
import threading
from collections.abc import Callable
from typing import Any, Literal

import websockets
from opentelemetry._logs import LoggerProvider
from opentelemetry.trace import Tracer, TracerProvider
from reactivex import Subject
from websockets import ClientConnection, Server, ServerConnection

from ..mechanism import RxException
from ..utils import TaggedData, get_short_error_info
from ._otel_mixin import OTelLoggingMixin
from .datatypes import WSConnectionConfig, WSDatatype, wsdt_factory


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


class WSChannels:
    """Manages the websocket channels of the same path.

    Thread-safe access to channels and queues via internal lock.
    """

    def __init__(self, datatype: Literal["string", "bytes", "object"] = "string"):
        self.adapter: WSDatatype = wsdt_factory(datatype)
        self._lock = threading.Lock()
        self.channels: set[ClientConnection] = set()
        self.queues: set[asyncio.Queue[str | bytes]] = set()

    def add_client(
        self, websocket: ClientConnection, queue: asyncio.Queue[str | bytes]
    ) -> None:
        """Thread-safe registration of a client."""
        with self._lock:
            self.channels.add(websocket)
            self.queues.add(queue)

    def remove_client(
        self, websocket: ClientConnection, queue: asyncio.Queue[str | bytes]
    ) -> None:
        """Thread-safe removal of a client."""
        with self._lock:
            self.channels.discard(websocket)
            self.queues.discard(queue)

    def get_queues_snapshot(self) -> list[asyncio.Queue[str | bytes]]:
        """Return a snapshot of queues for iteration."""
        with self._lock:
            return list(self.queues)


class RxWSServer(Subject, OTelLoggingMixin):
    """WebSocket server for bi-directional communication between ReactiveX components.

    The server can handle connections from multiple clients on different
    paths. Here different paths means the original URI path.

    It will wrap the data in a ``TaggedData`` object with the path, and
    call ``on_next``.

    When ``on_next`` is called, it will check whether the value is a
    ``TaggedData``. If it is, it will send the data to the corresponding
    path's channels. If not, it will send the data to the default
    path's channels (i.e., the empty path).

    Use datatype parameter to control the data type sent through the websocket.
    The server will be closed upon receiving on_completed signal.

    Telemetry is optional -- pass tracer_provider and/or logger_provider to enable
    OTel instrumentation. Without providers, the component operates silently.
    """

    def __init__(
        self,
        config: WSConnectionConfig,
        datatype: (
            Callable[[str], Literal["string", "bytes", "object"]]
            | Literal["string", "bytes", "object"]
        ) = "string",
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 30.0,
        name: str | None = None,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
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

        # the function to determine the datatype of the path
        self.datatype_func: Callable[[str], Literal["string", "bytes", "object"]]
        if datatype in ["string", "bytes", "object"]:
            self.datatype_func = lambda path: datatype  # type: ignore[assignment,return-value]
        elif callable(datatype):
            self.datatype_func = datatype
        else:
            raise ValueError(
                f"Unsupported datatype '{datatype}'."
                " Expected 'string', 'bytes', 'object',"
                " or a callable function."
            )

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        # Thread-safe access to path_channels
        self._path_channels_lock = threading.Lock()
        self.path_channels: dict[str, WSChannels] = {}

        # Private asyncio loop in a background thread
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._stop_flag = threading.Event()

        self.serve: Server | None = None

        self._start_server_thread()

    def _get_path_channels(self, path: str) -> WSChannels:
        """Get the WSChannels instance for the given path.

        If the path does not exist, create a new WSChannels instance.
        Thread-safe via _path_channels_lock.
        """
        with self._path_channels_lock:
            if path not in self.path_channels:
                datatype = self.datatype_func(path)
                self.path_channels[path] = WSChannels(datatype=datatype)
            return self.path_channels[path]

    def on_next(self, value: TaggedData) -> None:
        """Route outbound messages to the proper channel queues."""
        # determine channel and data
        if isinstance(value, TaggedData):
            # get the channels for the given path
            ws_channels = self._get_path_channels(value.tag)
            data = value.data
        else:
            rx_exception = RxException(
                ValueError(f"Expected TaggedData, but got {type(value)}"),
                source=self._name,
                note="RxWSServer.on_next",
            )
            super().on_error(rx_exception)
            return

        # type check data
        ws_channels.adapter.package_type_check(data)

        # push the data
        if not self._loop_ready.wait(timeout=5.0):
            rx_exception = RxException(
                RuntimeError("Server event loop failed to start"),
                source=self._name,
                note="RxWSServer.on_next",
            )
            super().on_error(rx_exception)
            return

        loop = self._loop
        if loop is None:
            # Should not happen because _loop_ready is set when loop is created.
            rx_exception = RxException(
                RuntimeError("Server event loop not available"),
                source=self._name,
                note="RxWSServer.on_next",
            )
            super().on_error(rx_exception)
            return

        for queue in ws_channels.get_queues_snapshot():
            loop.call_soon_threadsafe(queue.put_nowait, data)

    def on_error(self, error: Exception) -> None:
        """Forward errors to subscribers."""
        super().on_error(error)

    def on_completed(self) -> None:
        """Complete the server after closing all connections."""
        self._shutdown_server()

    async def handle_client(self, websocket: ClientConnection) -> None:
        """Serve a connected WebSocket client until the link closes."""

        path = _ws_path(websocket)
        remote_desc = f"[{websocket.remote_address} on path {path}]"

        self._log(f"Client established from {remote_desc}", "INFO")

        try:
            ws_channels = self._get_path_channels(path)

            queue: asyncio.Queue[str | bytes] = asyncio.Queue()

            # Register client (thread-safe)
            ws_channels.add_client(websocket, queue)

            sender_task = asyncio.create_task(
                self._server_sender(queue, websocket, ws_channels.adapter, remote_desc)
            )
            receiver_task = asyncio.create_task(
                self._server_receiver(websocket, path, ws_channels.adapter, remote_desc)
            )

            tasks = {sender_task, receiver_task}
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            for task in pending:
                task.cancel()

            await asyncio.gather(*pending, return_exceptions=True)

            for task in done:
                # Propagate unexpected exceptions to the outer handler.
                task.result()

        except asyncio.CancelledError:
            self._log(f"Client {remote_desc} connection cancelled.", "INFO")
            raise

        except Exception as e:
            rx_exception = RxException(
                e, source=self._name, note=f"Error while handling client {remote_desc}"
            )
            super().on_error(rx_exception)

        finally:
            await websocket.close()
            self._log(f"Client {remote_desc} resources released.", "INFO")

            # Unregister client (thread-safe)
            ws_channels.remove_client(websocket, queue)

    async def _server_sender(
        self,
        queue: asyncio.Queue[str | bytes],
        websocket: ClientConnection,
        adapter: WSDatatype,
        remote_desc: str,
    ) -> None:
        try:
            while True:
                value = await queue.get()
                try:
                    await websocket.send(adapter.package(value))
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    self._log(
                        f"Failed to send to client {remote_desc},"
                        f" connection broken: {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
                except websockets.exceptions.ConnectionClosed as e:
                    self._log(
                        f"Failed to send to client {remote_desc},"
                        f" connection closed: {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
        except asyncio.CancelledError:
            raise

    async def _server_receiver(
        self,
        websocket: ClientConnection,
        path: str,
        adapter: WSDatatype,
        remote_desc: str,
    ) -> None:
        try:
            while True:
                try:
                    data = await websocket.recv()
                except ConnectionResetError as e:
                    self._log(
                        f"Connection reset: {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
                except OSError as e:
                    self._log(
                        f"Network error (OSError): {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
                except websockets.exceptions.ConnectionClosedError as e:
                    self._log(
                        f"Client {remote_desc} disconnected"
                        f" with error: {get_short_error_info(e)}.",
                        "WARN",
                    )
                    return
                except websockets.exceptions.ConnectionClosedOK:
                    self._log(f"Client {remote_desc} disconnected gracefully.", "INFO")
                    return

                try:
                    payload = adapter.unpackage(data)
                except Exception as e:
                    self._log(
                        f"Failed to unpackage from {remote_desc}:"
                        f" {get_short_error_info(e)}",
                        "ERROR",
                    )
                    continue

                wrapped_data = TaggedData(path, payload)
                super().on_next(wrapped_data)
        except asyncio.CancelledError:
            raise

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
                max_size=None,
                process_request=self._process_request,
            )
            if dual_sock is not None:
                serve_kwargs["sock"] = dual_sock
                self.serve = await websockets.serve(
                    self.handle_client,  # type: ignore[arg-type]
                    **serve_kwargs,
                )
            else:
                self.serve = await websockets.serve(
                    self.handle_client,  # type: ignore[arg-type]
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

    def _shutdown_server(self) -> None:
        self._log("Closing...", "INFO")
        try:
            if self._loop is not None:

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
                    finally:
                        asyncio.get_running_loop().stop()

                assert self._loop is not None
                self._loop.call_soon_threadsafe(
                    lambda: self._loop.create_task(_async_close())  # type: ignore[union-attr]
                )

            if self._thread is not None:
                self._thread.join(timeout=3.0)

            self._log("Closed.", "INFO")
            super().on_completed()
        except Exception as e:
            rx_exception = RxException(
                e, source=self._name, note="RxWSServer.on_completed"
            )
            super().on_error(rx_exception)
