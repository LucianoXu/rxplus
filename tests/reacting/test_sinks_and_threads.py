"""Real time and threads: the background writer, sinks, concurrency, imports."""

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import websockets
from websockets.asyncio.server import ServerConnection
from websockets.http11 import Request

from rxplus.reacting import JsonlFileSink, MemorySink, Reporter, WebSocketSink
from tests.conformance import check


def _check(lines: list[str], tmp_path: Path, window: float = 0.1) -> None:
    path = tmp_path / "rec.jsonl"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    violations = check.check(path, direct=True, window=window)
    assert not violations, [f"{v.line}: {v.rule}: {v.message}" for v in violations]


def _wait_for(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_core_imports_only_the_standard_library():
    code = (
        "import sys, rxplus.reacting; "
        "bad = [m for m in ('reactivex', 'websockets', 'opentelemetry', 'numpy') "
        "if m in sys.modules]; print(bad)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_concurrent_counting_is_exact(tmp_path):
    sink = MemorySink()
    r = Reporter("test", sink, window=0.05)
    link = r.link(r.element("A"), r.element("B"))
    assert _wait_for(lambda: r.live)

    def work() -> None:
        for _ in range(5000):
            link.deliver()
            link.leave(link.enter())

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(0.12)  # let the last window end
    r.close()
    flows = [s for s in sink.signals() if s["type"] == "flow"]
    assert sum(f["items"] for f in flows) == 8 * 5000 * 2
    _check(sink.lines, tmp_path, window=0.05)


def test_slow_sink_triggers_loss_not_blocking(tmp_path):
    sink = MemorySink()
    r = Reporter("test", sink, queue=50)
    assert _wait_for(lambda: r.live)
    sink.hold()
    time.sleep(0.05)
    start = time.perf_counter()
    root = r.element("root")
    for i in range(500):
        root.child(f"c{i}")
    assert time.perf_counter() - start < 1.0  # the caller never waits
    sink.release()
    r.close()
    sigs = sink.signals()
    assert [s["seq"] for s in sigs] != list(range(len(sigs)))  # a gap: loss
    _check(sink.lines, tmp_path)


def test_file_sink(tmp_path):
    path = tmp_path / "session.jsonl"
    with Reporter("test", JsonlFileSink(path)) as r:
        link = r.link(r.element("A"), r.element("B"))
        link.deliver(5)
        time.sleep(0.15)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n") and '"type":"flow"' in text
    assert not check.check(path, direct=True)
    with pytest.raises(FileExistsError):
        JsonlFileSink(path)


class _Hub:
    """A local observer: records the frames and handshakes it receives."""

    def __init__(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.lines: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.pongs = 0
        self.connections: list[ServerConnection] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        ready = threading.Event()

        async def handler(ws: ServerConnection) -> None:
            self.connections.append(ws)
            await ws.send("data frames from the observer are ignored")
            await ws.send(b"\x00\x01")
            await ws.ping()
            self.pongs += 1  # ping() resolves on pong; counted when answered
            async for message in ws:
                assert isinstance(message, str)
                self.lines.append(message)

        async def record(conn: ServerConnection, request: Request) -> None:
            self.headers.append({k.lower(): v for k, v in request.headers.raw_items()})

        async def main() -> None:
            self._loop = asyncio.get_running_loop()
            async with websockets.serve(
                handler, "127.0.0.1", self.port, process_request=record
            ):
                ready.set()
                await asyncio.Future()

        threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
        assert ready.wait(5)

    def drop_all(self) -> None:
        loop = self._loop
        assert loop is not None
        for ws in list(self.connections):
            asyncio.run_coroutine_threadsafe(ws.close(), loop).result(5)


def test_websocket_sink_session_and_reconnect(tmp_path):
    hub = _Hub()
    sink = WebSocketSink(f"ws://127.0.0.1:{hub.port}/sources", backoff=(0.05, 0.2))
    r = Reporter("test", sink)
    link = r.link(r.element("A"), r.element("B"))
    assert _wait_for(lambda: r.live and hub.lines)
    link.deliver(2)
    assert _wait_for(lambda: any('"flow"' in line for line in hub.lines))
    assert _wait_for(lambda: hub.pongs >= 1)
    first = len(hub.lines)
    hub.drop_all()  # the observer closes: a new session on the next connection
    assert _wait_for(lambda: len(hub.connections) >= 2 and len(hub.lines) > first)
    r.close()
    second = [json.loads(line) for line in hub.lines[first:]]
    assert second[0]["type"] == "snapshot" and second[0]["seq"] == 0
    assert all("origin" not in h for h in hub.headers)
    assert all("authorization" not in h for h in hub.headers)  # loopback: no token
    _check(hub.lines[:first], tmp_path)
    _check(hub.lines[first:], tmp_path)


def test_websocket_sink_sends_bearer_token():
    hub = _Hub()
    sink = WebSocketSink(f"ws://127.0.0.1:{hub.port}/", token="placeholder-token")
    r = Reporter("test", sink)
    assert _wait_for(lambda: bool(hub.headers))
    r.close()
    assert hub.headers[0]["authorization"] == "Bearer placeholder-token"


def test_websocket_sink_refuses_non_loopback_without_token():
    sink = WebSocketSink("ws://192.0.2.1:9/")  # TEST-NET-1, never dialled
    assert sink.open() is False
    assert sink.retry_delay() is None
    assert sink.last_error is not None and "token" in sink.last_error
