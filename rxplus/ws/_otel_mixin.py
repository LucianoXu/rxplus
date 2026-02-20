"""Shared OTel logging mixin for WebSocket components."""

import time

from opentelemetry._logs import Logger, SeverityNumber
from opentelemetry._logs import LogRecord as OTelLogRecord


class OTelLoggingMixin:
    """Mixin providing _log() for WS components with OTel integration."""

    _logger: Logger | None
    _name: str

    def _log(self, body: str, level: str = "INFO") -> None:
        """Emit a log record via OTel logger if configured."""
        if self._logger is None:
            return
        severity_map: dict[str, SeverityNumber] = {
            "DEBUG": SeverityNumber.DEBUG,
            "INFO": SeverityNumber.INFO,
            "WARN": SeverityNumber.WARN,
            "ERROR": SeverityNumber.ERROR,
        }
        record = OTelLogRecord(
            timestamp=time.time_ns(),
            body=body,
            severity_text=level,
            severity_number=severity_map.get(level, SeverityNumber.INFO),
            attributes={"component": self._name},
        )
        self._logger.emit(record)
