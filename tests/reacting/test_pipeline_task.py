"""The end-to-end example (tasks/task_reacting_pipeline.py) records a valid
session: real components, probes, reconnects and a thread hop."""

import json

from tasks import task_reacting_pipeline as pipeline
from tests.conformance import check


def test_the_example_pipeline_records_a_conforming_session(tmp_path):
    path = tmp_path / "pipeline.jsonl"
    pipeline.run(pipeline.Options(record=str(path), duration=3.0, sensors=2, rate=50.0))
    violations = check.check(path, direct=True, window=0.1)
    assert not violations, [(v.line, v.rule, v.message) for v in violations][:5]
    sigs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    kinds = {
        e.get("kind")
        for s in sigs
        if s["type"] in ("snapshot", "add")
        for e in s.get("elements", [])
    }
    assert {"rxplus.ws.server", "port", "rxplus.ws.connection.instance"} <= kinds
    assert {"rxplus.ws.client", "rxplus.ws.socket.instance"} <= kinds
    flows = [s for s in sigs if s["type"] == "flow"]
    assert sum(f["items"] for f in flows) > 100
