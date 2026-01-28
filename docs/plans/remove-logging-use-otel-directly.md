# Remove Custom Logging — Use OTel Directly

## Current Context

- **rxplus** has a custom logging module (`rxplus/logging.py`, 854 lines) providing:
  - `create_log_record()` factory wrapping OTel `LogRecord`
  - `RxLogRecord` pickle-serializable subclass
  - `LogSink` Subject bridging to OTel `LoggerProvider`
  - Reactive operators: `log_filter`, `drop_log`, `log_redirect_to`
  - `FileLogRecordExporter` with rotation
  - `configure_otel_logging()` helper

- **Custom trace context** in `rxplus/mechanism.py` (lines 23–229):
  - `SpanContext`, `start_span()`, `TraceContext` — duplicates OTel trace API

- **Components** (`RxWSServer`, `RxWSClient`) use `_log()` method emitting `LogRecord` via reactive stream

- **Pain points**:
  - Custom abstractions duplicate OTel SDK functionality
  - LogRecords flowing through reactive streams conflate telemetry with data
  - Maintenance burden of custom trace context parallel to OTel

## Requirements

### Functional Requirements

- **DELETE** `rxplus/logging.py` entirely
- **REMOVE** custom trace context from `mechanism.py` (keep only `RxException`)
- **ADD** `configure_telemetry()` helper for one-line OTel setup
- **MODIFY** `RxWSServer`, `RxWSClient`, `RxWSClientGroup` to accept optional `TracerProvider` and `LoggerProvider`
- **INSTRUMENT** key operations with OTel spans (e.g., `handle_client`, `connect_client`)
- **EMIT** `ErrorRestartSignal` as span events (not reactive log items)
- **UPDATE** all task files to use standard OTel patterns

### Non-Functional Requirements

- No backward compatibility — clean break
- Components operate silently when no providers are configured
- Standard OTel SDK instrumentation patterns
- Each reactive component treated as an "instrumentation scope"

## Design Decisions

### 1. Provider Injection over Global State

Will inject `TracerProvider` and `LoggerProvider` as optional constructor params because:
- Explicit configuration — users see what telemetry is active
- No global state — better for testing and multi-tenant
- Aligns with OTel best practices for dependency injection

### 2. Instrumentation Scope per Component

Will create one OTel logger/tracer per component instance with scope name based on component name:
- `RxWSServer::{host}:{port}` → scope `rxplus.RxWSServer.{host}:{port}`
- `RxWSClient::ws://{host}:{port}{path}` → scope `rxplus.RxWSClient.{uri}`

This enables backend filtering by component type and instance.

### 3. ErrorRestartSignal as Span Event

Will emit `ErrorRestartSignal` as a span event on the active span instead of converting to LogRecord:
- Events are semantically correct (point-in-time occurrence within a span)
- No log pollution in reactive streams
- Retry logic naturally belongs in trace context

### 4. Telemetry Helper Function

Will provide `configure_telemetry()` in new `rxplus/telemetry.py`:
- Creates and returns `TracerProvider` + `LoggerProvider`
- Accepts common exporter configurations
- Does NOT set global providers (keeps explicit injection pattern)

## Technical Design

### 1. Core Components

```python
# rxplus/telemetry.py (NEW FILE)

from typing import Optional
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, BatchSpanProcessor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import LogRecordExporter, BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

def configure_telemetry(
    service_name: str = "rxplus",
    service_version: str = "",
    span_exporter: SpanExporter | None = None,
    log_exporter: LogRecordExporter | None = None,
) -> tuple[TracerProvider, LoggerProvider]:
    """
    Configure OTel providers for rxplus components.
    
    Returns (tracer_provider, logger_provider) for injection into components.
    Does not set global providers — keeps telemetry explicit.
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
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    
    return tracer_provider, logger_provider
```

```python
# rxplus/ws.py — RxWSServer changes

from typing import Optional
from opentelemetry.trace import TracerProvider, Tracer, Span
from opentelemetry._logs import LoggerProvider, Logger, SeverityNumber

class RxWSServer(Subject):
    def __init__(
        self,
        conn_cfg: dict,
        datatype: ... = "string",
        ping_interval: Optional[float] = 30.0,
        ping_timeout: Optional[float] = 30.0,
        name: Optional[str] = None,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
    ):
        # ... existing init ...
        self._name = name or f"RxWSServer:{self.host}:{self.port}"
        
        # OTel instrumentation
        self._tracer: Tracer | None = (
            tracer_provider.get_tracer(f"rxplus.{self._name}")
            if tracer_provider else None
        )
        self._logger: Logger | None = (
            logger_provider.get_logger(f"rxplus.{self._name}")
            if logger_provider else None
        )
    
    def _log(self, body: str, level: str = "INFO") -> None:
        """Emit log via OTel logger (no-op if no provider)."""
        if self._logger is None:
            return
        from opentelemetry._logs import LogRecord
        record = LogRecord(
            timestamp=time.time_ns(),
            body=body,
            severity_text=level,
            severity_number=getattr(SeverityNumber, level, SeverityNumber.INFO),
        )
        self._logger.emit(record)
    
    async def handle_client(self, websocket: Any):
        """Instrumented client handler with span."""
        if self._tracer is None:
            await self._handle_client_impl(websocket)
            return
        
        with self._tracer.start_as_current_span("handle_client") as span:
            span.set_attribute("ws.path", _ws_path(websocket))
            span.set_attribute("ws.remote_address", str(websocket.remote_address))
            await self._handle_client_impl(websocket)
```

### 2. Data Models

```python
# rxplus/mechanism.py — SIMPLIFIED

class RxException(Exception):
    """Base class for all RxPlus exceptions."""
    
    def __init__(self, exception: Exception, source: str = "Unknown", note: str = ""):
        super().__init__(f"<{source}> {note}: {exception}")
        self.exception = exception
        self.source = source
        self.note = note
    
    def __str__(self):
        return f"<{self.source}> {self.note}: {self.exception}"

# All SpanContext, TraceContext, start_span, etc. DELETED
```

```python
# rxplus/opt.py — ErrorRestartSignal handling

from opentelemetry import trace

@dataclass
class ErrorRestartSignal:
    error: RxException
    attempts: int
    
    def __str__(self) -> str:
        return f"ErrorRestartSignal(attempts={self.attempts}, error={self.error})"
    
    def record_as_span_event(self) -> None:
        """Record this signal as a span event on the current span."""
        span = trace.get_current_span()
        if span.is_recording():
            span.add_event(
                "error_restart",
                attributes={
                    "attempts": self.attempts,
                    "error.type": type(self.error.exception).__name__,
                    "error.message": str(self.error),
                    "error.source": self.error.source,
                }
            )
```

### 3. Integration Points

**Component → OTel Provider Flow:**
```
User Code
    │
    ├── configure_telemetry(service_name, exporters)
    │         │
    │         ▼
    │   (TracerProvider, LoggerProvider)
    │
    └── RxWSServer(..., tracer_provider=tp, logger_provider=lp)
              │
              ├── _tracer.start_as_current_span("handle_client")
              │         │
              │         ▼
              │   BatchSpanProcessor → SpanExporter (OTLP/Console)
              │
              └── _logger.emit(LogRecord)
                        │
                        ▼
                  BatchLogRecordProcessor → LogExporter (OTLP/Console)
```

### 4. Files Changed

**DELETE:**
- `rxplus/logging.py` (lines 1-854)
- `tests/test_logging.py` (lines 1-825)
- `docs/logging.md` (lines 1-302)

**CREATE:**
- `rxplus/telemetry.py` (~60 lines) — `configure_telemetry()` helper

**MODIFY:**
- `rxplus/mechanism.py` (lines 1-229) — Remove lines 23-229, keep only `RxException`
- `rxplus/ws.py` (lines 1-1053):
  - Line 21: Remove `from .logging import ...`
  - Lines 25-32: Remove `keep_log()` decorator
  - Lines 213-260: Add `tracer_provider`, `logger_provider` params to `RxWSServer.__init__`
  - Lines 325-380: Wrap `handle_client` in span
  - Lines 496-500: Rewrite `_log()` to use OTel logger
  - Lines 580-640: Add params to `RxWSClient.__init__`
  - Lines 634-638: Rewrite `_log()` for client
  - Lines 710-780: Wrap `connect_client` in span
  - Similar changes for `RxWSClientGroup`
- `rxplus/opt.py` (lines 1-211):
  - Line 14: Remove `from .logging import create_log_record`
  - Lines 93-118: Remove `error_restart_signal_to_logitem()`
  - Lines 79-92: Add `record_as_span_event()` method to `ErrorRestartSignal`
- `rxplus/__init__.py` (lines 1-154):
  - Lines 16-31: Remove all logging imports
  - Lines 33-42: Remove trace context imports (keep RxException)
  - Lines 44: Remove `error_restart_signal_to_logitem` from opt imports
  - Lines 97-115: Remove logging items from `__all__`
  - Add new export: `configure_telemetry`
- `tasks/task_wsserver.py` (lines 1-95) — Use `configure_telemetry()` and OTel tracer
- `tasks/task_wsclient.py` (lines 1-95) — Use `configure_telemetry()` and OTel tracer
- `tasks/task_mic_server.py` — Remove `log_filter` usage
- `tasks/task_speaker_client.py` — Remove `log_filter` usage
- `tasks/task_jpeg_client.py` — Remove `log_filter` usage
- `tasks/task_wavfile_client.py` — Remove `log_filter` usage
- `tasks/task_wavfile_server.py` — Remove `drop_log` usage
- `tasks/task_cli.py` — Remove `drop_log` usage
- `docs/research/logging-system-research.md` — Mark as historical/archive
- `docs/proposals/remove-logging-use-otel-directly.md` — Update status to "Implemented"

## Implementation Plan

### Phase 1: Create Telemetry Module & Simplify Mechanism

1. Create `rxplus/telemetry.py` with `configure_telemetry()` function
2. Strip `rxplus/mechanism.py` to only contain `RxException`
3. Update `rxplus/__init__.py` to remove trace context exports, add `configure_telemetry`

### Phase 2: Modify WebSocket Components

1. Update `RxWSServer.__init__` with `tracer_provider`, `logger_provider` params
2. Rewrite `RxWSServer._log()` to use OTel SDK logger
3. Wrap `RxWSServer.handle_client()` in span
4. Apply same changes to `RxWSClient`
5. Apply same changes to `RxWSClientGroup`
6. Remove `keep_log()` decorator

### Phase 3: Update Operators

1. Add `record_as_span_event()` to `ErrorRestartSignal`
2. Remove `error_restart_signal_to_logitem()` function
3. Remove `from .logging import` from `opt.py`

### Phase 4: Delete Logging Module

1. Delete `rxplus/logging.py`
2. Delete `tests/test_logging.py`
3. Update `rxplus/__init__.py` to remove all logging exports

### Phase 5: Update Task Files

1. Update `task_wsserver.py` to use `configure_telemetry()` + provider injection
2. Update `task_wsclient.py` similarly
3. Remove `log_filter`, `drop_log` from other task files
4. Update CLI argument parsing for OTel endpoints

### Phase 6: Documentation

1. Delete `docs/logging.md`
2. Create new `docs/telemetry.md` with OTel integration guide
3. Update `README.md` to reflect API changes
4. Archive research documents

## Testing Strategy

### Unit Tests

**New Tests (in `tests/test_telemetry.py`):**
- `test_configure_telemetry_returns_providers` — Verify tuple of providers returned
- `test_configure_telemetry_with_exporters` — Verify processors attached
- `test_configure_telemetry_resource_attributes` — Verify service.name set

**Modified Tests (in `tests/test_ws.py`):**
- `test_rxwsserver_with_tracer_logs` — Verify logs emitted when provider set
- `test_rxwsserver_without_tracer_silent` — Verify no errors when providers None
- `test_rxwsserver_span_created_for_handle_client` — Verify span attributes

**Modified Tests (in `tests/test_opt.py`):**
- `test_error_restart_signal_span_event` — Verify span event recorded

### Integration Tests

- Run `task_wsserver` + `task_wsclient` with `ConsoleSpanExporter` + `ConsoleLogExporter`
- Verify logs and traces appear in Grafana stack via OTLP

## Observability

### Logging

Components emit OTel LogRecords when `logger_provider` is configured:
- `severity_text`: INFO, WARN, ERROR
- `body`: Human-readable message
- Attributes: component name, connection details

### Metrics

Not in scope for this change. Future work may add OTel Metrics.

### Traces

Key spans instrumented:
- `RxWSServer.handle_client` — per-connection span
- `RxWSClient.connect_client` — reconnection loop span
- Span events for `ErrorRestartSignal`

## Future Considerations

### Potential Enhancements

- Add `MeterProvider` injection for metrics
- Auto-instrumentation decorator for Rx operators
- Propagate trace context through WebSocket frames

### Known Limitations

- No automatic trace context propagation over WebSocket (user must serialize)
- `RxLogRecord` pickle serialization removed — users must handle if needed
- Reactive log stream operators (`log_filter`, etc.) no longer available

## Dependencies

**Required (existing):**
- `opentelemetry-api` >= 1.20.0
- `opentelemetry-sdk` >= 1.20.0

**Optional (for export):**
- `opentelemetry-exporter-otlp` — OTLP gRPC/HTTP export
- `opentelemetry-exporter-jaeger` — Jaeger export

## Security Considerations

None — telemetry is optional and user-configured. No sensitive data exposed by default.

## Rollout Strategy

Single breaking release:
1. Bump major version (e.g., 2.0.0)
2. Update changelog with migration guide
3. Mark old PyPI versions as deprecated

## References

- Proposal: [docs/proposals/remove-logging-use-otel-directly.md](../proposals/remove-logging-use-otel-directly.md)
- Research: [docs/research/logging-system-research.md](../research/logging-system-research.md)
- OTel Python Docs: https://opentelemetry-python.readthedocs.io/
- Key code:
  - `rxplus/logging.py:1-854` (to delete)
  - `rxplus/mechanism.py:23-229` (to delete)
  - `rxplus/ws.py:213-260` (RxWSServer.__init__)
  - `rxplus/ws.py:496-500` (RxWSServer._log)
  - `rxplus/ws.py:580-640` (RxWSClient.__init__)
  - `rxplus/opt.py:79-118` (ErrorRestartSignal)
