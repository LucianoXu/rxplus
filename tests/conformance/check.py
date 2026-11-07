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
import heapq
import json
import math
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

PROTOCOL = "reacting/1"
MAX_INT = 2**53 - 1
SIGNAL_TYPES = ("snapshot", "add", "remove", "flow", "depth", "latency", "span.start", "span.end")
DEFAULT_SPAN_IDS = 100_000  # span IDs remembered per session for span-reuse; 0 remembers all
MAX_WINDOW_BOUNDARIES = 100_000  # distinct window boundaries kept per session to infer its window
MAX_NESTING = 64  # "A signal nests arrays and objects at most 64 levels deep, the signal itself being level 1"
DEFAULT_TOLERANCE = 1e-6  # absolute floor, in seconds, for window comparisons
RELATIVE_TOLERANCE = 1e-4  # window comparisons also allow this fraction of the window length
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "spec" / "Reacting.schema.json"

GENERIC_KEYS = frozenset({"source", "seq", "t", "type"})
TYPE_KEYS = {
    "snapshot": frozenset({"protocol", "label", "elements", "links", "spans", "backlogs"}),
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
BACKLOG_KEYS = frozenset({"id", "value", "capacity"})

# Every rule identifier the checker can report, with a one-line description (README lists them).
RULES = {
    "encoding": "a line is not UTF-8 (including escapes of lone surrogates), or carries a byte order mark",
    "json": "a non-empty line is not one JSON object (NaN and Infinity are not JSON)",
    "nesting-depth": "a signal nests arrays and objects more than 64 levels deep (the line is not read)",
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
    "backlogs-empty": "a snapshot has backlogs present but empty (it must be omitted)",
    "backlog-zero": "a snapshot's backlogs entry has value 0 (only non-zero backlogs are listed)",
    "backlog-duplicate": "a snapshot lists the backlog of one element or link twice",
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
    "backlogs-empty", "backlog-zero",
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
    depth = _nesting(text)
    if depth > MAX_NESTING:
        # Not read at all, so its source and seq are not known and do not count as received, as for
        # a line that is not JSON (and as the TypeScript reader does).
        problems.append(("nesting-depth", f"the line nests arrays and objects {depth} levels deep; a signal "
                                          f"nests at most {MAX_NESTING} (Encoding); the line is not read"))
        return Parsed(None, problems, duplicates)

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict:
        obj = dict(pairs)
        if len(obj) != len(pairs):
            seen: set[str] = set()
            for key, _ in pairs:
                if key in seen:
                    duplicates.append(key)
                seen.add(key)
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
    if _SURROGATE_ESCAPE.search(text) and _has_lone_surrogate(obj):
        problems.append(("encoding", "a string escapes a lone surrogate (\\uD800-\\uDFFF), "
                                     "which is not a Unicode scalar value and has no UTF-8 encoding"))
    return Parsed(obj, problems, duplicates)


# A lone surrogate can only come from a \\u escape (bytes that encode one are not UTF-8).
_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F]")


_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_BRACKET = re.compile(r"[\[\]{}]")


def _nesting(text: str) -> int:
    """The deepest nesting of arrays and objects in `text`, outside JSON strings; iterative.

    A line with at most MAX_NESTING brackets cannot nest deeper and is not scanned.
    """
    if text.count("[") + text.count("{") <= MAX_NESTING:
        return 0
    depth = deepest = 0
    for bracket in _BRACKET.findall(_JSON_STRING.sub("", text)):
        if bracket in "[{":
            depth += 1
            if depth > deepest:
                deepest = depth
        else:
            depth -= 1
    return deepest


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


def read_lines(path: str | Path) -> Iterator[tuple[int, Parsed]]:
    """Non-empty lines of a recording, with 1-based line numbers counting every line (streamed)."""
    with open(path, "rb") as f:
        for index, raw in enumerate(f, start=1):
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            if raw != b"":
                yield index, parse_line(raw)


def _is_int(v: Any) -> bool:
    return type(v) is int


def _is_number(v: Any) -> bool:
    """A JSON number usable as a time: finite and representable as an IEEE 754 double."""
    if type(v) is int:
        return abs(v) <= sys.float_info.max
    return type(v) is float and math.isfinite(v)


def infer_windows(lines: Iterator[tuple[int, Parsed]]) -> dict[tuple[str, int], float]:
    """Window length per session, keyed by (source, line of the session's seq-0 snapshot).

    The window option is not transmitted. Every flow and latency signal spans exactly one window,
    so the median of their `t - t0` is a first estimate `d`. Near T = 1.76e9 one ULP of a time is
    2.4e-7 s, so `d` may be off by that much, which over k windows grows k-fold. Every `t0` and `t`
    of flow and latency, and every `t` of depth, is a window boundary. So the estimate is refined
    over the boundaries in order of their distance from the first one: each boundary's window count
    `k` comes from the estimate so far, and `(b - b0) / k` is a better estimate, ending in the
    least-squares slope of boundary against window index. That slope and its decimal roundings are
    scored by the boundaries they explain (see _refine_window). A session without flow or latency
    signals gets no entry: its window length is unknown.
    """
    current: dict[str, int] = {}
    # bounded per session: widths by value (they take few distinct values), the first boundary, and at
    # most MAX_WINDOW_BOUNDARIES distinct boundaries
    widths: dict[tuple[str, int], Counter] = {}
    first: dict[tuple[str, int], float] = {}
    bounds: dict[tuple[str, int], set[float]] = {}
    for lineno, parsed in lines:
        obj = parsed.obj
        if obj is None:
            continue
        src, typ = obj.get("source"), obj.get("type")
        if not isinstance(src, str):
            continue
        if typ == "snapshot" and _is_int(obj.get("seq")) and obj.get("seq") == 0:
            current[src] = lineno
        elif typ in ("flow", "latency", "depth") and src in current:
            key = (src, current[src])
            t, t0 = obj.get("t"), obj.get("t0")
            if typ == "depth":
                if _is_number(t) and key in bounds and len(bounds[key]) < MAX_WINDOW_BOUNDARIES:
                    bounds[key].add(t)
            elif _is_number(t) and _is_number(t0) and t > t0:
                widths.setdefault(key, Counter())[t - t0] += 1
                first.setdefault(key, t0)
                b = bounds.setdefault(key, set())
                if len(b) < MAX_WINDOW_BOUNDARIES:
                    b.add(t0)
                    b.add(t)
    return {key: _refine_window(_median(ws), first[key], bounds[key]) for key, ws in widths.items()}


def _median(counts: Counter) -> float:
    """statistics.median of the multiset `counts`."""
    n = sum(counts.values())
    lo, hi = (n - 1) // 2, n // 2
    seen, a = 0, None
    for value in sorted(counts):
        seen += counts[value]
        if a is None and seen > lo:
            a = value
        if seen > hi:
            return a if lo == hi else (a + value) / 2
    raise ValueError("no values")


def _refine_window(d: float, b0: float, bounds: set[float]) -> float:
    points = sorted({b - b0 for b in bounds if b != b0}, key=abs)
    if not points:
        return d
    w = d
    for dist in points:  # nearer boundaries first: each fixes k for the farther ones
        k = round(dist / w)
        if k != 0 and abs(dist - k * w) <= 0.25 * w:
            estimate = dist / k
            if abs(estimate - w) <= 1e-3 * w:
                w = estimate
    # least squares of boundary against window index over the consistent boundaries (b0 included):
    # the rounding of every boundary, b0's too, averages out
    ks, ds = [0], [0.0]
    for dist in points:
        k = round(dist / w)
        if abs(dist - k * w) <= 0.25 * w:
            ks.append(k)
            ds.append(dist)
    mk, md = statistics.fmean(ks), statistics.fmean(ds)
    var = sum((k - mk) ** 2 for k in ks)
    fit = sum((k - mk) * (d - md) for k, d in zip(ks, ds)) / var if var else w
    # Over few windows the fit is still off by about an ULP of the times divided by the span (5e-8 s
    # for 10 windows near 1.79e9), which a later boundary of the session can exceed. A source's
    # window is a configured number, usually a short decimal. So candidates are the fit and the
    # decimal roundings of it and of the median; the one that puts the most boundaries within 4 ULPs
    # of b0 + k*w wins, and on a tie, the shortest decimal (0.01 over 0.010000051770891463).
    tol = 4 * math.ulp(abs(b0)) if b0 else 4 * math.ulp(1.0)
    candidates = [fit]
    for base in (fit, d):
        for digits in range(1, 10):
            c = round(base, digits)
            if c > 0 and abs(c - fit) <= 1e-3 * fit and c not in candidates:
                candidates.append(c)

    def score(c: float) -> tuple[int, int]:
        hits = sum(1 for dist in points if abs(dist - round(dist / c) * c) <= tol)
        return hits, -len(repr(c))

    return max(candidates, key=score)


# --------------------------------------------------------------------------------------------
# State


class RecentIds:
    """The most recent `limit` IDs added, oldest evicted first (limit 0: all of them)."""

    __slots__ = ("limit", "ids")

    def __init__(self, limit: int = DEFAULT_SPAN_IDS) -> None:
        self.limit = limit
        self.ids: dict[str, None] = {}

    def __contains__(self, ident: str) -> bool:
        return ident in self.ids

    def add(self, ident: str) -> None:
        self.ids[ident] = None
        if self.limit and len(self.ids) > self.limit:
            del self.ids[next(iter(self.ids))]


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
    used_spans: RecentIds = field(default_factory=RecentIds)  # recent span IDs of the session
    backlog: dict[str, dict] = field(default_factory=dict)  # since the last snapshot
    depth_last: dict[str, tuple[float, int]] = field(default_factory=dict)  # id -> (t, value)
    depth_due: dict[str, float] = field(default_factory=dict)  # id -> window end of the next depth
    depth_heap: list[tuple[float, str]] = field(default_factory=list)  # depth_due by time (may hold stale entries)
    window_seen: dict[str, dict[str, int]] = field(default_factory=dict)  # id -> {type: latest window}
    # indices, kept by the methods below, so that a removal costs what it removes
    children: dict[str, set[str]] = field(default_factory=dict)  # element -> child elements
    links_at: dict[str, set[str]] = field(default_factory=dict)  # element -> links with it as local end
    spans_at: dict[str, set[str]] = field(default_factory=dict)  # element -> open spans on it
    added_t: dict[str, float] = field(default_factory=dict)  # element or link -> t of its add signal
    phase: float | None = None  # a window boundary, when T is not known (relayed sessions)
    snap_t: float = 0.0  # t of the latest snapshot of the session
    snap_is_start: bool = True  # whether that snapshot is the session's seq-0 snapshot
    applied: bool = False  # whether the observer has applied a snapshot of this session


    def put_element(self, e: dict) -> None:
        self.elements[e["id"]] = e
        if "parent" in e:
            self.children.setdefault(e["parent"], set()).add(e["id"])

    def put_link(self, lk: dict) -> None:
        self.links[lk["id"]] = lk
        for end in (lk["from"], lk["to"]):
            if "/" not in end:
                self.links_at.setdefault(end, set()).add(lk["id"])

    def put_span(self, span: str, o: OpenSpan) -> None:
        self.spans[span] = o
        self.spans_at.setdefault(o.out["id"], set()).add(span)

    def pop_span(self, span: str) -> OpenSpan:
        o = self.spans.pop(span)
        at = self.spans_at.get(o.out["id"])
        if at is not None:
            at.discard(span)
            if not at:
                del self.spans_at[o.out["id"]]
        return o

    def pop_link(self, lid: str) -> None:
        lk = self.links.pop(lid)
        for end in (lk["from"], lk["to"]):
            at = self.links_at.get(end)
            if at is not None:
                at.discard(lid)
                if not at:
                    del self.links_at[end]

    def pop_element(self, eid: str) -> None:
        e = self.elements.pop(eid)
        if "parent" in e:
            siblings = self.children.get(e["parent"])
            if siblings is not None:
                siblings.discard(eid)
                if not siblings:
                    del self.children[e["parent"]]
        self.children.pop(eid, None)

    def replace(self, elements: dict, links: dict, spans: dict) -> None:
        self.added_t = {}
        self.elements, self.links, self.spans = {}, {}, {}
        self.children, self.links_at, self.spans_at = {}, {}, {}
        for e in elements.values():
            self.put_element(e)
        for lk in links.values():
            self.put_link(lk)
        for span, o in spans.items():
            self.put_span(span, o)

    def set_depth_due(self, ident: str, due: float) -> None:
        self.depth_due[ident] = due
        heapq.heappush(self.depth_heap, (due, ident))

    def clear_depth_due(self) -> None:
        self.depth_due = {}
        self.depth_heap = []


@dataclass
class SourceState:
    label: str | None = None
    session: Session | None = None
    lost: int = 0
    ended: list[dict] = field(default_factory=list)
    appeared: bool = False
    orphan_reported: bool = False  # snapshot-first was reported for signals before any session


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
        span_ids: int = DEFAULT_SPAN_IDS,
        on_violation: Callable[[Violation], None] | None = None,
        on_message: Callable[[int, str, str], None] | None = None,
    ) -> None:
        """`on_violation` and `on_message(line, "warning" | "note", text)` receive each one as it is
        found; without them, they are collected in `violations`, `warnings` and `notes`."""
        assert mode in ("check", "fold")
        self.span_ids = span_ids
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
        self.violation_count = 0
        self.on_violation = on_violation or self.violations.append
        self.on_message = on_message or self._collect_message
        self.stopped = False
        self._source: str | None = None
        self._gap = 0  # seq gap before the signal being applied  # source of the signal being read, for ref-global-self

    def _collect_message(self, lineno: int, kind: str, text: str) -> None:
        (self.warnings if kind == "warning" else self.notes).append((lineno, text))

    def ended(self, st: SourceState, rec: dict) -> None:
        """A span ended. Only `fold` keeps the record: its output lists every ended span."""
        if self.mode == "fold":
            st.ended.append(rec)

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

    def backlog(self, L: Line, b: Any, where: str) -> dict | None:
        if not isinstance(b, dict):
            L.add("attr-type", f"{where} must be an object")
            return None
        n = len(L.violations)
        out = {}
        for key, kind, req in (("id", "lref", True), ("value", "int", True), ("capacity", "int", False)):
            v = self.attr(L, b, key, kind, req, where)
            if v is not ABSENT and v is not BAD:
                out[key] = v
        self.warn_unknown(L, b, BACKLOG_KEYS, where)
        if out.get("value") == 0:
            L.add("backlog-zero", f"{where}: value is 0; backlogs lists non-zero backlogs only")
        elif "value" in out and "capacity" in out and out["value"] > out["capacity"]:
            L.add("depth-capacity", f"{where}: value {out['value']} exceeds capacity {out['capacity']}")
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
            sig["backlogs"] = self.array_of(L, obj, "backlogs", False, self.backlog, typ)
            if isinstance(obj.get("backlogs"), list) and not obj["backlogs"]:
                L.add("backlogs-empty", "snapshot: backlogs is present but empty; it is omitted when it would be empty")
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
        for v in L.violations:
            self.violation_count += 1
            self.on_violation(v)
        for w in L.warnings:
            self.on_message(lineno, "warning", w)

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
                # belongs to no session: ignored, and not received in Loss; reported once per source
                if not st.orphan_reported:
                    st.orphan_reported = True
                    L.add("snapshot-first", f"source {src!r} has no session: a session begins with a snapshot "
                                            f"with seq 0, but its first signal is {where} with seq {seq}. "
                                            "\"If the discarded signals include the snapshot that begins the "
                                            "session, the source begins a new session instead\" (Loss); "
                                            "signals of this source are ignored until a seq-0 snapshot")
                return
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
            fresh.replace(old.elements, old.links, old.spans)
            fresh.backlog = old.backlog
        fresh.applied_t = 0.0  # nothing of the new session has been applied
        st.session = fresh

    def new_session(self, src: str, lineno: int, t: float) -> Session:
        w = self.window if self.window is not None else self.windows.get((src, lineno))
        return Session(start_line=lineno, T=t, w=w, last_seq=0, last_t=t, applied_t=t, snap_t=t,
                       used_spans=RecentIds(self.span_ids))

    # ---- windows ----------------------------------------------------------------------------

    def check_depth_due(self, L: Line, sess: Session, t: float, gap: int) -> None:
        if gap > 0:  # the depth signal may have been among the lost ones
            sess.clear_depth_due()
            return
        heap, tol, overdue = sess.depth_heap, self.wtol(sess), []
        while heap and t > heap[0][0] + tol:
            due, ident = heapq.heappop(heap)
            if sess.depth_due.get(ident) == due:  # not a stale entry
                del sess.depth_due[ident]
                overdue.append((ident, due))
        for ident, due in sorted(overdue):
            L.add("depth-missing", f"{ident!r} had a non-zero backlog at window end {due - sess.w:.6f} "
                                   f"but no depth signal at the next window end {due:.6f}")

    def wtol(self, sess: Session) -> float:
        # the larger of the absolute floor, a fraction of the window, and a few ULPs of the times
        # (2.4e-7 s each near T = 1.76e9)
        floor = max(self.tol, 4 * math.ulp(sess.T))
        return max(floor, RELATIVE_TOLERANCE * sess.w) if sess.w is not None else floor

    def check_window(self, L: Line, sess: Session, typ: str, ident: str, t0: float | None, t: float) -> tuple | None:
        """Checks window alignment and duplicates; returns the window key of the signal.

        Returns None, checking nothing, when the session's window length is unknown.
        """
        if sess.w is None:
            if not sess.window_note:
                sess.window_note = True
                self.on_message(L.lineno, "note", f"the window length of source {self._source!r} in the session "
                                             f"starting at line {sess.start_line} cannot be inferred (no flow or "
                                             "latency signal); window rules are not checked (use --window)")
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
        if sess.window_seen.get(ident, {}).get(typ) == k_end:
            L.add("window-duplicate", f"{typ}: a second {typ} signal for {ident!r} in the window ending at {t}")
        return key

    def accept_window(self, sess: Session, key: tuple | None, boundary: float) -> None:
        if key is None:
            return
        typ, ident, k_end = key
        # applied signals of a session do not go back in t, so only the latest window can repeat
        sess.window_seen.setdefault(ident, {})[typ] = k_end
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
        backlogs: dict[str, dict] = {}
        for i, b in enumerate(sig["backlogs"] if sig["backlogs"] is not ABSENT else []):
            if b["id"] not in pos and b["id"] not in link_ids:
                L.add("ref-missing", f"snapshot: backlogs[{i}] names {b['id']!r}, which is not a listed element or link")
            elif b["id"] in backlogs:
                L.add("backlog-duplicate", f"snapshot: backlogs[{i}] lists {b['id']!r} a second time")
            else:
                backlogs[b["id"]] = {k: b[k] for k in ("value", "capacity") if k in b}
        if L.hard:
            return False
        if not new_session and self._gap == 0 and sess.applied and (elements or links or spans):
            self.compare_snapshot(L, sess, elements, links, opened)
        sess.replace({e["id"]: e for e in elements}, {lk["id"]: lk for lk in links}, opened)
        if new_session:
            sess.used_spans = RecentIds(self.span_ids)
            sess.window_seen = {}
            sess.phase = None
        for span in opened:
            sess.used_spans.add(span)
        # "the value of its last depth signal since the last snapshot, or else its value in the
        # backlogs of that snapshot, or else zero" (Windows)
        sess.backlog = backlogs
        sess.depth_last = {}
        sess.clear_depth_due()
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
            sess.put_element(e)
            sess.added_t[e["id"]] = t
        for lk in links:
            sess.put_link(lk)
            sess.added_t[lk["id"]] = t
        return True

    def sem_remove(self, L: Line, st: SourceState, sess: Session, sig: dict, t: float, new_session: bool) -> bool:
        for ident in sig["ids"]:
            if ident in sess.elements:
                gone, todo = {ident}, [ident]
                while todo:
                    for child in sess.children.get(todo.pop(), ()):
                        if child not in gone:
                            gone.add(child)
                            todo.append(child)
                dead = {lid for eid in gone for lid in sess.links_at.get(eid, ())}
                ending = sorted({span for eid in gone for span in sess.spans_at.get(eid, ())})
                for lid in dead:
                    sess.pop_link(lid)
                for span_id in ending:
                    rec = dict(sess.pop_span(span_id).out)
                    rec.update(t1=t, status="error")
                    self.ended(st, rec)
                for eid in gone:
                    sess.pop_element(eid)
                self.forget(sess, gone | dead)
            elif ident in sess.links:
                sess.pop_link(ident)
                self.forget(sess, {ident})
            # otherwise skipped
        return True

    @staticmethod
    def forget(sess: Session, ids: set[str]) -> None:
        for ident in ids:
            sess.backlog.pop(ident, None)
            sess.depth_last.pop(ident, None)
            sess.depth_due.pop(ident, None)  # its heap entry becomes stale
            sess.added_t.pop(ident, None)
            sess.window_seen.pop(ident, None)

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
                    (self.direct and sess.snap_is_start) or t - sess.w > sess.snap_t + self.wtol(sess)
                    or sess.added_t.get(ident, -math.inf) > t - sess.w + self.wtol(sess)):
                # "the backlog at the end of the window before window 0, and at the end of a window
                # before an element or link existed, is zero" (Windows)
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
            sess.set_depth_due(ident, t + sess.w)
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
        sess.put_span(span, OpenSpan(out, (ident, sig["label"], raw_parent, t)))
        sess.used_spans.add(span)
        return True

    def sem_span_end(self, L: Line, st: SourceState, sess: Session, sig: dict, t: float, new_session: bool) -> bool:
        span = sig["span"]
        if span not in sess.spans:
            L.add("span-not-open", f"span.end: span {span!r} is not open")
            return False
        rec = dict(sess.pop_span(span).out)
        rec.update(t1=t, status=sig["status"])
        if sig["error"] is not ABSENT:
            rec["error"] = sig["error"]
        self.ended(st, rec)
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
        tolerance: float = DEFAULT_TOLERANCE, span_ids: int = DEFAULT_SPAN_IDS, **callbacks: Any) -> Engine:
    """Two passes over the file, the window lengths first, then the signals. Memory is bounded by
    the live state, not by the recording's length (except `fold`'s list of ended spans, which is
    its output, and violations when no on_violation callback is given). Violations come in line
    order."""
    windows = infer_windows(read_lines(path)) if window is None else {}
    engine = Engine(mode, windows, window, direct, tolerance, span_ids, **callbacks)
    for lineno, parsed in read_lines(path):
        engine.feed(lineno, parsed)
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
        sp.add_argument("--span-ids", type=int, default=DEFAULT_SPAN_IDS, metavar="N",
                        help=f"span IDs remembered per session to detect span-reuse (default {DEFAULT_SPAN_IDS}; "
                             "0 remembers all, with memory growing with the session)")
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
    if args.span_ids < 0:
        parser.error("--span-ids must be 0 or more")
    options = dict(window=args.window, direct=args.direct, tolerance=args.tolerance, span_ids=args.span_ids)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")
    schema_rules: list[Violation] = []  # (line, rule) only, for --schema

    def print_violation(v: Violation) -> None:
        print(f"line {v.line}: {v.rule}: {v.message}")
        if getattr(args, "schema", False):
            schema_rules.append(Violation(v.line, v.rule, ""))

    def print_message(lineno: int, kind: str, text: str) -> None:
        print(f"line {lineno}: {kind}: {text}", file=sys.stderr)

    if args.command == "check":
        options.update(on_violation=print_violation)
    options.update(on_message=print_message)
    try:
        engine = run(args.recording, args.command, **options)
    except OSError as exc:
        print(f"check.py: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # a bug in the checker, never a verdict on the recording
        print(f"check.py: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.command == "fold":
        json.dump(engine.state(), sys.stdout, sort_keys=True, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if args.schema:
        notes = schema_disagreements(args.recording, schema_rules)
        if notes is None:
            print("check.py: --schema: the jsonschema module is not importable; skipped", file=sys.stderr)
        else:
            for note in notes:
                print(note)
    return 1 if engine.violation_count else 0


if __name__ == "__main__":
    sys.exit(main())
