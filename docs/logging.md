# Logging

OpenTelemetry-native structured logging that flows through reactive pipelines.

## Design Intention

Traditional loggers write directly to files or consoles, which doesn't compose well with reactive streams. `rxplus` logging uses OpenTelemetry's `LogRecord` as the native data model, allowing log entries to flow through reactive pipelines while being fully compatible with the OTel observability ecosystem.

Key benefits:
- **Native OTel integration** — Export to any OTel-compatible backend (Jaeger, Grafana, etc.)
- **Structured attributes** — Attach rich metadata to every log record
- **Trace correlation** — Link logs to distributed traces
- **Reactive composition** — Filter, redirect, and process logs using Rx operators

## API

```python
from rxplus import (
    # Core types
    LogRecord,          # OpenTelemetry LogRecord (re-exported)
    LOG_LEVEL,          # Literal type for level strings
    SEVERITY_MAP,       # Maps LOG_LEVEL to SeverityNumber
    
    # Factory functions
    create_log_record,  # Create LogRecord with rxplus conventions
    format_log_record,  # Format LogRecord as human-readable string
    configure_otel_logging,  # Configure OTel exporters
    
    # Reactive operators
    log_filter,
    log_redirect_to,
    drop_log,
    
    # Components
    Logger,
)
```

## Creating Log Records

Use `create_log_record()` to create OpenTelemetry LogRecords with rxplus conventions:

```python
from rxplus import create_log_record

# Basic usage
record = create_log_record("Server started", "INFO", source="HTTPServer")

# With structured attributes
record = create_log_record(
    "Request processed",
    "DEBUG",
    source="API",
    attributes={"request_id": "abc123", "duration_ms": 42},
)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `body` | `Any` | — | Log message (any serializable type) |
| `level` | `LOG_LEVEL` | `"INFO"` | `"TRACE"`, `"DEBUG"`, `"INFO"`, `"WARN"`, `"ERROR"`, `"FATAL"` |
| `source` | `str` | `"Unknown"` | Component name (stored in `log.source` attribute) |
| `attributes` | `dict` | `None` | Additional structured key-value attributes |
| `resource` | `Resource` | `None` | OTel Resource for service identification |

### Severity Mapping

Log levels map to OTel SeverityNumber:

| LOG_LEVEL | SeverityNumber |
|-----------|----------------|
| `TRACE` | TRACE (1) |
| `DEBUG` | DEBUG (5) |
| `INFO` | INFO (9) |
| `WARN` | WARN (13) |
| `ERROR` | ERROR (17) |
| `FATAL` | FATAL (21) |

## Logger

A `Subject` that processes `LogRecord` entries, optionally writing to files and exporting via OTel.

```python
from datetime import timedelta
from rxplus import Logger, create_log_record

# Basic file logging (creates timestamped file: app_20250120T103045.log)
logger = Logger(logfile="app.log")
logger.subscribe(lambda record: print(record.body))
logger.on_next(create_log_record("Started", "INFO", source="Main"))

# With rotation every 1000 records
logger = Logger(logfile="app.log", rotate_interval=1000)

# With time-based rotation and cleanup
logger = Logger(
    logfile="app.log",
    rotate_interval=timedelta(hours=1),
    max_log_age=timedelta(days=7),
)
```

### OTel Export

Export logs to OTel collectors or console:

```python
from opentelemetry.sdk._logs.export import ConsoleLogExporter
from rxplus import Logger, create_log_record, configure_otel_logging

# Using an exporter directly
logger = Logger(
    logfile="app.log",
    otel_exporter=ConsoleLogExporter(),
)

# Using configure_otel_logging helper
provider = configure_otel_logging(
    service_name="my-service",
    service_version="1.0.0",
    otlp_endpoint="localhost:4317",  # OTLP collector
    console_export=True,             # Also print to console
)
logger = Logger(otel_provider=provider)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `logfile` | `str \| None` | `None` | Base path for log files (timestamped) |
| `rotate_interval` | `int \| timedelta \| None` | `None` | When to rotate: `int` = N records, `timedelta` = time elapsed |
| `max_log_age` | `timedelta \| None` | `None` | Delete older logs during rotation |
| `lock_timeout` | `float` | `10.0` | Timeout for acquiring file lock |
| `lock_poll_interval` | `float` | `0.05` | Poll interval when waiting for lock |
| `otel_provider` | `LoggerProvider \| None` | `None` | Custom OTel LoggerProvider |
| `otel_exporter` | `LogExporter \| None` | `None` | OTel exporter (auto-creates provider) |
| `resource` | `Resource \| None` | `None` | OTel Resource with service attributes |

**Features:**

- Emits to OTel LoggerProvider for export to collectors
- Optional file logging with rotation for local debugging
- Cross-process file locking (fcntl on POSIX, lockfile fallback on Windows)
- Errors converted to LogRecords (never terminates the stream)
- `on_completed()` is a no-op to keep the logger alive

## Reactive Operators

| Operator | Purpose |
|----------|---------|
| `log_filter(levels)` | Keep only `LogRecord`s matching specified severity levels |
| `log_redirect_to(observer, levels)` | Route matching logs to another observer, forward other items |
| `drop_log()` | Remove all `LogRecord`s from the stream |

### Example: Separate logs from data

```python
from rxplus import Logger, log_redirect_to, drop_log, create_log_record

logger = Logger(logfile="app.log")

stream.pipe(
    log_redirect_to(logger, {"WARN", "ERROR"}),
    drop_log(),  # only data flows downstream
).subscribe(process_data)
```

### Example: Filter by severity

```python
from rxplus import log_filter

# Only process ERROR and FATAL logs
error_stream = source.pipe(log_filter({"ERROR", "FATAL"}))
```

## Trace Context Integration

Log records can automatically include trace and span IDs for distributed tracing correlation. This is powered by the `rxplus` trace context system.

### Automatic Trace Injection

When you create log records within a span, trace IDs are automatically injected:

```python
from rxplus import create_log_record, start_span

with start_span() as span:
    record = create_log_record("Processing request", "INFO", source="API")
    # record.attributes now contains:
    # - "trace_id": span.trace_id (32 hex chars)
    # - "span_id": span.span_id (16 hex chars)
    # - "parent_span_id": (if nested, 16 hex chars)
```

### Trace Context in Log Output

Formatted log output includes short trace/span prefixes for debugging:

```
[INFO] 2025-01-25T10:30:45Z [a1b2c3d4:12345678] API: Processing request
```

### Disabling Trace Injection

If you don't want trace context in a specific log record:

```python
record = create_log_record("Simple log", "INFO", include_trace=False)
```

### TraceContext for Long-Running Operations

For operations that span multiple async tasks or threads:

```python
from rxplus import TraceContext, create_log_record

trace = TraceContext()

# Start the root span
trace.start_root_span()

# All logs created will share the same trace_id
log = create_log_record("Starting work", "INFO", source="Worker")

with trace.span():
    log = create_log_record("In child span", "DEBUG", source="Worker")
```

## Configuration Helper

`configure_otel_logging()` simplifies OTel setup:

```python
from rxplus import configure_otel_logging, Logger

provider = configure_otel_logging(
    service_name="my-app",
    service_version="1.2.3",
    otlp_endpoint="localhost:4317",  # Optional: OTLP collector
    otlp_insecure=True,              # Use insecure connection
    console_export=True,             # Optional: console output
)

logger = Logger(otel_provider=provider)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `service_name` | `str` | `"rxplus"` | Service identifier for resource attributes |
| `service_version` | `str` | `""` | Service version for resource attributes |
| `otlp_endpoint` | `str \| None` | `None` | OTLP collector endpoint (e.g., "localhost:4317") |
| `otlp_insecure` | `bool` | `True` | Use insecure connection for OTLP |
| `console_export` | `bool` | `False` | Enable console output for debugging |

## Full Example

```python
import reactivex as rx
from rxplus import (
    Logger,
    create_log_record,
    configure_otel_logging,
    log_redirect_to,
    drop_log,
)

# Configure OTel with console export
provider = configure_otel_logging(
    service_name="data-processor",
    console_export=True,
)

# Create logger with both file and OTel export
logger = Logger(
    logfile="logs/app.log",
    otel_provider=provider,
)

# Create a processing pipeline
def process_item(item):
    # Processing logic...
    return item * 2

source = rx.interval(1.0).pipe(
    rx.operators.map(lambda i: create_log_record(f"Processing {i}", "DEBUG", source="Pipeline")),
    rx.operators.merge(rx.interval(1.0).pipe(rx.operators.map(lambda i: i))),
    log_redirect_to(logger),
    drop_log(),
    rx.operators.map(process_item),
)

source.subscribe(print)
```

## Migration from LogItem

If you're migrating from the old `LogItem`-based API:

| Old API | New API |
|---------|---------|
| `LogItem(msg, level, source)` | `create_log_record(body, level, source)` |
| `item.msg` | `record.body` |
| `item.level` | `record.severity_text` |
| `item.source` | `record.attributes["log.source"]` |
| `item.timestamp_str` | `format_log_record(record)` for string output |
| `keep_log(func)` | Removed (use direct filtering) |
