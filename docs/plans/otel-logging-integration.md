# OpenTelemetry Logging Integration Design Document

## Current Context
- `rxplus` provides reactive logging via `LogItem`, `Logger`, and related operators (`log_filter`, `log_redirect_to`, `drop_log`)
- `LogItem` is a simple data class with `level`, `timestamp_str`, `source`, and `msg` fields
- `Logger` is a `Subject` that writes logs to timestamped files with rotation and cross-process locking
- Components implement `LogComp` interface to emit logs into reactive streams
- Current implementation uses a custom data model not aligned with industry observability standards

## Requirements

### Functional Requirements
- **Replace `LogItem` entirely with OpenTelemetry `LogRecord`** - use OTel's native data model directly
- Map `LOG_LEVEL` literals to OTel `SeverityNumber` enum
- Integrate `LoggerProvider` and log processors/exporters into `Logger`
- Support multiple export destinations: console, file, OTLP (gRPC/HTTP)
- Update reactive operators (`log_filter`, `log_redirect_to`, `drop_log`) to work with `LogRecord`
- Leverage OTel attributes for structured logging
- Support trace context propagation for correlated observability

### Non-Functional Requirements
- **No backward compatibility** - clean break from `LogItem` to native OTel `LogRecord`
- Performance: batch log export to avoid blocking reactive streams
- OTel packages become required dependencies (not optional)
- Type safety: maintain full type hints throughout

## Design Decisions

### 1. Data Model: Direct Use of OTel LogRecord
Will use OpenTelemetry `LogRecord` directly (no wrapper class) because:
- Cleanest integration with OTel ecosystem
- No translation layer or adapter overhead
- Full access to all OTel features (trace context, resource, attributes)
- Idiomatic OTel usage - logs are first-class citizens in the observability stack
- Simpler codebase with less custom code to maintain

### 2. Severity Mapping
Will map `LOG_LEVEL` to OTel `SeverityNumber`:
| LOG_LEVEL | SeverityNumber |
|-----------|----------------|
| DEBUG     | DEBUG (5)      |
| INFO      | INFO (9)       |
| WARN   | WARN (13)      |
| ERROR     | ERROR (17)     |
| FATAL  | FATAL (21)     |

Rationale: Direct mapping per OTel Log Data Model specification

### 3. Export Strategy
Will use `BatchLogRecordProcessor` with configurable exporters because:
- Batching prevents blocking the reactive stream on I/O
- Supports multiple simultaneous exporters (file + OTLP)
- Aligns with OTel SDK best practices

### 4. Breaking Change Strategy
Will completely replace `LogItem` with `LogRecord` because:
- Clean design without legacy baggage
- Users get full OTel benefits immediately
- No confusing dual-model period
- Easier to maintain single code path

## Technical Design

### 1. Core Components

```python
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider, LogRecord
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

# Re-export LogRecord as the primary log data type
from opentelemetry.sdk._logs import LogRecord

# Type alias for log levels (maps to OTel SeverityNumber)
LOG_LEVEL = Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]

# Severity mapping constant
SEVERITY_MAP: dict[LOG_LEVEL, SeverityNumber] = {
    "DEBUG": SeverityNumber.DEBUG,
    "INFO": SeverityNumber.INFO,
    "WARN": SeverityNumber.WARN,
    "ERROR": SeverityNumber.ERROR,
    "FATAL": SeverityNumber.FATAL,
}

def create_log_record(
    body: Any,
    level: LOG_LEVEL = "INFO",
    source: str = "Unknown",
    attributes: dict[str, Any] | None = None,
    resource: Resource | None = None,
) -> LogRecord:
    """
    Factory function to create OTel LogRecord with rxplus conventions.
    
    Args:
        body: Log message (any serializable type)
        level: Log level string (mapped to SeverityNumber)
        source: Component name (stored in log.source attribute)
        attributes: Additional structured attributes
        resource: OTel Resource (uses default if None)
    
    Returns:
        OpenTelemetry LogRecord ready for emission
    """
    merged_attributes = {"log.source": source, **(attributes or {})}
    
    return LogRecord(
        timestamp=time.time_ns(),
        observed_timestamp=time.time_ns(),
        severity_number=SEVERITY_MAP.get(level, SeverityNumber.INFO),
        severity_text=level,
        body=body,
        attributes=merged_attributes,
        resource=resource or Resource.create({}),
    )


def format_log_record(record: LogRecord) -> str:
    """
    Format a LogRecord as a human-readable string for file/console output.
    
    Format: [LEVEL] YYYY-MM-DDTHH:MM:SSZ source: body
    """
    timestamp_str = datetime.utcfromtimestamp(
        record.timestamp / 1e9
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    source = record.attributes.get("log.source", "Unknown") if record.attributes else "Unknown"
    return f"[{record.severity_text}] {timestamp_str} {source}\t: {record.body}\n"
```

### 2. Updated Operators

```python
def log_filter(
    levels: set[LOG_LEVEL] = {"DEBUG", "INFO", "WARN", "ERROR", "FATAL"}
) -> Callable[[Observable], Observable]:
    """
    Filter LogRecords by severity level.
    """
    severity_set = {SEVERITY_MAP[lvl] for lvl in levels}
    return ops.filter(
        lambda item: isinstance(item, LogRecord) and item.severity_number in severity_set
    )


def drop_log() -> Callable[[Observable], Observable]:
    """
    Remove all LogRecords from the stream, passing through other items.
    """
    return ops.filter(lambda item: not isinstance(item, LogRecord))


def log_redirect_to(
    log_observer: Observer | Callable,
    levels: set[LOG_LEVEL] = {"DEBUG", "INFO", "WARN", "ERROR", "FATAL"},
) -> Callable[[Observable], Observable]:
    """
    Redirect LogRecords to another observer/function, forward other items.
    """
    severity_set = {SEVERITY_MAP[lvl] for lvl in levels}
    
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
                        redirect_fn(value)
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
```

### 3. Logger with Native OTel Integration

```python
class Logger(Subject):
    """
    Logger is a Subject that processes and forwards OTel LogRecords.
    
    Features:
    - Emits LogRecords to OTel LoggerProvider for export
    - Optional file logging with rotation (legacy feature)
    - Cross-process file locking for concurrent writes
    - Never terminates the stream on errors
    
    Parameters:
        logfile: Base path for file logging (timestamped, optional)
        rotate_interval: Record count (int) or time (timedelta) for rotation
        max_log_age: Delete logs older than this during rotation
        otel_provider: Custom LoggerProvider (auto-created if None with exporter)
        otel_exporter: Log exporter (ConsoleLogExporter, OTLPLogExporter, etc.)
        resource: OTel Resource with service attributes
    """
    
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
        otel_exporter: Any | None = None,
        resource: Resource | None = None,
    ):
        super().__init__()
        
        # File logging setup (preserved for local debugging)
        self._logfile_base = logfile
        self._rotate_interval = rotate_interval
        self._max_log_age = max_log_age
        self._lock_timeout = lock_timeout
        self._lock_poll_interval = lock_poll_interval
        # ... existing file rotation state ...
        
        # OTel setup
        self._resource = resource or Resource.create({
            "service.name": "rxplus",
        })
        
        if otel_provider:
            self._otel_provider = otel_provider
        elif otel_exporter:
            self._otel_provider = LoggerProvider(resource=self._resource)
            self._otel_provider.add_log_record_processor(
                BatchLogRecordProcessor(otel_exporter)
            )
        else:
            self._otel_provider = None
        
        self._otel_logger = (
            self._otel_provider.get_logger("rxplus.logger")
            if self._otel_provider else None
        )
    
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
    
    def on_error(self, error: Exception) -> None:
        """Convert errors to LogRecords and forward."""
        source = error.source if isinstance(error, RxException) else "Unknown"
        record = create_log_record(str(error), "ERROR", source=source)
        self.on_next(record)
```

### 4. Updated LogComp Interface

```python
class LogComp(ABC):
    """
    Abstract base for components that emit logs into reactive streams.
    """
    
    @abstractmethod
    def set_super(self, obs: rx.abc.ObserverBase | Callable) -> None: ...
    
    @abstractmethod
    def log(
        self, 
        body: Any, 
        level: LOG_LEVEL = "INFO",
        attributes: dict[str, Any] | None = None,
    ) -> None: ...
    
    @abstractmethod
    def get_rx_exception(self, error: Exception, note: str = "") -> RxException: ...


class NamedLogComp(LogComp):
    """LogComp implementation with a named source."""
    
    def __init__(self, name: str = "LogSource"):
        self.name = name
        self.super_obs: rx.abc.ObserverBase | Callable | None = None
    
    def set_super(self, obs: rx.abc.ObserverBase | Callable) -> None:
        self.super_obs = obs
    
    def log(
        self,
        body: Any,
        level: LOG_LEVEL = "INFO",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Emit a LogRecord to the super observer."""
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
            self.super_obs(record)
    
    def get_rx_exception(self, error: Exception, note: str = "") -> RxException:
        return RxException(error, source=self.name, note=note)
```

### 5. Helper Functions

```python
def configure_otel_logging(
    service_name: str = "rxplus",
    service_version: str = "",
    otlp_endpoint: str | None = None,
    otlp_insecure: bool = True,
    console_export: bool = False,
) -> LoggerProvider:
    """
    Configure OTel logging with common exporters.
    
    Args:
        service_name: Service identifier for resource attributes
        service_version: Service version for resource attributes
        otlp_endpoint: OTLP collector endpoint (e.g., "localhost:4317")
        otlp_insecure: Use insecure connection for OTLP
        console_export: Enable console output for debugging
    
    Returns:
        Configured LoggerProvider ready for use with Logger
    
    Example:
        provider = configure_otel_logging(
            service_name="my-app",
            otlp_endpoint="localhost:4317",
            console_export=True,
        )
        logger = Logger(otel_provider=provider)
    """
    resource = Resource.create({
        "service.name": service_name,
        "service.version": service_version,
    })
    provider = LoggerProvider(resource=resource)
    
    if console_export:
        from opentelemetry.sdk._logs.export import ConsoleLogExporter
        provider.add_log_record_processor(
            BatchLogRecordProcessor(ConsoleLogExporter())
        )
    
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=otlp_endpoint, insecure=otlp_insecure)
            )
        )
    
    return provider
```

### 6. Files Changed

| File | Changes | Lines |
|------|---------|-------|
| `rxplus/logging.py` | Remove `LogItem`, add OTel imports, `SEVERITY_MAP`, `create_log_record()`, `format_log_record()`, update operators, update `Logger`, update `LogComp` | 1-456 (full rewrite) |
| `rxplus/__init__.py` | Update exports: remove `LogItem`, add `LogRecord`, `create_log_record`, `SEVERITY_MAP`, `configure_otel_logging` | TBD |
| `tests/test_logging.py` | Rewrite all tests for `LogRecord`-based API | 1-179 (full rewrite) |
| `docs/logging.md` | Full rewrite documenting OTel-native API | 1-111 (full rewrite) |
| `pyproject.toml` | Add OTel dependencies as required | TBD |
| `requirements.txt` | Add OTel packages | TBD |

## Implementation Plan

### Phase 1: Dependencies & Imports
1. Add `opentelemetry-sdk`, `opentelemetry-exporter-otlp` as required dependencies in `pyproject.toml`
2. Update `requirements.txt` with OTel packages
3. Add OTel imports to `logging.py`

### Phase 2: Core Data Model
1. Remove `LogItem` class entirely
2. Define `SEVERITY_MAP` constant
3. Implement `create_log_record()` factory function
4. Implement `format_log_record()` for string output

### Phase 3: Update Operators
1. Rewrite `log_filter()` to check `LogRecord` and `severity_number`
2. Rewrite `drop_log()` to filter `LogRecord` instances
3. Rewrite `log_redirect_to()` to handle `LogRecord`
4. Remove `keep_log()` decorator (no longer needed)

### Phase 4: Update Logger
1. Add OTel parameters to `Logger.__init__` (`otel_provider`, `otel_exporter`, `resource`)
2. Implement `LoggerProvider` setup with `BatchLogRecordProcessor`
3. Update `on_next()` to emit `LogRecord` to OTel logger
4. Update file writing to use `format_log_record()`
5. Update `on_error()` to create `LogRecord` via `create_log_record()`
6. Preserve file rotation and locking logic

### Phase 5: Update LogComp Interface
1. Update `LogComp.log()` signature to accept `attributes` parameter
2. Update `NamedLogComp.log()` to create `LogRecord` via `create_log_record()`
3. Update `EmptyLogComp` to match new interface

### Phase 6: Exports & Helpers
1. Implement `configure_otel_logging()` helper function
2. Update `rxplus/__init__.py` exports
3. Re-export `LogRecord` from OTel SDK

### Phase 7: Documentation & Tests
1. Rewrite `docs/logging.md` with new API documentation
2. Rewrite all tests in `tests/test_logging.py`
3. Add new tests for OTel export functionality

## Testing Strategy

### Unit Tests
- `test_create_log_record_default`: Verify factory creates LogRecord with INFO severity
- `test_create_log_record_all_levels`: Verify each LOG_LEVEL maps to correct SeverityNumber
- `test_create_log_record_attributes`: Verify custom attributes merge with log.source
- `test_create_log_record_timestamp`: Verify timestamp is populated in nanoseconds
- `test_format_log_record`: Verify string output format matches expected pattern
- `test_log_filter_by_level`: Verify filtering by severity_number works
- `test_log_filter_multiple_levels`: Verify filtering with multiple levels
- `test_drop_log_removes_records`: Verify LogRecords are dropped, other items pass
- `test_log_redirect_to_observer`: Verify LogRecords redirect to target observer
- `test_log_redirect_to_callable`: Verify LogRecords redirect to callable
- `test_logger_emits_to_otel`: Verify Logger emits to OTel provider (mock exporter)
- `test_logger_writes_to_file`: Verify Logger writes formatted output to file
- `test_logger_file_rotation`: Verify file rotation by count
- `test_logger_file_rotation_by_time`: Verify file rotation by timedelta
- `test_logger_max_log_age_cleanup`: Verify old log cleanup
- `test_named_log_comp_creates_record`: Verify NamedLogComp creates LogRecord with source
- `test_named_log_comp_attributes`: Verify NamedLogComp passes attributes
- `test_configure_otel_logging_console`: Verify helper configures console exporter
- `test_configure_otel_logging_otlp`: Verify helper configures OTLP exporter

### Integration Tests
- `test_full_pipeline_otel_export`: End-to-end test with mock collector
- `test_full_pipeline_file_and_otel`: Verify both file and OTel export together
- `test_reactive_pipeline_with_log_operators`: Verify operators work in rx pipeline

## Observability

### Logging
- This IS the logging system; native OTel integration enables:
  - Structured attributes on every log
  - Trace context correlation (span_id, trace_id)
  - Export to any OTel-compatible backend

### Metrics
- Not applicable for this change

## Future Considerations

### Potential Enhancements
- Add `with_trace_context()` helper to inject current span context
- Support OTel Events (structured logs with event.name)
- Add OTLP file exporter for offline scenarios
- Integrate with OTel Tracing SDK for automatic span correlation
- Add log sampling for high-volume scenarios

### Known Limitations
- OTel packages add ~5MB to install size
- File logging uses custom format (not OTLP JSON)
- Trace context requires separate OTel tracing setup

## Dependencies

### Required
- `reactivex>=4.0.0` - Reactive extensions (existing)
- `opentelemetry-sdk>=1.20.0` - Core OTel SDK with logging support
- `opentelemetry-exporter-otlp>=1.20.0` - OTLP gRPC/HTTP exporters

### pyproject.toml update
```toml
[project]
dependencies = [
    "reactivex>=4.0.0",
    "opentelemetry-sdk>=1.20.0",
    "opentelemetry-exporter-otlp>=1.20.0",
]
```

## Security Considerations
- OTLP exporters transmit logs over network; use TLS for production endpoints
- Avoid logging sensitive data in body or attributes (PII, credentials, tokens)
- File permissions remain user-controlled (same as before)

## Rollout Strategy
- Release as major version bump (breaking change)
- Clear changelog documenting `LogItem` removal
- No deprecation period - clean break

## References
- OpenTelemetry Python SDK: https://opentelemetry-python.readthedocs.io/
- OTel Log Data Model: https://opentelemetry.io/docs/specs/otel/logs/data-model/
- OTel Severity Numbers: https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/logs/data-model.md#field-severitynumber
- OTel Python Logs Example: https://opentelemetry-python.readthedocs.io/en/stable/examples/logs/
