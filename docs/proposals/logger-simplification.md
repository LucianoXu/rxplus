# Proposal: Logger Simplification - API LogRecord as First-Class Citizen

## Context

- **Request**: Simplify the logging architecture by making API LogRecord the first-class citizen in reactive pipelines.
- **Research Source**: [logging.py](../../rxplus/logging.py), [otel-logging-integration.md](../plans/otel-logging-integration.md)

## Design Philosophy

**API LogRecord is the first-class citizen in the reactive pipeline.** LogRecords flow through reactive streams as data, and are transformed into SDK LogRecords at the LogSink for export.

Key principles:
1. LogRecords should contain complete meta-information when created
2. LogSink is an observer that transforms API LogRecords → SDK emission
3. File writing is handled by exporters, not by LogSink itself
4. Trace context is always present (generated if not available)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Application                               │
└─────────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│ Component A     │  │ Component B     │  │ Component C     │
│ create_log_     │  │ create_log_     │  │ create_log_     │
│ record(...)     │  │ record(...)     │  │ record(...)     │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │                    │                    │
         │     API LogRecords (first-class)        │
         └────────────────────┴────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              │     Reactive Pipeline          │
              │   • log_filter()               │
              │   • log_redirect_to()          │
              │   • drop_log()                 │
              └───────────────┬───────────────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │          LogSink              │
              │  (Observer, transforms to SDK │
              │   and emits via Provider)     │
              │                               │
              │  provider.get_logger().emit() │
              └───────────────┬───────────────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │       LoggerProvider          │
              │  • BatchLogRecordProcessor    │
              │    ├── OTLPLogExporter        │
              │    ├── ConsoleLogExporter     │
              │    └── FileLogExporter (user) │
              └───────────────────────────────┘
```

---

## Implementation Details

### 1. `create_log_record` - Always Include Trace

LogRecords must always have trace context. If no span context is available, generate a random trace_id and span_id:

```python
def create_log_record(
    body: Any,
    level: LOG_LEVEL = "INFO",
    source: str = "Unknown",
    attributes: dict[str, Any] | None = None,
) -> LogRecord:
    """
    Create an OpenTelemetry LogRecord with complete meta-information.
    
    Always includes trace context - uses current span if available,
    otherwise generates random trace_id and span_id.
    """
    merged_attributes = {"log.source": source, **(attributes or {})}

    # Always include trace context
    span_ctx = get_current_span()
    if span_ctx is not None:
        merged_attributes["trace_id"] = span_ctx.trace_id
        merged_attributes["span_id"] = span_ctx.span_id
        if span_ctx.parent_span_id:
            merged_attributes["parent_span_id"] = span_ctx.parent_span_id
    else:
        # Generate random trace context for standalone logs
        merged_attributes["trace_id"] = generate_trace_id()
        merged_attributes["span_id"] = generate_span_id()

    return LogRecord(...)
```

### 2. `LogSink` - Simple Observer

LogSink becomes a simple observer that:
- Receives API LogRecords from reactive streams
- Emits them via the OTel LoggerProvider
- Converts errors to LogRecords (never terminates stream)

No file writing - that's the responsibility of exporters.

```python
class LogSink(Subject):
    """
    LogSink is an Observer that emits API LogRecords to OTel LoggerProvider.
    
    LogSink transforms API LogRecords into SDK emissions via the provider.
    File output should be handled by adding appropriate exporters to the provider.
    
    Example:
        >>> provider = configure_otel_logging(console_export=True)
        >>> sink = LogSink(otel_provider=provider)
        >>> source.pipe(log_redirect_to(sink)).subscribe()
    """
    
    def __init__(
        self,
        otel_provider: LoggerProvider | None = None,
        otel_exporter: LogRecordExporter | None = None,
        resource: Resource | None = None,
    ):
        super().__init__()
        # Setup provider and logger
        ...
    
    def on_next(self, value: Any) -> None:
        if isinstance(value, LogRecord):
            if self._otel_logger is not None:
                self._otel_logger.emit(value)
            super().on_next(value)
```

### 3. File Logging via Exporters

Users who need file logging should:
1. Use or implement a `FileLogExporter`
2. Add it to the LoggerProvider via `BatchLogRecordProcessor`

This follows standard OTel patterns and keeps LogSink simple.

---

## Migration

No backward compatibility needed. Direct implementation:

1. Remove `include_trace` parameter from `create_log_record`
2. Always generate trace context if not available
3. Remove all file-related code from `Logger`/`LogSink`
4. Rename `Logger` to `LogSink`
5. Update tests accordingly

---

## Benefits

1. **Simplicity**: LogSink is just an observer, not a file manager
2. **Consistency**: All LogRecords have trace context
3. **OTel Compliance**: File output follows exporter pattern
4. **Testability**: Easier to test without file system dependencies
5. **Separation of Concerns**: Creation, routing, and export are separate

