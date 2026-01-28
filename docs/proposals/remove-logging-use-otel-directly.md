# Solution Proposals: Remove Custom Logging, Use OTel Directly

## Context

- **Request**: Completely remove the custom `logging.py` module and all logging-related utilities (`create_log_record`, `LogSink`, `log_filter`, `drop_log`, `log_redirect_to`, etc.). Replace with direct usage of OTel `TracerProvider` and `LoggerProvider`. Components like `RxWSServer`/`RxWSClient` should optionally accept providers for observability.

- **Research Source**: [docs/research/logging-system-research.md](../research/logging-system-research.md)
  - Key sections: Core Components, Integration Points, Cross-Component Connections

---

## Proposal 1 — Provider Injection with Optional Observability

### Overview

Remove `rxplus/logging.py` entirely and the custom trace context in `mechanism.py`. Components (`RxWSServer`, `RxWSClient`) accept optional `TracerProvider` and `LoggerProvider` parameters. When provided, internal events are emitted via OTel SDK loggers/tracers. When not provided, components operate silently (no observability). This keeps rxplus lightweight while enabling full OTel integration for users who want it.

### Key Changes

| File | Change |
|------|--------|
| `rxplus/logging.py` | **DELETE** entirely |
| `rxplus/mechanism.py` | Remove `SpanContext`, `start_span()`, `TraceContext`; keep only `RxException` |
| `rxplus/ws.py` | Replace `_log()` with OTel `logger.emit()`; add `tracer`/`logger_provider` params |
| `rxplus/opt.py` | Remove `error_restart_signal_to_logitem()`; errors become span events or OTel logs |
| `rxplus/__init__.py` | Remove all logging exports; export only core reactive utilities |
| `tests/test_logging.py` | **DELETE** entirely |
| `docs/logging.md` | **DELETE** or convert to "OTel Integration Guide" |

### Component API Changes

**Before:**
```python
class RxWSServer(Subject):
    def __init__(self, conn_cfg: dict, datatype=..., name=None):
        self._source = name or f"RxWSServer:{host}:{port}"
        # logs via create_log_record() + super().on_next()
```

**After:**
```python
from opentelemetry.trace import TracerProvider, get_tracer
from opentelemetry._logs import LoggerProvider, get_logger

class RxWSServer(Subject):
    def __init__(
        self,
        conn_cfg: dict,
        datatype=...,
        name: str | None = None,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
    ):
        self._name = name or f"RxWSServer:{host}:{port}"
        self._tracer = (
            tracer_provider.get_tracer(self._name) if tracer_provider else None
        )
        self._logger = (
            logger_provider.get_logger(self._name) if logger_provider else None
        )
    
    def _log(self, body: str, level: str = "INFO") -> None:
        if self._logger is None:
            return
        from opentelemetry._logs import LogRecord, SeverityNumber
        record = LogRecord(
            timestamp=time.time_ns(),
            body=body,
            severity_text=level,
            severity_number=getattr(SeverityNumber, level, SeverityNumber.INFO),
        )
        self._logger.emit(record)
```

### Usage Example

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

# Configure providers
tracer_provider = TracerProvider()
tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

logger_provider = LoggerProvider()
logger_provider.add_log_record_processor(BatchLogRecordProcessor(ConsoleLogExporter()))

# Create observable server with telemetry
server = RxWSServer(
    {"host": "::", "port": 8888},
    tracer_provider=tracer_provider,
    logger_provider=logger_provider,
)
```

### Trade-offs

| Benefit | Risk |
|---------|------|
| Clean separation — rxplus is just reactive primitives | Users must configure OTel themselves |
| No custom abstractions to maintain | Loss of reactive log stream operators (`log_filter`, etc.) |
| Standard OTel ecosystem compatibility | Migration effort for existing users |
| Optional telemetry = lighter footprint | Two provider params may feel verbose |

### Files to Delete

- `rxplus/logging.py` (854 lines)
- `tests/test_logging.py` (825 lines)
- `docs/logging.md` (302 lines)

### Files to Modify

| File | Scope |
|------|-------|
| `rxplus/ws.py` | ~50 lines: add provider params, replace `_log()` implementation |
| `rxplus/opt.py` | ~30 lines: remove `error_restart_signal_to_logitem`, adjust imports |
| `rxplus/mechanism.py` | ~150 lines: remove trace context, keep `RxException` |
| `rxplus/__init__.py` | ~40 lines: remove logging exports |
| `tasks/*.py` | Update task files to use standard OTel patterns |

### Validation

1. **Unit Tests**: Remove `test_logging.py`; add tests for provider injection in `test_ws.py`
2. **Integration Test**: Run `task_wsserver` + `task_wsclient` with OTel console exporter
3. **Grafana Stack**: Verify logs/traces appear in Tempo/Loki via OTLP export
4. **No-Provider Mode**: Verify components work without telemetry (silent operation)

### Open Questions

1. Should we provide a helper function like `configure_telemetry()` that creates both providers with common defaults?
2. How should `ErrorRestartSignal` be observed? As span events? OTel logs? Or just dropped?
3. Should we add `tracer.start_as_current_span()` around key operations like `handle_client()`?

---

## Proposal 2 — Global Provider + Logging Handler Adapter

### Overview

Remove custom logging module but provide a thin adapter using Python's standard `logging` module with OTel's `LoggingHandler`. Components use `logging.getLogger(__name__)` internally. Users configure global OTel providers once; rxplus components automatically emit via OTel when `LoggingHandler` is attached to the root logger. This provides familiar Python logging ergonomics with OTel export.

### Key Changes

| File | Change |
|------|--------|
| `rxplus/logging.py` | **DELETE** entirely |
| `rxplus/mechanism.py` | Remove trace context; keep `RxException` |
| `rxplus/ws.py` | Use `logging.getLogger(__name__).info(...)` instead of `_log()` |
| `rxplus/opt.py` | Remove `error_restart_signal_to_logitem()` |
| `rxplus/__init__.py` | Remove logging exports; optionally export `setup_otel_logging()` helper |
| New: `rxplus/telemetry.py` | Optional helper for one-line OTel setup |

### Component Implementation

**Before:**
```python
from .logging import LOG_LEVEL, create_log_record

class RxWSServer(Subject):
    def _log(self, body: str, level: LOG_LEVEL = "INFO") -> None:
        record = create_log_record(body, level, source=self._source)
        super().on_next(record)  # Emit to reactive stream
```

**After:**
```python
import logging

class RxWSServer(Subject):
    def __init__(self, conn_cfg, ...):
        self._logger = logging.getLogger(f"rxplus.ws.{self._name}")
    
    def _log(self, body: str, level: str = "INFO") -> None:
        getattr(self._logger, level.lower(), self._logger.info)(body)
```

### Setup Helper (Optional)

```python
# rxplus/telemetry.py
import logging
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.trace import set_tracer_provider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

def setup_otel(
    service_name: str = "rxplus",
    log_exporter=None,
    span_exporter=None,
):
    """One-line OTel setup for rxplus applications."""
    # Logger
    logger_provider = LoggerProvider()
    if log_exporter:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(logger_provider)
    
    # Attach OTel handler to root logger
    handler = LoggingHandler(logger_provider=logger_provider)
    logging.getLogger().addHandler(handler)
    
    # Tracer
    tracer_provider = TracerProvider()
    if span_exporter:
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    set_tracer_provider(tracer_provider)
    
    return tracer_provider, logger_provider
```

### Usage Example

```python
from opentelemetry.sdk._logs.export import ConsoleLogExporter
from opentelemetry.sdk.trace.export import ConsoleSpanExporter
from rxplus import RxWSServer
from rxplus.telemetry import setup_otel

# One-line setup
setup_otel(
    service_name="my-app",
    log_exporter=ConsoleLogExporter(),
    span_exporter=ConsoleSpanExporter(),
)

# Components auto-log via standard logging -> OTel
server = RxWSServer({"host": "::", "port": 8888})
# Logs go to OTel automatically
```

### Trade-offs

| Benefit | Risk |
|---------|------|
| Familiar Python `logging` API | Global state (providers set once) |
| Zero config for basic usage | Less explicit than provider injection |
| OTel's `LoggingHandler` handles translation | Requires understanding OTel + logging interplay |
| Simple migration path | Loss of reactive log stream semantics |

### Files to Delete

Same as Proposal 1:
- `rxplus/logging.py`
- `tests/test_logging.py`
- `docs/logging.md`

### Files to Modify/Add

| File | Scope |
|------|-------|
| `rxplus/ws.py` | ~30 lines: use `logging.getLogger()` |
| `rxplus/opt.py` | ~30 lines: remove log conversion operator |
| `rxplus/mechanism.py` | ~150 lines: remove trace context |
| `rxplus/__init__.py` | ~40 lines: update exports |
| **NEW** `rxplus/telemetry.py` | ~50 lines: optional helper |
| `tasks/*.py` | Use `setup_otel()` instead of custom LogSink |

### Validation

1. **Unit Tests**: Verify logs appear with `LoggingHandler` attached
2. **Integration**: Run tasks with console exporters
3. **Grafana Stack**: OTLP export to Loki/Tempo
4. **No-Setup Mode**: Verify components work without OTel (logs go to standard logging)

### Open Questions

1. Should `setup_otel()` be in a separate optional module to avoid OTel import if unused?
2. How to handle log levels? OTel uses `SeverityNumber`, Python uses numeric levels.
3. Should we add a `rxplus.telemetry.setup_tracing_decorator` for auto-tracing Rx pipelines?

---

## Comparison Summary

| Aspect | Proposal 1 (Provider Injection) | Proposal 2 (Global + Handler) |
|--------|--------------------------------|------------------------------|
| **Explicitness** | High — providers passed per-component | Low — global configuration |
| **Ergonomics** | More verbose | Simpler for common cases |
| **Flexibility** | Can use different providers per component | Single global provider set |
| **Python Familiarity** | Uses OTel SDK directly | Uses `logging` module |
| **Migration Complexity** | Medium — update all component instantiations | Low — add one setup call |
| **Testability** | Easy to mock providers | Requires `LoggingHandler` setup |

## Recommendation

**Proposal 1 (Provider Injection)** is recommended for rxplus because:

1. **Explicit is better than implicit** — Users see exactly what telemetry is configured
2. **No global state** — Better for testing and multi-tenant scenarios
3. **Aligns with OTel best practices** — Providers are dependency-injected
4. **Lighter default footprint** — No telemetry if not configured
5. **Clearer separation of concerns** — rxplus handles reactivity, OTel handles observability

Proposal 2 is viable for simpler applications but introduces global state and implicit behavior that may surprise users in complex deployments.
