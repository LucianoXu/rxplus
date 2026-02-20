"""OpenTelemetry configuration helpers for rxplus components.

This package provides OTel provider configuration, a structured logger
wrapper, log-record exporters, and a metrics helper.  All public names
are re-exported here for backward compatibility with code that previously
imported from the monolithic ``rxplus.telemetry`` module.
"""

from .config import (
    configure_metrics,
    configure_telemetry,
    get_default_providers,
)
from .exporters import (
    LOG_FORMAT,
    ConsoleLogRecordExporter,
    FileLogRecordExporter,
)
from .logger import (
    LogContext,
    OTelLogger,
    format_log_record,
    format_log_record_json,
)
from .metrics import MetricsHelper

__all__ = [
    # config
    "configure_telemetry",
    "configure_metrics",
    "get_default_providers",
    # logger
    "OTelLogger",
    "LogContext",
    "format_log_record",
    "format_log_record_json",
    # exporters
    "ConsoleLogRecordExporter",
    "FileLogRecordExporter",
    "LOG_FORMAT",
    # metrics
    "MetricsHelper",
]
