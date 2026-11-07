"""Probe operators (rxplus.reacting.rx) and ContextScheduler."""

import threading

import pytest
import reactivex
from reactivex import operators as ops
from reactivex.scheduler import NewThreadScheduler
from reactivex.subject import Subject

from rxplus.reacting import MemorySink, Reporter, current_span
from rxplus.reacting import rx as rr
from tests.reacting.helpers import by_type, conform, make


def test_a_probe_without_a_handle_is_the_identity():
    source = reactivex.of(1, 2)
    for op in (rr.count(None), rr.enter(None), rr.leave(None), rr.work(None)):
        assert op(source) is source
    assert rr.span(None, "x")(source) is source


def test_count_delivers_each_item_with_its_size(tmp_path):
    r, sink, clock = make()
    a, b = r.element("A"), r.element("B")
    link = r.link(a, b)
    r.poll()
    out: list[bytes] = []
    src: Subject[bytes] = Subject()
    src.pipe(rr.count(link, size=len)).subscribe(out.append)
    for item in (b"ab", b"cde", b"f"):
        src.on_next(item)
    clock.advance(0.1)
    r.poll()
    assert out == [b"ab", b"cde", b"f"]  # unchanged, in order
    (flow,) = by_type(sink.signals(), "flow")
    assert flow["items"] == 3 and flow["bytes"] == 6
    conform(sink, tmp_path)


def test_without_a_session_a_probe_does_not_measure():
    r, _, clock = make()
    r.close()  # no session from now on
    a, b = r.element("A"), r.element("B")
    sizes: list[object] = []
    out: list[int] = []
    src: Subject[int] = Subject()
    src.pipe(rr.count(r.link(a, b), size=lambda v: sizes.append(v) or 1)).subscribe(
        out.append
    )
    src.on_next(1)
    assert out == [1] and sizes == []  # size() is never called


def test_enter_and_leave_measure_backlog_and_latency_across_a_hop(tmp_path):
    r, sink, clock = make()
    a, b = r.element("A"), r.element("B")
    link = r.link(a, b)
    r.poll()
    hop: list[int] = []
    src: Subject[int] = Subject()
    src.pipe(rr.enter(link)).subscribe(hop.append)
    far: Subject[int] = Subject()
    out: list[int] = []
    far.pipe(rr.leave(link)).subscribe(out.append)
    src.on_next(1)
    src.on_next(2)
    clock.advance(0.1)
    r.poll()  # window 0: two waiting
    clock.advance(0.05)
    for item in hop:  # the hop delivers them, in order
        far.on_next(item)
    clock.advance(0.05)
    r.poll()
    assert out == [1, 2]
    sigs = sink.signals()
    assert [d["value"] for d in by_type(sigs, "depth")] == [2, 0]
    (flow,) = by_type(sigs, "flow")
    assert flow["items"] == 2
    (lat,) = by_type(sigs, "latency")
    assert lat["count"] == 2 and lat["max"] == pytest.approx(0.15)
    conform(sink, tmp_path)


def test_leave_without_a_token_counts_flow_only(tmp_path):
    r, sink, clock = make()
    link = r.link(r.element("A"), r.element("B"))
    r.poll()
    src: Subject[int] = Subject()
    src.pipe(rr.leave(link)).subscribe(lambda _: None)
    src.on_next(1)  # entered before the session, or not through `enter`
    clock.advance(0.1)
    r.poll()
    sigs = sink.signals()
    assert by_type(sigs, "flow")[0]["items"] == 1
    assert not by_type(sigs, "depth") and not by_type(sigs, "latency")
    conform(sink, tmp_path)


def test_work_measures_the_downstream_call(tmp_path):
    r, sink, clock = make()
    element = r.element("Store")
    r.poll()
    src: Subject[int] = Subject()
    src.pipe(rr.work(element)).subscribe(lambda _: clock.advance(0.02))
    src.on_next(1)
    src.on_next(2)
    clock.advance(0.1 - 0.04)
    r.poll()
    (lat,) = by_type(sink.signals(), "latency")
    assert lat["id"] == element.id and lat["count"] == 2
    assert lat["sum"] == pytest.approx(0.04)
    conform(sink, tmp_path)


def test_span_wraps_each_item_and_reports_errors(tmp_path):
    r, sink, clock = make()
    element = r.element("Handler")
    inner = r.element("Inner")
    r.poll()
    src: Subject[int] = Subject()
    parents: list[object] = []

    def downstream(value: int) -> None:
        with inner.span("nested") as s:
            parents.append(current_span() is s)
        if value < 0:
            raise ValueError("secret data")

    src.pipe(rr.span(element, "item")).subscribe(downstream)
    src.on_next(1)
    with pytest.raises(ValueError):
        src.on_next(-1)  # the exception propagates unchanged
    r.poll()
    sigs = sink.signals()
    starts = by_type(sigs, "span.start")
    outer = [s for s in starts if s["label"] == "item"]
    nested = [s for s in starts if s["label"] == "nested"]
    assert len(outer) == 2 and len(nested) == 2
    assert [n["parent"] for n in nested] == [o["span"] for o in outer]
    ends = {e["span"]: e for e in by_type(sigs, "span.end")}
    assert ends[outer[0]["span"]]["status"] == "ok"
    assert ends[outer[1]["span"]]["status"] == "error"
    assert ends[outer[1]["span"]]["error"] == "ValueError"  # never str(e)
    conform(sink, tmp_path)


def test_context_scheduler_keeps_the_current_span_across_threads():
    r = Reporter("ctx", MemorySink(), timer=True)
    element = r.element("A")
    try:
        assert _wait(lambda: r.live)
        seen: list[object] = []
        done = threading.Event()
        scheduler = rr.ContextScheduler(NewThreadScheduler())

        def action(sch: object, state: object) -> None:
            seen.append(current_span())
            done.set()

        with element.span("outer") as s:
            scheduler.schedule(action)
        assert done.wait(5.0)
        assert seen == [s]

        # And through observe_on in a pipeline.
        got: list[object] = []
        finished = threading.Event()
        src: Subject[int] = Subject()
        src.pipe(ops.observe_on(scheduler)).subscribe(
            lambda _: (got.append(current_span()), finished.set())
        )
        with element.span("outer2") as s2:
            src.on_next(1)
        assert finished.wait(5.0)
        assert got == [s2]
    finally:
        r.close()


def _wait(cond, timeout: float = 5.0) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_context_scheduler_drives_interval():
    """Review of #28: it was not a periodic scheduler (rx.interval failed)."""
    from reactivex.scheduler import HistoricalScheduler

    inner = HistoricalScheduler()
    ticks: list[int] = []
    reactivex.interval(1.0, scheduler=rr.ContextScheduler(inner)).subscribe(
        ticks.append
    )
    inner.advance_by(3.5)
    assert ticks == [0, 1, 2]


def test_enter_and_leave_stay_paired_across_a_session_gap(tmp_path):
    """Review of #28: tokens were kept only while live, so after a gap
    every item paired with the wrong token (0.01 s reported as 0.09 s)."""
    r, sink, clock = make(window=0.1)
    link = r.link(r.element("A"), r.element("B"))
    src: Subject[int] = Subject()
    src.pipe(rr.enter(link)).subscribe(lambda _: None)
    out: Subject[int] = Subject()
    out.pipe(rr.leave(link)).subscribe(lambda _: None)
    r.poll()
    clock.advance(0.05)
    src.on_next(1)
    src.on_next(2)
    sink.disconnect()
    r.poll()
    out.on_next(1)  # leave while no session: still pops its token
    out.on_next(2)
    sink.connect()
    r.poll()
    for i in range(5):
        clock.advance(0.03)
        src.on_next(i)
        clock.advance(0.01)
        out.on_next(i)
        r.poll()
    clock.advance(0.3)
    r.poll()
    lats = by_type(sink.signals(), "latency")
    assert lats and all(lat["max"] == pytest.approx(0.01, abs=1e-6) for lat in lats)
    assert not link._tokens
    conform(sink, tmp_path, window=0.1)
