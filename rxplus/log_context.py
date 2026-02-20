"""Immutable dimensional log context for structured observability.

Provides a ``LogContext`` dataclass that bundles dimensional attributes
(service, node, scope, component, connection, stream) and attaches them
to every log record emitted by an ``OTelLogger`` carrying this context.

``LogContext`` is a pure data structure with no OTel imports — it can be
used independently for attribute bundling and child-context derivation.
"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class LogContext:
    """Immutable bundle of dimensional log attributes.

    Automatically attached to every log record emitted by an
    OTelLogger carrying this context.
    """

    service: str = ""
    node: str = ""
    scope: str = ""
    component: str = ""
    connection_id: str = ""
    stream_id: int | None = None

    def as_attributes(self) -> dict[str, str | int]:
        """Convert to OTel log record attributes dict. Omits empty/None values."""
        attrs: dict[str, str | int] = {}
        if self.service:
            attrs["service.name"] = self.service
        if self.node:
            attrs["node.name"] = self.node
        if self.scope:
            attrs["log.scope"] = self.scope
        if self.component:
            attrs["component.name"] = self.component
        if self.connection_id:
            attrs["connection.id"] = self.connection_id
        if self.stream_id is not None:
            attrs["stream.id"] = self.stream_id
        return attrs

    def child(self, **overrides: str | int | None) -> "LogContext":
        """Derive a child context, inheriting parent values for unspecified fields."""
        return LogContext(**{**asdict(self), **overrides})
