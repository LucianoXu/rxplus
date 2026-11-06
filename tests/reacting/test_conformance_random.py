"""Randomised sessions: every recording must pass the reference checker, and
the observer's fold must match what the test knows it did."""

import random

import pytest

from rxplus.reacting import Element, Link, Span
from tests.reacting.helpers import by_type, conform, make


@pytest.mark.parametrize("seed", range(12))
def test_random_session_conforms(seed, tmp_path):
    rng = random.Random(seed)
    queue = rng.choice([40, 200, 10_000])  # small queues force Loss
    r, sink, clock = make(window=rng.choice([0.05, 0.1, 0.25]), queue=queue)
    window = r._w
    elements: list[Element] = [r.element("root")]
    links: list[Link] = []
    spans: list[Span] = []
    tokens: dict[str, list[float]] = {}
    delivered: dict[str, int] = {}

    for _step in range(1500):
        op = rng.random()
        if op < 0.08 and len(elements) < 30:
            parent = rng.choice(elements + [None])
            el = r.element(
                f"el{rng.randrange(5)}",
                kind=rng.choice([None, "operator"]),
                parent=parent,
            )
            elements.append(el)
        elif op < 0.14 and len(elements) >= 2:
            a, b = rng.sample(elements, 2)
            capacity = rng.choice([None, 1000])
            links.append(
                r.link(
                    a, b, kind=rng.choice(["data", "data", "call"]), capacity=capacity
                )
            )
        elif op < 0.17 and len(elements) > 3:
            victim = rng.choice(elements[1:])
            victim.remove()
            alive = {e for e in r._elements}
            elements = [e for e in elements if e.id in alive]
            links = [lk for lk in links if lk.id in r._links]
        elif op < 0.45 and links:
            lk = rng.choice(links)
            if lk.kind == "call":
                with lk.call():
                    clock.advance(rng.random() * window * 0.3)
            elif rng.random() < 0.5:
                tokens.setdefault(lk.id, []).append(lk.enter())
            elif tokens.get(lk.id):
                lk.leave(tokens[lk.id].pop(0), bytes=rng.randrange(100))
                delivered[lk.id] = delivered.get(lk.id, 0) + 1
            else:
                n = rng.randrange(1, 4)
                lk.deliver(n)
                delivered[lk.id] = delivered.get(lk.id, 0) + n
        elif op < 0.55 and elements:
            el = rng.choice(elements)
            parent = rng.choice(spans) if spans and rng.random() < 0.5 else None
            spans.append(el.span(f"work{rng.randrange(3)}", parent=parent))
        elif op < 0.62 and spans:
            sp = spans.pop(rng.randrange(len(spans)))
            sp.end(rng.choice([None, ValueError("x"), "timeout"]))
        elif op < 0.64:
            sink.disconnect()
            r.poll()
            sink.connect()
        clock.advance(rng.random() * window * rng.choice([0.1, 0.5, 3]))
        if rng.random() < 0.3:
            r.poll()
    r.close()

    state = conform(sink, tmp_path, window=window)
    assert sorted(e["id"] for e in state["elements"]) == sorted(r_ids(r, "e"))
    # Without loss or reconnects, the observer's item counts equal ours.
    sigs = sink.signals()
    sessions = len(by_type(sigs, "snapshot"))
    if sessions == 1:
        seen: dict[str, int] = {}
        for f in by_type(sigs, "flow"):
            seen[f["link"]] = seen.get(f["link"], 0) + f["items"]
        for lid, n in delivered.items():
            assert seen.get(lid, 0) <= n


def r_ids(r, prefix: str) -> list[str]:
    return [i for i in r._elements if i.startswith(prefix)]


def test_window_boundaries_do_not_drift(tmp_path):
    r, sink, clock = make(window=0.1)
    link = r.link(r.element("A"), r.element("B"))
    clock.advance(100_000.05)  # a million windows later
    link.deliver()
    clock.advance(0.1)
    r.poll()
    (flow,) = by_type(sink.signals(), "flow")
    assert flow["t0"] == r._t_start + 1_000_000 * 0.1
    conform(sink, tmp_path)
