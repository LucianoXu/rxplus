"""Graph state of a source: elements, links, open spans (standard library only).

A reporter keeps two of these. The *registry* is updated by the callers'
threads and validates their calls. The *mirror* is updated only by the clock
thread, in signal order, and is what snapshots are built from, so a snapshot
always shows the state at its position in the signal stream.
"""

from dataclasses import dataclass

from ._encode import Signal


@dataclass(slots=True)
class ElementRec:
    id: str
    label: str
    parent: str | None
    kind: str | None

    def signal(self) -> Signal:
        d: Signal = {"id": self.id, "label": self.label}
        if self.parent is not None:
            d["parent"] = self.parent
        if self.kind is not None:
            d["kind"] = self.kind
        return d


@dataclass(slots=True)
class LinkRec:
    id: str
    src: str
    dst: str
    kind: str

    def signal(self) -> Signal:
        return {"id": self.id, "from": self.src, "to": self.dst, "kind": self.kind}


@dataclass(slots=True)
class SpanRec:
    span: str
    element: str
    label: str
    parent: str | None  # the parent as sent in the current session
    t0: float


class Graph:
    """Elements, links and open spans, with the spec's removal cascade."""

    __slots__ = ("elements", "links", "spans")

    def __init__(self) -> None:
        self.elements: dict[str, ElementRec] = {}
        self.links: dict[str, LinkRec] = {}
        self.spans: dict[str, SpanRec] = {}

    def exists(self, ident: str) -> bool:
        return ident in self.elements or ident in self.links

    def remove(self, ids: list[str]) -> list[str]:
        """Remove in order (spec, Remove signals); return every removed
        element and link ID. Spans of removed elements go silently."""
        removed: list[str] = []
        for ident in ids:
            if ident in self.links:
                del self.links[ident]
                removed.append(ident)
                continue
            if ident not in self.elements:
                continue
            gone = {ident}
            for rec in self.elements.values():  # insertion order: parents first
                if rec.parent in gone:
                    gone.add(rec.id)
            for e in [e for e in self.elements if e in gone]:
                del self.elements[e]
                removed.append(e)
            for lid in [
                i for i, r in self.links.items() if r.src in gone or r.dst in gone
            ]:
                del self.links[lid]
                removed.append(lid)
            for sid in [s for s, r in self.spans.items() if r.element in gone]:
                del self.spans[sid]
        return removed

    def snapshot(self, t: float, new_session: bool) -> Signal:
        """The snapshot attributes (without the generic ones)."""
        spans = []
        for rec in self.spans.values():  # insertion order: parents first
            if new_session and rec.parent not in self.spans:
                rec.parent = None  # what this session's snapshot sends
            d: Signal = {"span": rec.span, "id": rec.element, "label": rec.label}
            if rec.parent is not None:
                d["parent"] = rec.parent
            d["t0"] = min(rec.t0, t)
            spans.append(d)
        attrs: Signal = {
            "elements": [r.signal() for r in self.elements.values()],
            "links": [r.signal() for r in self.links.values()],
        }
        if spans:
            attrs["spans"] = spans
        return attrs
