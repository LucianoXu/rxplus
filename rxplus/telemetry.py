"""OpenTelemetry configuration helpers for rxplus components.

This module provides a simple way to configure OTel providers for use with
rxplus reactive components. Providers are returned for explicit injection
into components rather than being set globally.
"""

import errno
import glob
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal, Sequence

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, BatchSpanProcessor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    LogRecordExporter,
    BatchLogRecordProcessor,
    SimpleLogRecordProcessor,
    LogRecordExportResult,
)
from opentelemetry.sdk._logs._internal import ReadableLogRecord
from opentelemetry.sdk.resources import Resource
from opentelemetry._logs import LogRecord


def configure_telemetry(
    service_name: str = "rxplus",
    service_version: str = "",
    span_exporter: SpanExporter | None = None,
    log_exporter: LogRecordExporter | None = None,
    batch_logs: bool = True,
) -> tuple[TracerProvider, LoggerProvider]:
    """
    Configure OTel providers for rxplus components.

    Creates TracerProvider and LoggerProvider with the specified resource
    attributes and exporters. Returns the providers for explicit injection
    into components — does NOT set global providers.

    Args:
        service_name: Service identifier for resource attributes.
        service_version: Service version for resource attributes.
        span_exporter: Optional span exporter (e.g., OTLPSpanExporter, ConsoleSpanExporter).
        log_exporter: Optional log exporter (e.g., OTLPLogExporter, ConsoleLogExporter).
        batch_logs: If True, use BatchLogRecordProcessor (better for network exporters).
            If False, use SimpleLogRecordProcessor (immediate output, better for console).

    Returns:
        Tuple of (TracerProvider, LoggerProvider) for injection into components.

    Example:
        >>> from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        >>> from opentelemetry.sdk._logs.export import ConsoleLogExporter
        >>> 
        >>> tracer_provider, logger_provider = configure_telemetry(
        ...     service_name="my-app",
        ...     span_exporter=ConsoleSpanExporter(),
        ...     log_exporter=ConsoleLogExporter(),
        ... )
        >>> 
        >>> server = RxWSServer(
        ...     {"host": "::", "port": 8888},
        ...     tracer_provider=tracer_provider,
        ...     logger_provider=logger_provider,
        ... )
    """
    resource = Resource.create({
        "service.name": service_name,
        "service.version": service_version,
    })

    tracer_provider = TracerProvider(resource=resource)
    if span_exporter:
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))

    logger_provider = LoggerProvider(resource=resource)
    if log_exporter:
        if batch_logs:
            logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        else:
            logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))

    return tracer_provider, logger_provider


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
    timestamp_str = datetime.fromtimestamp(timestamp_ns / 1e9, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    source = (
        record.attributes.get("log.source", "Unknown")
        if record.attributes
        else "Unknown"
    )

    # Include trace context in output (from LogRecord fields)
    trace_part = ""
    if record.trace_id and record.span_id:
        # Convert integers to hex strings and show short versions for readability
        trace_id_hex = f"{record.trace_id:032x}"
        span_id_hex = f"{record.span_id:016x}"
        trace_part = f" [{trace_id_hex[:8]}:{span_id_hex[:8]}]"

    return f"{timestamp_str} [{record.severity_text}] {trace_part} {source}\t: {record.body}\n"


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
        "timestamp": datetime.fromtimestamp(timestamp_ns / 1e9, tz=timezone.utc).isoformat(),
        "timestamp_ns": timestamp_ns,
        "severity_text": record.severity_text,
        "severity_number": record.severity_number.value if record.severity_number else None,
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
# Console Log Record Exporter
# =============================================================================


class ConsoleLogRecordExporter(LogRecordExporter):
    """OTel LogRecordExporter that writes CLI-friendly output to stderr.
    
    Unlike OTel's ConsoleLogExporter which outputs verbose JSON,
    this exporter produces human-readable format suitable for CLI applications.
    
    Example output:
        [INFO] 2026-02-03T10:30:00Z MyComponent: Connection established
        [DEBUG] 2026-02-03T10:30:01Z MyComponent: Processing message
    """
    
    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        """Export log records to stderr in CLI-friendly format.
        
        Args:
            batch: Sequence of ReadableLogRecord objects to export.
            
        Returns:
            LogRecordExportResult.SUCCESS on success.
        """
        import sys
        try:
            for readable_record in batch:
                record = readable_record.log_record
                sys.stderr.write(format_log_record(record))
            sys.stderr.flush()
            return LogRecordExportResult.SUCCESS
        except Exception:
            return LogRecordExportResult.FAILURE
    
    def shutdown(self) -> None:
        """Shutdown the exporter (no-op for console)."""
        pass
    
    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Force flush any buffered data.
        
        Returns:
            True always, as stderr is line-buffered.
        """
        import sys
        sys.stderr.flush()
        return True


# =============================================================================
# OTel Logger Wrapper
# =============================================================================

from opentelemetry._logs import SeverityNumber


class OTelLogger:
    """Thin wrapper for OTel Logger with convenient emit methods.
    
    Provides a familiar logging interface (info, debug, warning, error)
    while emitting logs via the OTel API.
    
    Example:
        >>> logger = OTelLogger(logger_provider.get_logger("myapp"), source="MyClass")
        >>> logger.info("Connection established", peer_id="abc123")
        >>> logger.error("Failed to connect", host="localhost", port=8765)
    """
    
    def __init__(self, logger, source: str):
        """Initialize OTel logger wrapper.
        
        Args:
            logger: OTel Logger instance from LoggerProvider.get_logger()
            source: Source identifier for log.source attribute
        """
        self._logger = logger
        self._source = source
    
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
    
    def _emit(self, severity_number: SeverityNumber, severity_text: str, 
              message: str, attrs: dict) -> None:
        """Emit a log record.
        
        Args:
            severity_number: OTel severity number
            severity_text: Human-readable severity text
            message: Log message body
            attrs: Additional attributes
        """
        record = LogRecord(
            timestamp=time.time_ns(),
            body=message,
            severity_text=severity_text,
            severity_number=severity_number,
            attributes={"log.source": self._source, **attrs},
        )
        self._logger.emit(record)


# =============================================================================
# Default Providers
# =============================================================================

from typing import Optional

_default_tracer_provider: Optional[TracerProvider] = None
_default_logger_provider: Optional[LoggerProvider] = None


def get_default_providers(
    service_name: str = "rxplus",
) -> tuple[TracerProvider, LoggerProvider]:
    """Get or create default providers with console output.
    
    Lazily initializes default providers on first call. Returns the same
    providers on subsequent calls (singleton pattern).
    
    The default configuration uses ConsoleLogRecordExporter for CLI-friendly
    output to stderr with immediate (non-batched) processing.
    Users can override by passing custom providers to component constructors.
    
    Args:
        service_name: Service name for the default providers (only used on first call).
    
    Returns:
        Tuple of (TracerProvider, LoggerProvider) for injection into components.
        
    Example:
        >>> tracer_provider, logger_provider = get_default_providers()
        >>> server = RxWSServer(config, logger_provider=logger_provider)
    """
    global _default_tracer_provider, _default_logger_provider
    
    if _default_logger_provider is None:
        _default_tracer_provider, _default_logger_provider = configure_telemetry(
            service_name=service_name,
            log_exporter=ConsoleLogRecordExporter(),
            batch_logs=False,  # Immediate output for CLI
        )
    
    # At this point both providers are guaranteed to be initialized
    assert _default_tracer_provider is not None
    return _default_tracer_provider, _default_logger_provider


# =============================================================================
# File Log Record Exporter
# =============================================================================

LOG_FORMAT = Literal["text", "json"]


class FileLogRecordExporter(LogRecordExporter):
    """
    OpenTelemetry LogRecordExporter that writes logs to files.

    This exporter implements the standard OTel LogRecordExporter interface,
    allowing it to be used with BatchLogRecordProcessor or SimpleLogRecordProcessor.
    It supports both human-readable text format and structured JSON format.

    Features:
    - File rotation by record count or time interval
    - Automatic cleanup of old log files
    - Cross-process file locking for concurrent writes
    - Text or JSON output formats

    Parameters:
        logfile: Base path for log files. Timestamp will be appended.
        format: Output format - "text" for human-readable, "json" for structured.
        rotate_interval: When to rotate log files. int = after N records,
            timedelta = after time elapsed. None = no rotation.
        max_log_age: Delete log files older than this during rotation.
        lock_timeout: Maximum time to wait for file lock.
        lock_poll_interval: Sleep interval when waiting for lock.

    Example:
        >>> from opentelemetry.sdk._logs import LoggerProvider
        >>> from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
        >>> 
        >>> exporter = FileLogRecordExporter("app.log", format="text")
        >>> provider = LoggerProvider()
        >>> provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
        >>> 
        >>> # Or with JSON format and rotation
        >>> exporter = FileLogRecordExporter(
        ...     "logs/app.log",
        ...     format="json",
        ...     rotate_interval=1000,  # Rotate after 1000 records
        ...     max_log_age=timedelta(days=7),
        ... )
    """

    # Regex pattern to match timestamped log files: base_YYYYMMDDTHHmmss.ext
    _TIMESTAMP_PATTERN = re.compile(r"^(.+)_(\d{8}T\d{6})(\.[^.]+)?$")

    def __init__(
        self,
        logfile: str,
        *,
        format: LOG_FORMAT = "text",
        rotate_interval: int | timedelta | None = None,
        max_log_age: timedelta | None = None,
        lock_timeout: float = 10.0,
        lock_poll_interval: float = 0.05,
    ):
        self._logfile_base = logfile
        self._format = format
        self._rotate_interval = rotate_interval
        self._max_log_age = max_log_age
        self._lock_timeout = lock_timeout
        self._lock_poll_interval = lock_poll_interval

        # Current active log file path (with timestamp)
        self._current_logfile: str | None = None
        self._file = None

        # Rotation tracking
        self._record_count = 0
        self._file_created_time: datetime | None = None

        # Select formatter based on format
        self._formatter = format_log_record_json if format == "json" else format_log_record

    @property
    def logfile(self) -> str | None:
        """Return the current active log file path."""
        return self._current_logfile

    def _generate_logfile_path(self) -> str:
        """Generate a new log file path with timestamp postfix."""
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        base, ext = os.path.splitext(self._logfile_base)
        return f"{base}_{timestamp}{ext}"

    def _should_rotate(self) -> bool:
        """Check if we should rotate to a new log file."""
        if self._rotate_interval is None:
            return False

        if isinstance(self._rotate_interval, int):
            return self._record_count >= self._rotate_interval

        if isinstance(self._rotate_interval, timedelta):
            if self._file_created_time is None:
                return False
            return datetime.now() - self._file_created_time >= self._rotate_interval

        return False

    def _cleanup_old_logs(self) -> None:
        """Delete log files older than max_log_age."""
        if self._max_log_age is None:
            return

        base, ext = os.path.splitext(self._logfile_base)
        dir_path = os.path.dirname(self._logfile_base) or "."

        pattern = f"{os.path.basename(base)}_*{ext}"
        log_files = glob.glob(os.path.join(dir_path, pattern))

        cutoff_time = datetime.now() - self._max_log_age

        for log_file in log_files:
            if log_file == self._current_logfile:
                continue

            filename = os.path.basename(log_file)
            match = self._TIMESTAMP_PATTERN.match(filename)
            if match:
                timestamp_str = match.group(2)
                try:
                    file_time = datetime.strptime(timestamp_str, "%Y%m%dT%H%M%S")
                    if file_time < cutoff_time:
                        try:
                            os.remove(log_file)
                        except OSError:
                            pass
                except ValueError:
                    pass

    def _rotate_file(self) -> None:
        """Close current file and prepare for a new one."""
        if self._file is not None and not self._file.closed:
            self._file.close()
        self._file = None
        self._current_logfile = None
        self._record_count = 0
        self._file_created_time = None

    def _ensure_file_open(self) -> None:
        """Ensure the log file is open, creating a new one if necessary."""
        if self._should_rotate():
            self._rotate_file()

        if self._current_logfile is None:
            self._current_logfile = self._generate_logfile_path()
            self._file_created_time = datetime.now()
            self._record_count = 0
            self._cleanup_old_logs()

        if self._file is None or self._file.closed:
            dir_name = os.path.dirname(self._current_logfile)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            self._file = open(self._current_logfile, "a")
            self._file.write("\n")

    def _lock_path(self) -> str | None:
        """Return the path for the lock file."""
        if self._current_logfile is None:
            return None
        return f"{self._current_logfile}.lock"

    @contextmanager
    def _acquire_lock(self):
        """Cross-process file lock with waiting."""
        lock_path = self._lock_path()
        if lock_path is None:
            yield
            return

        dir_name = os.path.dirname(lock_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        # Try fcntl-based advisory lock first (POSIX platforms)
        if os.name != "nt":
            try:
                import fcntl

                f = open(lock_path, "a+")
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    yield
                finally:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    finally:
                        f.close()
                return
            except Exception:
                pass

        # Fallback: spin on exclusive create of lock file
        start = time.time()
        fd = None
        contended_errnos = {errno.EEXIST, errno.EACCES, errno.EPERM, errno.EBUSY}

        try:
            while True:
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    break
                except OSError as exc:
                    if exc.errno in contended_errnos:
                        if time.time() - start > self._lock_timeout:
                            raise TimeoutError(
                                f"Timeout acquiring log lock: {lock_path}"
                            ) from exc
                        time.sleep(self._lock_poll_interval)
                        continue
                    raise

            yield
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
            except PermissionError:
                pass

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        """
        Export a batch of log records to the file.

        Args:
            batch: Sequence of ReadableLogRecord objects to export.

        Returns:
            LogRecordExportResult.SUCCESS on success, LogRecordExportResult.FAILURE on error.
        """
        try:
            self._ensure_file_open()

            with self._acquire_lock():
                # Re-check file state after acquiring lock
                self._ensure_file_open()

                if self._file is not None:
                    for readable_record in batch:
                        record = readable_record.log_record
                        self._file.write(self._formatter(record))
                        self._record_count += 1
                    self._file.flush()

            return LogRecordExportResult.SUCCESS

        except Exception:
            return LogRecordExportResult.FAILURE

    def shutdown(self) -> None:
        """
        Shutdown the exporter, closing any open file handles.
        """
        if self._file is not None and not self._file.closed:
            self._file.close()
        self._file = None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """
        Force flush any buffered data.

        Since we flush after each export, this is effectively a no-op.

        Returns:
            True always, as nothing is buffered beyond OS-level buffers.
        """
        if self._file is not None and not self._file.closed:
            self._file.flush()
        return True
