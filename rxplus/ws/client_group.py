"""Multiplexing WebSocket client group.

Manages a dynamic set of RxWSClient instances -- one per WebSocket path --
behind a single ReactiveX interface.
"""

import threading
from collections.abc import Callable
from typing import Literal

from opentelemetry._logs import LoggerProvider
from opentelemetry.trace import TracerProvider
from reactivex import Subject
from reactivex import operators as ops

from ..utils import TaggedData
from .client import RetryPolicy, RxWSClient
from .datatypes import WSConnectionConfig


class RxWSClientGroup(Subject):
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

    Notes
    -----
    Closed or errored clients are removed from the internal ``_clients`` cache,
    allowing their resources to be garbage-collected.
    """

    def __init__(
        self,
        config: WSConnectionConfig,
        datatype: (
            Callable[[str], Literal["string", "bytes", "object"]]
            | Literal["string", "bytes", "object"]
        ) = "string",
        retry_policy: RetryPolicy | None = None,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 30.0,
        name: str | None = None,
        buffer_while_disconnected: bool = False,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
    ):
        super().__init__()

        self._name = name
        self._tracer_provider = tracer_provider
        self._logger_provider = logger_provider
        self._buffer_while_disconnected = buffer_while_disconnected
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
                datatype=self.datatype_func(path),
                retry_policy=retry_policy,
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
        self._bus: Subject[TaggedData] = Subject()  # merged inbound stream

    # ============ Observer interface ============ #
    def on_next(self, tagged: TaggedData) -> None:
        client = self._ensure_client(tagged.tag)
        # Push upstream - API depends on RxWSClient; assume it is Observer-like
        client.on_next(tagged.data)

    def on_error(self, err: Exception) -> None:
        # propagate error to every open client and downstream
        for c in self._clients.values():
            c.on_error(err)
        self._bus.on_error(err)

    def on_completed(self) -> None:
        for c in self._clients.values():
            c.on_completed()
        self._bus.on_completed()

    # ============ Observable interface ============ #
    def _subscribe_core(self, observer, scheduler=None):
        return self._bus.subscribe(observer, scheduler=scheduler)

    def open_path(self, path: str) -> None:
        """Open a new path for the client group."""
        self._ensure_client(path)

    # ============ internal helpers ============ #
    def _ensure_client(self, tag: str) -> RxWSClient:
        """Get or create a client for the given tag. Thread-safe."""
        with self._clients_lock:
            if tag not in self._clients:
                # build new client and bridge its inbound traffic
                client = self._client_factory(tag)

                # When the client emits, wrap again with tag and push to bus
                client.pipe(ops.map(lambda data: TaggedData(tag, data))).subscribe(
                    self._bus
                )

                self._clients[tag] = client
            return self._clients[tag]
