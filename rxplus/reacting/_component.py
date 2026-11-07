"""How rxplus components report themselves (D2; standard library only).

A component takes ``reacting: ReportTo | None = None``, last in its
signature. ``None`` installs nothing. A :class:`~rxplus.reacting.Reporter`
makes the component's element top-level; an
:class:`~rxplus.reacting.Element` makes it that element's child.

Kind conventions shared with the Reacting viewer (DESIGN §4.1): an element
of kind ``port`` is drawn on its parent's border; a kind ending in
``.instance`` marks short-lived dynamic children, which the viewer groups.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .handles import Element, Link
from .reporter import Reporter

if TYPE_CHECKING:
    import asyncio

type ReportTo = Reporter | Element

PORT = "port"
_LABEL_MAX = 80  # dynamic labels built from runtime values are cut here


def path_label(path: str) -> str:
    """A request path as structure: without its query and fragment, which
    often carry data (tokens, ids). The path itself is treated as structure:
    an application that puts data in paths should not report its sockets."""
    return path.split("?", 1)[0].split("#", 1)[0] or "/"


def dynamic_label(prefix: str, path: str) -> str:
    """A label such as ``"connection /chat"`` from a request path (see
    :func:`path_label`), cut to a reasonable length (it comes from the
    peer)."""
    value = path_label(path)
    text = f"{prefix} {value}" if value else prefix
    return text if len(text) <= _LABEL_MAX else text[: _LABEL_MAX - 1] + "…"


class ComponentReport:
    """A component's element and its ports; removed with the component."""

    __slots__ = ("element", "reporter", "ports")

    def __init__(
        self, to: ReportTo, label: str, kind: str, ports: Sequence[str] = ()
    ) -> None:
        if isinstance(to, Reporter):
            self.element = to.element(label, kind=kind)
        else:
            self.element = to.child(label, kind=kind)
        self.reporter = self.element.reporter
        self.ports = {name: self.element.child(name, kind=PORT) for name in ports}

    def port(self, name: str) -> Element | None:
        return self.ports.get(name)

    def instance(self, label: str, kind: str) -> Element:
        """A dynamic child; ``kind`` gets the ``.instance`` suffix."""
        return self.element.child(label, kind=f"{kind}.instance")

    def link(
        self,
        src: Element,
        dst: Element,
        *,
        capacity: int | None = None,
    ) -> Link:
        return self.reporter.link(src, dst, capacity=capacity)

    def retire(self, element: Element, loop: asyncio.AbstractEventLoop) -> None:
        """Remove a dynamic child once the current window has ended.

        A window's counts are reported at its end, and only for links that
        still exist: removed at once, a short-lived socket would lose the
        counts of its last window. So it stays one window longer (on
        ``loop``); removing the component removes it in any case."""
        try:
            loop.call_later(self.reporter.window * 1.01, element.remove)
        except RuntimeError:  # the loop is closed
            element.remove()

    def remove(self) -> None:
        """Remove the element with its children, links and open spans, once
        the current window has ended (see :meth:`retire`)."""
        timer = threading.Timer(self.reporter.window * 1.01, self.element.remove)
        timer.daemon = True
        timer.start()


def port_of(report: ComponentReport | None, name: str) -> Element | None:
    return None if report is None else report.port(name)


__all__ = [
    "PORT",
    "ComponentReport",
    "ReportTo",
    "dynamic_label",
    "path_label",
    "port_of",
]
