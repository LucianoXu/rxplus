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
from .utils import get_short_error_info, get_full_error_info


class WSDatatype(ABC):

    @abstractmethod
    def package(self, value) -> Any:
        ...

    @abstractmethod
    def unpackage(self, value) -> Any:
        ...
    
class WSStr(WSDatatype):

    def package(self, value):
        return str(value)
    
    def unpackage(self, value):
        return value
    

class WSBytes(WSDatatype):
    def package(self, value):
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("WSRawBytes expects a bytes-like object")
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
# }

class RxWSServer(Subject):
    '''
    The websocket server for bi-directional communication between ReactiveX components.
    The server can be connected by multiple clients.
    Use datatype parameter to control the data type sent through the websocket.
    The server will be closed upon receiving on_completed signal.
    '''
    def __init__(self,
                 conn_cfg: dict, 
                 logcomp: Optional[LogComp] = None,
                 recv_timeout: float = 0.001,
                 datatype: Literal['string', 'byte'] = 'string',
                 ping_interval: Optional[int] = 20,
                 ping_timeout: Optional[int] = 20):
        
        super().__init__()
        self.host = conn_cfg['host']
        self.port = int(conn_cfg['port'])

        # setup the log source
        if logcomp is None:
            logcomp = EmptyLogComp()
        else:
            self.logcomp = logcomp

        self.logcomp.set_super(super())

        self.datatype = datatype
        self.adapter: WSDatatype = wsdt_factory(datatype)

        # Store connected clients
        self.connections: set[websockets.WebSocketServerProtocol] = set()
        self.connected_queue : set[asyncio.Queue] = set()
        self.recv_timeout = recv_timeout

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout


        asyncio.create_task(self.start_server())

        self.serve : Optional[websockets.WebSocketServer] = None
        self.stop = asyncio.Future()

    def on_next(self, value):
        for queue in self.connected_queue:
            queue.put_nowait(value)

    def on_error(self, error):
        for queue in self.connected_queue:
            queue.put_nowait(error)

        super().on_error(error)

    def on_completed(self) -> None:
        asyncio.create_task(self.async_completing())

    async def async_completing(self):

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
        
        self.logcomp.log(f"Client established from {websocket.remote_address}.", "INFO")

        try:
            queue = asyncio.Queue()

            # Register client
            self.connections.add(websocket)
            self.connected_queue.add(queue)
            # the connection information is received
            
            while True:
                await asyncio.sleep(0)
                # try to send data to the client
                if not queue.empty():
                    value = queue.get_nowait()

                    try:
                        # Broadcast message to all connected clients
                        await websocket.send(self.adapter.package(value))

                    except (ConnectionResetError, BrokenPipeError, OSError) as e:
                        self.logcomp.log(f"Failed to send data to client {websocket.remote_address}, connection may be broken: {get_short_error_info(e)}", "WARNING")
                        break
                        
                    except websockets.exceptions.ConnectionClosed as e:
                        self.logcomp.log(f"Failed to send data to client {websocket.remote_address}, connection closed: {get_short_error_info(e)}", "WARNING")
                        break

                try:
                    # try to recieve from the client
                    data = await asyncio.wait_for(websocket.recv(), self.recv_timeout)
                    # process the received data
                    super().on_next(self.adapter.unpackage(data))

                except asyncio.TimeoutError:
                    pass

                except ConnectionResetError as e:
                    self.logcomp.log(f"Connection reset (ConnectionResetError): {get_short_error_info(e)}", "WARNING")
                    break

                except OSError as e:
                    self.logcomp.log(f"Network error or connection lost (OSError): {get_short_error_info(e)}", "WARNING")
                    break

                except websockets.exceptions.ConnectionClosedError as e:
                    self.logcomp.log(f"Client {websocket.remote_address} disconnected with error: {get_short_error_info(e)}.", "WARNING")
                    break

                except websockets.exceptions.ConnectionClosedOK:
                    self.logcomp.log(f"Client {websocket.remote_address} disconnected gracefully.", "INFO")
                    break

        except asyncio.CancelledError:
            self.logcomp.log(f"Client {websocket.remote_address} connection cancelled.", "INFO")
            raise
                

        except Exception as e:
            self.logcomp.log(f"Error while handling client {websocket.remote_address}:\n{get_full_error_info(e)}", "ERROR")
            super().on_error(e)


        finally:

            await websocket.close()
            self.logcomp.log(f"Client {websocket.remote_address} resources released.", "INFO")

            # Unregister client
            if queue in self.connected_queue:
                self.connected_queue.remove(queue)

            if websocket in self.connections:
                self.connections.remove(websocket)

    async def start_server(self):
        # Start WebSocket server
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

        super().__init__()

        self.host = conn_cfg['host']
        self.port = int(conn_cfg['port'])
        self.recv_timeout = recv_timeout

        # setup the log source
        if logcomp is None:
            logcomp = EmptyLogComp()
        else:
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

    def on_next(self, value):
        self.queue.put_nowait(value)

    def on_error(self, error):
        self.queue.put_nowait(error)

        self.logcomp.log(f"Error: {error}.", "ERROR")
        super().on_error(error)

    def on_completed(self) -> None:
        asyncio.create_task(self.async_completing())


    async def async_completing(self):
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
        try:
            # Repeatedly attempt to connect to the server
            while True:

                # Attempt to connect to the server
                self.logcomp.log(f"Connecting to server {self.host}:{self.port}...", "INFO")

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
                                f"ws://{self.host}:{self.port}",
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
                        self.logcomp.log(f"Invalid handshake with server ({self.host}:{self.port}): {get_short_error_info(e)}", "WARNING")
                        await asyncio.sleep(self.conn_retry_timeout)
                        pass

                    # Catch invalid URI errors
                    except websockets.InvalidURI as e:
                        self.logcomp.log(f"Invalid URI for server ({self.host}:{self.port}): {get_short_error_info(e)}", "ERROR")
                        super().on_error(e)
                        return
                
                self.logcomp.log(f"Server {self.host}:{self.port} Connected.", "INFO")


                # Start receiving messages
                while True:
                    await asyncio.sleep(0)
                    
                    # try to send data to the server
                    if not self.queue.empty():
                        try:
                            value = self.queue.get_nowait()
                            await self.ws.send(self.adapter.package(value))
                            
                        except (ConnectionResetError, BrokenPipeError, OSError) as e:
                            self.logcomp.log(f"Failed to send data to server {self.ws.remote_address}, connection may be broken: {get_short_error_info(e)}", "WARNING")
                            break
                            
                        except websockets.exceptions.ConnectionClosed as e:
                            self.logcomp.log(f"Failed to send data to server {self.ws.remote_address}, connection closed: {get_short_error_info(e)}", "WARNING")
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
                        self.logcomp.log(f"Server ({self.host}:{self.port}) Connection closed with error: {get_short_error_info(e)}.", "WARNING")
                        break

                    except websockets.ConnectionClosedOK:
                        self.logcomp.log(f"Server ({self.host}:{self.port}) Connection closed gracefully.", "INFO")
                        break


        except asyncio.CancelledError:
            self.logcomp.log(f"WebSocket client connection cancelled.", "INFO")
            raise    

        except Exception as e:
            self.logcomp.log(f"Error while connecting to server ({self.host}:{self.port}):\n{get_full_error_info(e)}", "ERROR")
            super().on_error(e)

        finally:
            if self.ws is not None:
                await self.ws.close()
                self.ws = None

                self.logcomp.log(f"Connection to server ({self.host}:{self.port}) resources released.", "INFO")

        
