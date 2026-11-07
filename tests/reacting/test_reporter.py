"""Reporter: sessions, seq, windows, depth, latency, spans, Loss.

Every test that produces signals also runs them through the vendored
reference checker (``conform``), which is the only guard against a source
bug: observers ignore malformed signals silently.
"""

import pytest

from rxplus.reacting import current_span
from tests.reacting.helpers import T0, by_type, conform, make


def test_session_starts_with_snapshot(tmp_path):
    r, sink, clock = make()
    app = r.element("App", kind="pipeline")
    a = app.child("A")
    b = app.child("B")
    r.link(a, b)
    r.poll()
    sigs = sink.signals()
    assert sigs[0]["type"] == "snapshot" and sigs[0]["seq"] == 0 and sigs[0]["t"] == T0
    assert sigs[0]["label"] == "Test" and sigs[0]["protocol"] == "reacting/1"
    assert [s["seq"] for s in sigs] == list(range(len(sigs)))
    assert [s["type"] for s in sigs] == ["snapshot", "add", "add", "add", "add"]
    state = conform(sink, tmp_path)
    assert [e["id"] for e in state["elements"]] == ["e1", "e2", "e3"]
    assert state["links"] == [{"id": "l1", "from": "e2", "to": "e3", "kind": "data"}]


def test_graph_built_before_session_is_in_the_snapshot(tmp_path):
    r, sink, clock = make()
    sink.disconnect()
    r.poll()  # write fails: session over
    a = r.element("A")
    r.link(a, r.element("B"))
    assert not r.live and len(sink.lines) == 1  # only the first, empty session
    before = len(sink.lines)
    sink.connect()
    r.poll()
    snap = sink.signals()[before]
    assert snap["type"] == "snapshot" and snap["seq"] == 0
    assert [e["id"] for e in snap["elements"]] == ["e1", "e2"]
    assert [link["id"] for link in snap["links"]] == ["l1"]


def test_flow_per_window_aligned_to_session_start(tmp_path):
    r, sink, clock = make()
    a, b = r.element("A"), r.element("B")
    link = r.link(a, b)
    clock.advance(0.01)
    link.deliver(3, bytes=30)
    clock.advance(0.2)  # window 1 stays empty
    link.deliver()
    link.drop(2)
    clock.advance(0.1)
    r.poll()
    flows = by_type(sink.signals(), "flow")
    assert [
        (f["t0"], f["t"], f["items"], f.get("bytes"), f["dropped"]) for f in flows
    ] == [
        (T0, T0 + 0.1, 3, 30, 0),
        (T0 + 0.2, T0 + 0.30000000000000004, 1, None, 2),
    ]
    conform(sink, tmp_path)


def test_drop_only_window_has_no_bytes(tmp_path):
    r, sink, clock = make()
    link = r.link(r.element("A"), r.element("B"))
    link.drop()
    clock.advance(0.1)
    r.poll()
    (flow,) = by_type(sink.signals(), "flow")
    assert flow["items"] == 0 and "bytes" not in flow and flow["dropped"] == 1
    conform(sink, tmp_path)


def test_depth_rules_with_capacity(tmp_path):
    r, sink, clock = make()
    link = r.link(r.element("A"), r.element("B"), capacity=8)
    tokens = [link.enter(), link.enter()]
    clock.advance(0.1)  # end of window 0: backlog 2
    link.leave(tokens.pop())
    clock.advance(0.1)  # end of window 1: backlog 1
    link.leave(tokens.pop())
    clock.advance(0.1)  # end of window 2: backlog 0 -> depth 0 once
    clock.advance(0.1)  # end of window 3: nothing
    r.poll()
    depths = by_type(sink.signals(), "depth")
    assert [(d["t"] - T0, d["value"], d["capacity"]) for d in depths] == [
        pytest.approx((0.1, 2, 8)),
        pytest.approx((0.2, 1, 8)),
        pytest.approx((0.3, 0, 8)),
    ]
    state = conform(sink, tmp_path)
    assert state["backlog"] == {}


def test_backlog_at_boundary_excludes_later_changes(tmp_path):
    """Windows are closed late (poll after the boundary): the depth signal
    still reports the backlog at the boundary, not the one now."""
    r, sink, clock = make()
    link = r.link(r.element("A"), r.element("B"))
    tok = link.enter()
    clock.advance(0.15)  # now in window 1
    link.leave(tok)  # backlog 0 now, but it was 1 at the end of window 0
    r.poll()
    clock.advance(0.1)
    r.poll()
    depths = by_type(sink.signals(), "depth")
    assert [d["value"] for d in depths] == [1, 0]
    conform(sink, tmp_path)


def test_sampled_depth(tmp_path):
    backlog = [3]
    r, sink, clock = make()
    r.link(r.element("A"), r.element("B"), capacity=4, depth=lambda: backlog[0])
    clock.advance(0.1)
    r.poll()
    backlog[0] = 9  # above capacity: reported as capacity
    clock.advance(0.1)
    r.poll()
    backlog[0] = 0
    clock.advance(0.2)
    r.poll()
    assert [d["value"] for d in by_type(sink.signals(), "depth")] == [3, 4, 0]
    conform(sink, tmp_path)


def test_latency_of_element_and_link(tmp_path):
    r, sink, clock = make()
    el = r.element("Worker")
    link = r.link(r.element("Src"), el)
    for d in (0.01, 0.03):
        tok, etok = link.enter(), el.begin()
        clock.advance(d)
        link.leave(tok)
        el.end(etok)
    clock.advance(0.1)
    r.poll()
    lats = {s["id"]: s for s in by_type(sink.signals(), "latency")}
    assert lats[link.id]["count"] == 2
    assert lats[link.id]["max"] == pytest.approx(0.03)
    assert lats[link.id]["sum"] == pytest.approx(0.04)
    assert lats[el.id]["count"] == 2
    conform(sink, tmp_path)


def test_call_link(tmp_path):
    r, sink, clock = make()
    call = r.link(r.element("Client"), r.element("Db"), kind="call")
    with call.call():
        clock.advance(0.15)  # the call ends in window 1
    clock.advance(0.1)
    r.poll()
    sigs = sink.signals()
    (flow,) = by_type(sigs, "flow")
    assert flow["t0"] == T0 and flow["items"] == 1  # counted when made
    (lat,) = by_type(sigs, "latency")
    assert lat["t0"] == pytest.approx(T0 + 0.1) and lat["count"] == 1
    assert [d["value"] for d in by_type(sigs, "depth")] == [1, 0]
    conform(sink, tmp_path)
    with pytest.raises(ValueError):
        r.link(r.element("x"), r.element("y")).call()


def test_structural_change_closes_ended_windows_first(tmp_path):
    r, sink, clock = make()
    link = r.link(r.element("A"), r.element("B"))
    link.deliver()
    clock.advance(0.12)
    r.element("C")  # no poll in between: the flow of window 0 must come first
    r.poll()
    types = [s["type"] for s in sink.signals()]
    assert types.index("flow") < len(types) - 1 and types[-1] == "add"
    conform(sink, tmp_path)


def test_remove_cascades_and_ends_spans_silently(tmp_path):
    r, sink, clock = make()
    parent = r.element("P")
    child = parent.child("C")
    other = r.element("O")
    r.link(child, other)
    span = child.span("work")
    clock.advance(0.01)
    parent.remove()
    span.end()  # already ended by the removal: no span.end
    r.poll()
    sigs = sink.signals()
    assert sigs[-1]["type"] == "remove" and sigs[-1]["ids"] == ["e1"]
    assert not by_type(sigs, "span.end")
    state = conform(sink, tmp_path)
    assert [e["id"] for e in state["elements"]] == ["e3"]
    assert state["links"] == [] and state["spans"] == []
    assert state["ended"][0]["status"] == "error"


def test_spans_nest_through_the_context(tmp_path):
    r, sink, clock = make()
    a, b = r.element("A"), r.element("B")
    with a.span("outer") as outer:
        assert current_span() is outer
        with b.span("inner"):
            clock.advance(0.01)
        clock.advance(0.01)
    assert current_span() is None
    with pytest.raises(KeyError):
        with a.span("failing"):
            raise KeyError("a secret value")
    r.poll()
    sigs = sink.signals()
    starts = by_type(sigs, "span.start")
    assert "parent" not in starts[0] and starts[1]["parent"] == starts[0]["span"]
    ends = by_type(sigs, "span.end")
    assert ends[-1] == {**ends[-1], "status": "error", "error": "KeyError"}
    assert "secret" not in sink.lines[-1]
    conform(sink, tmp_path)


def test_ended_parent_is_not_sent(tmp_path):
    r, sink, clock = make()
    a = r.element("A")
    parent = a.span("p")
    parent.end()
    child = a.span("c", parent=parent)
    child.end()
    r.poll()
    assert "parent" not in by_type(sink.signals(), "span.start")[1]
    conform(sink, tmp_path)


def test_loss_discards_queue_and_resends_snapshot(tmp_path):
    r, sink, clock = make(queue=5)
    r.poll()  # the seq-0 snapshot is written
    for i in range(12):
        r.element(f"E{i}")
    r.poll()
    sigs = sink.signals()
    snaps = by_type(sigs, "snapshot")
    assert len(snaps) >= 2 and snaps[-1]["seq"] > snaps[0]["seq"] + 1
    gap_next = sigs[1]
    assert gap_next["type"] == "snapshot" and gap_next["seq"] > 1
    state = conform(sink, tmp_path)
    assert len(state["elements"]) == 12
    assert state["lost"] == sigs[-1]["seq"] + 1 - len(sigs)


def test_new_session_after_reconnect_keeps_open_spans(tmp_path):
    r, sink, clock = make()
    a = r.element("A")
    span = a.span("long")
    r.poll()
    sink.disconnect()
    a.child("lost while disconnected")  # queued, then discarded with the session
    r.poll()
    assert not r.live
    sink.connect()
    clock.advance(0.05)
    r.poll()
    span.end()
    r.poll()
    sigs = sink.signals()
    snaps = by_type(sigs, "snapshot")
    assert [s["seq"] for s in snaps] == [0, 0]
    assert snaps[1]["spans"][0]["span"] == "s1"
    assert sigs[-1]["type"] == "span.end"
    conform(sink, tmp_path)


def test_no_session_builds_nothing():
    r, sink, clock = make()
    sink.disconnect()
    r.poll()
    link = r.link(r.element("A"), r.element("B"))
    written = len(sink.lines)
    for _ in range(1000):
        link.deliver()
        link.leave(link.enter())
    span = r.element("C").span("x")
    assert span.id is None
    assert len(r._q) == 0 and len(sink.lines) == written
    assert link._acc.next_window() is None


def test_validation():
    r, sink, clock = make()
    with pytest.raises(ValueError):
        r.element("")
    with pytest.raises(ValueError):
        r.link(r.element("A"), "test/e1")  # global reference to itself
    with pytest.raises(ValueError):
        r.link(r.element("A"), r.element("B"), kind="stream")  # type: ignore[arg-type]
    other, *_ = make()
    with pytest.raises(ValueError):
        r.link(r.element("A"), other.element("B"))
    r.link(r.element("A"), "hub/e9")  # a global reference to another source


def test_close_is_idempotent_and_final(tmp_path):
    r, sink, clock = make()
    link = r.link(r.element("A"), r.element("B"))
    link.deliver()
    clock.advance(0.1)
    r.close()
    r.close()
    assert not r.live
    assert by_type(sink.signals(), "flow")
    conform(sink, tmp_path)


def test_a_recovery_snapshot_lists_the_backlogs(tmp_path):
    # Spec 5ad5cdb: without them an observer shows every backlog as zero
    # after a Loss until the next depth signal.
    r, sink, clock = make(queue=5)
    a, b = r.element("A"), r.element("B")
    counted = r.link(a, b, capacity=10)
    sampled = r.link(b, a, depth=lambda: 4)
    r.poll()
    tokens = [counted.enter() for _ in range(3)]
    for i in range(12):  # overflow the queue: Loss
        r.element(f"E{i}")
    r.poll()
    snaps = [s for s in by_type(sink.signals(), "snapshot") if s["seq"] > 0]
    assert snaps
    assert snaps[0]["backlogs"] == [
        {"id": counted.id, "value": 3, "capacity": 10},
        {"id": sampled.id, "value": 4},
    ]
    state = conform(sink, tmp_path)
    assert state["backlog"][counted.id]["value"] == 3
    # The depth rules continue from the listed value: a drain reports 0.
    for token in tokens:
        counted.leave(token)
    clock.advance(0.1)
    r.poll()
    depths = [d for d in by_type(sink.signals(), "depth") if d["id"] == counted.id]
    assert depths and depths[-1]["value"] == 0
    state = conform(sink, tmp_path)
    assert counted.id not in state["backlog"]
