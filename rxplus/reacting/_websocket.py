"""A minimal RFC 6455 client for the WebSocket sink (standard library only, D10).

Only what a reacting/1 source needs: the opening handshake, masked text
frames out, and a reader thread that discards data frames, answers Ping
with Pong and Close with Close. No extensions, no ``Origin`` header.
"""

import base64
import hashlib
import ipaddress
import os
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlsplit

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEADERS = 16 * 1024
_MAX_INCOMING = 1 << 20  # larger incoming messages close the connection (1009)

_TEXT, _BINARY, _CLOSE, _PING, _PONG = 0x1, 0x2, 0x8, 0x9, 0xA


class HandshakeError(Exception):
    """The server did not accept the WebSocket upgrade."""


class NotLoopbackError(Exception):
    """A non-loopback peer and no token: the spec forbids connecting."""


def _is_loopback(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _frame(opcode: int, payload: bytes) -> bytes:
    """One final, masked client frame."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 1 << 16:
        head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
    mask = os.urandom(4)
    body = (
        bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        if n < 64
        else _xor(payload, mask)
    )
    return head + mask + body


def _xor(payload: bytes, mask: bytes) -> bytes:
    n = len(payload)
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(payload, "big") ^ m).to_bytes(n, "big")


class WSConnection:
    """An open client connection. Thread-safe sending; one reader thread."""

    def __init__(
        self,
        sock: socket.socket,
        buffered: bytes,
        ping: tuple[float, float] = (20.0, 20.0),
    ) -> None:
        self._sock = sock
        self._buf = buffered
        self._send_lock = threading.Lock()
        self.closed = False
        self._close_sent = False
        self._ping_interval, self._ping_timeout = ping
        self._last_heard = time.monotonic()
        self._ping_sent: float | None = None
        self._reader = threading.Thread(
            target=self._read_loop, name="rxplus-reacting-ws-reader", daemon=True
        )
        self._reader.start()

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        token: str | None,
        timeout: float,
        send_timeout: float = 10.0,
        ping: tuple[float, float] = (20.0, 20.0),
        ssl_context: ssl.SSLContext | None = None,
    ) -> "WSConnection":
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname:
            raise HandshakeError(f"not a ws:// or wss:// URL: {url!r}")
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

        sock = cls._dial(host, port, token, timeout)
        try:
            if parts.scheme == "wss":
                context = ssl_context or ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode()
            host_header = host if ":" not in host else f"[{host}]"
            lines = [
                f"GET {path} HTTP/1.1",
                f"Host: {host_header}:{port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            if token is not None:
                lines.append(f"Authorization: Bearer {token}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            head, rest = cls._read_head(sock)
            cls._check_response(head, key)
            # A send that makes no progress for send_timeout seconds fails
            # (a stalled observer must not hang the writer or close()); the
            # reader treats the same timeout as an idle tick (keepalive).
            sock.settimeout(send_timeout)
        except BaseException:
            sock.close()
            raise
        return cls(sock, rest, ping)

    @staticmethod
    def _dial(host: str, port: int, token: str | None, timeout: float) -> socket.socket:
        """Connect to an address of ``host``; without a token only to a
        loopback address, and always to the address that was checked."""
        last: OSError | None = None
        refused = False
        for family, kind, proto, _, addr in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        ):
            if token is None and not _is_loopback(str(addr[0])):
                refused = True  # spec, Transport: authenticate off loopback
                continue
            sock = socket.socket(family, kind, proto)
            sock.settimeout(timeout)
            try:
                sock.connect(addr)
                return sock
            except OSError as e:
                sock.close()
                last = e
        if last is not None:
            raise last
        if refused:
            raise NotLoopbackError(
                f"{host} is not a loopback address; a token is required"
            )
        raise OSError(f"no address for {host}")

    @staticmethod
    def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise HandshakeError("connection closed during the handshake")
            data += chunk
            if len(data) > _MAX_HEADERS:
                raise HandshakeError("handshake response too large")
        head, _, rest = data.partition(b"\r\n\r\n")
        return head, rest

    @staticmethod
    def _check_response(head: bytes, key: str) -> None:
        status, *header_lines = head.decode("latin-1").split("\r\n")
        if not status.startswith("HTTP/1.1 101"):
            raise HandshakeError(f"upgrade refused: {status}")
        headers: dict[str, str] = {}
        for line in header_lines:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        accept = base64.b64encode(
            hashlib.sha1((key + _GUID).encode()).digest()
        ).decode()
        if headers.get("upgrade", "").lower() != "websocket":
            raise HandshakeError("missing Upgrade: websocket")
        if "upgrade" not in headers.get("connection", "").lower():
            raise HandshakeError("missing Connection: Upgrade")
        if headers.get("sec-websocket-accept") != accept:
            raise HandshakeError("wrong Sec-WebSocket-Accept")
        if headers.get("sec-websocket-extensions"):
            raise HandshakeError("no extension was requested")

    # ---------------------------------------------------------------- sending

    def _send(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            if self._close_sent:
                raise OSError("connection closing")
            if opcode == _CLOSE:
                self._close_sent = True
            self._sock.sendall(_frame(opcode, payload))

    def send_text(self, text: str) -> None:
        if self.closed:
            raise OSError("connection closed")
        self._send(_TEXT, text.encode("utf-8"))

    def close(self, code: int = 1000) -> None:
        """Send a Close frame if the connection is idle, and release the
        socket. Never waits for a send in progress (a writer stuck on a peer
        that stopped reading): the socket is shut down under it instead."""
        if not self.closed and self._send_lock.acquire(blocking=False):
            try:
                if not self._close_sent:
                    self._close_sent = True
                    self._sock.sendall(_frame(_CLOSE, struct.pack("!H", code)))
            except OSError:
                pass
            finally:
                self._send_lock.release()
        self.abort()

    def abort(self) -> None:
        self.closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    # ---------------------------------------------------------------- reading

    def _keepalive(self) -> None:
        """Idle tick of the reader: ping a quiet peer, give up on a dead one."""
        now = time.monotonic()
        if self._ping_sent is not None:
            if now - self._ping_sent > self._ping_timeout:
                raise ConnectionError("no answer to ping")
        elif now - self._last_heard > self._ping_interval:
            self._ping_sent = now
            self._send(_PING, b"rxplus")

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = self._sock.recv(max(4096, n - len(self._buf)))
            except TimeoutError:
                self._keepalive()
                continue
            if not chunk:
                raise ConnectionError("connection closed by peer")
            self._last_heard = time.monotonic()
            self._ping_sent = None  # anything heard proves the peer alive
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _read_loop(self) -> None:
        """Discard data frames; Ping -> Pong; Close -> Close (RFC 6455 5.5)."""
        try:
            message = 0
            while not self.closed:
                b0, b1 = self._recv_exact(2)
                opcode = b0 & 0x0F
                n = b1 & 0x7F
                if n == 126:
                    (n,) = struct.unpack("!H", self._recv_exact(2))
                elif n == 127:
                    (n,) = struct.unpack("!Q", self._recv_exact(8))
                mask = self._recv_exact(4) if b1 & 0x80 else b""
                if opcode >= 0x8:  # control frame: small, never fragmented
                    payload = self._recv_exact(n)
                    if mask:
                        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
                    if opcode == _PING:
                        self._send(_PONG, payload)
                    elif opcode == _CLOSE:
                        code = (
                            payload[:2]
                            if len(payload) >= 2
                            else struct.pack("!H", 1000)
                        )
                        try:
                            self._send(_CLOSE, code)
                        except OSError:
                            pass
                        break
                    continue
                # Data frame (or continuation): ignored, as the spec requires.
                message = n if opcode in (_TEXT, _BINARY) else message + n
                if message > _MAX_INCOMING:
                    self._send(_CLOSE, struct.pack("!H", 1009))
                    break
                while n > 0:
                    step = min(n, 65536)
                    self._recv_exact(step)
                    n -= step
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            self.abort()
