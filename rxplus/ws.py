"""
Communication by websocket.

This module runs its asyncio websocket logic inside dedicated background
threads, so callers don't need an ambient asyncio event loop. It behaves like a
threaded Observable/Observer (similar to `rx.interval` on a new thread).
"""

import asyncio
import pickle
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal, Optional

import websockets
from websockets import ClientConnection, Server

from reactivex import Subject
from reactivex import operators as ops
from reactivex.subject import BehaviorSubject

from opentelemetry.trace import TracerProvider, Tracer
from opentelemetry._logs import LoggerProvider, SeverityNumber
from opentelemetry._logs import LogRecord as OTelLogRecord

from .mechanism import RxException
from .utils import TaggedData, get_full_error_info, get_short_error_info


class ConnectionState(Enum):
    """Observable states for RxWSClient connection lifecycle."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"  # terminal state after on_completed


@dataclass
class RetryPolicy:
    """Configurable retry behavior for WebSocket reconnection.
    
    Attributes:
        max_retries: Maximum number of retry attempts. None means infinite retries.
        base_delay: Initial delay between retries in seconds.
        max_delay: Maximum delay between retries in seconds.
        backoff_factor: Multiplier for exponential backoff.
        jitter: Randomization factor (0.0-1.0) to prevent thundering herd.
    """
    max_retries: int | None = None  # None = infinite
    base_delay: float = 0.5
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    jitter: float = 0.1  # randomization factor (0.0-1.0)

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number (0-indexed).
        
        Uses exponential backoff with jitter:
        delay = min(base_delay * (backoff_factor ^ attempt), max_delay) ± jitter
        """
        delay = min(self.base_delay * (self.backoff_factor ** attempt), self.max_delay)
        jitter_range = delay * self.jitter
        return delay + random.uniform(-jitter_range, jitter_range)


def _ws_path(ws: Any) -> str:
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


class WSDatatype(ABC):

    @abstractmethod
    def package_type_check(self, value) -> None:
        """
        Check whether the value can be sent through this datatype.
        If not, an error will be rased.
        """
        ...

    @abstractmethod
    def package(self, value) -> Any: ...

    @abstractmethod
    def unpackage(self, value) -> Any: ...


class WSStr(WSDatatype):

    def package_type_check(self, value) -> None:
        # Ensure text frames are actual strings to avoid implicit coercion
        # and unexpected payload formats at send time.
        if not isinstance(value, str):
            raise TypeError("WSStr expects a string payload")

    def package(self, value):
        return str(value)

    def unpackage(self, value):
        return value


class WSBytes(WSDatatype):

    def package_type_check(self, value) -> None:
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("WSBytes expects a bytes-like object")

    def package(self, value):
        return value

    def unpackage(self, value):
        # websockets binary frame → bytes ；text frame → str
        if isinstance(value, str):
            raise TypeError(
                f"WSBytes expects a bytes-like object, got str '{value}'"
            )
        return bytes(value)


class WSObject(WSDatatype):
    """
    WebSocket datatype for arbitrary Python objects using pickle serialization.
    
    Note: Standard OTel LogRecords may have issues with pickle serialization
    due to their context objects. Custom data classes are recommended for
    reliable serialization over WebSocket.
    """

    def package_type_check(self, value) -> None:
        # Accept any pickleable object; validation occurs in package().
        pass

    def package(self, value):
        try:
            return pickle.dumps(value)
        except Exception as e:
            raise TypeError(
                f"WSObject cannot pickle value of type {type(value)}: {e}"
            )

    def unpackage(self, value):
        if isinstance(value, str):
            raise TypeError(
                f"WSObject expects binary frame (bytes); got text frame: {value!r}"
            )
        try:
            return pickle.loads(value)
        except Exception as e:
            # Include exception type and repr for better debugging
            error_msg = str(e) or repr(e)
            raise TypeError(
                f"WSObject failed to unpickle payload: {type(e).__name__}: {error_msg}"
            ) from e


def wsdt_factory(datatype: Literal["string", "bytes", "object"]) -> WSDatatype:
    """
    Factory function to create a WSDatatype instance based on the datatype parameter.
    """
    if datatype == "string":
        return WSStr()
    elif datatype == "bytes":
        return WSBytes()
    elif datatype == "object":
        return WSObject()
    else:
        raise ValueError(f"Unsupported datatype '{datatype}'.")


# we use dictionary to serve as connection configuration
# example:
# {
#   host : 'localhost',
#   port : 1492,
#   path : '/',
# }


def _validate_conn_cfg(conn_cfg: dict, required_keys: list[str]) -> None:
    """Validate that conn_cfg contains all required keys."""
    missing = [k for k in required_keys if k not in conn_cfg]
    if missing:
        raise ValueError(f"conn_cfg missing required keys: {missing}")


class WS_Channels:
    """
    The class to manage the websocket channels of the same path.
    Thread-safe access to channels and queues via internal lock.
    """

    def __init__(self, datatype: Literal["string", "bytes", "object"] = "string"):
        self.adapter: WSDatatype = wsdt_factory(datatype)
        self._lock = threading.Lock()
        self.channels: set[Any] = set()
        self.queues: set[Any] = set()

    def add_client(self, websocket: Any, queue: Any) -> None:
        """Thread-safe registration of a client."""
        with self._lock:
            self.channels.add(websocket)
            self.queues.add(queue)

    def remove_client(self, websocket: Any, queue: Any) -> None:
        """Thread-safe removal of a client."""
        with self._lock:
            self.channels.discard(websocket)
            self.queues.discard(queue)

    def get_queues_snapshot(self) -> list[Any]:
        """Return a snapshot of queues for iteration."""
        with self._lock:
            return list(self.queues)


class RxWSServer(Subject):
    """
    The websocket server for bi-directional communication between ReactiveX components.
    The server can be connected by multiple clients.

    The server can handle connections from multiple clients on different paths. Here different paths means the original URI path.

    It will wrap the data in a `TaggedData` object with the path, and call `on_next`.

    When `on_next` is called, it will check whether the value is a `TaggedData`. If it is, it will send the data to the corresponding path's channels. If it is not, it will send the data to the default path's channels (i.e., the empty path).

    Use datatype parameter to control the data type sent through the websocket.
    The server will be closed upon receiving on_completed signal.
    
    Telemetry is optional — pass tracer_provider and/or logger_provider to enable
    OTel instrumentation. Without providers, the component operates silently.
    """

    def __init__(
        self,
        conn_cfg: dict,
        datatype: (
            Callable[[str], Literal["string", "bytes", "object"]]
            | Literal["string", "bytes", "object"]
        ) = "string",
        ping_interval: Optional[float] = 30.0,
        ping_timeout: Optional[float] = 30.0,
        name: Optional[str] = None,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
    ):
        """Initialize the WebSocket server and start listening."""
        super().__init__()
        _validate_conn_cfg(conn_cfg, ["host", "port"])
        self.host = conn_cfg["host"]
        self.port = int(conn_cfg["port"])

        # Source name for logging/tracing
        self._name = name if name else f"RxWSServer:{self.host}:{self.port}"

        # OTel instrumentation (optional)
        self._tracer: Tracer | None = (
            tracer_provider.get_tracer(f"rxplus.{self._name}")
            if tracer_provider else None
        )
        self._logger = (
            logger_provider.get_logger(f"rxplus.{self._name}")
            if logger_provider else None
        )

        # the function to determine the datatype of the path
        self.datatype_func: Callable[[str], Literal["string", "bytes", "object"]]
        if datatype in ["string", "bytes", "object"]:
            self.datatype_func = lambda path: datatype  # type: ignore
        elif callable(datatype):
            self.datatype_func = datatype
        else:
            raise ValueError(
                f"Unsupported datatype '{datatype}'. Expected 'string', 'bytes', 'object', or a callable function."
            )

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        # Thread-safe access to path_channels
        self._path_channels_lock = threading.Lock()
        self.path_channels: dict[str, WS_Channels] = {}

        # Private asyncio loop in a background thread
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._stop_flag = threading.Event()

        self.serve: Optional[Server] = None

        self._start_server_thread()

    def _get_path_channels(self, path: str) -> WS_Channels:
        """
        Get the WS_Channels instance for the given path.
        If the path does not exist, create a new WS_Channels instance.
        Thread-safe via _path_channels_lock.
        """
        with self._path_channels_lock:
            if path not in self.path_channels:
                datatype = self.datatype_func(path)
                self.path_channels[path] = WS_Channels(datatype=datatype)
            return self.path_channels[path]

    def on_next(self, value):
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

    def on_error(self, error):
        """Forward errors to subscribers."""
        super().on_error(error)

    def on_completed(self) -> None:
        """Complete the server after closing all connections."""
        self._shutdown_server()

    async def handle_client(self, websocket: Any):
        """Serve a connected WebSocket client until the link closes."""

        path = _ws_path(websocket)
        remote_desc = f"[{websocket.remote_address} on path {path}]"

        self._log(f"Client established from {remote_desc}", "INFO")

        try:
            ws_channels = self._get_path_channels(path)

            queue: asyncio.Queue[Any] = asyncio.Queue()

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
        queue: asyncio.Queue[Any],
        websocket: Any,
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
                        f"Failed to send data to client {remote_desc}, connection may be broken: {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
                except websockets.exceptions.ConnectionClosed as e:
                    self._log(
                        f"Failed to send data to client {remote_desc}, connection closed: {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
        except asyncio.CancelledError:
            raise

    async def _server_receiver(
        self,
        websocket: Any,
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
                        f"Connection reset (ConnectionResetError): {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
                except OSError as e:
                    self._log(
                        f"Network error or connection lost (OSError): {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
                except websockets.exceptions.ConnectionClosedError as e:
                    self._log(
                        f"Client {remote_desc} disconnected with error: {get_short_error_info(e)}.",
                        "WARN",
                    )
                    return
                except websockets.exceptions.ConnectionClosedOK:
                    self._log(
                        f"Client {remote_desc} disconnected gracefully.", "INFO"
                    )
                    return

                try:
                    payload = adapter.unpackage(data)
                except Exception as e:
                    self._log(
                        f"Failed to unpackage data from {remote_desc}: {get_short_error_info(e)}",
                        "ERROR",
                    )
                    continue

                wrapped_data = TaggedData(path, payload)
                super().on_next(wrapped_data)
        except asyncio.CancelledError:
            raise

    async def start_server(self):
        """Create the asyncio WebSocket server on the private loop."""
        try:
            self.serve = await websockets.serve(
                self.handle_client,
                self.host,
                self.port,
                ping_interval=self.ping_interval,
                ping_timeout=self.ping_timeout,
                max_size=None,
                process_request=self._process_request,
            )
            self._log(
                f"WebSocket server started on {self.host}:{self.port}", "INFO"
            )
        except asyncio.CancelledError:
            self._log(f"WebSocket server task cancelled.", "INFO")
            raise
    
    async def _process_request(self, connection, request):
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
        
        # WebSocket upgrade requires "Upgrade" in Connection header and "websocket" in Upgrade header
        if "upgrade" not in connection_header.lower() or upgrade_header.lower() != "websocket":
            self._log(
                f"Rejected non-WebSocket request on {path}: "
                f"Connection={connection_header!r}, Upgrade={upgrade_header!r}",
                "WARN"
            )
            # Return HTTP response to reject the request
            from websockets.http11 import Response
            return Response(
                426,
                "Upgrade Required",
                websockets.Headers([("Content-Type", "text/plain")]),
                b"WebSocket upgrade required. This is a WebSocket endpoint.\n"
            )
        
        # Proceed with WebSocket handshake
        return None

    # ---------------- threaded event loop plumbing ---------------- #
    def _run_server_loop(self):
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
                    self.serve.close(True)  # type: ignore[arg-type]
                    self.serve = None
            except Exception:
                pass
            loop.close()

    def _start_server_thread(self):
        self._thread = threading.Thread(target=self._run_server_loop, daemon=True)
        self._thread.start()

    def _log(self, body: str, level: str = "INFO") -> None:
        """Emit a log record via OTel logger if configured."""
        if self._logger is None:
            return
        severity_map = {
            "DEBUG": SeverityNumber.DEBUG,
            "INFO": SeverityNumber.INFO,
            "WARN": SeverityNumber.WARN,
            "ERROR": SeverityNumber.ERROR,
        }
        record = OTelLogRecord(
            timestamp=time.time_ns(),
            body=body,
            severity_text=level,
            severity_number=severity_map.get(level, SeverityNumber.INFO),
            attributes={"component": self._name},
        )
        self._logger.emit(record)

    def _shutdown_server(self):
        self._log("Closing...", "INFO")
        try:
            if self._loop is not None:
                async def _async_close():
                    try:
                        if self.serve is not None:
                            self.serve.close(True)  # type: ignore[arg-type]
                            # Wait for the close task to complete
                            if self.serve.close_task is not None:
                                try:
                                    await asyncio.wait_for(self.serve.close_task, timeout=2.0)
                                except asyncio.TimeoutError:
                                    pass
                            self.serve = None
                    finally:
                        asyncio.get_running_loop().stop()

                self._loop.call_soon_threadsafe(
                    lambda: self._loop.create_task(_async_close())
                )

            if self._thread is not None:
                self._thread.join(timeout=3.0)

            self._log("Closed.", "INFO")
            super().on_completed()
        except Exception as e:
            rx_exception = RxException(e, source=self._name, note="RxWSServer.on_completed")
            super().on_error(rx_exception)


class RxWSClient(Subject):
    """
    A resilient ReactiveX‑compatible WebSocket **client**.

    This subject behaves as both an *Observable*—emitting messages arriving
    _from_ the remote WebSocket endpoint—and an *Observer*—accepting messages
    that you want to _send_ to the endpoint.

    Key Features
    ------------
    * **Auto‑reconnect** – repeatedly attempts to reconnect every
      ``conn_retry_timeout`` seconds until the server becomes reachable.
    * **Back‑pressure friendly** – outbound messages are buffered in an
      ``asyncio.Queue`` while the socket is unavailable.
    * **Typed frames** – payloads are (de)serialized by a ``WSDatatype``
      adapter chosen via the ``datatype`` argument (``"string"``,
      ``"bytes"``, or ``"object"`` for pickled Python objects).

    Typical Usage
    -------------
    ```python
    cfg = {"host": "localhost", "port": 8888, "path": "/chat"}
    client = RxWSClient(cfg, datatype="string")

    # outbound (Observer side)
    rx.from_(["hello", "world"]).subscribe(client)

    # inbound (Observable side)
    client.subscribe(print)
    ```

    Parameters
    ----------
    conn_cfg : dict
        ``{"host": str, "port": int, "path": str}`` describing the remote
        WebSocket endpoint.
    datatype : Literal["string", "bytes", "object"]
        Frame representation handled by :func:`wsdt_factory`.
    conn_retry_timeout : float
        Delay between automatic reconnection attempts in seconds.
    ping_interval, ping_timeout : float | None
        Values forwarded to :pyfunc:`websockets.connect` for heartbeat
        management.
    name : str | None
        Custom name for log source identification. If not provided, defaults
        to ``"RxWSClient:ws://{host}:{port}{path}"``.

    Raises
    ------
    RxException
        Wrapped lower‑level exceptions forwarded through the ReactiveX error
        channel.
    """

    def __init__(
        self,
        conn_cfg: dict,
        datatype: Literal["string", "bytes", "object"] = "string",
        conn_retry_timeout: float = 0.5,
        ping_interval: Optional[float] = 30.0,
        ping_timeout: Optional[float] = 30.0,
        name: Optional[str] = None,
        buffer_while_disconnected: bool = False,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        """Create a reconnecting WebSocket client.

        Args:
            conn_cfg: Connection configuration with 'host', 'port', and optional 'path'.
            datatype: Payload serialization type.
            conn_retry_timeout: Delay between reconnection attempts in seconds.
                Deprecated: Use retry_policy instead.
            ping_interval: WebSocket heartbeat interval.
            ping_timeout: WebSocket heartbeat timeout.
            name: Custom name for log source identification.
            buffer_while_disconnected: If True, queue messages while disconnected.
                If False (default), messages are dropped when not connected.
            tracer_provider: Optional OTel TracerProvider for span instrumentation.
            logger_provider: Optional OTel LoggerProvider for log emission.
            retry_policy: Configurable retry behavior with exponential backoff.
                If None, uses default RetryPolicy with conn_retry_timeout as base_delay.
        """
        super().__init__()
        _validate_conn_cfg(conn_cfg, ["host", "port"])
        self.host = conn_cfg["host"]
        self.port = int(conn_cfg["port"])
        self.path = conn_cfg.get("path", "/")

        # Source name for identification
        self._name = name if name else f"RxWSClient:ws://{self.host}:{self.port}{self.path}"

        # OTel instrumentation
        self._tracer = tracer_provider.get_tracer(f"rxplus.{self._name}") if tracer_provider else None
        self._logger = logger_provider.get_logger(f"rxplus.{self._name}") if logger_provider else None

        self.datatype = datatype
        self.adapter: WSDatatype = wsdt_factory(datatype)

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self._buffer_while_disconnected = buffer_while_disconnected

        # Cross-thread outbound queue; created lazily in _run_loop
        self.queue: Optional[asyncio.Queue[Any]] = None
        self.ws: Optional[ClientConnection] = None

        # Retry policy: use provided or create default from conn_retry_timeout
        self._retry_policy = retry_policy if retry_policy else RetryPolicy(base_delay=conn_retry_timeout)
        self.conn_retry_timeout = conn_retry_timeout  # kept for backward compatibility

        # Private asyncio loop running in a background thread
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        
        # Connection state observable
        self._connection_state_subject: BehaviorSubject[ConnectionState] = BehaviorSubject(ConnectionState.DISCONNECTED)
        
        self._start_loop_thread()

        # whether the connection is established. this will influence the caching strategy.
        self._connected = False
        # flag to signal shutdown
        self._shutdown_requested = False

    def _log(self, body: str, level: str = "INFO") -> None:
        """Emit a log record via OTel logger if configured."""
        if self._logger is None:
            return
        severity_map = {
            "DEBUG": SeverityNumber.DEBUG,
            "INFO": SeverityNumber.INFO,
            "WARN": SeverityNumber.WARN,
            "ERROR": SeverityNumber.ERROR,
        }
        record = OTelLogRecord(
            timestamp=time.time_ns(),
            body=body,
            severity_text=level,
            severity_number=severity_map.get(level, SeverityNumber.INFO),
            attributes={"component": self._name},
        )
        self._logger.emit(record)

    @property
    def connection_state(self):
        """Observable stream of connection state changes.
        
        Emits on every state transition including each reconnect attempt.
        New subscribers immediately receive the current state.
        
        Returns:
            Observable[ConnectionState]: Stream of connection state values.
        """
        return self._connection_state_subject.pipe(ops.share())

    def _set_connection_state(self, state: ConnectionState) -> None:
        """Thread-safe state transition with logging."""
        self._log(f"Connection state: {state.value}", "DEBUG")
        self._connection_state_subject.on_next(state)

    def on_next(self, value):
        """Send a message to the server.

        If buffer_while_disconnected is False (default), messages are dropped
        when not connected. If True, messages are queued for delivery after
        reconnection.
        """
        if self._shutdown_requested:
            return

        if not self._connected and not self._buffer_while_disconnected:
            # Drop message when not connected and buffering is disabled
            return

        self.adapter.package_type_check(value)
        if not self._loop_ready.wait(timeout=5.0):
            rx_exception = RxException(
                RuntimeError("Client event loop failed to start"),
                source=self._name,
                note="RxWSClient.on_next",
            )
            super().on_error(rx_exception)
            return

        loop = self._loop
        if loop is None or self.queue is None:
            rx_exception = RxException(
                RuntimeError("Client event loop not available"),
                source=self._name,
                note="RxWSClient.on_next",
            )
            super().on_error(rx_exception)
            return

        loop.call_soon_threadsafe(self.queue.put_nowait, value)

    def on_error(self, error):
        """Report connection errors."""
        rx_exception = RxException(error, source=self._name, note="Error")
        super().on_error(rx_exception)

    def on_completed(self) -> None:
        """Close the connection before completing."""
        self._shutdown_requested = True
        self._set_connection_state(ConnectionState.CLOSED)
        self._shutdown_client()

    def _shutdown_client(self):
        """Shutdown the client, stop the event loop, and join the thread."""
        self._log("Closing...", "INFO")
        try:
            if self._loop is not None:
                async def _async_close():
                    try:
                        # Close the WebSocket connection if open
                        if self.ws is not None:
                            try:
                                await asyncio.wait_for(self.ws.close(), timeout=1.0)
                            except (asyncio.TimeoutError, Exception):
                                pass
                            self.ws = None
                        
                        # Cancel all remaining tasks except this one
                        current = asyncio.current_task()
                        for task in asyncio.all_tasks(self._loop):
                            if task is not current:
                                task.cancel()
                        
                        # Give tasks a moment to clean up
                        await asyncio.sleep(0.1)
                    finally:
                        asyncio.get_running_loop().stop()

                self._loop.call_soon_threadsafe(
                    lambda: self._loop.create_task(_async_close())
                )

            if self._thread is not None:
                self._thread.join(timeout=3.0)

            self._log("Closed.", "INFO")
            # Complete the connection state subject
            self._connection_state_subject.on_completed()
            super().on_completed()
        except Exception as e:
            rx_exception = RxException(e, source=self._name, note="RxWSClient.on_completed")
            super().on_error(rx_exception)

    async def connect_client(self):
        """Connect to the remote server and forward messages.
        
        Uses the configured RetryPolicy for exponential backoff.
        Emits ConnectionState on every state transition.
        Calls on_error if max_retries is exhausted.
        """
        # calculate the url
        url = f"ws://{self.host}:{self.port}{self.path}"
        remote_desc = f"[{url}]"
        attempt = 0

        try:
            # Repeatedly attempt to connect to the server
            while not self._shutdown_requested:

                self._connected = False
                
                # Emit state: CONNECTING on first attempt, RECONNECTING on subsequent
                self._set_connection_state(
                    ConnectionState.RECONNECTING if attempt > 0 else ConnectionState.CONNECTING
                )
                
                # Check max retries before attempting connection
                if (self._retry_policy.max_retries is not None 
                    and attempt >= self._retry_policy.max_retries):
                    self._log(
                        f"Max retries ({self._retry_policy.max_retries}) exhausted for {remote_desc}",
                        "ERROR",
                    )
                    self._set_connection_state(ConnectionState.DISCONNECTED)
                    super().on_error(RxException(
                        ConnectionError(f"Max retries exhausted connecting to {url}"),
                        source=self._name,
                        note="RxWSClient.connect_client",
                    ))
                    return

                # Attempt to connect to the server
                self._log(f"Connecting to server {remote_desc} (attempt {attempt + 1})", "INFO")

                try:
                    """
                    According to the documentation of websockets.connect:
                    Raises
                        InvalidURI
                        If uri isn't a valid WebSocket URI.

                        OSError
                        If the TCP connection fails.

                        InvalidHandshake
                        If the opening handshake fails.

                        ~asyncio.TimeoutError
                        If the opening handshake times out.
                    """

                    self.ws = await asyncio.wait_for(
                        websockets.connect(
                            url,
                            ping_interval=self.ping_interval,
                            ping_timeout=self.ping_timeout,
                            max_size=None,
                        ),
                        self._retry_policy.base_delay,
                    )
                    # Connection successful - reset attempt counter
                    attempt = 0

                except asyncio.TimeoutError:
                    attempt += 1
                    delay = self._retry_policy.get_delay(attempt - 1)
                    self._log(f"Connection timeout, retry {attempt} in {delay:.2f}s", "WARN")
                    self._set_connection_state(ConnectionState.DISCONNECTED)
                    await asyncio.sleep(delay)
                    continue

                except OSError as e:
                    attempt += 1
                    delay = self._retry_policy.get_delay(attempt - 1)
                    self._log(
                        f"Network error (OSError): {get_short_error_info(e)}, retry {attempt} in {delay:.2f}s",
                        "WARN",
                    )
                    self._set_connection_state(ConnectionState.DISCONNECTED)
                    await asyncio.sleep(delay)
                    continue

                except websockets.InvalidHandshake as e:
                    attempt += 1
                    delay = self._retry_policy.get_delay(attempt - 1)
                    self._log(
                        f"Invalid handshake: {get_short_error_info(e)}, retry {attempt} in {delay:.2f}s",
                        "WARN",
                    )
                    self._set_connection_state(ConnectionState.DISCONNECTED)
                    await asyncio.sleep(delay)
                    continue

                # Catch invalid URI errors - these are not retryable
                except websockets.InvalidURI as e:
                    self._log(
                        f"Invalid URI for server {remote_desc}: {get_short_error_info(e)}",
                        "ERROR",
                    )
                    self._set_connection_state(ConnectionState.DISCONNECTED)
                    super().on_error(e)
                    return

                assert self.ws is not None
                self._log(f"Server {remote_desc} Connected.", "INFO")
                self._connected = True
                self._set_connection_state(ConnectionState.CONNECTED)

                sender_task = asyncio.create_task(
                    self._client_sender(self.ws, remote_desc)
                )
                receiver_task = asyncio.create_task(
                    self._client_receiver(self.ws, remote_desc)
                )

                tasks = {sender_task, receiver_task}
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )

                for task in pending:
                    task.cancel()

                await asyncio.gather(*pending, return_exceptions=True)

                for task in done:
                    task.result()

                self._connected = False
                self._set_connection_state(ConnectionState.DISCONNECTED)
                
                # Increment attempt for reconnection
                attempt += 1

        except asyncio.CancelledError:
            self._log("WebSocket client connection cancelled.", "INFO")
            raise

        except Exception as e:
            self._log(
                f"Error while connecting to server {remote_desc}:\n{get_full_error_info(e)}",
                "ERROR",
            )
            self._set_connection_state(ConnectionState.DISCONNECTED)
            super().on_error(e)

        finally:
            if self.ws is not None:
                await self.ws.close()
                self.ws = None

                self._log(
                    f"Connection to server {remote_desc} resources released.", "INFO"
                )


    # ---------------- threaded event loop plumbing ---------------- #
    async def _client_sender(self, websocket: ClientConnection, remote_desc: str) -> None:
        try:
            assert self.queue is not None, "Queue must be initialized before _client_sender"
            while True:
                value = await self.queue.get()
                try:
                    await websocket.send(self.adapter.package(value))
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    self._log(
                        f"Failed to send data to server {remote_desc}, connection may be broken: {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
                except websockets.exceptions.ConnectionClosed as e:
                    self._log(
                        f"Failed to send data to server {remote_desc}, connection closed: {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
        except asyncio.CancelledError:
            raise

    async def _client_receiver(self, websocket: ClientConnection, remote_desc: str) -> None:
        try:
            while True:
                try:
                    data = await websocket.recv()
                except OSError as e:
                    self._log(
                        f"Network error or connection lost (OSError): {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
                except websockets.ConnectionClosedError as e:
                    self._log(
                        f"Server {remote_desc} Connection closed with error: {get_short_error_info(e)}.",
                        "WARN",
                    )
                    return
                except websockets.ConnectionClosedOK:
                    self._log(
                        f"Server {remote_desc} Connection closed gracefully.", "INFO",
                    )
                    return

                try:
                    payload = self.adapter.unpackage(data)
                except Exception as e:
                    self._log(
                        f"Failed to unpackage data from {remote_desc}: {get_short_error_info(e)}",
                        "ERROR",
                    )
                    continue

                super().on_next(payload)
        except asyncio.CancelledError:
            raise

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        # Create queue after event loop is set up
        self.queue = asyncio.Queue()
        self._loop_ready.set()
        asyncio.set_event_loop(loop)
        loop.create_task(self.connect_client())
        try:
            loop.run_forever()
        finally:
            loop.close()

    def _start_loop_thread(self):
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _schedule_on_loop(self, coro: Any) -> None:
        if self._loop is None:
            return
        def _task():
            asyncio.create_task(coro)
        self._loop.call_soon_threadsafe(_task)


class RxWSClientGroup(Subject):
    """
    A **multiplexing** subject that manages a dynamic set of
    :class:`RxWSClient` instances—one per WebSocket *path*—behind a single
    ReactiveX interface.

    Observer Side
    -------------
    Expects :class:`TaggedData` instances whose ``tag`` field contains the
    desired WebSocket path and whose ``data`` field holds the payload to send.
    A new client is lazily created the first time a path is encountered; later
    messages with the same path reuse the existing connection.

    Observable Side
    ---------------
    Emits the union of all inbound messages from every underlying client.  Each
    message is wrapped back into a :class:`TaggedData`, preserving its source
    path so downstream operators can demultiplex as needed.

    Example
    -------
    ```python
    group = RxWSClientGroup({"host": "localhost", "port": 8888})

    # Send audio frames to two different endpoints
    group.on_next(TaggedData("/mic/left",  left_bytes))
    group.on_next(TaggedData("/mic/right", right_bytes))

    # Filter to only left‑channel data
    group.pipe(
        ops.filter(lambda t: t.tag == "/mic/left")
    ).subscribe(handle_left)
    ```

    Parameters
    ----------
    conn_cfg : dict
        Base connection information (``host`` and ``port``).  The ``path`` for
        each connection is supplied by individual :class:`TaggedData` items.
    datatype, conn_retry_timeout, ping_interval, ping_timeout
        Passed through to the per‑path :class:`RxWSClient` factory.
    name : str | None
        Custom name for log source identification. Child clients will use
        ``"{name}:{path}"`` as their source name. If not provided, child
        clients use their default naming.

    Notes
    -----
    Closed or errored clients are removed from the internal ``_clients`` cache,
    allowing their resources to be garbage‑collected.
    """

    def __init__(
        self,
        conn_cfg: dict,
        datatype: (
            Callable[[str], Literal["string", "bytes", "object"]]
            | Literal["string", "bytes", "object"]
        ) = "string",
        conn_retry_timeout: float = 0.5,
        ping_interval: Optional[float] = 30.0,
        ping_timeout: Optional[float] = 30.0,
        name: Optional[str] = None,
        buffer_while_disconnected: bool = False,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
    ):
        super().__init__()
        _validate_conn_cfg(conn_cfg, ["host", "port"])

        self._name = name
        self._tracer_provider = tracer_provider
        self._logger_provider = logger_provider
        self._buffer_while_disconnected = buffer_while_disconnected
        self.datatype_func: Callable[[str], Literal["string", "bytes", "object"]]
        if datatype in ["string", "bytes", "object"]:
            self.datatype_func = lambda path: datatype  # type: ignore
        elif callable(datatype):
            self.datatype_func = datatype
        else:
            raise ValueError(
                f"Unsupported datatype '{datatype}'. Expected 'string', 'bytes', 'object', or a callable function."
            )

        def make_client(path: str) -> RxWSClient:
            new_conn_cfg = conn_cfg.copy()
            new_conn_cfg["path"] = path

            # Generate child name if parent has a name
            child_name = f"{name}:{path}" if name else None

            return RxWSClient(
                conn_cfg=new_conn_cfg,
                datatype=self.datatype_func(path),
                conn_retry_timeout=conn_retry_timeout,
                ping_interval=ping_interval,
                ping_timeout=ping_timeout,
                name=child_name,
                buffer_while_disconnected=buffer_while_disconnected,
                tracer_provider=tracer_provider,
                logger_provider=logger_provider,
            )

        self._client_factory = make_client
        self._clients_lock = threading.Lock()
        self._clients: dict[str, RxWSClient] = {}  # tag -> RxWSClient
        self._bus = Subject()  # merged inbound stream

    # ============ Observer interface ============ #
    def on_next(self, tagged: TaggedData):
        client = self._ensure_client(tagged.tag)
        # Push upstream – API depends on RxWSClient; assume it is Observer-like
        client.on_next(tagged.data)

    def on_error(self, err):
        # propagate error to every open client and downstream
        for c in self._clients.values():
            c.on_error(err)
        self._bus.on_error(err)

    def on_completed(self):
        for c in self._clients.values():
            c.on_completed()
        self._bus.on_completed()

    # ============ Observable interface ============ #
    def _subscribe_core(self, observer, scheduler=None):
        return self._bus.subscribe(observer, scheduler=scheduler)

    def open_path(self, path: str) -> None:
        """
        Open a new path for the client group.
        """
        self._ensure_client(path)

    # ============ internal helpers ============ #
    def _ensure_client(self, tag: str) -> RxWSClient:
        """Get or create a client for the given tag. Thread-safe."""
        with self._clients_lock:
            if tag not in self._clients:
                # build new client and bridge its inbound traffic
                client = self._client_factory(tag)

                # When the client emits, wrap again with tag and push to bus
                client.pipe(
                    ops.map(lambda data: TaggedData(tag, data))
                ).subscribe(self._bus)

                self._clients[tag] = client
            return self._clients[tag]
