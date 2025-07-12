'''
Communication by websocket.
'''

from typing import Any, Callable, Literal, Optional

import asyncio
import time
import websockets
import pickle
import os

from abc import ABC, abstractmethod

import reactivex as rx
from reactivex import Observable, Observer, Subject, create, operators as ops

from .logging import *
from .utils import TaggedData, get_short_error_info, get_full_error_info


class WSDatatype(ABC):

    @abstractmethod
    def package_type_check(self, value) -> None:
        '''
        Check whether the value can be sent through this datatype.
        If not, an error will be rased.
        '''
        ...

    @abstractmethod
    def package(self, value) -> Any:
        ...

    @abstractmethod
    def unpackage(self, value) -> Any:
        ...
    
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
            raise TypeError(f"WSRawBytes expects a bytes-like object, got str '{value}'")
        return bytes(value)
    
def wsdt_factory(datatype: Literal['string', 'byte']) -> WSDatatype:
    '''
    Factory function to create a WSDatatype instance based on the datatype parameter.
    '''
    if datatype == 'string':
        return WSStr()
    elif datatype == 'byte':
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
    '''
    The class to manage the websocket channels of the same path.
    '''
    def __init__(
        self, 
        datatype: Literal['string', 'byte'] = 'string'):
        self.adapter: WSDatatype = wsdt_factory(datatype)
        self.channels: set[websockets.WebSocketServerProtocol] = set()
        self.queues: set[asyncio.Queue] = set()

class RxWSServer(Subject):
    '''
    The websocket server for bi-directional communication between ReactiveX components.
    The server can be connected by multiple clients.
    
    The server can handle connections from multiple clients on different paths. Here different paths means the original URI path.

    For the channels without the path, it will call `on_next` on received data directly. For the channels with the path, it will wrap the data in a `TaggedData` object, and call `on_next`.

    When `on_next` is called, it will check whether the value is a `TaggedData`. If it is, it will send the data to the corresponding path's channels. If it is not, it will send the data to the default path's channels (i.e., the empty path).

    Use datatype parameter to control the data type sent through the websocket.
    The server will be closed upon receiving on_completed signal.
    '''
    def __init__(self,
                 conn_cfg: dict,
                 logcomp: Optional[LogComp] = None,
                 recv_timeout: float = 0.001,
                 datatype: Callable[[str], Literal['string', 'byte']] | Literal['string', 'byte'] = 'string',
                 ping_interval: Optional[int] = 20,
                 ping_timeout: Optional[int] = 20):
        """Initialize the WebSocket server and start listening."""
        super().__init__()
        self.host = conn_cfg['host']
        self.port = int(conn_cfg['port'])

        # setup the log source
        if logcomp is None:
            logcomp = EmptyLogComp()

        self.logcomp = logcomp
        self.logcomp.set_super(super())

        # Store connected clients
        self.recv_timeout = recv_timeout

        # the function to determine the datatype of the path
        self.datatype_func: Callable[[str], Literal['string', 'byte']]
        if datatype in ['string', 'byte']:
            self.datatype_func = lambda path: datatype # type: ignore
        elif callable(datatype):
            self.datatype_func = datatype
        else:
            raise ValueError(f"Unsupported datatype '{datatype}'. Expected 'string', 'byte', or a callable function.")

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        self.path_channels: dict[str, WS_Channels] = {}


        asyncio.create_task(self.start_server())

        self.serve : Optional[websockets.WebSocketServer] = None
        self.stop = asyncio.Future()

    def _get_path_channels(self, path: str) -> WS_Channels:
        '''
        Get the WS_Channels instance for the given path.
        If the path does not exist, create a new WS_Channels instance.
        '''
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
            ws_channels = self._get_path_channels('/')
            data = value
        
        # type check data
        ws_channels.adapter.package_type_check(data)

        # push the data
        for queue in ws_channels.queues:
            queue.put_nowait(data)
        
    def on_error(self, error):
        """Forward errors to subscribers and log them."""
        self.logcomp.log(f"Error: {error}.", "ERROR")
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


    async def handle_client(self, websocket: websockets.WebSocketServerProtocol):
        """Serve a connected WebSocket client until the link closes."""

        remote_desc = f"[{websocket.remote_address} on path {websocket.path}]"

        self.logcomp.log(f"Client established from {remote_desc}", "INFO")

        try:
            ws_channels = self._get_path_channels(websocket.path)

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
                        self.logcomp.log(f"Failed to send data to client {remote_desc}, connection may be broken: {get_short_error_info(e)}", "WARNING")
                        break
                        
                    except websockets.exceptions.ConnectionClosed as e:
                        self.logcomp.log(f"Failed to send data to client {remote_desc}, connection closed: {get_short_error_info(e)}", "WARNING")
                        break

                try:
                    # try to recieve from the client
                    data = await asyncio.wait_for(websocket.recv(), self.recv_timeout)

                    # process the received data
                    data = ws_channels.adapter.unpackage(data)
                    if websocket.path != '/':
                        data = TaggedData(websocket.path, data)
                        
                    super().on_next(data)

                except asyncio.TimeoutError:
                    pass

                except ConnectionResetError as e:
                    self.logcomp.log(f"Connection reset (ConnectionResetError): {get_short_error_info(e)}", "WARNING")
                    break

                except OSError as e:
                    self.logcomp.log(f"Network error or connection lost (OSError): {get_short_error_info(e)}", "WARNING")
                    break

                except websockets.exceptions.ConnectionClosedError as e:
                    self.logcomp.log(f"Client {remote_desc} disconnected with error: {get_short_error_info(e)}.", "WARNING")
                    break

                except websockets.exceptions.ConnectionClosedOK:
                    self.logcomp.log(f"Client {remote_desc} disconnected gracefully.", "INFO")
                    break

        except asyncio.CancelledError:
            self.logcomp.log(f"Client {remote_desc} connection cancelled.", "INFO")
            raise
                

        except Exception as e:
            self.logcomp.log(f"Error while handling client {remote_desc}:\n{get_full_error_info(e)}", "ERROR")
            super().on_error(e)


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
                max_size=None)
            await self.stop

        except asyncio.CancelledError:
            self.logcomp.log(f"WebSocket server initialization cancelled.", "INFO")
            raise


class RxWSClient(Subject):
    '''
    The client only recieves data from one server.
    When connection fails, it will repeatedly retry to connect to the server.
    The client will be closed upon receiving on_completed signal.
    '''
    def __init__(self,
        conn_cfg: dict,
        logcomp: Optional[LogComp] = None,
        recv_timeout: float = 0.001,
        datatype: Literal['string', 'byte'] = 'string',
        conn_retry_timeout: float = 0.5,
        ping_interval: Optional[int] = 20,
        ping_timeout: Optional[int] = 20):
        """Create a reconnecting WebSocket client."""
        super().__init__()

        self.host = conn_cfg['host']
        self.port = int(conn_cfg['port'])
        self.path = conn_cfg.get('path', '/')

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
        self.ws : Optional[websockets.WebSocketClientProtocol] = None

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
        self.logcomp.log(f"Error: {error}.", "ERROR")
        super().on_error(error)

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
                        '''
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
                        '''

                        self.ws = await asyncio.wait_for(
                            websockets.connect(
                                url,
                                ping_interval=self.ping_interval,
                                ping_timeout=self.ping_timeout,
                                max_size=None
                            ), 
                            self.conn_retry_timeout
                        )
                        break

                        
                    except asyncio.TimeoutError:
                        pass

                    except OSError as e:
                        self.logcomp.log(f"Network error or connection failed (OSError): {get_short_error_info(e)}", "WARNING")
                        await asyncio.sleep(self.conn_retry_timeout)
                        pass

                    except websockets.InvalidHandshake as e:
                        self.logcomp.log(f"Invalid handshake with server {remote_desc}: {get_short_error_info(e)}", "WARNING")
                        await asyncio.sleep(self.conn_retry_timeout)
                        pass

                    # Catch invalid URI errors
                    except websockets.InvalidURI as e:
                        self.logcomp.log(f"Invalid URI for server {remote_desc}: {get_short_error_info(e)}", "ERROR")
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
                            self.logcomp.log(f"Failed to send data to server {remote_desc}, connection may be broken: {get_short_error_info(e)}", "WARNING")
                            break
                            
                        except websockets.exceptions.ConnectionClosed as e:
                            self.logcomp.log(f"Failed to send data to server {remote_desc}, connection closed: {get_short_error_info(e)}", "WARNING")
                            break
                        
                    try:
                        data = await asyncio.wait_for(self.ws.recv(), self.recv_timeout)
                        super().on_next(self.adapter.unpackage(data))
                
                    except asyncio.TimeoutError:
                        pass

                    except OSError as e:
                        self.logcomp.log(f"Network error or connection lost (OSError): {get_short_error_info(e)}", "WARNING")
                        break

                    except websockets.ConnectionClosedError as e:
                        self.logcomp.log(f"Server {remote_desc} Connection closed with error: {get_short_error_info(e)}.", "WARNING")
                        break

                    except websockets.ConnectionClosedOK:
                        self.logcomp.log(f"Server {remote_desc} Connection closed gracefully.", "INFO")
                        break


        except asyncio.CancelledError:
            self.logcomp.log(f"WebSocket client connection cancelled.", "INFO")
            raise    

        except Exception as e:
            self.logcomp.log(f"Error while connecting to server {remote_desc}:\n{get_full_error_info(e)}", "ERROR")
            super().on_error(e)

        finally:
            if self.ws is not None:
                await self.ws.close()
                self.ws = None

                self.logcomp.log(f"Connection to server {remote_desc} resources released.", "INFO")

        
