"""Regression tests for the independent reviews of #22 (protocol, concurrency).

Each test reproduces one reviewer probe; the comment names the finding.
"""

import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from rxplus.reacting import (
    JsonlFileSink,
    ManualClock,
    MemorySink,
    Reporter,
    WebSocketSink,
)
from rxplus.reacting._websocket import NotLoopbackError, WSConnection
from rxplus.reacting._window import SHARDS, Accumulator, Shard, Totals
from tests.reacting.helpers import by_type, conform, make
from tests.reacting.rawws import RawServer

SURROGATE = os.fsdecode(b"cam\xff")  # a non-UTF-8 file name: a lone surrogate


class _Shared:
    """The reporter state an accumulator reads (for unit tests)."""

    def __init__(self) -> None:
        self.closed = 0
        self.shards = tuple(Shard() for _ in range(SHARDS))
        self.pending = 0
        self.cap = 100_000
        self.saturated = False


def _windows(st) -> dict:
    """``{k: {ident: Totals}}`` across the shards (a copy, under their locks)."""
    out: dict = {}
    for shard in st.shards:
        with shard.lock:
            for k, w in shard.windows.items():
                out.setdefault(k, {}).update(w)
    return out


def _totals(st) -> int:
    return sum(len(w) for w in _windows(st).values())


def _wait_for(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# --------------------------------------------------------------- protocol


class _TickingClock(ManualClock):
    """Real clocks move between two reads: this one moves 1 ms per read."""

    tick = False

    def monotonic(self) -> float:
        value = super().monotonic()
        if self.tick:
            self.advance(0.001)
        return value


def test_t_never_decreases_with_a_clock_that_moves_between_reads(tmp_path):
    # Finding 1: two clock reads per operation let t go backwards.
    sink, clock = MemorySink(), _TickingClock()
    r = Reporter("test", sink, window=0.1, clock=clock, timer=False)
    a, b = r.element("A"), r.element("B")
    link = r.link(a, b)
    link.deliver()
    clock.advance(0.0995)  # 0.5 ms before the end of window 0
    clock.tick = True
    span = a.span("work")
    link.deliver()
    r.element("C")
    span.end()
    clock.tick = False
    clock.advance(0.2)
    r.poll()
    ts = [s["t"] for s in sink.signals()]
    assert ts == sorted(ts)
    conform(sink, tmp_path)


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.element(SURROGATE),
        lambda r: r.element("A", kind=SURROGATE),
        lambda r: r.link(r.element("A"), f"hub/{SURROGATE}"),
        lambda r: r.element("A").span(SURROGATE),
        lambda r: r.element("A").span("s").end(SURROGATE),
        lambda r: Reporter(SURROGATE, MemorySink(), timer=False),
        lambda r: Reporter("x", MemorySink(), label=SURROGATE, timer=False),
    ],
)
def test_unencodable_strings_fail_at_the_call(call, tmp_path):
    # Finding 2: a lone surrogate was accepted, then broke the session later.
    r, sink, clock = make()
    with pytest.raises(ValueError, match="UTF-8"):
        call(r)
    r.element("After")
    r.poll()
    assert r.live
    conform(sink, tmp_path)


def test_loss_of_the_opening_snapshot_begins_a_new_session(tmp_path):
    # Finding 3 (spec fa173ab): the discarded signals include seq 0.
    r, sink, clock = make(queue=3)
    sink.disconnect()
    r.poll()  # session 1 over
    sink.connect()
    r._try_open()  # connected: session 2 begins with its snapshot queued
    r._step()
    assert r._opening_queued
    for i in range(6):  # overflow before the opening snapshot is written
        r.element(f"E{i}")
    r._step()
    r.poll()
    sigs = sink.signals()
    snaps = [(s["seq"], len(s["elements"])) for s in by_type(sigs, "snapshot")]
    assert all(seq == 0 for seq, _ in snaps), snaps  # never a recovery snapshot
    assert snaps[-1][1] == 6
    conform(sink, tmp_path)


@pytest.mark.parametrize("queue", [5, 50])
def test_spans_on_threads_with_reconnects_conform(queue, tmp_path):
    sink = MemorySink()
    r = Reporter("test", sink, queue=queue, window=0.05)
    elements = [r.element(f"E{i}") for i in range(4)]
    stop = threading.Event()

    def worker(el) -> None:
        while not stop.is_set():
            with el.span("work"):
                pass

    threads = [threading.Thread(target=worker, args=(e,)) for e in elements]
    for t in threads:
        t.start()
    for _ in range(10):
        time.sleep(0.05)
        sink.disconnect()
        # A drop the writer never sees (no write while it lasts) is no drop.
        assert _wait_for(lambda: not r.live)
        sink.connect()
    stop.set()
    for t in threads:
        t.join()
    r.close()
    assert len(by_type(sink.signals(), "snapshot")) >= 2
    conform(sink, tmp_path, window=0.05)


def test_loss_bound_counts_the_batch_in_flight(tmp_path):
    # Finding 4: the queue bound overshot by the writer's batch.
    queue = 100
    sink = MemorySink()
    r = Reporter("test", sink, queue=queue, window=10.0)
    a = r.element("A")
    assert _wait_for(lambda: r.live and len(sink.lines) >= 1)  # the snapshot
    sink.hold()
    for _ in range(400):
        with a.span("w"):
            pass
    time.sleep(0.3)
    assert len(r._q) + r._inflight <= queue
    sink.release()
    r.close()
    sigs = sink.signals()
    seqs = [s["seq"] for s in sigs]
    gaps = [(b - a - 1) for a, b in zip(seqs, seqs[1:], strict=False) if b != a + 1]
    # Several Loss events while stalled add up in one gap (a later one also
    # discards an earlier recovery snapshot); the bound is on memory, above.
    assert gaps
    conform(sink, tmp_path, window=10.0)


def test_backoff_after_an_observer_rejects_the_session():
    # Finding 4: reopen without backoff after a successful open that closes.
    server = RawServer("reject")
    r = Reporter("dup", WebSocketSink(server.url))  # backoff (0.5, 30)
    r.element("A")
    time.sleep(2.0)
    n = server.connections
    r.close()
    server.stop()
    assert 1 <= n <= 3, n


def test_connects_only_to_the_checked_address(monkeypatch):
    # Finding 4 (spec a39b37b): the token rule applies to the address dialled.
    real = socket.getaddrinfo
    dialled: list[str] = []

    def fake(host, port, *args, **kwargs):
        loop = real("127.0.0.1", port, type=socket.SOCK_STREAM)
        far = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port))
        return [far, *loop] if host == "mixed.example" else [far]

    real_connect = socket.socket.connect

    def connect(self, addr):
        dialled.append(addr[0])
        return real_connect(self, addr)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setattr(socket.socket, "connect", connect)
    server = RawServer("normal")
    conn = WSConnection.connect(
        f"ws://mixed.example:{server.port}/", token=None, timeout=2.0
    )
    conn.close()
    assert dialled == ["127.0.0.1"]
    dialled.clear()
    with pytest.raises(NotLoopbackError):
        WSConnection.connect(
            f"ws://far.example:{server.port}/", token=None, timeout=2.0
        )
    assert dialled == []
    server.stop()


def test_half_open_peer_is_detected():
    # Finding 4: a peer that answers nothing (no Pong) must be given up.
    server = RawServer("silent")
    sink = WebSocketSink(
        server.url, send_timeout=0.2, ping_interval=0.3, ping_timeout=0.3
    )
    assert sink.open()
    assert _wait_for(lambda: not sink.is_open(), timeout=3.0)
    sink.close()
    server.stop()


def test_submodules_are_attributes_and_concurrent_first_import_works():
    # Findings 4 and 13: `rxplus.ws` after `import rxplus`; threads racing.
    code = textwrap.dedent(
        """
        import importlib, threading, rxplus
        assert rxplus.ws.RxWSServer and rxplus.reacting.Reporter
        names = ["RxWSServer", "tag", "OTelLogger", "GatewayNode", "Duplex",
                 "from_cli", "retry_with_signal", "WSStr", "BoundedBuffer"] * 3
        errors = []
        barrier = threading.Barrier(len(names))
        def go(n):
            barrier.wait()
            try:
                getattr(importlib.import_module("rxplus"), n)
            except Exception as e:
                errors.append((n, e))
        threads = [threading.Thread(target=go, args=(n,)) for n in names]
        [t.start() for t in threads]; [t.join() for t in threads]
        print(len(errors))
        """
    )
    for _ in range(3):
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "0"


def test_typos_in_rxplus_imports_stay_type_errors(tmp_path):
    # Finding 13: module __getattr__ hid every name from mypy.
    user = tmp_path / "user.py"
    user.write_text("from rxplus import RxWSServr\nfrom rxplus import RxWSServer\n")
    out = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", str(user)],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 1 and 'no attribute "RxWSServr"' in out.stdout, out.stdout


# ------------------------------------------------------------ concurrency


def test_reconnect_storm_is_bounded():
    # Finding 5: an observer that closes at once got thousands of reconnects,
    # each leaking a socket and a reader thread.
    server = RawServer("close")
    threads_before = threading.active_count()
    fds_before = len(os.listdir("/proc/self/fd"))
    r = Reporter("storm", WebSocketSink(server.url, backoff=(0.2, 1.0)))
    r.element("A")
    time.sleep(2.0)
    n = server.connections
    threads = threading.active_count()
    fds = len(os.listdir("/proc/self/fd"))
    r.close()
    server.stop()
    assert 2 <= n <= 6, n
    assert threads - threads_before <= 3  # clock, writer, at most one reader
    assert fds - fds_before <= 3


def test_close_returns_when_the_observer_stops_reading():
    # Finding 6: close() hung in sendall.
    server = RawServer("noread")
    r = Reporter("hang", WebSocketSink(server.url, send_timeout=1.0), queue=100_000)
    elements = [r.element(f"Element-with-a-longish-label-{i}") for i in range(300)]
    links = [r.link(elements[i], elements[i + 1]) for i in range(299)]
    end = time.time() + 2.0
    while time.time() < end:
        for link in links:
            link.deliver()
    start = time.perf_counter()
    r.close(timeout=1.0)
    assert time.perf_counter() - start < 4.0
    server.stop()


def test_stalled_sink_memory_is_bounded():
    # Finding 7: window totals piled up while the sink was stalled.
    sink = MemorySink()
    queue = 2000
    r = Reporter("stall", sink, window=0.01, queue=queue)
    elements = [r.element(f"E{i}") for i in range(200)]
    links = [r.link(elements[i], elements[i + 1]) for i in range(199)]
    assert _wait_for(lambda: r.live)
    sink.hold()
    end = time.time() + 2.0  # 200 windows
    while time.time() < end:
        for link in links:
            link.deliver()
        time.sleep(0.001)
    pending = len([w for w in _windows(r._st).values() if w])  # windows with counts
    queued = len(r._q) + r._inflight
    latencies = []
    for _ in range(50):
        t = time.perf_counter()
        elements[0].span("job").end()
        latencies.append(time.perf_counter() - t)
    sink.release()
    r.close()
    assert pending <= 3, pending
    assert queued <= queue, queued
    assert max(latencies) < 0.05  # the caller never closes windows


def test_catch_up_never_runs_on_the_callers_thread():
    # Finding 7: many unclosed windows were closed inside the user's call.
    clock, sink = ManualClock(), MemorySink()
    r = Reporter("c", sink, window=0.1, queue=10**9, clock=clock, timer=False)
    elements = [r.element(f"E{i}") for i in range(301)]
    links = [r.link(elements[i], elements[i + 1]) for i in range(300)]
    for _ in range(200):
        for link in links:
            link.deliver()
        clock.advance(0.1)
    queued = len(r._q)
    start = time.perf_counter()
    elements[0].span("job").end()
    assert time.perf_counter() - start < 0.01
    assert len(r._q) == queued  # nothing built on this thread


def test_close_during_a_slow_connect_leaves_no_session():
    # Finding 8: a connect finishing after close() started a zombie session.
    server = RawServer("normal", hs_delay=2.0)
    sink = WebSocketSink(server.url)
    r = Reporter("z", sink)
    r.element("A")
    time.sleep(0.2)
    r.close(timeout=1.0)
    time.sleep(2.5)
    assert not r.live and not sink.is_open()
    assert not any(t.is_alive() for t in r._threads)
    server.stop()


def test_a_sampler_taking_a_user_lock_does_not_deadlock():
    # Finding 9: the sampler ran under the reporter lock (ABBA).
    lock = threading.Lock()
    buf: list[int] = []

    def depth() -> int:
        with lock:
            return len(buf)

    r = Reporter("d", MemorySink(), window=0.01)
    a, b = r.element("A"), r.element("B")
    r.link(a, b, depth=depth)
    assert _wait_for(lambda: r.live)
    done = threading.Event()

    def component() -> None:
        for i in range(500):
            with lock:  # takes the reporter lock while holding its own
                buf.append(i)
                a.span("conn").end()
            time.sleep(0.0005)
        done.set()

    threading.Thread(target=component, daemon=True).start()
    assert done.wait(10)
    r.close()


def test_validation_does_not_depend_on_the_session():
    # Finding 10: an invalid span label raised only while an observer was on.
    r, sink, clock = make()
    sink.disconnect()
    r.poll()
    el = r.element("e")
    with pytest.raises(ValueError):
        el.span("")
    sink.connect()
    r.poll()
    with pytest.raises(ValueError):
        el.span("")


def test_span_parents_do_not_leak_between_reporters(tmp_path):
    # Finding 11: B took A's current span ID as its parent.
    ra, sa, _ = make()
    rb, sb, _ = make()
    ea, eb = ra.element("a"), rb.element("b")
    eb.span("unrelated")  # B's s1
    with ea.span("a-work"):  # A's s1 is current
        child = eb.span("b-child")
    child.end()
    rb.poll()
    starts = by_type(sb.signals(), "span.start")
    assert all("parent" not in s for s in starts)
    conform(sb, tmp_path)


def test_a_stale_session_call_does_not_wipe_the_current_session():
    # Finding 12.
    shared = _Shared()
    acc = Accumulator("l1", shared)
    acc.start_session(2)
    acc.record(2, 0, items=5)
    acc.record(1, 0, items=1)  # read session 1 just before session 2 began
    assert acc.next_window() == 0
    assert _windows(shared)[0]["l1"].items == 5


def test_file_sink_is_flushed_at_exit(tmp_path):
    # Finding 14: the writer is a daemon; nothing flushed it at exit.
    path = tmp_path / "exit.jsonl"
    code = textwrap.dedent(
        f"""
        from rxplus.reacting import JsonlFileSink, Reporter
        r = Reporter("exit", JsonlFileSink({str(path)!r}))
        r.element("A")
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    raw = path.read_bytes()
    assert b'"type":"snapshot"' in raw
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw


def test_file_sink_format(tmp_path):
    path: Path = tmp_path / "format.jsonl"
    with Reporter("test", JsonlFileSink(path), label="Ünïcode 😀") as r:
        r.element("Ω")
        assert _wait_for(lambda: r.live)
    raw = path.read_bytes()
    assert (
        raw.endswith(b"\n") and b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    )
    assert "😀".encode() in raw


# ------------------------------------------------------ re-review of #22


def test_a_lagging_clock_turns_old_windows_into_loss(tmp_path):
    # N3: windows the clock thread could not close in time are not kept
    # without bound; they become a Loss (seq gap + snapshot).
    r, sink, clock = make(window=0.1)
    links = [r.link(r.element(f"A{i}"), r.element(f"B{i}")) for i in range(20)]
    r.poll()
    for _ in range(200):  # 200 windows of counts, never closed
        for link in links:
            link.deliver()
        clock.advance(0.1)
    assert max(link._acc.pending() for link in links) <= 200
    r.poll()
    assert max(link._acc.pending() for link in links) == 0
    sigs = sink.signals()
    seqs = [s["seq"] for s in sigs]
    gap = [i for i in range(1, len(seqs)) if seqs[i] != seqs[i - 1] + 1]
    assert gap and sigs[gap[0]]["type"] == "snapshot"
    links[0].deliver()
    clock.advance(0.1)
    r.poll()
    assert by_type(sink.signals(), "flow")  # reporting resumes
    conform(sink, tmp_path)


def test_late_record_racing_the_clock_is_not_stranded(tmp_path):
    # N4: counts that land in a window the clock already closed (the caller
    # read `closed` just before it advanced) are reported with the next one.
    r, sink, clock = make(window=0.1)
    a, b, c = r.element("A"), r.element("B"), r.element("C")
    l1, l2 = r.link(a, b), r.link(b, c)
    r.poll()
    clock.advance(0.05)
    l2.deliver()
    clock.advance(0.1)
    r.poll()  # window 0 closed
    assert r._st.closed >= 1
    # What such a late record leaves behind: window 0 again, for l1.
    shard = l1._acc._shard
    with shard.lock:
        shard.windows.setdefault(0, {})[l1.id] = Totals(items=5)
        shard.count += 1
    clock.advance(0.1)
    r.poll()
    reported = sum(
        f["items"] for f in by_type(sink.signals(), "flow") if f["link"] == l1.id
    )
    assert reported == 5
    assert l1._acc.pending() == 0
    conform(sink, tmp_path)


def test_removed_handles_leave_the_busy_set():
    # N5: one entry leaked per removal.
    r, sink, clock = make(window=0.1)
    for _ in range(50):
        link = r.link(r.element("A"), r.element("B"))
        link.deliver()
        link.remove()
        clock.advance(0.1)
        r.poll()
    clock.advance(0.2)
    r.poll()
    assert not _totals(r._st) and len(r._watched) <= 1


def test_queue_of_one_never_holds_two_unwritten(tmp_path):
    r, sink, clock = make(queue=1)
    sink.hold()
    # (timer=False: nothing is in flight; the bound is on the queue itself)
    for i in range(5):
        r.element(f"E{i}")
    r._step()
    assert len(r._q) <= 1 + 1  # at most the recovery snapshot beyond the bound
    sink.release()
    r.poll()
    conform(sink, tmp_path)


def test_close_honours_its_timeout_with_a_stalled_observer():
    # N1: close(timeout=1.0) took 8 s: sink.close() waited for the send lock.
    server = RawServer("noread")
    r = Reporter("ex", WebSocketSink(server.url), window=0.1, queue=2000)
    elements = [r.element(f"Element-with-a-longish-label-{i}") for i in range(1000)]
    links = [r.link(elements[i], elements[i + 1]) for i in range(999)]
    end = time.time() + 2.0
    while time.time() < end:
        for link in links:
            link.deliver()
    start = time.perf_counter()
    r.close(timeout=1.0)
    assert time.perf_counter() - start < 1.6
    server.stop()


def test_interpreter_exit_with_a_stalled_observer_is_bounded():
    server = RawServer("noread")
    code = textwrap.dedent(
        f"""
        import time
        from rxplus.reacting import Reporter, WebSocketSink
        r = Reporter("ex", WebSocketSink({server.url!r}), queue=2000)
        els = [r.element(f"Element-with-a-longish-label-{{i}}") for i in range(1000)]
        links = [r.link(els[i], els[i + 1]) for i in range(999)]
        end = time.time() + 2.0
        while time.time() < end:
            for link in links:
                link.deliver()
        """
    )
    start = time.perf_counter()
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert time.perf_counter() - start < 5.0  # 2 s of work + a bounded close
    server.stop()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_forked_child_reports_nothing_and_leaves_the_parent_alone():
    # N2.
    server = RawServer("normal")
    sink = WebSocketSink(server.url)
    r = Reporter("fk", sink, window=0.01)
    a, b = r.element("A"), r.element("B")
    link = r.link(a, b)
    assert _wait_for(lambda: r.live)
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child
        ok = not r.live
        for _ in range(20_000):
            link.deliver()
            a.span("x").end()
        ok = ok and not r._events and not r._accs
        os.write(write, b"1" if ok else b"0")
        os._exit(0)  # without atexit; a normal exit is covered by _closed
    os.close(write)
    child_ok = os.read(read, 1)
    os.waitpid(pid, 0)
    time.sleep(0.3)
    assert child_ok == b"1"
    assert r.live and sink.is_open()  # the parent's connection is untouched
    assert server.connections == 1
    r.close()
    server.stop()


def test_close_folds_windows_left_behind_by_a_late_record():
    # N4, the residual race: counts recorded into window 0 after the clock
    # moved on are reported with the next window it closes.
    shared = _Shared()
    acc = Accumulator("l1", shared)
    acc.start_session(1)
    shared.closed = 2  # the clock already closed windows 0 and 1 ...
    acc.record(1, 0, items=3)  # ... a caller that read the clock before
    windows = _windows(shared)
    assert list(windows) == [2] and windows[2]["l1"].items == 3


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_forked_children_never_block_on_the_parents_locks():
    # Re-review A: a parent thread may hold the reporter's lock at fork time.
    import signal
    import warnings

    r = Reporter("fl", MemorySink(), window=0.002)
    elements = [r.element(f"E{i}") for i in range(100)]
    links = [r.link(elements[i], elements[i + 1]) for i in range(99)]
    stop = threading.Event()

    def load() -> None:
        while not stop.is_set():
            for link in links:
                link.deliver()
            with elements[0].span("s"):
                pass
            r.element("tmp").remove()

    threads = [threading.Thread(target=load, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    hung = 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork with threads
        for _ in range(300):
            pid = os.fork()
            if pid == 0:
                signal.alarm(2)
                e = r.element("child")
                link = r.link(elements[0], e)
                link.deliver()
                e.span("c").end()
                r.remove(e)
                os._exit(0)
            _, status = os.waitpid(pid, 0)
            hung += os.WIFSIGNALED(status)
    stop.set()
    for t in threads:
        t.join()
    r.close()
    assert hung == 0


def test_catch_up_that_falls_further_behind_becomes_loss(tmp_path):
    # Re-review B: the lag was checked only when a catch-up started.
    clock = ManualClock()
    sink = MemorySink()
    r = Reporter("lag", sink, window=0.1, clock=clock, timer=False)
    links = [r.link(r.element(f"A{i}"), r.element(f"B{i}")) for i in range(50)]
    r.poll()
    for _ in range(30):  # under the 64-window limit when the catch-up starts
        for link in links:
            link.deliver()
        clock.advance(0.1)

    # Every clock read during the catch-up moves time on by 3 windows.
    st = r._st
    orig = st.monotonic

    def fast() -> float:
        value = orig()
        clock.advance(0.3)
        return value

    st.monotonic = fast
    r._step()
    st.monotonic = orig
    assert max(link._acc.pending() for link in links) <= 31
    r.poll()
    sigs = sink.signals()
    assert any(s["type"] == "snapshot" and s["seq"] > 0 for s in sigs)  # a Loss
    conform(sink, tmp_path)


def test_loss_with_events_pending_keeps_t_monotone_and_the_graph(tmp_path):
    # Re-review B: a Loss up to the current window while changes of the same
    # step are still to be emitted: they go into the snapshot, never after it.
    clock = ManualClock()
    sink = MemorySink()
    r = Reporter("test", sink, window=0.1, clock=clock, timer=False)
    links = [r.link(r.element(f"A{i}"), r.element(f"B{i}")) for i in range(50)]
    r.poll()
    for _ in range(30):
        for link in links:
            link.deliver()
        clock.advance(0.1)
    late = [r.element(f"late{i}") for i in range(5)]  # pending in this step
    st = r._st
    orig = st.monotonic

    def fast() -> float:
        value = orig()
        clock.advance(0.3)  # time runs away while the clock thread catches up
        return value

    st.monotonic = fast
    r._step()
    st.monotonic = orig
    r.element("after")
    clock.advance(0.2)
    r.poll()
    sigs = sink.signals()
    ts = [s["t"] for s in sigs]
    assert ts == sorted(ts)
    state = conform(sink, tmp_path)
    ids = {e["id"] for e in state["elements"]}
    assert all(e.id in ids for e in late)


def test_the_cap_on_window_totals_is_hard_and_becomes_loss(tmp_path):
    # Re-review B: however the clock thread is scheduled, the totals held
    # never exceed the cap; refused counts turn into a Loss.
    r, sink, clock = make(window=0.1)
    r._st.cap = 50  # a small cap for the test
    links = [r.link(r.element(f"A{i}"), r.element(f"B{i}")) for i in range(40)]
    r.poll()
    for _ in range(5):  # 200 totals wanted, no clock step in between
        for link in links:
            link.deliver()
        clock.advance(0.01)  # same window: 40 totals...
        clock.advance(0.1)  # ... then the next window: 40 more
    assert _totals(r._st) <= 50
    assert r._st.saturated
    r.poll()
    assert not r._st.saturated and not _totals(r._st)
    sigs = sink.signals()
    assert any(s["type"] == "snapshot" and s["seq"] > 0 for s in sigs)
    conform(sink, tmp_path)


def test_the_clock_waits_for_a_caller_write_in_progress(tmp_path):
    # Re-review 1fc2606, B: a caller wrote into a window dict the clock had
    # just popped, and the count was lost. The clock now moves a window out
    # under the lock the caller holds for its whole write.
    r, sink, clock = make(window=0.1)
    a, b = r.element("A"), r.element("B")
    link = r.link(a, b)
    r.poll()
    clock.advance(0.05)
    link.deliver()  # the shard holds a total: the clock must take its lock
    acc = link._acc
    shard = acc._shard
    shard.lock.acquire()  # a caller in record(), window 0, past reading `closed`
    try:
        clock.advance(0.1)
        closer = threading.Thread(target=r.poll)
        closer.start()
        time.sleep(0.2)
        assert closer.is_alive()  # the clock waits for the write to finish
        shard.windows[0][link.id].items += 5
    finally:
        shard.lock.release()
    closer.join(5.0)
    for _ in range(3):
        clock.advance(0.1)
        r.poll()
    r.close()
    flows = by_type(sink.signals(), "flow")
    assert sum(f["items"] for f in flows if f["link"] == link.id) == 6
    conform(sink, tmp_path)


def test_a_total_created_while_the_clock_skips_its_empty_shard_counts_later(
    tmp_path,
):
    # Issue #27: the clock skips a shard that holds nothing without its lock.
    # A total a caller creates meanwhile, for a window being closed, counts
    # in the next window the clock closes (R3), never lost.
    r, sink, clock = make(window=0.1)
    link = r.link(r.element("A"), r.element("B"))
    r.poll()
    clock.advance(0.15)
    r.poll()  # window 0 closed; the shard was empty and skipped
    shard = link._acc._shard
    with shard.lock:  # what a caller that read `closed` = 0 does
        shard.windows.setdefault(0, {})[link.id] = Totals(items=4)
        shard.count += 1
    for _ in range(2):
        clock.advance(0.1)
        r.poll()
    r.close()
    flows = [f for f in by_type(sink.signals(), "flow") if f["link"] == link.id]
    assert sum(f["items"] for f in flows) == 4
    conform(sink, tmp_path)


def test_callers_creating_windows_never_break_the_clock(tmp_path):
    # Re-review 1fc2606, A: callers added window keys while the clock thread
    # iterated them ("dictionary changed size during iteration") and the
    # clock thread died: reporting stopped for good.
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        sink = MemorySink()
        r = Reporter("race", sink, window=0.001, queue=10**7)
        els = [r.element(f"E{i}") for i in range(101)]
        links = [r.link(els[i], els[i + 1]) for i in range(100)]
        assert _wait_for(lambda: r.live)
        stop = time.monotonic() + 1.5

        def work(i: int) -> None:
            n = 0
            while time.monotonic() < stop:
                links[(n * 7 + i) % len(links)].deliver()
                n += 1

        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert r.clock_errors == 0
        assert r._threads[0].is_alive()
        r.close(timeout=10.0)
    finally:
        sys.setswitchinterval(old)
    conform(sink, tmp_path, window=0.001)


def test_the_clock_thread_survives_an_exception_in_a_step(tmp_path, caplog):
    # The clock thread must never die silently: the step is logged, counted,
    # and the reporter resynchronises with a new session.
    sink = MemorySink()
    r = Reporter("boom", sink, window=0.05)
    a, b = r.element("A"), r.element("B")
    link = r.link(a, b)
    assert _wait_for(lambda: r.live)
    real = r._close_one
    armed = [True]

    def close_one(k: int, closing: dict) -> bool:
        if armed[0]:
            armed[0] = False
            raise RuntimeError("injected")
        return real(k, closing)

    r._close_one = close_one  # type: ignore[method-assign]
    with caplog.at_level("ERROR", logger="rxplus.reacting"):
        link.deliver()
        assert _wait_for(lambda: r.clock_errors == 1)
        assert _wait_for(  # a new session
            lambda: sum(s["seq"] == 0 for s in sink.signals()) == 2 and r.live
        )
        assert r._threads[0].is_alive()
        link.deliver(3)
        time.sleep(0.15)
    r.close(timeout=10.0)
    assert any(
        rec.exc_info and str(rec.exc_info[1]) == "injected" for rec in caplog.records
    )
    sigs = sink.signals()
    openings = [
        i for i, s in enumerate(sigs) if s["type"] == "snapshot" and s["seq"] == 0
    ]
    assert len(openings) == 2
    after = sigs[openings[1] :]
    assert {e["label"] for e in after[0]["elements"]} == {"A", "B"}
    assert sum(f["items"] for f in after if f["type"] == "flow") == 3
    conform(sink, tmp_path, window=0.05)


def test_a_loss_of_the_clock_keeps_backlogs_in_its_snapshot(tmp_path):
    # Spec 5ad5cdb on the lag/cap Loss path (_shed): the backlog of an id
    # not in the busy set was forgotten until its next hot-path call.
    r, sink, clock = make(window=0.1)
    r._st.cap = 5
    a, b = r.element("A"), r.element("B")
    held = r.link(a, b)
    others = [r.link(r.element(f"X{i}"), r.element(f"Y{i}")) for i in range(10)]
    r.poll()
    held.enter()
    held.enter()
    clock.advance(0.1)
    r.poll()  # window 0 closed: depth 2
    for link in others:  # more totals than the cap: Loss at the next window end
        link.deliver()
    clock.advance(0.1)
    r.poll()
    snaps = [s for s in by_type(sink.signals(), "snapshot") if s["seq"] > 0]
    assert snaps and snaps[-1]["backlogs"] == [{"id": held.id, "value": 2}]
    clock.advance(0.1)
    r.poll()
    state = conform(sink, tmp_path)
    assert state["backlog"][held.id]["value"] == 2
