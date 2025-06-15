'''
Communication by websocket.
'''

from typing import Any, Callable, Literal, Optional

import asyncio
import time
import websockets
import pickle
import os

import reactivex as rx
from reactivex import Observable, Observer, Subject, create, operators as ops

from .logging import *


from abc import ABC, abstractmethod

class WSDatatype(ABC):

    @staticmethod
    @abstractmethod
    def package(value) -> Any:
        ...

    @staticmethod
    @abstractmethod
    def unpackage(value) -> Any:
        ...

class WSPyObj(WSDatatype):

    @staticmethod
    def package(value):
        return pickle.dumps(value)
    
    @staticmethod
    def unpackage(value):
        return pickle.loads(value)
    
class WSStr(WSDatatype):

    @staticmethod
    def package(value):
        return str(value)
    
    @staticmethod
    def unpackage(value):
        return value
    

class WSRawBytes(WSDatatype):
    """
    • package(value): 直接返回 bytes/bytearray，给 websockets 发送
    • unpackage(value): 直接把收到的 payload 还给上层；确保类型为 bytes
    """
    @staticmethod
    def package(value):
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("WSRawBytes expects a bytes-like object")
        return value                # websockets 会按 binary frame 发送

    @staticmethod
    def unpackage(value):
        # websockets binary frame → bytes ；text frame → str
        if isinstance(value, str):
            # 若 Unity 误发文本，可按需处理或抛错
            # return value.encode()   # 或者: raise TypeError
            raise TypeError(f"WSRawBytes expects a bytes-like object, got str '{value}'")
        return bytes(value)

    
# we use dictionary to serve as connection configuration
# example:
# {
#   host : 'localhost',
#   port : 1492,
#   password : 'turbo'
# }

class RxWSServer(Subject):
    '''
    The websocket server for bi-directional communication between ReactiveX components.
    The server can be connected by multiple clients with password checked.
    Use datatype parameter to control the data type sent through the websocket.
    '''
    def __init__(self,
                 conn_cfg: dict, 
                 name: str = "RxWSServer", 
                 recv_timeout: float = 0.001, 
                 pwd_timeout: float = 5, 
                 datatype: type[WSDatatype] = WSPyObj,
                 ping_interval: Optional[int] = 20,
                 ping_timeout: Optional[int] = 20):
        
        super().__init__()
        self.host = conn_cfg['host']
        self.port = int(conn_cfg['port'])
        self.password = str(conn_cfg['password'])

        self.name = name
        self.datatype = datatype

        # Store connected clients
        self.connections: set[websockets.WebSocketServerProtocol] = set()
        self.connected_queue : set[asyncio.Queue] = set()
        self.recv_timeout = recv_timeout
        self.pwd_timeout = pwd_timeout

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

        super().on_next(LogItem(f"{self.name} Error: {error}.", "ERROR", self.name))
        super().on_error(error)

    def on_completed(self) -> None:
        asyncio.create_task(self.async_completing())

    async def async_completing(self):
        # close all connections
        super().on_next(LogItem(f"{self.name} Closing...", "INFO", self.name))

        if not self.stop.done():
            self.stop.set_result(None)

        if self.serve is not None:
            self.serve.close(True)
            self.serve = None

        super().on_next(LogItem(f"{self.name} Closed.", "INFO", self.name))
        super().on_completed()


    async def handle_client(self, websocket: websockets.WebSocketServerProtocol):
        
        super().on_next(LogItem(f"Client connection attempt from {websocket.remote_address}.", "INFO", self.name))


        try:
            queue = asyncio.Queue()

            # wait for the password
            while True:
                await asyncio.sleep(0)
                try:
                    pwd = await asyncio.wait_for(websocket.recv(), self.pwd_timeout)
                except asyncio.TimeoutError:
                    super().on_next(LogItem(f"Client {websocket.remote_address} password timeout after {self.pwd_timeout}(s).", "INFO", self.name))
                    await websocket.close()
                    return

                if pwd == self.password:
                    await websocket.send("PASS")
                    break
                else:
                    super().on_next(LogItem(f"Client {websocket.remote_address} password rejected.", "INFO", self.name))
                    await websocket.send("REJECT")
                    await websocket.close()
                    return

            super().on_next(LogItem(f"Client {websocket.remote_address} password verified. Connection established.", "INFO", self.name))

            # Register client
            self.connections.add(websocket)
            self.connected_queue.add(queue)
            # the connection information is received
            
            while True:
                await asyncio.sleep(0)
                if not queue.empty():
                    value = queue.get_nowait()
                    # Broadcast message to all connected clients
                    await websocket.send(self.datatype.package(value))

                try:
                    # try to recieve from the client
                    data = await asyncio.wait_for(websocket.recv(), self.recv_timeout)

                    super().on_next(self.datatype.unpackage(data))

                except asyncio.TimeoutError:
                    pass

        except websockets.exceptions.ConnectionClosedError as e:
            super().on_next(LogItem(f"Client {websocket.remote_address} disconnected with error: {e}.", "WARNING", self.name))

        except websockets.exceptions.ConnectionClosedOK:
            super().on_next(LogItem(f"Client {websocket.remote_address} disconnected successfully.", "INFO", self.name))

        finally:
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
            asyncio.create_task(self.async_completing())


class RxWSClient(Subject):
    def __init__(self,
        conn_cfg: dict,
        recv_timeout: float = 0.001,
        pwd_response_timeout: float = 5,
        name: str = "RxWSClient",
        datatype: type[WSDatatype] = WSPyObj,
        conn_retry_timeout: float = 0.5,
        ping_interval: Optional[int] = 20,
        ping_timeout: Optional[int] = 20):

        super().__init__()

        self.host = conn_cfg['host']
        self.port = int(conn_cfg['port'])
        self.password = str(conn_cfg['password'])
        self.recv_timeout = recv_timeout
        self.pwd_response_timeout = pwd_response_timeout
        self.name = name
        self.datatype = datatype

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        self.queue = asyncio.Queue()
        self.ws : Optional[websockets.WebSocketClientProtocol] = None

        self.conn_retry_timeout = conn_retry_timeout

        asyncio.create_task(self.setup_client())

    def on_next(self, value):
        self.queue.put_nowait(value)

    def on_error(self, error):
        self.queue.put_nowait(error)

        super().on_next(LogItem(f"{self.name} Error: {error}.", "ERROR", self.name))
        super().on_error(error)

    def on_completed(self) -> None:
        asyncio.create_task(self.async_completing())


    async def async_completing(self):
        # close all connections
        super().on_next(LogItem(f"{self.name} Closing...", "INFO", self.name))

        if self.ws is not None:
            await self.ws.close()
            self.ws = None

        super().on_next(LogItem(f"{self.name} Closed.", "INFO", self.name))
        super().on_completed()


    async def setup_client(self):
        try:
            while True:
                await asyncio.sleep(0)
                try:
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

            # check for password
            await self.ws.send(self.password)
            # wait for response
            try:
                result = await asyncio.wait_for(self.ws.recv(), self.pwd_response_timeout)
                
            except asyncio.TimeoutError:
                super().on_next(LogItem(f"Password response timeout after {self.pwd_response_timeout}(s).", "ERROR", self.name))
                super().on_error(Exception(f"Password response timeout."))
                return
            
            if result != "PASS":
                super().on_next(LogItem(f"Password rejected.", "ERROR", self.name))
                super().on_error(Exception(f"Password rejected."))
                return
            
            super().on_next(LogItem(f"Server {self.ws.remote_address} Connected.", "INFO", self.name))

            while True:
                await asyncio.sleep(0)
                try:
                    if not self.queue.empty():
                        value = self.queue.get_nowait()
                        await self.ws.send(self.datatype.package(value))

                    data = await asyncio.wait_for(self.ws.recv(), self.recv_timeout)
                    super().on_next(self.datatype.unpackage(data))
            
                except asyncio.TimeoutError:
                    pass

        except websockets.ConnectionClosedOK:
            super().on_next(LogItem(f"Server ({self.host}:{self.port}) Connection closed.", "INFO", self.name))
            super().on_completed()

        except websockets.ConnectionClosedError as e:
            super().on_next(LogItem(f"Server ({self.host}:{self.port}) Connection Error: {type(e)} '{e}'.", "ERROR", self.name))
            super().on_error(e)

        except asyncio.CancelledError:
            asyncio.create_task(self.async_completing())