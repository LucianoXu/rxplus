"""Shared helpers: build reporters on a manual clock and check their recordings."""

import json
from pathlib import Path

from rxplus.reacting import ManualClock, MemorySink, Reporter
from tests.conformance import check

T0 = 1_762_430_400.0


def make(
    window: float = 0.1, queue: int = 10_000, label: str | None = "Test"
) -> tuple[Reporter, MemorySink, ManualClock]:
    """A reporter with no background thread: tests drive it with poll()."""
    sink = MemorySink()
    clock = ManualClock(T0)
    reporter = Reporter(
        "test", sink, label=label, window=window, queue=queue, clock=clock, timer=False
    )
    return reporter, sink, clock


def signals(sink: MemorySink) -> list[dict[str, object]]:
    return sink.signals()


def conform(sink: MemorySink, tmp_path: Path, window: float = 0.1) -> dict[str, object]:
    """Write the sink's lines as a recording; the reference checker must accept
    it (``--direct``). Returns the observer's folded state for this source."""
    path = tmp_path / f"recording-{len(list(tmp_path.iterdir()))}.jsonl"
    path.write_text("".join(f"{line}\n" for line in sink.lines), encoding="utf-8")
    violations = check.check(path, direct=True, window=window)
    assert not violations, (
        "\n".join(f"line {v.line}: {v.rule}: {v.message}" for v in violations)
        + "\n"
        + path.read_text()
    )
    state = check.fold(path, direct=True, window=window)
    sources = state["sources"]
    assert isinstance(sources, dict)
    return sources.get("test", {})


def by_type(sigs: list[dict[str, object]], typ: str) -> list[dict[str, object]]:
    return [s for s in sigs if s["type"] == typ]


def dumps(sigs: list[dict[str, object]]) -> str:
    return "\n".join(json.dumps(s) for s in sigs)
