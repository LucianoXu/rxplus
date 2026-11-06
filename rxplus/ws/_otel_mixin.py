"""Shared OTel logging mixin for WebSocket components."""

import time

from opentelemetry._logs import Logger, SeverityNumber
from opentelemetry._logs import LogRecord as OTelLogRecord

_SEVERITY: dict[str, SeverityNumber] = {
    "DEBUG": SeverityNumber.DEBUG,
    "INFO": SeverityNumber.INFO,
    "WARN": SeverityNumber.WARN,
    "ERROR": SeverityNumber.ERROR,
}


class OTelLoggingMixin:
    """Mixin providing _log() for WS components with OTel integration."""

    _logger: Logger | None
    _name: str

    def _log(self, body: str, level: str = "INFO") -> None:
        """Emit a log record via OTel logger if configured."""
        if self._logger is None:
            return
        record = OTelLogRecord(
            timestamp=time.time_ns(),
            body=body,
            severity_text=level,
            severity_number=_SEVERITY.get(level, SeverityNumber.INFO),
            # "log.source" is the attribute OTelLogger sets and the rxplus
            # exporters display; "component" is kept for existing queries.
            attributes={"log.source": self._name, "component": self._name},
        )
        self._logger.emit(record)
