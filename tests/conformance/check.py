#!/usr/bin/env python3
"""Reference checker for the reacting/1 protocol (docs/spec/Reacting.md).

    python3 conformance/check.py check <recording.jsonl> [--window W] [--direct] [--tolerance S] [--schema]
    python3 conformance/check.py fold  <recording.jsonl> [--window W] [--direct] [--tolerance S]

`check` decides whether the recording could have been produced by conforming sources and relays.
It prints one line per violation, `line <n>: <rule>: <message>`, and exits 0 if there is none, 1
otherwise. Exit status 2 means the checker could not decide: a usage or I/O error, or an internal
error (a bug in the checker, reported on stderr).

`fold` folds the recording as an observer does (spec, Receivers: a signal that breaks a rule is
ignored as a whole, except that a `parent` naming a span that is not open is treated as omitted;
an ignored signal with a valid source and seq still counts as received; the rules of Windows are
not checked) and prints the final state in the conformance-state format, sorted keys and 2-space
indentation.

Written from the spec text alone. Standard library only, Python >= 3.12. The interpretations of
ambiguous spec text are listed in conformance/README.md.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROTOCOL = "reacting/1"
MAX_INT = 2**53 - 1
SIGNAL_TYPES = ("snapshot", "add", "remove", "flow", "depth", "latency", "span.start", "span.end")
DEFAULT_TOLERANCE = 1e-6  # absolute floor, in seconds, for window comparisons
RELATIVE_TOLERANCE = 1e-4  # window comparisons also allow this fraction of the window length
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "spec" / "Reacting.schema.json"

GENERIC_KEYS = frozenset({"source", "seq", "t", "type"})
TYPE_KEYS = {
    "snapshot": frozenset({"protocol", "label", "elements", "links", "spans"}),
    "add": frozenset({"elements", "links"}),
    "remove": frozenset({"ids"}),
    "flow": frozenset({"link", "t0", "items", "bytes", "dropped", "coalesced"}),
    "depth": frozenset({"id", "value", "capacity"}),
    "latency": frozenset({"id", "t0", "count", "sum", "max"}),
    "span.start": frozenset({"span", "id", "label", "parent"}),
    "span.end": frozenset({"span", "status", "error"}),
}
ELEMENT_KEYS = frozenset({"id", "label", "parent", "kind"})
LINK_KEYS = frozenset({"id", "from", "to", "kind"})
OPEN_SPAN_KEYS = frozenset({"span", "id", "label", "parent", "t0"})

# Every rule identifier the checker can report, with a one-line description (README lists them).
RULES = {
    "encoding": "a line is not UTF-8 (including escapes of lone surrogates), or carries a byte order mark",
    "json": "a non-empty line is not one JSON object (NaN and Infinity are not JSON)",
    "duplicate-key": "a JSON object of a signal has a duplicate key",
    "attr-missing": "a required attribute is missing",
    "attr-type": "an attribute has the wrong JSON type",
    "attr-value": "an attribute is not one of its allowed values (link kind, span status)",
    "null-attr": "an attribute has the value null",
    "int-format": "an integer is written with a fraction or exponent",
    "int-range": "an integer is outside 0 .. 2^53 - 1",
    "time-negative": "a time or duration is negative",
    "label-empty": "a label is the empty string",
    "id-invalid": "a source name, ID or reference is empty, or an ID contains '/'",
    "ref-global": "a reference that may only be local contains '/'",
    "ref-invalid": "a global reference is not of the form <source>/<id>",
    "ref-global-self": "a global reference names the signal's own source",
    "protocol": "a snapshot's protocol is not reacting/1 (the observer closes the connection)",
    "t0-after-t": "a t0 is greater than the t of its signal",
    "add-empty": "an add has neither a non-empty elements nor a non-empty links",
    "remove-empty": "a remove has an empty ids",
    "spans-empty": "a snapshot has spans present but empty (it must be omitted)",
    "span-error-attr": "a span.end with status ok has an error",
    "flow-zero": "a flow counts no item (items + dropped + coalesced = 0)",
    "flow-bytes": "a flow has bytes > 0 but items = 0",
    "depth-capacity": "a depth value exceeds its capacity",
    "latency-count": "a latency count is less than 1",
    "latency-stats": "latency sum and max are inconsistent (max > sum, or sum > count * max)",
    "snapshot-first": "the first signal of a source is not a snapshot with seq 0",
    "seq-order": "seq does not strictly increase within a session",
    "t-order": "t decreases within a session",
    "loss-snapshot": "a seq gap (loss) is not followed by a snapshot as the next signal",
    "snapshot-no-gap-change": "a snapshot with no seq gap describes a state other than the one held",
    "ref-missing": "a local reference names no existing element or link, or no open span",
    "ref-kind": "a reference names a link where an element is required, or the reverse",
    "parent-order": "an element (or span) is listed before its parent in the same array",
    "id-duplicate": "an element or link ID is already in use in the shared namespace",
    "span-duplicate": "a snapshot lists the same span twice",
    "span-reuse": "a span ID is used for a second span in one session",
    "span-not-open": "a span.end names a span that is not open",
    "span-parent": "a span.start parent names a span that is not open (observer omits it)",
    "window-bounds": "a window's t0/t are not aligned to T + k*w, or t - t0 differs from w",
    "window-duplicate": "two flow, latency or depth signals for one ID in one window",
    "depth-zero": "a depth of value 0 not preceded by a non-zero depth at the previous window end",
    "depth-missing": "a non-zero backlog is not followed by a depth signal at the next window end",
}

# Rules about the shape of one signal that the JSON Schema also describes; used by --schema to
# report disagreements between the schema and this checker.
SCHEMA_RULES = frozenset({
    "attr-missing", "attr-type", "attr-value", "null-attr", "int-format", "int-range",
    "time-negative", "label-empty", "id-invalid", "ref-global", "ref-invalid", "protocol",
    "add-empty", "remove-empty", "latency-count", "span-error-attr", "spans-empty",
})

# Rules that check reports but that do not make the observer ignore the signal: a parent that is
# not open is treated as omitted (spec, Receivers), a missing depth signal is not a signal, and a
# snapshot replaces the state whatever it holds.
SOFT_RULES = frozenset({"span-parent", "depth-missing", "snapshot-no-gap-change"})

# The rules of the Windows section. "An observer need not check the rules of Windows" (spec,
# Receivers): fold applies signals that break them; check reports them.
WINDOW_RULES = frozenset({"window-bounds", "window-duplicate", "depth-zero", "depth-missing", "flow-zero"})


class _Absent:
    def __repr__(self) -> str:
        return "ABSENT"


class _Bad:
    def __repr__(self) -> str:
        return "BAD"


ABSENT: Any = _Absent()  # attribute not present
BAD: Any = _Bad()  # attribute present but invalid (already reported)


@dataclass
class Violation:
    line: int
    rule: str
    message: str


@dataclass
class Line:
    lineno: int
    soft: frozenset = SOFT_RULES  # rules that do not make the signal ignored
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, rule: str, message: str) -> None:
        assert rule in RULES, rule
        self.violations.append(Violation(self.lineno, rule, message))

    @property
    def hard(self) -> bool:
        return any(v.rule not in self.soft for v in self.violations)


# --------------------------------------------------------------------------------------------
# Reading lines


@dataclass
class Parsed:
    obj: dict | None
    problems: list[tuple[str, str]]
    duplicates: list[str]


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def parse_line(raw: bytes) -> Parsed:
    problems: list[tuple[str, str]] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append(("encoding", "the line starts with a UTF-8 byte order mark"))
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        problems.append(("encoding", f"not valid UTF-8 ({exc.reason} at byte {exc.start})"))
        text = raw.decode("utf-8", errors="replace")
    duplicates: list[str] = []

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict:
        obj: dict = {}
        for key, value in pairs:
            if key in obj:
                duplicates.append(key)
            obj[key] = value
        return obj

    try:
        obj = json.loads(text, object_pairs_hook=pairs_hook, parse_constant=_reject_constant)
    except ValueError as exc:
        problems.append(("json", f"not JSON: {exc}"))
        return Parsed(None, problems, duplicates)
    except RecursionError:
        problems.append(("json", "JSON nesting too deep to read"))
        return Parsed(None, problems, duplicates)
    if not isinstance(obj, dict):
        problems.append(("json", f"a line must be a JSON object, not {type(obj).__name__}"))
        return Parsed(None, problems, duplicates)
    if _has_lone_surrogate(obj):
        problems.append(("encoding", "a string escapes a lone surrogate (\\uD800-\\uDFFF), "
                                     "which is not a Unicode scalar value and has no UTF-8 encoding"))
    return Parsed(obj, problems, duplicates)


def _has_lone_surrogate(value: Any) -> bool:
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            if any("\ud800" <= ch <= "\udfff" for ch in v):
                return True
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return False


def read_lines(path: str | Path) -> list[tuple[int, Parsed]]:
    """Non-empty lines of a recording, with 1-based line numbers counting every line."""
    data = Path(path).read_bytes()
    out = []
    for index, raw in enumerate(data.split(b"\n"), start=1):
        if raw == b"":
            continue
        out.append((index, parse_line(raw)))
    return out


def _is_int(v: Any) -> bool:
    return type(v) is int


def _is_number(v: Any) -> bool:
    """A JSON number usable as a time: finite and representable as an IEEE 754 double."""
    if type(v) is int:
        return abs(v) <= sys.float_info.max
    return type(v) is float and math.isfinite(v)


def infer_windows(lines: list[tuple[int, Parsed]]) -> dict[tuple[str, int], float]:
    """Window length per session, keyed by (source, line of the session's seq-0 snapshot).

    The window option is not transmitted. Every flow and latency signal spans exactly one window,
    so `t - t0` is the window length; their median gives a first estimate `d`. Every `t0` and `t`
    is a window boundary, so `(b - b0) / k`, with `k` the number of windows between boundary `b`
    and the first boundary `b0`, estimates the length with an error shrinking as `k` grows. The
    median of those estimates that lie within 0.1 % of `d` is taken. A session without flow or
    latency signals gets no entry: its window length is unknown.
    """
    current: dict[str, int] = {}
    widths: dict[tuple[str, int], list[float]] = {}
    bounds: dict[tuple[str, int], list[float]] = {}
    for lineno, parsed in lines:
        obj = parsed.obj
        if obj is None:
            continue
        src, typ = obj.get("source"), obj.get("type")
        if not isinstance(src, str):
            continue
        if typ == "snapshot" and _is_int(obj.get("seq")) and obj.get("seq") == 0:
            current[src] = lineno
        elif typ in ("flow", "latency") and src in current:
            t, t0 = obj.get("t"), obj.get("t0")
            if _is_number(t) and _is_number(t0) and t > t0:
                key = (src, current[src])
                widths.setdefault(key, []).append(t - t0)
                bounds.setdefault(key, []).extend((t0, t))
    out = {}
    for key, ws in widths.items():
        d = statistics.median(ws)
        b0 = bounds[key][0]
        estimates = []
        for b in bounds[key]:
            k = round((b - b0) / d)
            if k != 0:
                e = (b - b0) / k
                if abs(e - d) <= 1e-3 * d:
                    estimates.append(e)
        out[key] = statistics.median(estimates) if estimates else d
    return out


# --------------------------------------------------------------------------------------------
# State


@dataclass
class OpenSpan:
    out: dict  # span, id, label, parent (only if it named an open span), t0
    raw: tuple  # (id, label, parent as sent, t0), to recognise the same span in a later snapshot


@dataclass
class Session:
    start_line: int
    T: float  # t of the seq-0 snapshot
    w: float | None  # window length; None when it is unknown
    last_seq: int = 0
    last_t: float = 0.0  # t of the last received signal (check judges the source by it)
    applied_t: float = 0.0  # t of the last applied signal (the observer judges t order by it)
    window_note: bool = False  # whether the unknown window length was reported
    elements: dict[str, dict] = field(default_factory=dict)
    links: dict[str, dict] = field(default_factory=dict)
    spans: dict[str, OpenSpan] = field(default_factory=dict)
    used_spans: set[str] = field(default_factory=set)
    backlog: dict[str, dict] = field(default_factory=dict)  # since the last snapshot
    depth_last: dict[str, tuple[float, int]] = field(default_factory=dict)  # id -> (t, value)
    depth_due: dict[str, float] = field(default_factory=dict)  # id -> window end of the next depth
    window_seen: set[tuple[str, str, int]] = field(default_factory=set)
    phase: float | None = None  # a window boundary, when T is not known (relayed sessions)
    snap_t: float = 0.0  # t of the latest snapshot of the session
    snap_is_start: bool = True  # whether that snapshot is the session's seq-0 snapshot
    applied: bool = False  # whether the observer has applied a snapshot of this session


@dataclass
class SourceState:
    label: str | None = None
    session: Session | None = None
    lost: int = 0
    ended: list[dict] = field(default_factory=list)
    appeared: bool = False


# --------------------------------------------------------------------------------------------
# The engine


class Engine:
    """Processes a recording line by line.

    mode "fold": the observer. A signal with a violation is ignored as a whole, except for the soft
    rules and the rules of Windows, which an observer need not check.

    mode "check": every violation is reported, and a signal with any violation other than a soft
    one is not applied to the state.

    In both modes a signal whose source and seq are valid counts as received for seq order and
    Loss, even when it is ignored (spec, Receivers), and so does a signal of a type not defined by
    the spec, which takes a seq like any other. A signal other than a snapshot is ignored, without
    a violation, until a snapshot of its session has been applied.
    """

    def __init__(
        self,
        mode: str,
        windows: dict[tuple[str, int], float] | None = None,
        window: float | None = None,
        direct: bool = False,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> None:
        assert mode in ("check", "fold")
        self.mode = mode
        self.windows = windows or {}
        self.window = window
        self.direct = direct
        self.tol = tolerance
        self.sources: dict[str, SourceState] = {}
        self.soft = SOFT_RULES | WINDOW_RULES if mode == "fold" else SOFT_RULES
        self.violations: list[Violation] = []
        self.warnings: list[tuple[int, str]] = []
        self.notes: list[tuple[int, str]] = []
        self.stopped = False
        self._source: str | None = None
        self._gap = 0  # seq gap before the signal being applied  # source of the signal being read, for ref-global-self

    # ---- attribute access -------------------------------------------------------------------

    def attr(self, L: Line, obj: dict, key: str, kind: Any, required: bool, where: str) -> Any:
        if key not in obj:
            if required:
                L.add("attr-missing", f'{where}: required attribute "{key}" is missing')
            return ABSENT
        v = obj[key]
        name = f'{where}: "{key}"'
        if v is None:
            L.add("null-attr", f"{name} is null; no attribute has the value null "
                               "(an optional attribute without a value is omitted)")
            return BAD
        if isinstance(kind, tuple):  # enumeration of strings
            if not isinstance(v, str):
                L.add("attr-type", f"{name} must be a string")
                return BAD
            if v not in kind:
                L.add("attr-value", f"{name} is {v!r}, not one of {', '.join(kind)}")
                return BAD
            return v
        if kind in ("str", "label", "id", "lref", "ref"):
            if not isinstance(v, str):
                L.add("attr-type", f"{name} must be a string")
                return BAD
            if kind == "label" and v == "":
                L.add("label-empty", f"{name} is empty")
                return BAD
            if kind in ("id", "lref", "ref") and v == "":
                L.add("id-invalid", f"{name} is empty")
                return BAD
            if kind == "id" and "/" in v:
                L.add("id-invalid", f"{name} {v!r} contains '/'")
                return BAD
            if kind == "lref" and "/" in v:
                L.add("ref-global", f"{name} {v!r} contains '/'; only link from and to may be global")
                return BAD
            if kind == "ref" and "/" in v:
                parts = v.split("/")
                if len(parts) != 2 or not parts[0] or not parts[1]:
                    L.add("ref-invalid", f"{name} {v!r} is not of the form <source>/<id>")
                    return BAD
                if self._source is not None and parts[0] == self._source:
                    L.add("ref-global-self",
                          f"{name} {v!r} is global but names the signal's own source")
                    return BAD
            return v
        if kind == "int":
            if type(v) is float and math.isfinite(v) and v.is_integer():
                L.add("int-format", f"{name} is written as {v!r}; integers have no fraction or exponent")
                return BAD
            if not _is_int(v):
                L.add("attr-type", f"{name} must be an integer")
                return BAD
            if v < 0 or v > MAX_INT:
                L.add("int-range", f"{name} is {v}, outside 0 .. 2^53 - 1")
                return BAD
            return v
        if kind == "time":
            if type(v) is int and not _is_number(v):
                L.add("attr-type", f"{name} is an integer too large to be a time (beyond the range of a double)")
                return BAD
            if not _is_number(v):
                L.add("attr-type", f"{name} must be a number")
                return BAD
            if v < 0:
                L.add("time-negative", f"{name} is {v}; times and durations are non-negative")
                return BAD
            return v
        if kind == "array":
            if not isinstance(v, list):
                L.add("attr-type", f"{name} must be an array")
                return BAD
            return v
        raise AssertionError(kind)

    def warn_unknown(self, L: Line, obj: dict, known: frozenset, where: str) -> None:
        for key in obj:
            if key not in known and not key.startswith("x-"):
                L.warnings.append(f'{where}: unknown attribute "{key}" ignored '
                                  "(attributes not defined by the spec should start with x-)")

    @staticmethod
    def ok(*values: Any) -> bool:
        return all(v is not ABSENT and v is not BAD for v in values)

    # ---- nested objects ---------------------------------------------------------------------

    def element(self, L: Line, e: Any, where: str) -> dict | None:
        if not isinstance(e, dict):
            L.add("attr-type", f"{where} must be an object")
            return None
        n = len(L.violations)
        out = {}
        for key, kind, req in (("id", "id", True), ("label", "label", True),
                               ("parent", "lref", False), ("kind", "str", False)):
            v = self.attr(L, e, key, kind, req, where)
            if v is not ABSENT and v is not BAD:
                out[key] = v
        self.warn_unknown(L, e, ELEMENT_KEYS, where)
        return out if len(L.violations) == n else None

    def link(self, L: Line, lk: Any, where: str) -> dict | None:
        if not isinstance(lk, dict):
            L.add("attr-type", f"{where} must be an object")
            return None
        n = len(L.violations)
        out = {}
        for key, kind in (("id", "id"), ("from", "ref"), ("to", "ref"), ("kind", ("data", "call"))):
            v = self.attr(L, lk, key, kind, True, where)
            if v is not ABSENT and v is not BAD:
                out[key] = v
        self.warn_unknown(L, lk, LINK_KEYS, where)
        return out if len(L.violations) == n else None

    def open_span(self, L: Line, s: Any, where: str, t: Any) -> dict | None:
        if not isinstance(s, dict):
            L.add("attr-type", f"{where} must be an object")
            return None
        n = len(L.violations)
        out = {}
        for key, kind, req in (("span", "id", True), ("id", "lref", True), ("label", "label", True),
                               ("parent", "lref", False), ("t0", "time", True)):
            v = self.attr(L, s, key, kind, req, where)
            if v is not ABSENT and v is not BAD:
                out[key] = v
        self.warn_unknown(L, s, OPEN_SPAN_KEYS, where)
        if "t0" in out and self.ok(t) and out["t0"] > t:
            L.add("t0-after-t", f"{where}: t0 {out['t0']} is after the snapshot's t {t}")
        return out if len(L.violations) == n else None

    def array_of(self, L: Line, obj: dict, key: str, required: bool, fn, where: str, *extra) -> Any:
        arr = self.attr(L, obj, key, "array", required, where)
        if arr is ABSENT or arr is BAD:
            return arr
        items = [fn(L, item, f"{key}[{i}]", *extra) for i, item in enumerate(arr)]
        return BAD if any(item is None for item in items) else items

    # ---- shapes per signal type -------------------------------------------------------------

    def shape(self, L: Line, obj: dict, typ: str, t: Any) -> dict:
        sig: dict = {}
        a = lambda key, kind, req=True: self.attr(L, obj, key, kind, req, typ)  # noqa: E731
        if typ == "snapshot":
            a("protocol", "str")  # missing or null; any other value than reacting/1 stopped the reader
            sig["label"] = a("label", "label", False)
            sig["elements"] = self.array_of(L, obj, "elements", True, self.element, typ)
            sig["links"] = self.array_of(L, obj, "links", True, self.link, typ)
            sig["spans"] = self.array_of(L, obj, "spans", False, self.open_span, typ, t)
            if isinstance(obj.get("spans"), list) and not obj["spans"]:
                L.add("spans-empty", "snapshot: spans is present but empty; it is omitted when no span is open")
        elif typ == "add":
            sig["elements"] = self.array_of(L, obj, "elements", False, self.element, typ)
            sig["links"] = self.array_of(L, obj, "links", False, self.link, typ)
            if not obj.get("elements") and not obj.get("links") \
                    and sig["elements"] is not BAD and sig["links"] is not BAD:
                L.add("add-empty", "add: at least one of elements and links must be present and non-empty")
        elif typ == "remove":
            ids = a("ids", "array")
            if isinstance(ids, list):
                if not ids:
                    L.add("remove-empty", "remove: ids must be non-empty")
                    ids = BAD
                else:
                    vals = [self.attr(L, {"ids": i}, "ids", "lref", True, f"remove: ids[{n}]")
                            for n, i in enumerate(ids)]
                    ids = vals if self.ok(*vals) else BAD
            sig["ids"] = ids
        elif typ == "flow":
            for key, kind, req in (("link", "lref", True), ("t0", "time", True), ("items", "int", True),
                                   ("bytes", "int", False), ("dropped", "int", True),
                                   ("coalesced", "int", True)):
                sig[key] = a(key, kind, req)
            if self.ok(sig["items"], sig["dropped"], sig["coalesced"]):
                if sig["items"] + sig["dropped"] + sig["coalesced"] == 0:
                    L.add("flow-zero", "flow: no item was counted in the window (items, dropped and coalesced are 0)")
                elif self.ok(sig["bytes"]) and sig["bytes"] > 0 and sig["items"] == 0:
                    L.add("flow-bytes", f"flow: bytes is {sig['bytes']} but items is 0")
        elif typ == "depth":
            for key, kind, req in (("id", "lref", True), ("value", "int", True), ("capacity", "int", False)):
                sig[key] = a(key, kind, req)
            if self.ok(sig["value"], sig["capacity"]) and sig["value"] > sig["capacity"]:
                L.add("depth-capacity", f"depth: value {sig['value']} exceeds capacity {sig['capacity']}")
        elif typ == "latency":
            for key, kind in (("id", "lref"), ("t0", "time"), ("count", "int"), ("sum", "time"), ("max", "time")):
                sig[key] = a(key, kind)
            if self.ok(sig["count"]) and sig["count"] < 1:
                L.add("latency-count", "latency: count is 0; a latency signal is emitted only when a duration ended")
            elif self.ok(sig["count"], sig["sum"], sig["max"]):
                c, s, m = sig["count"], sig["sum"], sig["max"]
                slack = 1e-9 * max(1.0, s, c * m)
                if m > s + slack or s > c * m + slack:
                    L.add("latency-stats", f"latency: sum {s} and max {m} are impossible for count {c}")
        elif typ == "span.start":
            for key, kind, req in (("span", "id", True), ("id", "lref", True), ("label", "label", True),
                                   ("parent", "lref", False)):
                sig[key] = a(key, kind, req)
        elif typ == "span.end":
            sig["span"] = a("span", "lref")
            sig["status"] = a("status", ("ok", "error"))
            sig["error"] = a("error", "str", False)
            if sig["status"] == "ok" and "error" in obj:
                L.add("span-error-attr", "span.end: error is present only when status is error")
        if typ in ("flow", "latency") and self.ok(sig["t0"], t) and sig["t0"] > t:
            L.add("t0-after-t", f"{typ}: t0 {sig['t0']} is after t {t}")
        self.warn_unknown(L, obj, GENERIC_KEYS | TYPE_KEYS[typ], typ)
        return sig

    # ---- main entry -------------------------------------------------------------------------

    def feed(self, lineno: int, parsed: Parsed) -> None:
        if self.stopped:
            return
        L = Line(lineno, self.soft)
        for rule, message in parsed.problems:
            L.add(rule, message)
        obj = parsed.obj
        if obj is not None:
            self.signal(L, obj, parsed.duplicates)
        self.violations.extend(L.violations)
        self.warnings.extend((lineno, w) for w in L.warnings)

    def signal(self, L: Line, obj: dict, duplicates: list[str]) -> None:
        typ = self.attr(L, obj, "type", "str", True, "signal")
        known = isinstance(typ, str) and typ in SIGNAL_TYPES
        if isinstance(typ, str) and not known:
            if typ.startswith("x-") and "source" not in obj:
                return  # a recording extension line: belongs to no session, skipped
            if not typ.startswith("x-"):
                L.warnings.append(f"signal type {typ!r} not recognised; ignored after its generic attributes "
                                  "(signal types not defined by the spec should start with x-)")
        for key in duplicates:
            L.add("duplicate-key", f'duplicate key "{key}"')
        if typ == "snapshot" and "protocol" in obj and obj["protocol"] is not None and obj["protocol"] != PROTOCOL:
            L.add("protocol", f"snapshot protocol is {obj['protocol']!r}, not {PROTOCOL!r}; "
                              "a reader of a recording stops reading at this line")
            self.stopped = True
            return

        where = typ if isinstance(typ, str) else "signal"
        self._source = None
        src = self.attr(L, obj, "source", "id", True, where)
        if self.ok(src):
            self._source = src
        seq = self.attr(L, obj, "seq", "int", True, where)
        t = self.attr(L, obj, "t", "time", True, where)
        sig = self.shape(L, obj, typ, t) if known else None
        if not self.ok(src, seq):
            return
        t_ok = self.ok(t)

        # ---- session, seq and t order: over every received signal (valid source and seq) ----
        st = self.sources.setdefault(src, SourceState())
        sess = st.session
        new_session = typ == "snapshot" and seq == 0
        gap = 0
        sequence_ok = True
        if not new_session:
            if sess is None:
                L.add("snapshot-first", f"the first signal of source {src!r} must be a snapshot with seq 0 "
                                        f"(got {where} with seq {seq})")
                return  # belongs to no session: not received in Loss
            if seq <= sess.last_seq:
                L.add("seq-order", f"seq {seq} does not increase over {sess.last_seq}")
                sequence_ok = False
            else:
                gap = seq - sess.last_seq - 1
                if gap > 0 and typ != "snapshot":
                    L.add("loss-snapshot", f"seq jumps from {sess.last_seq} to {seq} ({gap} lost) "
                                           f"but the next signal is of type {where}, not a snapshot")
                    sequence_ok = False
            ref_t, what = ((sess.last_t, "previous signal") if self.mode == "check"
                           else (sess.applied_t, "last applied signal"))
            if t_ok and t < ref_t:
                L.add("t-order", f"t {t} is before the {what}'s t {ref_t} in this session")
                sequence_ok = False

        if self.mode == "check" and sess is not None and not new_session and sequence_ok and t_ok:
            self.check_depth_due(L, sess, t, gap)

        # ---- references, windows, application ----
        # "An observer ignores a signal other than a snapshot unless it has applied a snapshot of the
        # same session" (Receivers). Such a signal breaks no rule; it is neither checked nor applied.
        session_open = new_session or typ == "snapshot" or sess.applied
        accepted = False
        if known and t_ok and session_open and not L.hard:
            target = self.new_session(src, L.lineno, t) if new_session else sess
            assert target is not None
            handler = getattr(self, "sem_" + typ.replace(".", "_"))
            self._gap = gap
            accepted = handler(L, st, target, sig, t, new_session)
            if accepted and new_session:
                st.session = target
        if accepted:
            st.appeared = True
            st.session.applied_t = t

        # ---- the signal counts as received, accepted or not ----
        if new_session:
            if not accepted:
                self.restart_session(st, src, L.lineno, t if t_ok else (sess.last_t if sess else 0.0))
        else:
            st.lost += gap
            sess.last_seq = max(sess.last_seq, seq)
            if t_ok:
                sess.last_t = max(sess.last_t, t)

    def restart_session(self, st: SourceState, src: str, lineno: int, t: float) -> None:
        """A seq-0 snapshot that was ignored: a new session's numbering begins, its content is unknown."""
        fresh = self.new_session(src, lineno, t)
        old = st.session
        if old is not None:  # the observer keeps the state it holds
            fresh.elements, fresh.links, fresh.spans, fresh.backlog = old.elements, old.links, old.spans, old.backlog
        fresh.applied_t = 0.0  # nothing of the new session has been applied
        st.session = fresh

    def new_session(self, src: str, lineno: int, t: float) -> Session:
        w = self.window if self.window is not None else self.windows.get((src, lineno))
        return Session(start_line=lineno, T=t, w=w, last_seq=0, last_t=t, applied_t=t, snap_t=t)

    # ---- windows ----------------------------------------------------------------------------

    def check_depth_due(self, L: Line, sess: Session, t: float, gap: int) -> None:
        if gap > 0:  # the depth signal may have been among the lost ones
            sess.depth_due.clear()
            return
        for ident, due in sorted(sess.depth_due.items()):
            if t > due + self.wtol(sess):
                L.add("depth-missing", f"{ident!r} had a non-zero backlog at window end {due - sess.w:.6f} "
                                       f"but no depth signal at the next window end {due:.6f}")
                del sess.depth_due[ident]

    def wtol(self, sess: Session) -> float:
        return max(self.tol, RELATIVE_TOLERANCE * sess.w) if sess.w is not None else self.tol

    def check_window(self, L: Line, sess: Session, typ: str, ident: str, t0: float | None, t: float) -> tuple | None:
        """Checks window alignment and duplicates; returns the window key of the signal.

        Returns None, checking nothing, when the session's window length is unknown.
        """
        if sess.w is None:
            if not sess.window_note:
                sess.window_note = True
                self.notes.append((L.lineno, f"the window length of source {self._source!r} in the session "
                                             f"starting at line {sess.start_line} cannot be inferred (no flow or "
                                             "latency signal); window rules are not checked (use --window)"))
            return None
        w, tol = sess.w, self.wtol(sess)
        if t0 is not None and abs((t - t0) - w) > tol:
            L.add("window-bounds", f"{typ}: t - t0 is {t - t0:.9f}, not the window length {w}")
        boundary = t0 if t0 is not None else t
        if self.direct:
            phase = sess.T
        else:
            phase = sess.phase if sess.phase is not None else boundary
        k = round((boundary - phase) / w)
        if abs(boundary - phase - k * w) > tol:
            what = "t0" if t0 is not None else "t"
            L.add("window-bounds", f"{typ}: {what} {boundary} is not on a window boundary "
                                   f"(T + k*w, w = {w}, nearest {phase + k * w:.6f})")
        elif self.direct and (k < 0 if t0 is not None else k < 1):
            L.add("window-bounds", f"{typ}: window ends at {t}, before the end of the session's first window")
        k_end = k + 1 if t0 is not None else k
        key = (typ, ident, k_end)
        if key in sess.window_seen:
            L.add("window-duplicate", f"{typ}: a second {typ} signal for {ident!r} in the window ending at {t}")
        return key

    def accept_window(self, sess: Session, key: tuple | None, boundary: float) -> None:
        if key is None:
            return
        sess.window_seen.add(key)
        if sess.phase is None:
            sess.phase = boundary

    # ---- semantics per type: validate, then apply when there is no violation -----------------

    def sem_snapshot(self, L: Line, st: SourceState, sess: Session, sig: dict, t: float, new_session: bool) -> bool:
        elements, links = sig["elements"], sig["links"]
        spans = sig["spans"] if sig["spans"] is not ABSENT else []
        pos: dict[str, int] = {}
        for i, e in enumerate(elements):
            if e["id"] in pos:
                L.add("id-duplicate", f"snapshot: element {e['id']!r} is listed twice")
            else:
                pos[e["id"]] = i
        link_ids: set[str] = set()
        for lk in links:
            if lk["id"] in pos or lk["id"] in link_ids:
                L.add("id-duplicate", f"snapshot: ID {lk['id']!r} of a link is already used by an element or link")
            link_ids.add(lk["id"])
        for i, e in enumerate(elements):
            p = e.get("parent")
            if p is None:
                continue
            if p in pos and pos[p] < i:
                continue
            if p in pos:
                L.add("parent-order", f"snapshot: element {e['id']!r} is listed before its parent {p!r}")
            elif p in link_ids:
                L.add("ref-kind", f"snapshot: parent {p!r} of element {e['id']!r} is a link")
            else:
                L.add("ref-missing", f"snapshot: parent {p!r} of element {e['id']!r} is not listed")
        for lk in links:
            for end in ("from", "to"):
                r = lk[end]
                if "/" in r or r in pos:
                    continue
                if r in link_ids:
                    L.add("ref-kind", f"snapshot: {end} {r!r} of link {lk['id']!r} is a link, not an element")
                else:
                    L.add("ref-missing", f"snapshot: {end} {r!r} of link {lk['id']!r} is not a listed element")
        span_pos: dict[str, int] = {}
        for i, s in enumerate(spans):
            if s["span"] in span_pos:
                L.add("span-duplicate", f"snapshot: span {s['span']!r} is listed twice")
            else:
                span_pos[s["span"]] = i
        opened: dict[str, OpenSpan] = {}
        for i, s in enumerate(spans):
            if s["id"] not in pos:
                if s["id"] in link_ids:
                    L.add("ref-kind", f"snapshot: span {s['span']!r} names link {s['id']!r}, not an element")
                else:
                    L.add("ref-missing", f"snapshot: span {s['span']!r} names element {s['id']!r}, which is not listed")
            out = {k: s[k] for k in ("span", "id", "label", "t0")}
            p = s.get("parent")
            if p is not None:
                if p in span_pos and span_pos[p] < i:
                    out["parent"] = p
                elif p in span_pos:
                    L.add("parent-order", f"snapshot: span {s['span']!r} is listed before its parent span {p!r}")
                # a parent that is not listed is not open: treated as omitted
            raw = (s["id"], s["label"], p, s["t0"])
            cur = sess.spans.get(s["span"]) if not new_session else None
            same = cur is not None and self.same_span(cur, raw)
            if not new_session and s["span"] in sess.used_spans and not same:
                L.add("span-reuse", f"snapshot: span ID {s['span']!r} was already used for another span in this session")
            if same and p is None:
                # the same span, listed without the parent the source sent (a relay's snapshot omits a
                # parent that was not open): it keeps the parent its source first sent
                raw = raw[:2] + (cur.raw[2],) + raw[3:]
            opened.setdefault(s["span"], OpenSpan(out, raw))
        if L.hard:
            return False
        if not new_session and self._gap == 0 and sess.applied and (elements or links or spans):
            self.compare_snapshot(L, sess, elements, links, opened)
        sess.elements = {e["id"]: e for e in elements}
        sess.links = {lk["id"]: lk for lk in links}
        sess.spans = opened
        if new_session:
            sess.used_spans = set()
            sess.window_seen = set()
            sess.phase = None
        sess.used_spans |= set(opened)
        sess.backlog = {}
        sess.depth_last = {}
        sess.depth_due = {}
        sess.snap_t = t
        sess.snap_is_start = new_session
        sess.applied = True
        st.label = sig["label"] if sig["label"] is not ABSENT else None
        return True

    def same_span(self, cur: OpenSpan, raw: tuple) -> bool:
        """Is the span a snapshot lists as `raw` (id, label, parent, t0) the open span `cur`?

        Coordinator ruling: the same `id`, `label` and `t0`, and a `parent` equal to the one the source
        sent or to the one the observer kept (omitted when the sent parent was not open).
        """
        ident, label, parent, t0 = raw
        return (cur.raw[0] == ident and cur.raw[1] == label and abs(cur.raw[3] - t0) <= self.tol
                and (parent == cur.raw[2] or parent == cur.out.get("parent")))

    def compare_snapshot(self, L: Line, sess: Session, elements: list, links: list, opened: dict) -> None:
        """A snapshot that follows the previous signal without a gap must restate the held state."""
        def first_difference(held: dict, sent: dict, what: str) -> str | None:
            for ident in sorted(set(held) | set(sent)):
                if ident not in sent:
                    return f"{what} {ident!r} exists but is not listed"
                if ident not in held:
                    return f"{what} {ident!r} is listed but does not exist"
                if held[ident] != sent[ident]:
                    return f"{what} {ident!r} is listed with other attributes"
            return None

        def span_difference() -> str | None:
            for span in sorted(set(sess.spans) | set(opened)):
                if span not in opened:
                    return f"open span {span!r} is not listed"
                if span not in sess.spans:
                    return f"open span {span!r} is listed but is not open"
                if not self.same_span(sess.spans[span], opened[span].raw):
                    return f"open span {span!r} is listed with other attributes"
            return None

        diff = (first_difference(sess.elements, {e["id"]: e for e in elements}, "element")
                or first_difference(sess.links, {lk["id"]: lk for lk in links}, "link")
                or span_difference())
        if diff:
            L.add("snapshot-no-gap-change", f"snapshot: no signal was lost since seq {sess.last_seq}, "
                                            f"yet it differs from the state held: {diff}")

    def sem_add(self, L: Line, st: SourceState, sess: Session, sig: dict, t: float, new_session: bool) -> bool:
        elements = sig["elements"] if sig["elements"] is not ABSENT else []
        links = sig["links"] if sig["links"] is not ABSENT else []
        pos: dict[str, int] = {}
        new_links: set[str] = set()
        for i, e in enumerate(elements):
            if e["id"] in sess.elements or e["id"] in sess.links or e["id"] in pos:
                L.add("id-duplicate", f"add: element ID {e['id']!r} is already in use")
            pos.setdefault(e["id"], i)
        for lk in links:
            if lk["id"] in sess.elements or lk["id"] in sess.links or lk["id"] in pos or lk["id"] in new_links:
                L.add("id-duplicate", f"add: link ID {lk['id']!r} is already in use")
            new_links.add(lk["id"])
        for i, e in enumerate(elements):
            p = e.get("parent")
            if p is None or p in sess.elements or (p in pos and pos[p] < i):
                continue
            if p in pos:
                L.add("parent-order", f"add: element {e['id']!r} is listed before its parent {p!r}")
            elif p in sess.links or p in new_links:
                L.add("ref-kind", f"add: parent {p!r} of element {e['id']!r} is a link")
            else:
                L.add("ref-missing", f"add: parent {p!r} of element {e['id']!r} does not exist")
        for lk in links:
            for end in ("from", "to"):
                r = lk[end]
                if "/" in r or r in sess.elements or r in pos:
                    continue
                if r in sess.links or r in new_links:
                    L.add("ref-kind", f"add: {end} {r!r} of link {lk['id']!r} is a link, not an element")
                else:
                    L.add("ref-missing", f"add: {end} {r!r} of link {lk['id']!r} does not exist")
        if L.hard:
            return False
        for e in elements:
            sess.elements[e["id"]] = e
        for lk in links:
            sess.links[lk["id"]] = lk
        return True

    def sem_remove(self, L: Line, st: SourceState, sess: Session, sig: dict, t: float, new_session: bool) -> bool:
        for ident in sig["ids"]:
            if ident in sess.elements:
                gone = {ident}
                grew = True
                while grew:
                    grew = False
                    for eid, e in sess.elements.items():
                        if eid not in gone and e.get("parent") in gone:
                            gone.add(eid)
                            grew = True
                for eid in gone:
                    del sess.elements[eid]
                dead = [lid for lid, lk in sess.links.items() if lk["from"] in gone or lk["to"] in gone]
                for lid in dead:
                    del sess.links[lid]
                for span_id in sorted(s for s, sp in sess.spans.items() if sp.out["id"] in gone):
                    rec = dict(sess.spans.pop(span_id).out)
                    rec.update(t1=t, status="error")
                    st.ended.append(rec)
                self.forget(sess, gone | set(dead))
            elif ident in sess.links:
                del sess.links[ident]
                self.forget(sess, {ident})
            # otherwise skipped
        return True

    @staticmethod
    def forget(sess: Session, ids: set[str]) -> None:
        for ident in ids:
            sess.backlog.pop(ident, None)
            sess.depth_last.pop(ident, None)
            sess.depth_due.pop(ident, None)
        sess.window_seen = {k for k in sess.window_seen if k[1] not in ids}

    def ref_measured(self, L: Line, sess: Session, typ: str, key: str, ident: str, links_only: bool) -> None:
        if ident in sess.links or (not links_only and ident in sess.elements):
            return
        if links_only and ident in sess.elements:
            L.add("ref-kind", f"{typ}: {key} {ident!r} is an element, not a link")
        else:
            what = "link" if links_only else "element or link"
            L.add("ref-missing", f"{typ}: {key} {ident!r} names no existing {what}")

    def sem_flow(self, L: Line, st: SourceState, sess: Session, sig: dict, t: float, new_session: bool) -> bool:
        self.ref_measured(L, sess, "flow", "link", sig["link"], True)
        if L.hard:
            return False
        key = self.check_window(L, sess, "flow", sig["link"], sig["t0"], t)
        if L.hard:
            return False
        self.accept_window(sess, key, sig["t0"])
        return True

    def sem_latency(self, L: Line, st: SourceState, sess: Session, sig: dict, t: float, new_session: bool) -> bool:
        self.ref_measured(L, sess, "latency", "id", sig["id"], False)
        if L.hard:
            return False
        key = self.check_window(L, sess, "latency", sig["id"], sig["t0"], t)
        if L.hard:
            return False
        self.accept_window(sess, key, sig["t0"])
        return True

    def sem_depth(self, L: Line, st: SourceState, sess: Session, sig: dict, t: float, new_session: bool) -> bool:
        ident, value = sig["id"], sig["value"]
        self.ref_measured(L, sess, "depth", "id", ident, False)
        if L.hard:
            return False
        key = self.check_window(L, sess, "depth", ident, None, t)
        if L.hard:
            return False
        if value == 0:
            prev = sess.depth_last.get(ident)
            if prev is not None and prev[1] == 0:
                L.add("depth-zero", f"depth: {ident!r} reports 0 again; its previous depth was already 0")
            elif prev is None and sess.w is not None and (
                    (self.direct and sess.snap_is_start) or t - sess.w > sess.snap_t + self.wtol(sess)):
                prev_end = t - sess.w
                L.add("depth-zero", f"depth: {ident!r} reports 0 but had no non-zero depth at the previous "
                                    f"window end {prev_end:.6f}")
            if L.hard:
                return False
        self.accept_window(sess, key, t)
        sess.depth_last[ident] = (t, value)
        entry = {"value": value}
        if sig["capacity"] is not ABSENT:
            entry["capacity"] = sig["capacity"]
        sess.backlog[ident] = entry
        if value > 0 and sess.w is not None:
            sess.depth_due[ident] = t + sess.w
        else:
            sess.depth_due.pop(ident, None)
        return True

    def sem_span_start(self, L: Line, st: SourceState, sess: Session, sig: dict, t: float, new_session: bool) -> bool:
        span, ident, parent = sig["span"], sig["id"], sig["parent"]
        if span in sess.used_spans:
            L.add("span-reuse", f"span.start: span ID {span!r} was already used in this session")
        if ident not in sess.elements:
            if ident in sess.links:
                L.add("ref-kind", f"span.start: id {ident!r} is a link, not an element")
            else:
                L.add("ref-missing", f"span.start: element {ident!r} does not exist")
        if L.hard:
            return False
        out = {"span": span, "id": ident, "label": sig["label"], "t0": t}
        if parent is not ABSENT:
            if parent in sess.spans:
                out["parent"] = parent
            else:
                L.add("span-parent", f"span.start: parent {parent!r} is not an open span (treated as omitted)")
        raw_parent = parent if parent is not ABSENT else None
        sess.spans[span] = OpenSpan(out, (ident, sig["label"], raw_parent, t))
        sess.used_spans.add(span)
        return True

    def sem_span_end(self, L: Line, st: SourceState, sess: Session, sig: dict, t: float, new_session: bool) -> bool:
        span = sig["span"]
        if span not in sess.spans:
            L.add("span-not-open", f"span.end: span {span!r} is not open")
            return False
        rec = dict(sess.spans.pop(span).out)
        rec.update(t1=t, status=sig["status"])
        if sig["error"] is not ABSENT:
            rec["error"] = sig["error"]
        st.ended.append(rec)
        return True

    # ---- results ----------------------------------------------------------------------------

    def state(self) -> dict:
        sources = {}
        for name, st in self.sources.items():
            if not st.appeared or st.session is None:
                continue
            s = st.session
            entry: dict = {}
            if st.label is not None:
                entry["label"] = st.label
            entry["elements"] = [s.elements[k] for k in sorted(s.elements)]
            entry["links"] = [s.links[k] for k in sorted(s.links)]
            entry["spans"] = [s.spans[k].out for k in sorted(s.spans)]
            entry["backlog"] = {k: v for k, v in s.backlog.items()
                                if v["value"] != 0 and (k in s.elements or k in s.links)}
            entry["lost"] = st.lost
            entry["ended"] = st.ended
            sources[name] = entry
        return {"sources": sources}


# --------------------------------------------------------------------------------------------
# API


def run(path: str | Path, mode: str, window: float | None = None, direct: bool = False,
        tolerance: float = DEFAULT_TOLERANCE) -> Engine:
    lines = read_lines(path)
    engine = Engine(mode, infer_windows(lines), window, direct, tolerance)
    for lineno, parsed in lines:
        engine.feed(lineno, parsed)
    engine.violations.sort(key=lambda v: v.line)
    return engine


def check(path: str | Path, **options: Any) -> list[Violation]:
    return run(path, "check", **options).violations


def fold(path: str | Path, **options: Any) -> dict:
    return run(path, "fold", **options).state()


def schema_disagreements(path: str | Path, violations: list[Violation]) -> list[str] | None:
    """Lines where the JSON Schema and this checker disagree on the shape of one signal.

    Returns None when the jsonschema module is not importable.
    """
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError:
        return None
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    by_line: dict[int, set[str]] = {}
    for v in violations:
        by_line.setdefault(v.line, set()).add(v.rule)
    out = []
    for lineno, parsed in read_lines(path):
        obj = parsed.obj
        if obj is None or parsed.duplicates:
            continue
        typ = obj.get("type")
        if not isinstance(typ, str) or typ not in SIGNAL_TYPES:
            continue
        errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.path))
        spec_bad = sorted(by_line.get(lineno, set()) & SCHEMA_RULES)
        if errors and not spec_bad:
            out.append(f"line {lineno}: schema-disagreement: the schema rejects the signal "
                       f"({errors[0].message}) but the spec text accepts its shape")
        elif spec_bad and not errors:
            out.append(f"line {lineno}: schema-disagreement: the schema accepts the signal "
                       f"but the spec text rejects it ({', '.join(spec_bad)})")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check.py", description="Reference checker for reacting/1 recordings.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("check", "report every rule a recording breaks"),
                            ("fold", "print the state an observer holds at the end of the recording")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("recording", help="a recording: JSON lines, one signal per line")
        sp.add_argument("--window", type=float, default=None,
                        help="window length in seconds for every source (default: inferred per session "
                             "from flow and latency signals; without them, window rules are not checked)")
        sp.add_argument("--direct", action="store_true",
                        help="the recording was taken directly from its sources, not through a relay: "
                             "anchor windows at the t of each seq-0 snapshot")
        sp.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                        help=f"absolute tolerance in seconds for window boundaries (default {DEFAULT_TOLERANCE}); "
                             f"{RELATIVE_TOLERANCE:g} of the window length is also allowed")
        if name == "check":
            sp.add_argument("--schema", action="store_true",
                            help="also validate each signal against docs/spec/Reacting.schema.json "
                                 "(needs the jsonschema module) and report disagreements")
    args = parser.parse_args(argv)
    if args.window is not None and not args.window > 0:
        parser.error("--window must be positive")
    options = dict(window=args.window, direct=args.direct, tolerance=args.tolerance)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")
    try:
        engine = run(args.recording, args.command, **options)
    except OSError as exc:
        print(f"check.py: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # a bug in the checker, never a verdict on the recording
        print(f"check.py: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for lineno, warning in engine.warnings:
        print(f"line {lineno}: warning: {warning}", file=sys.stderr)
    for lineno, note in engine.notes:
        print(f"line {lineno}: note: {note}", file=sys.stderr)
    if args.command == "fold":
        json.dump(engine.state(), sys.stdout, sort_keys=True, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    for v in engine.violations:
        print(f"line {v.line}: {v.rule}: {v.message}")
    if args.schema:
        notes = schema_disagreements(args.recording, engine.violations)
        if notes is None:
            print("check.py: --schema: the jsonschema module is not importable; skipped", file=sys.stderr)
        else:
            for note in notes:
                print(note)
    return 1 if engine.violations else 0


if __name__ == "__main__":
    sys.exit(main())
