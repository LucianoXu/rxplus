"""
OpenTelemetry-native logging for reactive streams.

This module provides logging facilities that integrate OpenTelemetry's LogRecord
data model with RxPY reactive streams. Log records flow through reactive pipelines
and can be filtered, redirected, and exported using standard OTel exporters.
"""

import os
import time
import errno
import glob
import re
from contextlib import contextmanager
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Optional, Union

import reactivex as rx
from reactivex import Observable, Observer, Subject
from reactivex import operators as ops

from opentelemetry._logs import SeverityNumber, LoggerProvider, LogRecord

from opentelemetry.sdk._logs import LoggerProvider as SdkLoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.resources import Resource

from .mechanism import RxException

# =============================================================================
# Type Definitions
# =============================================================================

LOG_LEVEL = Literal["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]

# Mapping from LOG_LEVEL strings to OTel SeverityNumber
SEVERITY_MAP: dict[str, SeverityNumber] = {
    "TRACE": SeverityNumber.TRACE,
    "DEBUG": SeverityNumber.DEBUG,
    "INFO": SeverityNumber.INFO,
    "WARN": SeverityNumber.WARN,
    "ERROR": SeverityNumber.ERROR,
    "FATAL": SeverityNumber.FATAL,
}

# Reverse mapping for filtering by level name
SEVERITY_TO_LEVEL: dict[SeverityNumber, str] = {v: k for k, v in SEVERITY_MAP.items()}


# =============================================================================
# Factory Functions
# =============================================================================

def create_log_record(
    body: Any,
    level: LOG_LEVEL = "INFO",
    source: str = "Unknown",
    attributes: dict[str, Any] | None = None,
) -> LogRecord:
    """
    Create an OpenTelemetry LogRecord with rxplus conventions.

    This factory function creates OTel LogRecords that are compatible with
    both the OTel ecosystem and rxplus reactive pipelines.

    Note: Resource information is associated at the LoggerProvider level,
    not per LogRecord. Use configure_otel_logging() to set resource info.

    Args:
        body: Log message (any serializable type).
        level: Log level string, one of "DEBUG", "INFO", "WARN", "ERROR", "FATAL".
        source: Component name, stored in the "log.source" attribute.
        attributes: Additional structured key-value attributes.

    Returns:
        OpenTelemetry LogRecord ready for emission through reactive streams
        or OTel exporters.

    Example:
        >>> record = create_log_record("Server started", "INFO", source="HTTPServer")
        >>> record = create_log_record(
        ...     "Request processed",
        ...     "DEBUG",
        ...     source="API",
        ...     attributes={"request_id": "abc123", "duration_ms": 42}
        ... )
    """
    merged_attributes = {"log.source": source, **(attributes or {})}

    return LogRecord(
        timestamp=time.time_ns(),
        observed_timestamp=time.time_ns(),
        severity_number=SEVERITY_MAP.get(level, SeverityNumber.INFO),
        severity_text=level,
        body=body,
        attributes=merged_attributes,
    )


def format_log_record(record: LogRecord) -> str:
    """
    Format a LogRecord as a human-readable string for file/console output.

    Format: [LEVEL] YYYY-MM-DDTHH:MM:SSZ source\\t: body\\n

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
    return f"[{record.severity_text}] {timestamp_str} {source}\t: {record.body}\n"


# =============================================================================
# Reactive Operators
# =============================================================================

def log_filter(
    levels: set[LOG_LEVEL] = {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"}
) -> Callable[[Observable], Observable]:
    """
    Filter LogRecords by severity level.

    Only LogRecords with severity matching the specified levels pass through.
    Non-LogRecord items are dropped.

    Args:
        levels: Set of level strings to allow through the filter.

    Returns:
        An RxPY operator that filters LogRecords by level.

    Example:
        >>> source.pipe(log_filter({"ERROR", "FATAL"})).subscribe(print)
    """
    severity_set = {SEVERITY_MAP[lvl] for lvl in levels if lvl in SEVERITY_MAP}
    return ops.filter(
        lambda item: isinstance(item, LogRecord) and item.severity_number in severity_set
    )


def drop_log() -> Callable[[Observable], Observable]:
    """
    Remove all LogRecords from the stream, passing through other items.

    Useful when you want to separate log records from data in a mixed stream.

    Returns:
        An RxPY operator that drops LogRecords.

    Example:
        >>> mixed_stream.pipe(drop_log()).subscribe(process_data)
    """
    return ops.filter(lambda item: not isinstance(item, LogRecord))


def log_redirect_to(
    log_observer: Observer | Callable,
    levels: set[LOG_LEVEL] = {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"},
) -> Callable[[Observable], Observable]:
    """
    Redirect LogRecords to another observer/function, forward other items.

    LogRecords matching the specified levels are sent to the log_observer,
    while all other items continue through the main stream.

    Args:
        log_observer: Observer or callable to receive the LogRecords.
        levels: Set of level strings to redirect.

    Returns:
        An RxPY operator that redirects matching LogRecords.

    Example:
        >>> logger = Logger(logfile="app.log")
        >>> source.pipe(log_redirect_to(logger, {"ERROR"})).subscribe(process)
    """
    severity_set = {SEVERITY_MAP[lvl] for lvl in levels if lvl in SEVERITY_MAP}

    def _log_redirect_to(source: Observable) -> Observable:
        def subscribe(observer, scheduler=None):
            redirect_fn = (
                log_observer.on_next
                if hasattr(log_observer, "on_next")
                else log_observer
            )

            def on_next(value: Any) -> None:
                if isinstance(value, LogRecord):
                    if value.severity_number in severity_set:
                        redirect_fn(value)  # type: ignore
                else:
                    observer.on_next(value)

            return source.subscribe(
                on_next=on_next,
                on_error=observer.on_error,
                on_completed=observer.on_completed,
                scheduler=scheduler,
            )

        return Observable(subscribe)

    return _log_redirect_to


# =============================================================================
# LogComp Interface
# =============================================================================

class LogComp(ABC):
    """
    Abstract base for components that emit logs into reactive streams.

    Components that produce log output should implement this interface
    to integrate with the rxplus logging system.
    """

    @abstractmethod
    def set_super(self, obs: rx.abc.ObserverBase | Callable) -> None:
        """Set the parent observer to receive log records."""
        ...

    @abstractmethod
    def log(
        self,
        body: Any,
        level: LOG_LEVEL = "INFO",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Emit a log record with the specified message, level, and attributes."""
        ...

    @abstractmethod
    def get_rx_exception(self, error: Exception, note: str = "") -> RxException:
        """Create an RxException with this component's context."""
        ...


class EmptyLogComp(LogComp):
    """
    A no-op LogComp implementation for testing or silent operation.
    """

    def set_super(self, obs: rx.abc.ObserverBase | Callable) -> None:
        pass

    def log(
        self,
        body: Any,
        level: LOG_LEVEL = "INFO",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        pass

    def get_rx_exception(self, error: Exception, note: str = "") -> RxException:
        return RxException(error, note=note)


class NamedLogComp(LogComp):
    """
    LogComp implementation with a named source.

    Emits LogRecords with the component name as the "log.source" attribute.
    """

    def __init__(self, name: str = "LogSource"):
        self.name = name
        self.super_obs: rx.abc.ObserverBase | Callable | None = None

    def set_super(self, obs: rx.abc.ObserverBase | Callable) -> None:
        """Set the parent observer to receive log records."""
        self.super_obs = obs

    def log(
        self,
        body: Any,
        level: LOG_LEVEL = "INFO",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """
        Emit a LogRecord to the parent observer.

        Args:
            body: Log message (any serializable type).
            level: Log level string.
            attributes: Additional structured attributes.

        Raises:
            RuntimeError: If set_super() has not been called.
        """
        if self.super_obs is None:
            raise RuntimeError("Super observer not set. Call set_super() first.")

        record = create_log_record(
            body=body,
            level=level,
            source=self.name,
            attributes=attributes,
        )

        if hasattr(self.super_obs, "on_next"):
            self.super_obs.on_next(record)
        else:
            self.super_obs(record)  # type: ignore

    def get_rx_exception(self, error: Exception, note: str = "") -> RxException:
        """Create an RxException with this component's name as the source."""
        return RxException(error, source=self.name, note=note)


# =============================================================================
# Logger Subject
# =============================================================================

class Logger(Subject):
    """
    Logger is a Subject that processes and forwards OTel LogRecords.

    Features:
    - Emits LogRecords to OTel LoggerProvider for export (OTLP, console, etc.)
    - Optional file logging with rotation for local debugging
    - Cross-process file locking for concurrent writes
    - Never terminates the stream on errors (converts to log records)

    Parameters:
        logfile: Base path for file logging (timestamped). Optional.
        rotate_interval: When to rotate log files. int = after N records,
            timedelta = after time elapsed. None = no rotation.
        max_log_age: Delete log files older than this during rotation.
        lock_timeout: Maximum time to wait for file lock.
        lock_poll_interval: Sleep interval when waiting for lock.
        otel_provider: Custom LoggerProvider. Auto-created if otel_exporter is provided.
        otel_exporter: Log exporter (ConsoleLogExporter, OTLPLogExporter, etc.)
        resource: OTel Resource with service attributes.

    Example:
        >>> from opentelemetry.sdk._logs.export import ConsoleLogExporter
        >>> logger = Logger(
        ...     logfile="app.log",
        ...     otel_exporter=ConsoleLogExporter(),
        ... )
        >>> logger.on_next(create_log_record("Hello", "INFO", source="Main"))
    """

    # Regex pattern to match timestamped log files: base_YYYYMMDDTHHmmss.ext
    _TIMESTAMP_PATTERN = re.compile(r"^(.+)_(\d{8}T\d{6})(\.[^.]+)?$")

    def __init__(
        self,
        logfile: str | None = None,
        *,
        rotate_interval: int | timedelta | None = None,
        max_log_age: timedelta | None = None,
        lock_timeout: float = 10.0,
        lock_poll_interval: float = 0.05,
        # OTel parameters
        otel_provider: LoggerProvider | None = None,
        otel_exporter: LogRecordExporter | None = None,
        resource: Resource | None = None,
    ):
        super().__init__()

        # File logging setup
        self._logfile_base = logfile
        self._rotate_interval = rotate_interval
        self._max_log_age = max_log_age
        self._lock_timeout = lock_timeout
        self._lock_poll_interval = lock_poll_interval

        # Current active log file path (with timestamp)
        self._current_logfile: str | None = None
        self.pfile = None

        # Rotation tracking
        self._record_count = 0
        self._file_created_time: datetime | None = None

        # OTel setup
        self._resource = resource or Resource.create({"service.name": "rxplus"})

        if otel_provider:
            self._otel_provider = otel_provider
        elif otel_exporter:
            self._otel_provider = SdkLoggerProvider(resource=self._resource)
            self._otel_provider.add_log_record_processor(
                BatchLogRecordProcessor(otel_exporter)
            )
        else:
            self._otel_provider = None

        self._otel_logger = (
            self._otel_provider.get_logger("rxplus.logger")
            if self._otel_provider
            else None
        )

    @property
    def logfile(self) -> str | None:
        """Return the current active log file path."""
        return self._current_logfile

    def _generate_logfile_path(self) -> str:
        """Generate a new log file path with timestamp postfix."""
        if self._logfile_base is None:
            raise ValueError("logfile base is not set")

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
        if self._max_log_age is None or self._logfile_base is None:
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
        if self.pfile is not None and not self.pfile.closed:
            self.pfile.close()
        self.pfile = None
        self._current_logfile = None
        self._record_count = 0
        self._file_created_time = None

    def _ensure_file_open(self) -> None:
        """Ensure the log file is open, creating a new one if necessary."""
        if self._logfile_base is None:
            return

        if self._should_rotate():
            self._rotate_file()

        if self._current_logfile is None:
            self._current_logfile = self._generate_logfile_path()
            self._file_created_time = datetime.now()
            self._record_count = 0
            self._cleanup_old_logs()

        if self.pfile is None or self.pfile.closed:
            dir_name = os.path.dirname(self._current_logfile)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            self.pfile = open(self._current_logfile, "a")
            self.pfile.write("\n")

    def _lock_path(self) -> str | None:
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

    def on_next(self, value: Any) -> None:
        if isinstance(value, LogRecord):
            try:
                # Emit to OTel provider
                if self._otel_logger is not None:
                    self._otel_logger.emit(value)

                # Write to file if configured
                if self._logfile_base is not None:
                    self._ensure_file_open()

                    with self._acquire_lock():
                        self._ensure_file_open()

                        if self.pfile is not None:
                            self.pfile.write(format_log_record(value))
                            self.pfile.flush()
                            self._record_count += 1

                # Forward to subscribers
                super().on_next(value)

            except Exception as e:
                rx_exception = RxException(e, note="Error in Logger")
                super().on_error(rx_exception)

    def on_completed(self) -> None:
        """The logger never completes (keeps stream alive)."""
        pass

    def on_error(self, error: Exception) -> None:
        """Convert errors to LogRecords and forward."""
        source = error.source if isinstance(error, RxException) else "Unknown"
        record = create_log_record(str(error), "ERROR", source=source)
        self.on_next(record)


# =============================================================================
# Configuration Helpers
# =============================================================================

def configure_otel_logging(
    service_name: str = "rxplus",
    service_version: str = "",
    otlp_endpoint: str | None = None,
    otlp_insecure: bool = True,
    console_export: bool = False,
) -> LoggerProvider:
    """
    Configure OTel logging with common exporters.

    This helper creates a LoggerProvider with the specified exporters,
    ready for use with the Logger class.

    Args:
        service_name: Service identifier for resource attributes.
        service_version: Service version for resource attributes.
        otlp_endpoint: OTLP collector endpoint (e.g., "localhost:4317").
        otlp_insecure: Use insecure connection for OTLP (default True).
        console_export: Enable console output for debugging.

    Returns:
        Configured LoggerProvider ready for use with Logger.

    Example:
        >>> provider = configure_otel_logging(
        ...     service_name="my-app",
        ...     otlp_endpoint="localhost:4317",
        ...     console_export=True,
        ... )
        >>> logger = Logger(otel_provider=provider)
    """
    resource = Resource.create({
        "service.name": service_name,
        "service.version": service_version,
    })
    provider = SdkLoggerProvider(resource=resource)

    if console_export:
        from opentelemetry.sdk._logs.export import ConsoleLogRecordExporter

        provider.add_log_record_processor(
            BatchLogRecordProcessor(ConsoleLogRecordExporter())
        )

    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=otlp_endpoint, insecure=otlp_insecure)
            )
        )

    return provider
