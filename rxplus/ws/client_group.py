"""Multiplexing WebSocket client group.

Manages a dynamic set of RxWSClient instances -- one per WebSocket path --
behind a single ReactiveX interface.
"""

import threading

from opentelemetry._logs import LoggerProvider
from opentelemetry.trace import TracerProvider
from reactivex import Observable, Subject

from ..utils import TaggedData
from ._otel_mixin import OTelLoggingMixin
from ._sendqueue import SendOverflow
from .client import DEFAULT_MAX_SIZE, RetryPolicy, RxWSClient
from .datatypes import DatatypeSpec, WSConnectionConfig, resolve_codecs


class RxWSClientGroup(Subject, OTelLoggingMixin):
    """A multiplexing subject that manages a dynamic set of RxWSClient instances.

    One client per WebSocket path, behind a single ReactiveX interface.

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

    Parameters
    ----------
    config : WSConnectionConfig
        Base connection information (host and port). The path for each
        connection is supplied by individual :class:`TaggedData` items.
    datatype
        Passed through to the per-path :class:`RxWSClient` factory.
    retry_policy : RetryPolicy | None
        Forwarded to child clients for reconnection behavior.
    ping_interval, ping_timeout
        Passed through to the per-path :class:`RxWSClient` factory.
    name : str | None
        Custom name for log source identification. Child clients will use
        ``"{name}:{path}"`` as their source name. If not provided, child
        clients use their default naming.
    open_timeout : float | None
        Forwarded to child clients: maximum time for one connection attempt.

    allow_pickle, max_size, send_queue, overflow
        Passed to every child client (see :class:`RxWSClient`).

    Failures (0.2)
    --------------
    A child that ends with an error (e.g. its retries are exhausted) is
    removed, and ``(path, error)`` is emitted on :attr:`child_errors`; the
    group and its other children go on, and the next value for that path
    starts a new child. The group ends only on its own ``on_completed`` or
    ``on_error`` (0.1: any child's error or completion ended the group).
    """

    def __init__(
        self,
        config: WSConnectionConfig,
        datatype: DatatypeSpec = "string",
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
    ):
        super().__init__()
        self._name = name or f"RxWSClientGroup:{config.host}:{config.port}"
        self._logger = (
            logger_provider.get_logger(f"rxplus.{self._name}")
            if logger_provider
            else None
        )
        self._dropped = 0
        self._dropped_lock = threading.Lock()

        self._tracer_provider = tracer_provider
        self._logger_provider = logger_provider
        self._buffer_while_disconnected = buffer_while_disconnected
        # Codec per path; a fixed "object" without allow_pickle fails here.
        codec_for_path = resolve_codecs(datatype, allow_pickle=allow_pickle)

        def make_client(path: str) -> RxWSClient:
            child_config = WSConnectionConfig(
                host=config.host,
                port=config.port,
                path=path,
            )

            # Generate child name if parent has a name
            child_name = f"{name}:{path}" if name else None

            return RxWSClient(
                config=child_config,
                datatype=codec_for_path(path),  # checked for pickle here
                allow_pickle=allow_pickle,
                retry_policy=retry_policy,
                ping_interval=ping_interval,
                ping_timeout=ping_timeout,
                name=child_name,
                buffer_while_disconnected=buffer_while_disconnected,
                tracer_provider=tracer_provider,
                logger_provider=logger_provider,
                open_timeout=open_timeout,
                max_size=max_size,
                send_queue=send_queue,
                overflow=overflow,
                send_queue_bytes=send_queue_bytes,
            )

        self._client_factory = make_client
        self._clients_lock = threading.Lock()
        self._clients: dict[str, RxWSClient] = {}  # tag -> RxWSClient
        self._bus: Subject[TaggedData] = Subject()  # merged inbound stream
        self._child_errors: Subject[tuple[str, Exception]] = Subject()
        self._stopped = False

    @property
    def child_errors(self) -> Observable[tuple[str, Exception]]:
        """``(path, error)`` for every child that ended with an error."""
        return self._child_errors

    # ============ Observer interface ============ #
    @property
    def dropped(self) -> int:
        """Values the group itself dropped (not ``TaggedData``, or a path
        whose datatype is refused); each child counts its own."""
        with self._dropped_lock:
            return self._dropped

    def _count_dropped(self) -> None:
        with self._dropped_lock:
            self._dropped += 1

    def on_next(self, tagged: TaggedData) -> None:
        """Send ``tagged.data`` on the path ``tagged.tag``.

        A value that is not ``TaggedData``, or a path whose datatype is
        refused (a per-path function returning ``"object"`` without
        ``allow_pickle``), is logged and dropped. After the group ended,
        values are ignored.
        """
        if not isinstance(tagged, TaggedData):
            self._count_dropped()
            self._log(
                f"Dropped a {type(tagged).__name__}: RxWSClientGroup expects"
                " TaggedData",
                "ERROR",
            )
            return
        try:
            client = self._ensure_client(tagged.tag)
        except Exception as e:
            self._count_dropped()
            self._log(
                f"Dropped a value for path {tagged.tag!r}: {type(e).__name__}: {e}",
                "ERROR",
            )
            return
        if client is not None:
            client.on_next(tagged.data)

    def _stop_children(self) -> None:
        with self._clients_lock:
            self._stopped = True
            children = list(self._clients.values())
            self._clients.clear()
        for child in children:
            child.on_completed()

    def on_error(self, err: Exception) -> None:
        """Close every child, then forward the error downstream."""
        self._stop_children()
        self._child_errors.on_completed()
        self._bus.on_error(err)

    def on_completed(self) -> None:
        self._stop_children()
        self._child_errors.on_completed()
        self._bus.on_completed()

    # ============ Observable interface ============ #
    def _subscribe_core(self, observer, scheduler=None):
        return self._bus.subscribe(observer, scheduler=scheduler)

    def open_path(self, path: str) -> None:
        """Open a new path for the client group."""
        self._ensure_client(path)

    # ============ internal helpers ============ #
    def _ensure_client(self, tag: str) -> RxWSClient | None:
        """Get or create a client for the given tag (None once stopped)."""
        with self._clients_lock:
            if self._stopped:
                return None
            client = self._clients.get(tag)
            if client is None:
                client = self._clients[tag] = self._client_factory(tag)
                self._bridge(tag, client)
            return client

    def _bridge(self, tag: str, client: RxWSClient) -> None:
        """Forward a child's messages; a child's end affects only itself."""

        def forget() -> bool:
            with self._clients_lock:
                if self._clients.get(tag) is client:
                    del self._clients[tag]
                    return True
                return False

        def on_error(error: Exception) -> None:
            if forget():
                self._child_errors.on_next((tag, error))

        def on_completed() -> None:
            forget()

        client.subscribe(
            on_next=lambda data: self._bus.on_next(TaggedData(tag, data)),
            on_error=on_error,
            on_completed=on_completed,
        )
