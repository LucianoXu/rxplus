"""
Communication by websocket.
"""

import asyncio
import os
import pickle
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Literal, Optional

import reactivex as rx
import websockets

# Compat helpers for newer websockets API

from websockets import ClientConnection, Server, ServerConnection

from reactivex import Observable, Observer, Subject, create
from reactivex import operators as ops

from .logging import *
from .mechanism import RxException
from .utils import TaggedData, get_full_error_info, get_short_error_info


def _ws_path(ws: ServerConnection | ClientConnection) -> str:
    """Return the request path of a WebSocket connection."""
    req = ws.request
    if req is not None:
        return getattr(req, "path", "")
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
        pass

    def package(self, value):
        return str(value)

    def unpackage(self, value):
        return value


class WSBytes(WSDatatype):

    def package_type_check(self, value) -> None:
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("WSRawBytes expects a bytes-like object")

    def package(self, value):
        return value

    def unpackage(self, value):
        # websockets binary frame → bytes ；text frame → str
        if isinstance(value, str):
            raise TypeError(
                f"WSRawBytes expects a bytes-like object, got str '{value}'"
            )
        return bytes(value)


def wsdt_factory(datatype: Literal["string", "bytes"]) -> WSDatatype:
    """
    Factory function to create a WSDatatype instance based on the datatype parameter.
    """
    if datatype == "string":
        return WSStr()
    elif datatype == "bytes":
        return WSBytes()
    else:
        raise ValueError(f"Unsupported datatype '{datatype}'.")


# we use dictionary to serve as connection configuration
# example:
# {
#   host : 'localhost',
#   port : 1492,
#   path : '/',
# }


class WS_Channels:
    """
    The class to manage the websocket channels of the same path.
    """

    def __init__(self, datatype: Literal["string", "bytes"] = "string"):
        self.adapter: WSDatatype = wsdt_factory(datatype)
        self.channels: set[ServerConnection] = set()
        self.queues: set[asyncio.Queue] = set()


class RxWSServer(Subject):
    """
    The websocket server for bi-directional communication between ReactiveX components.
    The server can be connected by multiple clients.

    The server can handle connections from multiple clients on different paths. Here different paths means the original URI path.

    It will wrap the data in a `TaggedData` object with the path, and call `on_next`.

    When `on_next` is called, it will check whether the value is a `TaggedData`. If it is, it will send the data to the corresponding path's channels. If it is not, it will send the data to the default path's channels (i.e., the empty path).

    Use datatype parameter to control the data type sent through the websocket.
    The server will be closed upon receiving on_completed signal.
    """

    def __init__(
        self,
        conn_cfg: dict,
        logcomp: Optional[LogComp] = None,
        recv_timeout: float = 0.001,
        datatype: (
            Callable[[str], Literal["string", "bytes"]] | Literal["string", "bytes"]
        ) = "string",
        ping_interval: Optional[int] = 20,
        ping_timeout: Optional[int] = 20,
    ):
        """Initialize the WebSocket server and start listening."""
        super().__init__()
        self.host = conn_cfg["host"]
        self.port = int(conn_cfg["port"])

        # setup the log source
        if logcomp is None:
            logcomp = EmptyLogComp()

        self.logcomp = logcomp
        self.logcomp.set_super(super())

        # Store connected clients
        self.recv_timeout = recv_timeout

        # the function to determine the datatype of the path
        self.datatype_func: Callable[[str], Literal["string", "bytes"]]
        if datatype in ["string", "bytes"]:
            self.datatype_func = lambda path: datatype  # type: ignore
        elif callable(datatype):
            self.datatype_func = datatype
        else:
            raise ValueError(
                f"Unsupported datatype '{datatype}'. Expected 'string', 'bytes', or a callable function."
            )

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        self.path_channels: dict[str, WS_Channels] = {}

        asyncio.create_task(self.start_server())

        self.serve: Optional[Server] = None
        self.stop = asyncio.Future()

    def _get_path_channels(self, path: str) -> WS_Channels:
        """
        Get the WS_Channels instance for the given path.
        If the path does not exist, create a new WS_Channels instance.
        """
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
            rx_exception = self.logcomp.get_rx_exception(
                ValueError(f"Expected TaggedData, but got {type(value)}"),
                note=f"RxWSServer.on_next",
            )
            super().on_error(rx_exception)
            return

        # type check data
        ws_channels.adapter.package_type_check(data)

        # push the data
        for queue in ws_channels.queues:
            queue.put_nowait(data)

    def on_error(self, error):
        """Forward errors to subscribers."""
        super().on_error(error)

    def on_completed(self) -> None:
        """Complete the server after closing all connections."""
        asyncio.create_task(self.async_completing())

    async def async_completing(self):
        """Async helper to close connections and finish the server."""

        try:
            # close all connections
            self.logcomp.log(f"Closing...", "INFO")

            if not self.stop.done():
                self.stop.set_result(None)

            if self.serve is not None:
                self.serve.close(True)
                self.serve = None

            self.logcomp.log(f"Closed.", "INFO")
            super().on_completed()

        except asyncio.CancelledError:
            self.logcomp.log(f"Async completing cancelled.", "INFO")
            raise

    async def handle_client(self, websocket: ServerConnection):
        """Serve a connected WebSocket client until the link closes."""

        path = _ws_path(websocket)
        remote_desc = f"[{websocket.remote_address} on path {path}]"

        self.logcomp.log(f"Client established from {remote_desc}", "INFO")

        try:
            ws_channels = self._get_path_channels(path)

            queue = asyncio.Queue()

            # Register client
            ws_channels.channels.add(websocket)
            ws_channels.queues.add(queue)

            # the connection information is received

            while True:
                await asyncio.sleep(0)
                # try to send data to the client
                if not queue.empty():
                    value = queue.get_nowait()

                    try:
                        # Broadcast message to all connected clients
                        await websocket.send(ws_channels.adapter.package(value))
                        queue.task_done()

                    except (ConnectionResetError, BrokenPipeError, OSError) as e:
                        self.logcomp.log(
                            f"Failed to send data to client {remote_desc}, connection may be broken: {get_short_error_info(e)}",
                            "WARNING",
                        )
                        break

                    except websockets.exceptions.ConnectionClosed as e:
                        self.logcomp.log(
                            f"Failed to send data to client {remote_desc}, connection closed: {get_short_error_info(e)}",
                            "WARNING",
                        )
                        break

                try:
                    # try to recieve from the client
                    data = await asyncio.wait_for(websocket.recv(), self.recv_timeout)

                    # process the received data
                    data = ws_channels.adapter.unpackage(data)
                    wrapped_data = TaggedData(path, data)

                    super().on_next(wrapped_data)

                except asyncio.TimeoutError:
                    pass

                except ConnectionResetError as e:
                    self.logcomp.log(
                        f"Connection reset (ConnectionResetError): {get_short_error_info(e)}",
                        "WARNING",
                    )
                    break

                except OSError as e:
                    self.logcomp.log(
                        f"Network error or connection lost (OSError): {get_short_error_info(e)}",
                        "WARNING",
                    )
                    break

                except websockets.exceptions.ConnectionClosedError as e:
                    self.logcomp.log(
                        f"Client {remote_desc} disconnected with error: {get_short_error_info(e)}.",
                        "WARNING",
                    )
                    break

                except websockets.exceptions.ConnectionClosedOK:
                    self.logcomp.log(
                        f"Client {remote_desc} disconnected gracefully.", "INFO"
                    )
                    break

        except asyncio.CancelledError:
            self.logcomp.log(f"Client {remote_desc} connection cancelled.", "INFO")
            raise

        except Exception as e:
            rx_exception = self.logcomp.get_rx_exception(
                e, note=f"Error while handling client {remote_desc}"
            )
            super().on_error(rx_exception)

        finally:

            await websocket.close()
            self.logcomp.log(f"Client {remote_desc} resources released.", "INFO")

            # Unregister client
            ws_channels.channels.discard(websocket)
            ws_channels.queues.discard(queue)

    async def start_server(self):
        """Spin up the asyncio WebSocket server."""
        try:
            self.serve = await websockets.serve(
                self.handle_client,
                self.host,
                self.port,
                ping_interval=self.ping_interval,
                ping_timeout=self.ping_timeout,
                max_size=None,
            )
            await self.stop

        except asyncio.CancelledError:
            self.logcomp.log(f"WebSocket server stopped.", "INFO")
            raise


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
      adapter chosen via the ``datatype`` argument (``"string"`` or
      ``"bytes"``).

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
    logcomp : LogComp | None
        Optional logger component; defaults to :class:`EmptyLogComp`.
    recv_timeout : float
        Seconds to wait for incoming data before yielding control to the event
        loop.
    datatype : Literal["string", "bytes"]
        Frame representation handled by :func:`wsdt_factory`.
    conn_retry_timeout : float
        Delay between automatic reconnection attempts in seconds.
    ping_interval, ping_timeout : int | None
        Values forwarded to :pyfunc:`websockets.connect` for heartbeat
        management.

    Raises
    ------
    RxException
        Wrapped lower‑level exceptions forwarded through the ReactiveX error
        channel.
    """

    def __init__(
        self,
        conn_cfg: dict,
        logcomp: Optional[LogComp] = None,
        recv_timeout: float = 0.001,
        datatype: Literal["string", "bytes"] = "string",
        conn_retry_timeout: float = 0.5,
        ping_interval: Optional[int] = 20,
        ping_timeout: Optional[int] = 20,
    ):
        """Create a reconnecting WebSocket client."""
        super().__init__()

        self.host = conn_cfg["host"]
        self.port = int(conn_cfg["port"])
        self.path = conn_cfg.get("path", "/")

        self.recv_timeout = recv_timeout

        # setup the log source
        if logcomp is None:
            logcomp = EmptyLogComp()

        self.logcomp = logcomp
        self.logcomp.set_super(super())

        self.datatype = datatype
        self.adapter: WSDatatype = wsdt_factory(datatype)

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        self.queue = asyncio.Queue()
        self.ws: Optional[ClientConnection] = None

        self.conn_retry_timeout = conn_retry_timeout

        asyncio.create_task(self.connect_client())

        # whether the connection is established. this will influence the caching strategy.
        self._connected = False

    def on_next(self, value):
        """Send a message to the server when connected."""
        if self._connected:
            self.queue.put_nowait(value)

    def on_error(self, error):
        """Report connection errors."""
        rx_exception = self.logcomp.get_rx_exception(error, note="Error")
        super().on_error(rx_exception)

    def on_completed(self) -> None:
        """Close the connection before completing."""
        asyncio.create_task(self.async_completing())

    async def async_completing(self):
        """Async helper to close the WebSocket client."""
        try:
            # close all connections
            self.logcomp.log(f"Closing...", "INFO")

            if self.ws is not None:
                await self.ws.close()
                self.ws = None

            self.logcomp.log(f"Closed.", "INFO")
            super().on_completed()

        except asyncio.CancelledError:
            self.logcomp.log(f"Async completing cancelled.", "INFO")
            raise

    async def connect_client(self):
        """Connect to the remote server and forward messages."""
        # calculate the url
        url = f"ws://{self.host}:{self.port}{self.path}"
        remote_desc = f"[{url}]"

        try:
            # Repeatedly attempt to connect to the server
            while True:

                self._connected = False
                # Attempt to connect to the server
                self.logcomp.log(f"Connecting to server {remote_desc}", "INFO")

                while True:
                    await asyncio.sleep(0)
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
                            self.conn_retry_timeout,
                        )
                        break

                    except asyncio.TimeoutError:
                        pass

                    except OSError as e:
                        self.logcomp.log(
                            f"Network error or connection failed (OSError): {get_short_error_info(e)}",
                            "WARNING",
                        )
                        await asyncio.sleep(self.conn_retry_timeout)
                        pass

                    except websockets.InvalidHandshake as e:
                        self.logcomp.log(
                            f"Invalid handshake with server {remote_desc}: {get_short_error_info(e)}",
                            "WARNING",
                        )
                        await asyncio.sleep(self.conn_retry_timeout)
                        pass

                    # Catch invalid URI errors
                    except websockets.InvalidURI as e:
                        self.logcomp.log(
                            f"Invalid URI for server {remote_desc}: {get_short_error_info(e)}",
                            "ERROR",
                        )
                        super().on_error(e)
                        return

                self.logcomp.log(f"Server {remote_desc} Connected.", "INFO")
                self._connected = True

                # Start receiving messages
                while True:
                    await asyncio.sleep(0)

                    # try to send data to the server
                    if not self.queue.empty():
                        try:
                            value = self.queue.get_nowait()
                            await self.ws.send(self.adapter.package(value))
                            self.queue.task_done()

                        except (ConnectionResetError, BrokenPipeError, OSError) as e:
                            self.logcomp.log(
                                f"Failed to send data to server {remote_desc}, connection may be broken: {get_short_error_info(e)}",
                                "WARNING",
                            )
                            break

                        except websockets.exceptions.ConnectionClosed as e:
                            self.logcomp.log(
                                f"Failed to send data to server {remote_desc}, connection closed: {get_short_error_info(e)}",
                                "WARNING",
                            )
                            break

                    try:
                        data = await asyncio.wait_for(self.ws.recv(), self.recv_timeout)
                        super().on_next(self.adapter.unpackage(data))

                    except asyncio.TimeoutError:
                        pass

                    except OSError as e:
                        self.logcomp.log(
                            f"Network error or connection lost (OSError): {get_short_error_info(e)}",
                            "WARNING",
                        )
                        break

                    except websockets.ConnectionClosedError as e:
                        self.logcomp.log(
                            f"Server {remote_desc} Connection closed with error: {get_short_error_info(e)}.",
                            "WARNING",
                        )
                        break

                    except websockets.ConnectionClosedOK:
                        self.logcomp.log(
                            f"Server {remote_desc} Connection closed gracefully.",
                            "INFO",
                        )
                        break

        except asyncio.CancelledError:
            self.logcomp.log(f"WebSocket client connection cancelled.", "INFO")
            raise

        except Exception as e:
            self.logcomp.log(
                f"Error while connecting to server {remote_desc}:\n{get_full_error_info(e)}",
                "ERROR",
            )
            super().on_error(e)

        finally:
            if self.ws is not None:
                await self.ws.close()
                self.ws = None

                self.logcomp.log(
                    f"Connection to server {remote_desc} resources released.", "INFO"
                )


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
    logcomp : LogComp | None
        Logger component shared by all spawned clients.
    recv_timeout, datatype, conn_retry_timeout, ping_interval, ping_timeout
        Passed through to the per‑path :class:`RxWSClient` factory.

    Notes
    -----
    Closed or errored clients are removed from the internal ``_clients`` cache,
    allowing their resources to be garbage‑collected.
    """

    def __init__(
        self,
        conn_cfg: dict,
        logcomp: Optional[LogComp] = None,
        recv_timeout: float = 0.001,
        datatype: (
            Callable[[str], Literal["string", "bytes"]] | Literal["string", "bytes"]
        ) = "string",
        conn_retry_timeout: float = 0.5,
        ping_interval: Optional[int] = 20,
        ping_timeout: Optional[int] = 20,
    ):
        super().__init__()

        self.datatype_func: Callable[[str], Literal["string", "bytes"]]
        if datatype in ["string", "bytes"]:
            self.datatype_func = lambda path: datatype  # type: ignore
        elif callable(datatype):
            self.datatype_func = datatype
        else:
            raise ValueError(
                f"Unsupported datatype '{datatype}'. Expected 'string', 'bytes', or a callable function."
            )

        def make_client(path: str) -> RxWSClient:
            new_conn_cfg = conn_cfg.copy()
            new_conn_cfg["path"] = path

            return RxWSClient(
                conn_cfg=new_conn_cfg,
                logcomp=logcomp,
                recv_timeout=recv_timeout,
                datatype=self.datatype_func(path),
                conn_retry_timeout=conn_retry_timeout,
                ping_interval=ping_interval,
                ping_timeout=ping_timeout,
            )

        self._client_factory = make_client
        self._clients = {}  # tag -> RxWSClient
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
    def _ensure_client(self, tag: str):
        if tag not in self._clients:
            # build new client and bridge its inbound traffic
            client = self._client_factory(tag)

            # When the client emits, wrap again with tag and push to bus
            client.pipe(
                ops.map(keep_log(lambda data: TaggedData(tag, data)))
            ).subscribe(self._bus)

            self._clients[tag] = client
        return self._clients[tag]
