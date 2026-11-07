"""The vendored reacting/1 checker and corpus agree with PIN and with each other."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.conformance import check

HERE = Path(__file__).resolve().parent


def _pin() -> tuple[str, dict[str, str]]:
    lines = (HERE / "PIN").read_text(encoding="utf-8").splitlines()
    commit = lines[0].removeprefix("commit ").strip()
    hashes = {}
    for line in lines[1:]:
        digest, name = line.split(maxsplit=1)
        hashes[name.strip()] = digest
    return commit, hashes


def test_pin_matches_vendored_files():
    commit, hashes = _pin()
    assert len(commit) == 40
    vendored = sorted(
        [name for name in ("check.py", "README.md")]
        + [
            str(p.relative_to(HERE))
            for folder in ("valid", "invalid")
            for p in (HERE / folder).rglob("*")
            if p.is_file()
        ]
    )
    assert sorted(hashes) == vendored
    for name, digest in hashes.items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == digest, name


CASES = sorted((HERE / "valid").glob("*.jsonl")) + sorted(
    (HERE / "invalid").glob("*.jsonl")
)


@pytest.mark.parametrize("recording", CASES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_checker_agrees_with_expectation(recording: Path):
    expect = json.loads(recording.with_suffix(".expect.json").read_text("utf-8"))
    violations = check.check(recording)
    assert (not violations) == expect["valid"]
    got = [{"line": v.line, "rule": v.rule} for v in violations]
    assert got == [{"line": v["line"], "rule": v["rule"]} for v in expect["violations"]]
    assert check.fold(recording) == expect["state"]
