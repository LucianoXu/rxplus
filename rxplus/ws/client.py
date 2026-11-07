"""Resilient ReactiveX-compatible WebSocket client.

Provides WSConnectionState, RetryPolicy, and RxWSClient which connects to a
remote server on a background thread with automatic reconnection.
"""

import asyncio
import random
import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

import websockets
from opentelemetry._logs import LoggerProvider
from opentelemetry.trace import Tracer, TracerProvider
from reactivex import Observable, Subject
from reactivex import operators as ops
from reactivex.subject import BehaviorSubject
from websockets import ClientConnection

from ..mechanism import RxException
from ..utils import get_full_error_info, get_short_error_info
from ._otel_mixin import OTelLoggingMixin
from ._sendqueue import (
    OFF,
    LiveFlag,
    QueueOverflow,
    SendOverflow,
    SendQueue,
    wire_size,
)
from .codec import WSCodec
from .datatypes import WSConnectionConfig, WSDatatypeName, codec_for

if TYPE_CHECKING:
    from ..reacting import ReportTo
    from ..reacting._component import ComponentReport
    from ..reacting.handles import Element, Link

DEFAULT_MAX_SIZE = 1 << 20  # 1 MiB: largest incoming message by default


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
        stable_after: A connection that stays open at least this many seconds
            resets the backoff, and the client reconnects at once when it
            drops. A connection that closes sooner counts as a failed attempt,
            so a server that accepts and immediately closes is retried with
            backoff instead of in a tight loop; such drops also count toward
            ``max_retries``.
    """

    max_retries: int | None = None  # None = infinite
    base_delay: float = 0.5
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    jitter: float = 0.1  # randomization factor (0.0-1.0)
    stable_after: float = 1.0

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number (0-indexed).

        Uses exponential backoff with jitter:
        delay = min(base_delay * (backoff_factor ^ attempt), max_delay) +/- jitter
        """
        delay = min(self.base_delay * (self.backoff_factor**attempt), self.max_delay)
        jitter_range = delay * self.jitter
        return delay + random.uniform(-jitter_range, jitter_range)


class RxWSClient(Subject[Any], OTelLoggingMixin):
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
        If True, queue messages while disconnected (up to ``send_queue``).
        If False (default), messages are dropped when not connected.
    open_timeout : float | None
        Maximum time in seconds for the TCP connect plus the WebSocket
        handshake of one attempt. ``None`` waits indefinitely.
    allow_pickle : bool
        Allow ``datatype="object"``. Unpickling runs the peer's code:
        trusted links only; use :class:`~rxplus.ws.codec.JsonCodec` for
        custom objects.
    max_size : int | None
        Largest incoming message in bytes (None: unlimited).
    send_queue : int | None
        Frames queued before ``overflow`` applies (None: unbounded).
    overflow : "drop_old" | "drop_new" | "disconnect"
        What a full send queue does while connected; ``"disconnect"`` drops
        the connection (the client then reconnects with an empty queue).
        While offline (``buffer_while_disconnected``) a full buffer always
        drops its oldest frame: there is no connection to drop.
    send_queue_bytes : int | None
        Optional bound on the queued bytes; ``overflow`` applies when either
        bound is reached.

    Error semantics (0.2): a value that cannot be encoded or decoded, or a
    subscriber that raises, is logged and the item dropped. ``on_error``
    shuts the client down and forwards the error; only lifecycle failures
    (retries exhausted, invalid URI) error the client by themselves.

    Raises
    ------
    RxException
        Wrapped lower-level exceptions forwarded through the ReactiveX error
        channel.
    """

    def __init__(
        self,
        config: WSConnectionConfig,
        datatype: WSDatatypeName | WSCodec[object] = "string",
        retry_policy: RetryPolicy | None = None,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 30.0,
        name: str | None = None,
        buffer_while_disconnected: bool = False,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
        open_timeout: float | None = 10.0,
        *,
        allow_pickle: bool = False,
        max_size: int | None = DEFAULT_MAX_SIZE,
        send_queue: int | None = 1024,
        overflow: SendOverflow = "drop_old",
        send_queue_bytes: int | None = None,
        reacting: "ReportTo | None" = None,
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
            open_timeout: Maximum time in seconds for one connection attempt
                (TCP connect plus handshake). ``None`` waits indefinitely.
            reacting: Report to a :class:`~rxplus.reacting.Reporter` or under
                an :class:`~rxplus.reacting.Element`; ``None`` installs
                nothing. See ``docs/reacting.md`` (Components).
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
        # "object" without allow_pickle fails here.
        self.adapter: WSCodec[object] = codec_for(datatype, allow_pickle=allow_pickle)
        SendQueue(send_queue, overflow, send_queue_bytes)  # validate now
        self._send_queue = send_queue
        self._send_queue_bytes = send_queue_bytes
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._overflow: SendOverflow = overflow
        self._max_size = max_size

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self._open_timeout = open_timeout
        self._buffer_while_disconnected = buffer_while_disconnected

        # Outbound frames; created in _run_loop, used on the loop thread only
        self.queue: SendQueue | None = None
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

        # Reporting: the client's element and ports; while connected, a
        # `socket` child with its links (loop thread).
        self._report: ComponentReport | None = None
        self._socket_element: Element | None = None
        self._recv_link: Link | None = None
        self._recv_st: LiveFlag = OFF  # its reporter's live state: one check
        if reacting is not None:
            from ..reacting import _component

            self._report = _component.ComponentReport(
                reacting,
                name or f"RxWSClient {_component.path_label(self.path)}",
                "rxplus.ws.client",
                ("send", "recv"),
            )

        self._start_loop_thread()

    @property
    def reacting_element(self) -> "Element | None":
        """The client's element, when it reports (``reacting=``)."""
        return None if self._report is None else self._report.element

    def reacting_port(self, name: str) -> "Element | None":
        """The ``"send"`` or ``"recv"`` port, to link your probes to."""
        return None if self._report is None else self._report.port(name)

    def _report_socket(self) -> None:
        """Loop thread, on connect: the socket's element and links."""
        report = self._report
        if report is None or self.queue is None:
            return
        element = report.instance("socket", "rxplus.ws.socket")
        self._socket_element = element
        self.queue.bind(
            report.link(report.ports["send"], element, capacity=self._send_queue)
        )
        self._recv_link = report.link(element, report.ports["recv"])
        self._recv_st = self._recv_link._st

    def _unreport_socket(self) -> None:
        """Loop thread, on disconnect: the queued frames stay, unreported
        until the next socket."""
        if self.queue is not None:
            self.queue.bind(None)
        self._recv_link = None
        self._recv_st = OFF
        if self._socket_element is not None and self._report is not None:
            # With its links, after the window that holds its last counts.
            self._report.retire(self._socket_element, asyncio.get_running_loop())
            self._socket_element = None

    @property
    def connection_state(self) -> Observable[WSConnectionState]:
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

        try:
            frame = self.adapter.encode(value)
        except Exception as e:
            # 0.1 raised into the caller's on_next (type check) or ended the
            # client (packaging); 0.2 drops the one value.
            self._count_dropped()
            self._log(
                f"Dropped a value that could not be encoded: {get_short_error_info(e)}",
                "ERROR",
            )
            return
        if not self._loop_ready.wait(timeout=5.0):
            self._log("Dropped a value: the client event loop did not start", "ERROR")
            return

        loop = self._loop
        if loop is not None and loop.is_closed():
            # The client ended (terminal error); like after on_completed,
            # values are dropped.
            return
        if loop is None or self.queue is None:
            rx_exception = RxException(
                RuntimeError("Client event loop not available"),
                source=self._name,
                note="RxWSClient.on_next",
            )
            super().on_error(rx_exception)
            return

        try:
            loop.call_soon_threadsafe(self._enqueue, frame)
        except RuntimeError:
            # The loop closed between the check above and this call.
            return

    @property
    def dropped(self) -> int:
        """Items dropped so far: values that could not be encoded, frames
        that did not decode, subscribers raising, and frames discarded by the
        send queue. A reporter (X4) reports them as ``dropped``."""
        with self._dropped_lock:
            return self._dropped

    def _count_dropped(self, n: int = 1) -> None:
        with self._dropped_lock:
            self._dropped += n

    def _enqueue(self, frame: str | bytes) -> None:
        """Loop thread: queue one frame, applying the overflow policy."""
        queue = self.queue
        assert queue is not None
        if queue.overflowed:
            self._count_dropped()  # disconnecting: the frame is lost
            queue.lost()
            return
        # Offline there is no connection to drop: the buffer drops oldest.
        offline = self.ws is None or not self._connected
        before = queue.dropped
        if not queue.put(frame, "drop_old" if offline else None):
            self._count_dropped()
            queue.lost()
            self._log(  # once per connection
                f"Send queue overflowed ({queue.limit} frames); disconnecting", "WARN"
            )
            if self.ws is not None:
                asyncio.get_running_loop().create_task(self.ws.close())
        elif queue.dropped != before:
            self._count_dropped(queue.dropped - before)
            if before == 0 or queue.dropped // 1000 != before // 1000:
                self._log(
                    f"Send queue full; {queue.dropped} frames dropped so far"
                    f" ({'drop_old' if offline else queue.overflow})",
                    "WARN",
                )

    def on_error(self, error: Exception) -> None:
        """Shut the client down, then forward the error to subscribers."""
        self._shutdown_requested = True
        self._set_connection_state(WSConnectionState.CLOSED)
        self._shutdown_client(error)

    def on_completed(self) -> None:
        """Close the connection before completing."""
        self._shutdown_requested = True
        self._set_connection_state(WSConnectionState.CLOSED)
        self._shutdown_client(None)

    def _shutdown_client(self, error: Exception | None) -> None:
        """Shutdown the client, stop the event loop, and join the thread."""
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
            # Complete the connection state subject
            self._connection_state_subject.on_completed()
            if self._report is not None:
                self._report.remove()  # with its socket, ports and links
            if error is None:
                super().on_completed()
            else:
                super().on_error(error)
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

                connect_span = (
                    None
                    if self._report is None
                    else self._report.element.span("connect")
                )
                try:
                    try:
                        # websockets.connect has its own open_timeout (default
                        # 10 s); pass ours so None really means "no limit".
                        self.ws = await asyncio.wait_for(
                            websockets.connect(
                                url,
                                ping_interval=self.ping_interval,
                                ping_timeout=self.ping_timeout,
                                max_size=self._max_size,
                                open_timeout=self._open_timeout,
                            ),
                            self._open_timeout,
                        )
                    except BaseException as e:
                        if connect_span is not None:
                            connect_span.end(e)  # error: the exception's class
                        raise
                    if connect_span is not None:
                        connect_span.end()

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
                connected_at = asyncio.get_running_loop().time()
                self._log(f"Server {remote_desc} Connected.", "INFO")
                assert self.queue is not None
                if self.queue.overflowed:
                    # The previous connection overflowed under "disconnect"
                    # but its sender ended another way (e.g. ConnectionClosed
                    # while blocked in send): start this one clean, and count
                    # what the overflow discarded.
                    self._count_dropped(self.queue.reset())
                self._connected = True
                self._report_socket()
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

                self._connected = False
                self._unreport_socket()
                for task in done:
                    task.result()

                self._set_connection_state(WSConnectionState.DISCONNECTED)

                # A connection that stayed up resets the backoff and is
                # re-established at once; one that closed quickly counts as a
                # failed attempt and waits, so accept-then-close servers are
                # not hammered in a tight loop.
                uptime = asyncio.get_running_loop().time() - connected_at
                if uptime >= self._retry_policy.stable_after:
                    attempt = 1
                else:
                    attempt += 1
                    delay = self._retry_policy.get_delay(attempt - 1)
                    self._log(
                        f"Connection closed after {uptime:.2f}s,"
                        f" retry {attempt} in {delay:.2f}s",
                        "WARN",
                    )
                    await asyncio.sleep(delay)

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
            self._unreport_socket()
            if self.ws is not None:
                await self.ws.close()
                self.ws = None

                self._log(
                    f"Connection to server {remote_desc} resources released.", "INFO"
                )

            if not self._shutdown_requested:
                # Terminal error (retries exhausted, invalid URI, unexpected
                # exception): nothing will reconnect, so release the loop
                # thread instead of leaving it running forever.
                self._shutdown_requested = True
                self._connected = False
                asyncio.get_running_loop().stop()

    # ---------------- threaded event loop plumbing ---------------- #
    async def _client_sender(
        self, websocket: ClientConnection, remote_desc: str
    ) -> None:
        try:
            assert self.queue is not None, (
                "Queue must be initialized before _client_sender"
            )
            while True:
                try:
                    frame = await self.queue.get()  # encoded in on_next
                except QueueOverflow:
                    # overflow="disconnect": drop this connection; the next
                    # one starts with an empty queue.
                    self._count_dropped(self.queue.reset())
                    return
                try:
                    await websocket.send(frame)
                    self.queue.sent(frame)
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    self.queue.lost(taken=True)
                    self._count_dropped()  # the frame in flight is lost
                    self._log(
                        f"Failed to send to server {remote_desc},"
                        f" connection broken: {get_short_error_info(e)}",
                        "WARN",
                    )
                    return
                except websockets.exceptions.ConnectionClosed as e:
                    self.queue.lost(taken=True)
                    self._count_dropped()  # the frame in flight is lost
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

                recv = self._recv_link
                try:
                    payload = self.adapter.decode(data)
                except Exception as e:
                    if recv is not None:
                        recv.drop()
                    self._count_dropped()
                    self._log(
                        f"Dropped a message from {remote_desc} that did not"
                        f" decode: {get_short_error_info(e)}",
                        "ERROR",
                    )
                    continue
                if self._recv_st.live and recv is not None:
                    recv.deliver(1, bytes=wire_size(data))
                try:
                    super().on_next(payload)
                except Exception as e:
                    # A subscriber raised: its problem, not the connection's.
                    self._count_dropped()
                    self._log(
                        f"Dropped a message from {remote_desc}: a subscriber"
                        f" raised {get_short_error_info(e)}",
                        "ERROR",
                    )
        except asyncio.CancelledError:
            raise

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        # Create queue after event loop is set up
        self.queue = SendQueue(self._send_queue, self._overflow, self._send_queue_bytes)
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
