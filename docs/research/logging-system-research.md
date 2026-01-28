# Logging System Research

This document provides a comprehensive analysis of how logging is utilized throughout the rxplus codebase.

## Summary

The rxplus logging system is built on **OpenTelemetry's LogRecord data model**, enabling structured, trace-correlated logging that flows through reactive pipelines. The architecture centers on API-level `LogRecord` objects as first-class citizens in reactive streams, with `LogSink` serving as the bridge to OTel SDK exporters. Key features include automatic trace context injection, pickle-serializable log records for WebSocket transport, file-based export with rotation, and reactive operators for filtering and redirection.

---

## Core Components

### 1. Data Model: OTel LogRecord

**File:** [rxplus/logging.py](../rxplus/logging.py#L22-L52)

The logging system uses OpenTelemetry's `LogRecord` directly, with severity mapping:

| LOG_LEVEL | SeverityNumber |
|-----------|----------------|
| TRACE     | TRACE (1)      |
| DEBUG     | DEBUG (5)      |
| INFO      | INFO (9)       |
| WARN      | WARN (13)      |
| ERROR     | ERROR (17)     |
| FATAL     | FATAL (21)     |

```python
LOG_LEVEL = Literal["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]

SEVERITY_MAP: dict[str, SeverityNumber] = {
    "TRACE": SeverityNumber.TRACE,
    "DEBUG": SeverityNumber.DEBUG,
    "INFO": SeverityNumber.INFO,
    "WARN": SeverityNumber.WARN,
    "ERROR": SeverityNumber.ERROR,
    "FATAL": SeverityNumber.FATAL,
}
```

### 2. LogRecord Factory: `create_log_record()`

**File:** [rxplus/logging.py](../rxplus/logging.py#L58-L142)

The primary factory function creates OTel LogRecords with rxplus conventions:

**Parameters:**
- `body: Any` — Log message content
- `level: LOG_LEVEL` — Severity level (default: "INFO")
- `source: str` — Component name stored in `log.source` attribute
- `attributes: dict` — Additional structured key-value pairs

**Trace Context Behavior:**
- If within a span (via `start_span()`), inherits `trace_id` and `span_id`
- Otherwise, generates random 128-bit trace_id and 64-bit span_id
- Parent span ID stored in attributes when nested

**Returns:** `RxLogRecord` (pickle-friendly subclass)

### 3. RxLogRecord: Pickle-Serializable LogRecord

**File:** [rxplus/logging.py](../rxplus/logging.py#L807-L854)

Standard OTel LogRecords cannot be pickled due to their context object. `RxLogRecord` implements `__reduce__` for serialization:

```python
class RxLogRecord(LogRecord):
    def __reduce__(self):
        state = {
            'timestamp': self.timestamp,
            'trace_id': self.trace_id,
            'span_id': self.span_id,
            # ... all fields serialized
        }
        return (_reconstruct_log_record, (state,))
```

**Key Detail:** Deserialized records have `is_remote=True` in their span context to indicate wire transport.

### 4. LogSink: OTel Export Bridge

**File:** [rxplus/logging.py](../rxplus/logging.py#L596-L706)

`LogSink` is a `Subject` that:
1. Receives API `LogRecord` objects via `on_next()`
2. Maps `log.source` attribute to OTel instrumentation scopes (e.g., `"rxplus.HTTPServer"`)
3. Caches SDK loggers per source for efficiency
4. Emits records to OTel LoggerProvider for export
5. Forwards records to Rx subscribers
6. Converts errors to LogRecords (never terminates stream)

**Constructor Options:**
- `otel_provider: LoggerProvider` — Custom provider
- `otel_exporter: LogRecordExporter` — Auto-creates provider if provided
- `resource: Resource` — OTel resource attributes
- `sync_export: bool` — True for `SimpleLogRecordProcessor`, False for `BatchLogRecordProcessor`

### 5. FileLogRecordExporter

**File:** [rxplus/logging.py](../rxplus/logging.py#L221-L492)

OTel-compliant file exporter with advanced features:

| Feature | Implementation |
|---------|----------------|
| Format | `"text"` (human-readable) or `"json"` (structured) |
| Rotation | By record count (`int`) or time interval (`timedelta`) |
| Cleanup | Auto-delete logs older than `max_log_age` |
| Locking | Cross-process file locking via `fcntl` (POSIX) |
| Timestamp | Files named `base_YYYYMMDDTHHmmss.ext` |

### 6. Reactive Operators

**File:** [rxplus/logging.py](../rxplus/logging.py#L499-L590)

| Operator | Purpose | Signature |
|----------|---------|-----------|
| `log_filter(levels)` | Keep only LogRecords matching severity levels | `set[LOG_LEVEL] -> Observable -> Observable` |
| `drop_log()` | Remove all LogRecords from stream | `Observable -> Observable` |
| `log_redirect_to(observer, levels)` | Route matching logs to observer, forward others | `(Observer, set[LOG_LEVEL]) -> Observable -> Observable` |

---

## Integration Points

### WebSocket Components (ws.py)

**File:** [rxplus/ws.py](../rxplus/ws.py#L21-L499)

Both `RxWSServer` and `RxWSClient` use logging internally:

1. **Internal `_log()` method** ([ws.py#L496-L499](../rxplus/ws.py#L496-L499)):
   ```python
   def _log(self, body: str, level: LOG_LEVEL = "INFO") -> None:
       record = create_log_record(body, level, source=self._source)
       super().on_next(record)
   ```

2. **Log Emission Points:**
   - Client connection/disconnection events
   - Send/receive failures
   - Reconnection attempts
   - Server shutdown events

3. **LogRecord Transport:**
   - `WSObject` datatype enables pickle serialization
   - `RxLogRecord` enables LogRecords to traverse WebSocket boundaries
   - Trace context preserved through serialization

**Usage in [task_wsserver.py](../tasks/task_wsserver.py#L53-L65):**
```python
exporter = FileLogRecordExporter("wsserver_logs.txt", format="json")
log_sink = LogSink(otel_exporter=exporter)

sender.pipe(
    stream_print_out(prompt="[STREAM PRINT] "),
    log_redirect_to(log_sink),
    untag()
).subscribe(log_sink)
```

### Operators Module (opt.py)

**File:** [rxplus/opt.py](../rxplus/opt.py#L93-L118)

`error_restart_signal_to_logitem()` converts `ErrorRestartSignal` to LogRecord:

```python
def error_restart_signal_to_logitem(log_source: str) -> Callable[[Observable], Observable]:
    def _op(source: Observable[Any]) -> Observable[Any]:
        def _subscribe(observer, scheduler=None):
            def _on_next(v: Any):
                if isinstance(v, ErrorRestartSignal):
                    log_record = create_log_record(
                        body=str(v),
                        level="WARN",
                        source=log_source,
                    )
                    observer.on_next(log_record)
                else:
                    observer.on_next(v)
            # ...
```

### Utility: keep_log Decorator (ws.py)

**File:** [rxplus/ws.py](../rxplus/ws.py#L26-L32)

Preserves LogRecords while applying functions to other items:

```python
def keep_log(func):
    """Decorator that preserves LogRecords while applying func to other items."""
    from opentelemetry._logs import LogRecord
    def wrapper(item):
        if isinstance(item, LogRecord):
            return item
        return func(item)
    return wrapper
```

---

## Trace Context System

### Mechanism Module

**File:** [rxplus/mechanism.py](../rxplus/mechanism.py#L23-L175)

Provides trace context via `ContextVar`:

| Component | Purpose |
|-----------|---------|
| `SpanContext` | Immutable dataclass with `trace_id`, `span_id`, `parent_span_id` |
| `start_span()` | Context manager creating new spans, inheriting trace_id from parent |
| `get_current_span()` | Returns current `SpanContext` from contextvars |
| `TraceContext` | Class for managing traces across multiple operations |

**Integration with Logging:**
- `create_log_record()` calls `get_current_span()` to inject trace context
- Trace IDs stored as integers on LogRecord fields for OTel compatibility
- Parent span IDs stored in attributes for nested span tracking

---

## Public API Exports

**File:** [rxplus/__init__.py](../rxplus/__init__.py#L16-L31)

Exported logging symbols:
```python
from .logging import (
    LOG_FORMAT,
    LOG_LEVEL,
    SEVERITY_MAP,
    FileLogRecordExporter,
    LogSink,
    RxLogRecord,
    configure_otel_logging,
    create_log_record,
    drop_log,
    format_log_record,
    format_log_record_json,
    log_filter,
    log_redirect_to,
)
from opentelemetry.sdk._logs._internal import LogRecord
```

---

## Task File Usage Patterns

### Pattern 1: WebSocket Server with File Export

**File:** [tasks/task_wsserver.py](../tasks/task_wsserver.py)

```python
from rxplus import (
    create_log_record, format_log_record, start_span, drop_log,
    LogSink, FileLogRecordExporter, log_redirect_to
)

exporter = FileLogRecordExporter("wsserver_logs.txt", format="json")
log_sink = LogSink(otel_exporter=exporter)

sender.pipe(
    log_redirect_to(log_sink),
    untag()
).subscribe(log_sink)

# Create LogRecords within spans for trace correlation
with start_span() as span:
    log = create_log_record(
        body=f"Server message #{i}",
        level="INFO",
        source="WSServer",
        attributes={"message_id": i},
    )
    sender.on_next(TaggedData("/", log))
```

### Pattern 2: WebSocket Client with OTLP Export

**File:** [tasks/task_wsclient.py](../tasks/task_wsclient.py)

```python
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

loki_exporter = OTLPLogExporter(endpoint=parsed_args.loki_endpoint)
log_sink = LogSink(otel_exporter=loki_exporter)

receiver.subscribe(log_sink)  # All received LogRecords exported to Loki

with start_span() as span:
    log = create_log_record(
        body=f"Client ping #{i}",
        level="DEBUG",
        source="WSClient",
    )
    receiver.on_next(log)
```

### Pattern 3: Audio/Video Tasks with log_filter

**Files:** [tasks/task_mic_server.py](../tasks/task_mic_server.py), [tasks/task_speaker_client.py](../tasks/task_speaker_client.py)

```python
from rxplus import log_filter, drop_log

# Filter to only process specific log levels
sender.pipe(log_filter()).subscribe(print)

# Or drop logs entirely from data stream
client.pipe(
    drop_log(),
    # ... process non-log data
)
```

---

## Test Coverage

**File:** [tests/test_logging.py](../tests/test_logging.py)

Comprehensive test suite covering:

| Test Area | Coverage |
|-----------|----------|
| `create_log_record` | Default values, all levels, source, attributes, timestamps |
| `format_log_record` | Text format pattern, trace prefix |
| `format_log_record_json` | JSON structure, trace context, attributes |
| `log_filter` | Single/multiple levels, non-LogRecord filtering |
| `drop_log` | LogRecord removal, other item passthrough |
| `log_redirect_to` | Observer/callable targets, level filtering |
| `LogSink` | Forwarding, error conversion, scope mapping, caching |
| `RxLogRecord` | Pickle roundtrip, trace preservation, TaggedData |
| `FileLogRecordExporter` | Text/JSON formats, rotation, directory creation |
| Trace integration | Span injection, parent tracking, formatting |

---

## Configuration Helper

**File:** [rxplus/logging.py](../rxplus/logging.py#L710-L766)

`configure_otel_logging()` simplifies provider setup:

```python
provider = configure_otel_logging(
    service_name="my-app",
    service_version="1.0.0",
    otlp_endpoint="localhost:4317",  # Optional OTLP collector
    otlp_insecure=True,
    console_export=True,             # Debug to console
)
sink = LogSink(otel_provider=provider)
```

---

## Architecture Decisions

**File:** [ADR/2026-01-27-logging-philosophy.md](../ADR/2026-01-27-logging-philosophy.md)

> "I decided to keep API LogRecord as the first-class citizen, as the observable data in the reactive pipeline. This makes the framework more general and avoids drastic refactorization. They are emitted to the OTel SDK in LogSink."

**Design Plan:** [docs/plans/otel-logging-integration.md](../docs/plans/otel-logging-integration.md)

Key decisions:
1. **Direct OTel LogRecord usage** — No wrapper class, cleanest ecosystem integration
2. **Batch export via LoggerProvider** — Non-blocking for reactive streams
3. **Severity mapping** — Direct mapping per OTel Log Data Model specification
4. **No backward compatibility** — Clean break from previous `LogItem` implementation

---

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     Application Code                             │
│  create_log_record("msg", "INFO", source="MyComp", attributes={})│
└──────────────────────────────┬──────────────────────────────────┘
                               │ Returns RxLogRecord
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Reactive Pipeline                             │
│  source.pipe(                                                    │
│      log_redirect_to(log_sink, {"INFO", "ERROR"}),              │
│      drop_log()                                                  │
│  ).subscribe(process_data)                                       │
└──────────────┬────────────────────────────────────┬─────────────┘
               │ LogRecords                         │ Other data
               ▼                                    ▼
┌──────────────────────────┐              ┌─────────────────────┐
│        LogSink           │              │   Data Processing   │
│  - Maps source to scope  │              └─────────────────────┘
│  - Caches SDK loggers    │
│  - Emits to provider     │
└──────────────┬───────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                   OTel LoggerProvider                            │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────┐  │
│  │ ConsoleExporter  │  │  OTLPExporter    │  │ FileExporter  │  │
│  │   (debug)        │  │ (Grafana/Loki)   │  │  (rotation)   │  │
│  └──────────────────┘  └──────────────────┘  └───────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Cross-Component Connections

1. **mechanism.py → logging.py**: Trace context (`get_current_span()`) injected into LogRecords
2. **logging.py → ws.py**: `create_log_record` used for internal WebSocket event logging
3. **ws.py → logging.py**: `RxLogRecord` enables LogRecord pickle transport over WebSocket
4. **opt.py → logging.py**: `ErrorRestartSignal` converted to LogRecords for observability
5. **tasks/* → logging.py**: Demo tasks show FileLogRecordExporter and OTLP export patterns
