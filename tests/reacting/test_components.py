"""Components that report (D2): RxWSServer, RxWSClient, RxWSClientGroup,
retry_with_signal. Every recording goes through the reference checker."""

import asyncio
import socket
import threading
import time
from collections.abc import Callable

import reactivex
import websockets
from reactivex import operators as ops

from rxplus import (
    RxException,
    RxWSClient,
    RxWSClientGroup,
    RxWSServer,
    TaggedData,
    WSConnectionConfig,
    retry_with_signal,
)
from rxplus.reacting import MemorySink, Reporter
from rxplus.reacting import rx as rr
from rxplus.ws import RetryPolicy
from tests.reacting.helpers import by_type, conform, make

W = 0.05


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _live_reporter(source: str = "test") -> tuple[Reporter, MemorySink]:
    sink = MemorySink()
    reporter = Reporter(source, sink, window=W, queue=100_000)
    assert _wait_for(lambda: reporter.live)
    return reporter, sink


def _elements(sink: MemorySink) -> list[dict]:
    """Every element ever reported (snapshots and adds), in order."""
    out: list[dict] = []
    for s in sink.signals():
        if s["type"] in ("snapshot", "add"):
            out.extend(s.get("elements", []))
    return out


def _links(sink: MemorySink) -> list[dict]:
    out: list[dict] = []
    for s in sink.signals():
        if s["type"] in ("snapshot", "add"):
            out.extend(s.get("links", []))
    return out


def test_without_reacting_nothing_is_installed():
    server = RxWSServer(WSConnectionConfig("127.0.0.1", _free_port()))
    try:
        assert server.reacting_element is None
        assert server.reacting_port("send") is None
    finally:
        server.on_completed()


def test_server_reports_ports_connections_and_flows(tmp_path):
    reporter, sink = _live_reporter()
    port = _free_port()
    server = RxWSServer(
        WSConnectionConfig("127.0.0.1", port), name="Frontend", reacting=reporter
    )
    try:
        app = reporter.element("App", kind="pipeline")
        recv_to_app = reporter.link(server.reacting_port("recv"), app)
        app_to_send = reporter.link(app, server.reacting_port("send"))
        server.pipe(
            rr.count(recv_to_app),
            ops.map(lambda td: TaggedData(td.tag, td.data.upper())),
            rr.count(app_to_send),
        ).subscribe(server)
        assert _wait_for(lambda: _listening(port))

        async def peer() -> list[str]:
            async with websockets.connect(f"ws://127.0.0.1:{port}/chat") as ws:
                for i in range(5):
                    await ws.send(f"m{i}")
                return [await ws.recv() for _ in range(5)]

        assert asyncio.run(peer()) == [f"M{i}" for i in range(5)]
        time.sleep(3 * W)
    finally:
        server.on_completed()
        time.sleep(2 * W)
        reporter.close(timeout=10.0)

    elements = {e["id"]: e for e in _elements(sink)}
    by_label = {e["label"]: e for e in elements.values()}
    srv = by_label["Frontend"]
    assert srv["kind"] == "rxplus.ws.server"
    assert (
        by_label["send"]["kind"] == "port" and by_label["send"]["parent"] == srv["id"]
    )
    assert by_label["recv"]["kind"] == "port"
    conn = by_label["connection /chat"]
    assert conn["kind"] == "rxplus.ws.connection.instance"
    assert conn["parent"] == srv["id"]
    links = {link["id"]: link for link in _links(sink)}
    send_link = next(i for i, li in links.items() if li["to"] == conn["id"])
    recv_link = next(i for i, li in links.items() if li["from"] == conn["id"])
    sigs = sink.signals()
    flows = by_type(sigs, "flow")

    def items(link_id: str) -> int:
        return sum(f["items"] for f in flows if f["link"] == link_id)

    assert items(recv_link) == 5 and items(send_link) == 5, (
        recv_link,
        send_link,
        [(f["link"], f["items"]) for f in flows],
    )
    assert items(recv_to_app.id) == 5 and items(app_to_send.id) == 5
    assert sum(f.get("bytes", 0) for f in flows if f["link"] == send_link) == 10
    lat = [s for s in by_type(sigs, "latency") if s["id"] == send_link]
    assert sum(s["count"] for s in lat) == 5  # enqueue -> sent
    removed = [i for s in by_type(sigs, "remove") for i in s["ids"]]
    assert conn["id"] in removed and srv["id"] in removed
    conform(sink, tmp_path, window=W)


def test_client_reports_its_socket_and_connect_spans(tmp_path):
    reporter, sink = _live_reporter()
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port))
    server.subscribe(server)  # echo
    assert _wait_for(lambda: _listening(port))
    group_parent = reporter.element("Service")
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", port, "/echo"), reacting=group_parent
    )
    got: list[str] = []
    try:
        client.subscribe(got.append)
        assert _wait_for(lambda: client._connected)
        for i in range(3):
            client.on_next(f"x{i}")
        assert _wait_for(lambda: len(got) == 3)
        time.sleep(3 * W)
    finally:
        client.on_completed()
        server.on_completed()
        time.sleep(2 * W)
        reporter.close(timeout=10.0)
    by_label = {e["label"]: e for e in _elements(sink)}
    cl = by_label["RxWSClient /echo"]
    assert cl["kind"] == "rxplus.ws.client" and cl["parent"] == group_parent.id
    sock = by_label["socket"]
    assert sock["kind"] == "rxplus.ws.socket.instance" and sock["parent"] == cl["id"]
    sigs = sink.signals()
    starts = [s for s in by_type(sigs, "span.start") if s["label"] == "connect"]
    assert len(starts) == 1
    (end,) = [e for e in by_type(sigs, "span.end") if e["span"] == starts[0]["span"]]
    assert end["status"] == "ok"
    links = _links(sink)
    send_link = next(li["id"] for li in links if li["to"] == sock["id"])
    recv_link = next(li["id"] for li in links if li["from"] == sock["id"])
    flows = by_type(sigs, "flow")
    assert sum(f["items"] for f in flows if f["link"] == send_link) == 3
    assert sum(f["items"] for f in flows if f["link"] == recv_link) == 3
    conform(sink, tmp_path, window=W)


def test_a_failed_connect_attempt_is_an_error_span(tmp_path):
    reporter, sink = _live_reporter()
    client = RxWSClient(
        WSConnectionConfig("127.0.0.1", _free_port(), "/x"),  # nobody listens
        retry_policy=RetryPolicy(max_retries=2, base_delay=0.02, jitter=0.0),
        reacting=reporter,
    )
    errors: list[Exception] = []
    client.subscribe(on_error=errors.append)
    try:
        assert _wait_for(lambda: bool(errors), timeout=10.0)
    finally:
        client.on_completed()
        reporter.close(timeout=10.0)
    sigs = sink.signals()
    ends = by_type(sigs, "span.end")
    assert len(ends) == 2 and all(e["status"] == "error" for e in ends)
    assert all(e["error"] == "ConnectionRefusedError" for e in ends)
    conform(sink, tmp_path, window=0.1)


def test_group_children_report_under_the_group(tmp_path):
    reporter, sink = _live_reporter()
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port))
    assert _wait_for(lambda: _listening(port))
    group = RxWSClientGroup(
        WSConnectionConfig("127.0.0.1", port), name="Fanout", reacting=reporter
    )
    try:
        group.open_path("/a")
        group.open_path("/b")
        child = group.reacting_client("/a")
        assert child is not None and child.reacting_port("send") is not None
        time.sleep(3 * W)
    finally:
        group.on_completed()
        server.on_completed()
        time.sleep(2 * W)
        reporter.close(timeout=10.0)
    by_label = {e["label"]: e for e in _elements(sink)}
    grp = by_label["Fanout"]
    assert grp["kind"] == "rxplus.ws.group"
    assert by_label["Fanout:/a"]["parent"] == grp["id"]
    assert by_label["Fanout:/b"]["parent"] == grp["id"]
    conform(sink, tmp_path, window=W)


def test_retry_with_signal_reports_attempt_spans(tmp_path):
    r, sink, clock = make()
    tries = [0]

    def subscribe(observer, scheduler=None):
        tries[0] += 1
        if tries[0] < 3:
            observer.on_error(RxException(TimeoutError("slow"), source="test"))
        else:
            observer.on_next(1)
            observer.on_completed()

    out: list[object] = []
    reactivex.create(subscribe).pipe(
        retry_with_signal(3, reacting=r, label="Fetch")
    ).subscribe(out.append)
    r.poll()
    sigs = sink.signals()
    (op,) = [e for e in _elements(sink) if e["label"] == "Fetch"]
    assert op["kind"] == "rxplus.operator"
    starts = by_type(sigs, "span.start")
    assert [s["label"] for s in starts] == ["attempt"] * 3
    ends = by_type(sigs, "span.end")
    assert [e["status"] for e in ends] == ["error", "error", "ok"]
    assert ends[0]["error"] == "TimeoutError"  # the underlying exception
    conform(sink, tmp_path)


def test_components_report_from_many_threads_conform(tmp_path):
    # A server with several clients sending concurrently.
    reporter, sink = _live_reporter()
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port), reacting=reporter)
    server.subscribe(server)
    assert _wait_for(lambda: _listening(port))
    clients = [
        RxWSClient(WSConnectionConfig("127.0.0.1", port, f"/p{i}"), reacting=reporter)
        for i in range(3)
    ]
    counts = [0, 0, 0]
    try:
        for i, c in enumerate(clients):
            c.subscribe(lambda _, i=i: counts.__setitem__(i, counts[i] + 1))
        assert _wait_for(lambda: all(c._connected for c in clients))

        def send(c: RxWSClient) -> None:
            for j in range(200):
                c.on_next(str(j))

        threads = [threading.Thread(target=send, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert _wait_for(lambda: counts == [200, 200, 200], timeout=10.0)
        time.sleep(3 * W)
    finally:
        for c in clients:
            c.on_completed()
        server.on_completed()
        time.sleep(2 * W)
        reporter.close(timeout=10.0)
    conform(sink, tmp_path, window=W)


def test_labels_never_carry_the_query_string(tmp_path):
    """Review of #28: '/chat?token=SUPERSECRET' became a label."""
    reporter, sink = _live_reporter()
    port = _free_port()
    server = RxWSServer(WSConnectionConfig("127.0.0.1", port), reacting=reporter)
    try:
        assert _wait_for(lambda: _listening(port))

        async def peer() -> None:
            url = f"ws://127.0.0.1:{port}/chat?token=SUPERSECRET"
            async with websockets.connect(url) as ws:
                await ws.send("hi")
                await asyncio.sleep(2 * W)

        asyncio.run(peer())
        client = RxWSClient(
            WSConnectionConfig("127.0.0.1", port, "/feed?key=SUPERSECRET"),
            reacting=reporter,
        )
        time.sleep(2 * W)
        client.on_completed()
    finally:
        server.on_completed()
        time.sleep(3 * W)
        reporter.close(timeout=10.0)
    assert not any("SUPERSECRET" in line for line in sink.lines)
    labels = {e["label"] for e in _elements(sink)}
    assert "connection /chat" in labels and "RxWSClient /feed" in labels
    assert "RxWSServer" in labels  # never the (often ephemeral) port
    conform(sink, tmp_path, window=W)


def test_frames_that_are_never_sent_are_counted_as_dropped(tmp_path):
    """Review of #28: frames still queued when a connection closed, and
    values for a path without clients, vanished without a count."""
    reporter, sink = _live_reporter()
    port = _free_port()
    server = RxWSServer(
        WSConnectionConfig("127.0.0.1", port), reacting=reporter, send_queue=None
    )
    try:
        assert _wait_for(lambda: _listening(port))
        server.on_next(TaggedData("/nobody", "x"))
        assert _wait_for(lambda: server.dropped == 1)

        # A raw client that completes the handshake and then never reads:
        # the server's sends stall, frames stay queued.
        raw = socket.create_connection(("127.0.0.1", port))
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        raw.sendall(
            b"GET /slow HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        response = b""
        while b"\r\n\r\n" not in response:
            response += raw.recv(1)
        assert b" 101 " in response
        time.sleep(0.2)
        for _ in range(400):
            server.on_next(TaggedData("/slow", "y" * 50_000))
        time.sleep(0.5)
        raw.close()
        time.sleep(4 * W)
    finally:
        server.on_completed()
        time.sleep(3 * W)
        reporter.close(timeout=10.0)
    by_label = {e["label"]: e for e in _elements(sink)}
    conn = by_label["connection /slow"]
    send_link = next(li["id"] for li in _links(sink) if li["to"] == conn["id"])
    flows = [f for f in by_type(sink.signals(), "flow") if f["link"] == send_link]
    sent = sum(f["items"] for f in flows)
    dropped = sum(f["dropped"] for f in flows)
    assert dropped > 0 and sent + dropped == 400, (sent, dropped)
    assert server.dropped == 1 + dropped
    conform(sink, tmp_path, window=W)


def test_the_retry_operator_element_goes_with_its_last_subscription(tmp_path):
    r, sink, clock = make()
    op = retry_with_signal(1, reacting=r, label="Fetch")
    sub = reactivex.never().pipe(op).subscribe(lambda _: None)
    r.poll()
    (fetch,) = [e for e in _elements(sink) if e["label"] == "Fetch"]
    sub.dispose()
    time.sleep(r.window * 1.5)  # removed one window later
    clock.advance(0.2)
    r.poll()
    removed = [i for s in by_type(sink.signals(), "remove") for i in s["ids"]]
    assert fetch["id"] in removed
    conform(sink, tmp_path)
