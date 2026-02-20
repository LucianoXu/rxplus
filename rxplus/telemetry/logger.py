"""OTel logger wrapper and log context for structured observability.

Provides :class:`OTelLogger` — a thin wrapper around the OTel Logger API
with convenience ``info``/``debug``/``warning``/``error`` methods — and
:class:`LogContext`, an immutable bundle of dimensional log attributes.

Also contains :func:`format_log_record` and :func:`format_log_record_json`
helper functions used by log-record exporters.
"""

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from opentelemetry._logs import LogRecord, SeverityNumber

# =============================================================================
# LogContext (absorbed from log_context.py)
# =============================================================================


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


# =============================================================================
# Log Record Formatting
# =============================================================================


def format_log_record(record: LogRecord) -> str:
    """
    Format a LogRecord as a human-readable string for file/console output.

    Format: [LEVEL] YYYY-MM-DDTHH:MM:SSZ [trace:span] source\\t: body\\n

    Includes a short trace/span identifier (first 8 characters of each) for debugging.

    Args:
        record: OpenTelemetry LogRecord to format.

    Returns:
        Formatted string suitable for file or console output.
    """
    timestamp_ns = record.timestamp or 0
    timestamp_str = datetime.fromtimestamp(timestamp_ns / 1e9, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    attrs = record.attributes or {}
    source = attrs.get("log.source", "Unknown")
    service = attrs.get("service.name", "")
    node = attrs.get("node.name", "")
    scope = attrs.get("log.scope", "")

    parts = [p for p in (service, node, scope) if p]
    dim_prefix = "/".join(str(v) for v in parts) + " " if parts else ""

    # Include trace context in output (from LogRecord fields)
    trace_part = ""
    if record.trace_id and record.span_id:
        # Convert integers to hex strings and show short versions for readability
        trace_id_hex = f"{record.trace_id:032x}"
        span_id_hex = f"{record.span_id:016x}"
        trace_part = f" [{trace_id_hex[:8]}:{span_id_hex[:8]}]"

    return (
        f"{timestamp_str} [{record.severity_text}]{trace_part} "
        f"{dim_prefix}{source}\t: {record.body!r}\n"
    )


def format_log_record_json(record: LogRecord) -> str:
    """
    Format a LogRecord as a JSON line for structured file output.

    The JSON output includes all log record fields in a machine-readable format,
    suitable for log aggregation systems and structured log analysis.

    Args:
        record: OpenTelemetry LogRecord to format.

    Returns:
        JSON string (single line) with newline terminator.
    """
    timestamp_ns = record.timestamp or 0

    data = {
        "timestamp": datetime.fromtimestamp(timestamp_ns / 1e9, tz=UTC).isoformat(),
        "timestamp_ns": timestamp_ns,
        "severity_text": record.severity_text,
        "severity_number": record.severity_number.value
        if record.severity_number
        else None,
        "body": record.body,
        "attributes": dict(record.attributes) if record.attributes else {},
    }

    # Add trace context if present
    if record.trace_id:
        data["trace_id"] = f"{record.trace_id:032x}"
    if record.span_id:
        data["span_id"] = f"{record.span_id:016x}"

    return json.dumps(data, default=str) + "\n"


# =============================================================================
# OTel Logger Wrapper
# =============================================================================


class OTelLogger:
    """Thin wrapper for OTel Logger with convenient emit methods.

    Provides a familiar logging interface (info, debug, warning, error)
    while emitting logs via the OTel API.  Supports dimensional
    ``LogContext`` for structured attributes and severity filtering.

    Example:
        >>> logger = OTelLogger(logger_provider.get_logger("myapp"), source="MyClass")
        >>> logger.info("Connection established", peer_id="abc123")
        >>> logger.error("Failed to connect", host="localhost", port=8765)
        >>>
        >>> # With LogContext
        >>> ctx = LogContext(service="dhproto", node="Backend@:13810")
        >>> logger = OTelLogger(provider.get_logger("x"), source="X", context=ctx)
        >>> child = logger.with_context(connection_id="ab12cd34")
    """

    def __init__(
        self,
        logger,
        source: str,
        context: LogContext | None = None,
        min_severity: SeverityNumber | None = None,
    ):
        """Initialize OTel logger wrapper.

        Args:
            logger: OTel Logger instance from LoggerProvider.get_logger()
            source: Source identifier for log.source attribute
            context: Optional LogContext with dimensional attributes.
            min_severity: Optional minimum severity -- records below this
                level are silently dropped.
        """
        self._logger = logger
        self._source = source
        self._context = context or LogContext()
        self._min_severity = min_severity

    def info(self, message: str, **attrs) -> None:
        """Emit INFO level log.

        Args:
            message: Log message body
            **attrs: Additional attributes to include
        """
        self._emit(SeverityNumber.INFO, "INFO", message, attrs)

    def debug(self, message: str, **attrs) -> None:
        """Emit DEBUG level log.

        Args:
            message: Log message body
            **attrs: Additional attributes to include
        """
        self._emit(SeverityNumber.DEBUG, "DEBUG", message, attrs)

    def warning(self, message: str, **attrs) -> None:
        """Emit WARN level log.

        Args:
            message: Log message body
            **attrs: Additional attributes to include
        """
        self._emit(SeverityNumber.WARN, "WARN", message, attrs)

    def error(self, message: str, **attrs) -> None:
        """Emit ERROR level log.

        Args:
            message: Log message body
            **attrs: Additional attributes to include
        """
        self._emit(SeverityNumber.ERROR, "ERROR", message, attrs)

    def with_context(self, **overrides) -> "OTelLogger":
        """Derive a child logger inheriting this logger's context with overrides.

        Args:
            **overrides: LogContext field overrides.  A ``source`` key is
                popped and used as the child's source string.

        Returns:
            New OTelLogger sharing the same underlying OTel Logger but
            with a derived LogContext.
        """
        new_source = overrides.pop("source", self._source)
        return OTelLogger(
            self._logger,
            source=new_source,
            context=self._context.child(**overrides),
            min_severity=self._min_severity,
        )

    def _emit(
        self,
        severity_number: SeverityNumber,
        severity_text: str,
        message: str,
        attrs: dict,
    ) -> None:
        """Emit a log record.

        Args:
            severity_number: OTel severity number
            severity_text: Human-readable severity text
            message: Log message body
            attrs: Additional attributes
        """
        if self._min_severity and severity_number.value < self._min_severity.value:
            return
        merged = {
            "log.source": self._source,
            **self._context.as_attributes(),
            **attrs,
        }
        record = LogRecord(
            timestamp=time.time_ns(),
            body=message,
            severity_text=severity_text,
            severity_number=severity_number,
            attributes=merged,
        )
        self._logger.emit(record)
