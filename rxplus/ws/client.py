"""Resilient ReactiveX-compatible WebSocket client.

Provides WSConnectionState, RetryPolicy, and RxWSClient which connects to a
remote server on a background thread with automatic reconnection.
"""

import asyncio
import random
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Literal

import websockets
from opentelemetry._logs import LoggerProvider
from opentelemetry.trace import Tracer, TracerProvider
from reactivex import Subject
from reactivex import operators as ops
from reactivex.subject import BehaviorSubject
from websockets import ClientConnection

from ..mechanism import RxException
from ..utils import get_full_error_info, get_short_error_info
from ._otel_mixin import OTelLoggingMixin
from .datatypes import WSConnectionConfig, WSDatatype, wsdt_factory


class WSConnectionState(Enum):
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
        delay = min(base_delay * (backoff_factor ^ attempt), max_delay) +/- jitter
        """
        delay = min(self.base_delay * (self.backoff_factor**attempt), self.max_delay)
        jitter_range = delay * self.jitter
        return delay + random.uniform(-jitter_range, jitter_range)


class RxWSClient(Subject, OTelLoggingMixin):
    """A resilient ReactiveX-compatible WebSocket client.

    This subject behaves as both an *Observable* -- emitting messages arriving
    from the remote WebSocket endpoint -- and an *Observer* -- accepting messages
    that you want to send to the endpoint.

    Key Features
    ------------
    * **Auto-reconnect** -- repeatedly attempts to reconnect using the
      configured ``RetryPolicy`` with exponential backoff.
    * **Back-pressure friendly** -- outbound messages are buffered in an
      ``asyncio.Queue`` while the socket is unavailable.
    * **Typed frames** -- payloads are (de)serialized by a ``WSDatatype``
      adapter chosen via the ``datatype`` argument (``"string"``,
      ``"bytes"``, or ``"object"`` for pickled Python objects).

    Parameters
    ----------
    config : WSConnectionConfig
        Typed connection configuration with host, port, and path.
    datatype : Literal["string", "bytes", "object"]
        Frame representation handled by :func:`wsdt_factory`.
    retry_policy : RetryPolicy | None
        Configurable retry behavior with exponential backoff.
        If None, uses default RetryPolicy().
    ping_interval, ping_timeout : float | None
        Values forwarded to :pyfunc:`websockets.connect` for heartbeat
        management.
    name : str | None
        Custom name for log source identification. If not provided, defaults
        to ``"RxWSClient:ws://{host}:{port}{path}"``.
    buffer_while_disconnected : bool
        If True, queue messages while disconnected. If False (default),
        messages are dropped when not connected.

    Raises
    ------
    RxException
        Wrapped lower-level exceptions forwarded through the ReactiveX error
        channel.
    """

    def __init__(
        self,
        config: WSConnectionConfig,
        datatype: Literal["string", "bytes", "object"] = "string",
        retry_policy: RetryPolicy | None = None,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 30.0,
        name: str | None = None,
        buffer_while_disconnected: bool = False,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
    ):
        """Create a reconnecting WebSocket client.

        Args:
            config: Typed connection configuration with host, port, and path.
            datatype: Payload serialization type.
            retry_policy: Configurable retry behavior with exponential backoff.
                If None, uses default RetryPolicy().
            ping_interval: WebSocket heartbeat interval.
            ping_timeout: WebSocket heartbeat timeout.
            name: Custom name for log source identification.
            buffer_while_disconnected: If True, queue messages while disconnected.
                If False (default), messages are dropped when not connected.
            tracer_provider: Optional OTel TracerProvider for span instrumentation.
            logger_provider: Optional OTel LoggerProvider for log emission.
        """
        super().__init__()
        self.host = config.host
        self.port = config.port
        self.path = config.path

        # Source name for identification
        self._name = (
            name if name else f"RxWSClient:ws://{self.host}:{self.port}{self.path}"
        )

        # OTel instrumentation
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

        self.datatype = datatype
        self.adapter: WSDatatype = wsdt_factory(datatype)

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self._buffer_while_disconnected = buffer_while_disconnected

        # Cross-thread outbound queue; created lazily in _run_loop
        self.queue: asyncio.Queue[str | bytes | object] | None = None
        self.ws: ClientConnection | None = None

        # Retry policy: use provided or create default
        self._retry_policy = retry_policy if retry_policy else RetryPolicy()

        # Private asyncio loop running in a background thread
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_ready = threading.Event()

        # Connection state observable
        self._connection_state_subject: BehaviorSubject[WSConnectionState] = (
            BehaviorSubject(WSConnectionState.DISCONNECTED)
        )

        # Whether connected. Influences the caching strategy.
        self._connected = False
        # flag to signal shutdown - must be set before starting the thread
        self._shutdown_requested = False

        self._start_loop_thread()

    @property
    def connection_state(self):
        """Observable stream of connection state changes.

        Emits on every state transition including each reconnect attempt.
        New subscribers immediately receive the current state.

        Returns:
            Observable[WSConnectionState]: Stream of connection state values.
        """
        return self._connection_state_subject.pipe(ops.share())

    def _set_connection_state(self, state: WSConnectionState) -> None:
        """Thread-safe state transition with logging."""
        self._log(f"Connection state: {state.value}", "DEBUG")
        self._connection_state_subject.on_next(state)

    def on_next(self, value: str | bytes | object) -> None:
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

    def on_error(self, error: Exception) -> None:
        """Report connection errors."""
        rx_exception = RxException(error, source=self._name, note="Error")
        super().on_error(rx_exception)

    def on_completed(self) -> None:
        """Close the connection before completing."""
        self._shutdown_requested = True
        self._set_connection_state(WSConnectionState.CLOSED)
        self._shutdown_client()

    def _shutdown_client(self) -> None:
        """Shutdown the client, stop the event loop, and join the thread."""
        self._log("Closing...", "INFO")
        try:
            if self._loop is not None:

                async def _async_close() -> None:
                    try:
                        # Close the WebSocket connection if open
                        if self.ws is not None:
                            try:
                                await asyncio.wait_for(self.ws.close(), timeout=1.0)
                            except (TimeoutError, Exception):
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

                assert self._loop is not None
                self._loop.call_soon_threadsafe(
                    lambda: self._loop.create_task(_async_close())  # type: ignore[union-attr]
                )

            if self._thread is not None:
                self._thread.join(timeout=3.0)

            self._log("Closed.", "INFO")
            # Complete the connection state subject
            self._connection_state_subject.on_completed()
            super().on_completed()
        except Exception as e:
            rx_exception = RxException(
                e, source=self._name, note="RxWSClient.on_completed"
            )
            super().on_error(rx_exception)

    async def connect_client(self) -> None:
        """Connect to the remote server and forward messages.

        Uses the configured RetryPolicy for exponential backoff.
        Emits WSConnectionState on every state transition.
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
                    WSConnectionState.RECONNECTING
                    if attempt > 0
                    else WSConnectionState.CONNECTING
                )

                # Check max retries before attempting connection
                if (
                    self._retry_policy.max_retries is not None
                    and attempt >= self._retry_policy.max_retries
                ):
                    self._log(
                        f"Max retries ({self._retry_policy.max_retries})"
                        f" exhausted for {remote_desc}",
                        "ERROR",
                    )
                    self._set_connection_state(WSConnectionState.DISCONNECTED)
                    super().on_error(
                        RxException(
                            ConnectionError(
                                f"Max retries exhausted connecting to {url}"
                            ),
                            source=self._name,
                            note="RxWSClient.connect_client",
                        )
                    )
                    return

                # Attempt to connect to the server
                self._log(
                    f"Connecting to server {remote_desc} (attempt {attempt + 1})",
                    "INFO",
                )

                try:
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

                except TimeoutError:
                    attempt += 1
                    delay = self._retry_policy.get_delay(attempt - 1)
                    self._log(
                        f"Connection timeout, retry {attempt} in {delay:.2f}s", "WARN"
                    )
                    self._set_connection_state(WSConnectionState.DISCONNECTED)
                    await asyncio.sleep(delay)
                    continue

                except OSError as e:
                    attempt += 1
                    delay = self._retry_policy.get_delay(attempt - 1)
                    self._log(
                        f"Network error (OSError): "
                        f"{get_short_error_info(e)},"
                        f" retry {attempt} in {delay:.2f}s",
                        "WARN",
                    )
                    self._set_connection_state(WSConnectionState.DISCONNECTED)
                    await asyncio.sleep(delay)
                    continue

                except websockets.InvalidHandshake as e:
                    attempt += 1
                    delay = self._retry_policy.get_delay(attempt - 1)
                    self._log(
                        f"Invalid handshake: "
                        f"{get_short_error_info(e)},"
                        f" retry {attempt} in {delay:.2f}s",
                        "WARN",
                    )
                    self._set_connection_state(WSConnectionState.DISCONNECTED)
                    await asyncio.sleep(delay)
                    continue

                # Catch invalid URI errors - these are not retryable
                except websockets.InvalidURI as e:
                    self._log(
                        f"Invalid URI for {remote_desc}: {get_short_error_info(e)}",
                        "ERROR",
                    )
                    self._set_connection_state(WSConnectionState.DISCONNECTED)
                    super().on_error(e)
                    return

                assert self.ws is not None
                self._log(f"Server {remote_desc} Connected.", "INFO")
                self._connected = True
                self._set_connection_state(WSConnectionState.CONNECTED)

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
                self._set_connection_state(WSConnectionState.DISCONNECTED)

                # Increment attempt for reconnection
                attempt += 1

        except asyncio.CancelledError:
            self._log("WebSocket client connection cancelled.", "INFO")
            raise

        except Exception as e:
            self._log(
                f"Error connecting to {remote_desc}:\n{get_full_error_info(e)}",
                "ERROR",
            )
            self._set_connection_state(WSConnectionState.DISCONNECTED)
            super().on_error(e)

        finally:
            if self.ws is not None:
                await self.ws.close()
                self.ws = None

                self._log(
                    f"Connection to server {remote_desc} resources released.", "INFO"
                )

    # ---------------- threaded event loop plumbing ---------------- #
    async def _client_sender(
        self, websocket: ClientConnection, remote_desc: str
    ) -> None:
        try:
            assert self.queue is not None, (
                "Queue must be initialized before _client_sender"
            )
            while True:
                value = await self.queue.get()
                try:
                    await websocket.send(self.adapter.package(value))
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    self._log(
                        f"Failed to send to server {remote_desc},"
                        f" connection broken: {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
                except websockets.exceptions.ConnectionClosed as e:
                    self._log(
                        f"Failed to send to server {remote_desc},"
                        f" connection closed: {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
        except asyncio.CancelledError:
            raise

    async def _client_receiver(
        self, websocket: ClientConnection, remote_desc: str
    ) -> None:
        try:
            while True:
                try:
                    data = await websocket.recv()
                except OSError as e:
                    self._log(
                        f"Network error (OSError): {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
                except websockets.ConnectionClosedError as e:
                    self._log(
                        f"Server {remote_desc} closed"
                        f" with error: {get_short_error_info(e)}.",
                        "WARN",
                    )
                    return
                except websockets.ConnectionClosedOK:
                    self._log(
                        f"Server {remote_desc} Connection closed gracefully.",
                        "INFO",
                    )
                    return

                try:
                    payload = self.adapter.unpackage(data)
                except Exception as e:
                    self._log(
                        f"Failed to unpackage from {remote_desc}:"
                        f" {get_short_error_info(e)}",
                        "ERROR",
                    )
                    continue

                super().on_next(payload)
        except asyncio.CancelledError:
            raise

    def _run_loop(self) -> None:
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

    def _start_loop_thread(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
