"""A raw, misbehaving WebSocket server for sink tests (standard library only).

Modes: ``"normal"`` reads and discards; ``"close"`` sends Close right after the
handshake; ``"noread"`` completes the handshake and never reads again;
``"silent"`` reads but never answers anything (no Pong: a half-open peer);
``"reject"`` reads the first frame then closes with 1008 (an observer that
refuses a duplicate source name).
"""

import base64
import hashlib
import socket
import struct
import threading
import time

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class RawServer:
    def __init__(self, mode: str, hs_delay: float = 0.0) -> None:
        self.mode = mode
        self.hs_delay = hs_delay
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self.handshakes: list[dict[str, str]] = []
        self._clients: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/sources"

    def stop(self) -> None:
        self.sock.close()
        for c in self._clients:
            try:
                c.close()
            except OSError:
                pass

    def _accept(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            self._clients.append(c)
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c: socket.socket) -> None:
        try:
            if self.mode == "noread":
                c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = c.recv(4096)
                if not chunk:
                    return
                data += chunk
            if self.hs_delay:
                time.sleep(self.hs_delay)
            lines = data.decode("latin-1").split("\r\n")
            headers = {
                k.strip().lower(): v.strip()
                for k, _, v in (line.partition(":") for line in lines[1:] if line)
            }
            self.handshakes.append(headers)
            key = headers["sec-websocket-key"]
            accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest())
            c.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            if self.mode == "close":
                c.sendall(struct.pack("!BBH", 0x88, 2, 1008))
                time.sleep(0.05)
                c.close()
                return
            if self.mode == "noread":
                time.sleep(3600)
                return
            if self.mode == "reject":
                c.recv(65536)  # the first snapshot
                c.sendall(struct.pack("!BBH", 0x88, 2, 1008))
                time.sleep(0.05)
                c.close()
                return
            while True:  # "normal" and "silent": read and discard
                if not c.recv(65536):
                    return
        except OSError:
            pass
