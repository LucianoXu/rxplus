# Telemetry Integration

rxplus integrates directly with OpenTelemetry (OTel) SDK for observability. This document describes how to configure telemetry for rxplus components.

## Overview

rxplus uses **provider injection** pattern for telemetry. Components like `RxWSServer`, `RxWSClient`, and `RxWSClientGroup` accept optional `tracer_provider` and `logger_provider` parameters. When provided, these components will emit spans and logs via the OTel SDK.

## Quick Start

```python
from rxplus import RxWSServer, RxWSClient, configure_telemetry

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# Configure OTel providers with OTLP exporters
log_exporter = OTLPLogExporter(endpoint="http://localhost:4318/v1/logs")
span_exporter = OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")

tracer_provider, logger_provider = configure_telemetry(
    log_exporter=log_exporter,
    span_exporter=span_exporter,
)

# Create server with telemetry
server = RxWSServer(
    {"host": "localhost", "port": 8888},
    tracer_provider=tracer_provider,
    logger_provider=logger_provider,
)

# Create client with telemetry
client = RxWSClient(
    {"host": "localhost", "port": 8888},
    tracer_provider=tracer_provider,
    logger_provider=logger_provider,
)
```

## configure_telemetry()

The `configure_telemetry()` helper function provides a convenient way to set up OTel providers:

```python
from rxplus import configure_telemetry

# Basic setup (in-memory, for testing)
tracer_provider, logger_provider = configure_telemetry()

# With custom exporters
from opentelemetry.sdk._logs.export import ConsoleLogExporter
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

tracer_provider, logger_provider = configure_telemetry(
    log_exporter=ConsoleLogExporter(),
    span_exporter=ConsoleSpanExporter(),
)
```

**Parameters:**
- `service_name`: Service identifier for resource attributes (default: "rxplus")
- `service_version`: Service version for resource attributes
- `span_exporter`: Optional span exporter (e.g., `OTLPSpanExporter`, `ConsoleSpanExporter`)
- `log_exporter`: Optional log exporter (e.g., `OTLPLogExporter`, `ConsoleLogExporter`)

**Returns:**
- `tuple[TracerProvider, LoggerProvider]`: The configured providers

## Provider Injection

All WebSocket components accept telemetry providers:

### RxWSServer

```python
server = RxWSServer(
    conn_cfg={"host": "::", "port": 8888},
    tracer_provider=tracer_provider,
    logger_provider=logger_provider,
)
```

When providers are set:
- Server emits INFO logs for connection events
- WARN logs for recoverable errors
- ERROR logs for critical failures

### RxWSClient

```python
client = RxWSClient(
    conn_cfg={"host": "localhost", "port": 8888},
    tracer_provider=tracer_provider,
    logger_provider=logger_provider,
)
```

When providers are set:
- Client emits INFO logs for connection/reconnection events
- WARN logs for connection issues
- ERROR logs for fatal errors

### RxWSClientGroup

```python
group = RxWSClientGroup(
    conn_cfg={"host": "localhost", "port": 8888},
    tracer_provider=tracer_provider,
    logger_provider=logger_provider,
)
```

The `RxWSClientGroup` passes the providers to all child `RxWSClient` instances it creates.

## Manual Log Emission

For components outside of rxplus, you can emit logs directly using the OTel Logger:

```python
import time
from opentelemetry._logs import LogRecord, SeverityNumber

# Get a logger from the provider
logger = logger_provider.get_logger("my-component")

# Emit a log record
record = LogRecord(
    timestamp=time.time_ns(),
    body="Operation completed",
    severity_text="INFO",
    severity_number=SeverityNumber.INFO,
    attributes={"operation": "process_data", "items": 42},
)
logger.emit(record)
```

## Span Creation

For tracing operations, use the tracer:

```python
# Get a tracer from the provider
tracer = tracer_provider.get_tracer("my-component")

# Create spans around operations
with tracer.start_as_current_span("process_batch") as span:
    span.set_attribute("batch_size", 100)
    # ... do work ...
    span.set_status(StatusCode.OK)
```

## ErrorRestartSignal Integration

When using `retry_with_signal()` operator, you can record retry events as span events:

```python
from rxplus import ErrorRestartSignal, retry_with_signal
from reactivex import operators as ops

# Get a tracer
tracer = tracer_provider.get_tracer("my-pipeline")

# Handle ErrorRestartSignal in the pipeline
def handle_signal(item):
    if isinstance(item, ErrorRestartSignal):
        # Record as span event if in a span context
        current_span = tracer.start_span("retry_event")
        item.record_as_span_event(current_span)
        current_span.end()
    return item

observable.pipe(
    retry_with_signal(max_retries=3, delay_s=1.0),
    ops.map(handle_signal),
)
```

## No Telemetry Mode

If you don't need telemetry, simply omit the provider parameters:

```python
# No telemetry - components work normally without any overhead
server = RxWSServer({"host": "localhost", "port": 8888})
client = RxWSClient({"host": "localhost", "port": 8888})
```

## Shutdown

Always shutdown providers when your application exits to ensure all telemetry is flushed:

```python
try:
    # ... your application ...
except KeyboardInterrupt:
    pass
finally:
    tracer_provider.shutdown()
    logger_provider.shutdown()
```

## OTLP Export Example

A complete example with OTLP export to an OpenTelemetry Collector:

```python
import time
from rxplus import RxWSServer, TaggedData, configure_telemetry

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# Configure with OTLP exporters
OTLP_ENDPOINT = "http://localhost:4318"

tracer_provider, logger_provider = configure_telemetry(
    log_exporter=OTLPLogExporter(endpoint=f"{OTLP_ENDPOINT}/v1/logs"),
    span_exporter=OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT}/v1/traces"),
)

# Create server
server = RxWSServer(
    {"host": "::", "port": 8888},
    tracer_provider=tracer_provider,
    logger_provider=logger_provider,
)

# Subscribe to handle data
server.subscribe(
    on_next=lambda x: print(f"Received: {x}"),
    on_error=lambda e: print(f"Error: {e}"),
)

# Get tracer for custom spans
tracer = tracer_provider.get_tracer("my-server")

try:
    i = 0
    while True:
        time.sleep(1.0)
        with tracer.start_as_current_span("send_message"):
            server.on_next(TaggedData("/", f"Message {i}"))
        i += 1
except KeyboardInterrupt:
    server.on_completed()
    tracer_provider.shutdown()
    logger_provider.shutdown()
```
