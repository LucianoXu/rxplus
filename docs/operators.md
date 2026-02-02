# Operators

Custom RxPY operators for stream control and error handling.

## API

```python
from rxplus import (
    redirect_to, stream_print_out,
    retry_with_signal, ErrorRestartSignal,
)
from reactivex import operators as ops
```

### `redirect_to(cond, target)`

Conditional routing: items matching `cond` go to `target`; others continue downstream.

```python
errors = Subject()

stream.pipe(
    redirect_to(lambda x: isinstance(x, Exception), errors)
).subscribe(handle_data)

errors.subscribe(handle_error)
```

---

### `stream_print_out(prompt="")`

Debug operator that prints and forwards every item.

```python
stream.pipe(stream_print_out("[DEBUG] ")).subscribe(...)
```

---

### `retry_with_signal(max_retries, delay_s, should_retry)`

Restarts the upstream observable on error, emitting an `ErrorRestartSignal` before each retry.

| Parameter | Type | Description |
|-----------|------|-------------|
| `max_retries` | `int \| None` | Max attempts; `None` for unlimited |
| `delay_s` | `float \| Callable[[int, RxException], float]` | Delay between retries |
| `should_retry` | `Callable[[RxException], bool] \| None` | Predicate to decide retry |

```python
source.pipe(
    retry_with_signal(max_retries=3, delay_s=1.0)
).subscribe(
    on_next=lambda x: (
        print(f"Retry #{x.attempts}") if isinstance(x, ErrorRestartSignal) else process(x)
    ),
    on_error=lambda e: print(f"Failed after retries: {e}"),
)
```

### `ErrorRestartSignal.record_as_span_event(span)`

Record a retry signal as an event on an OpenTelemetry span.

```python
from opentelemetry import trace

tracer = tracer_provider.get_tracer("my-pipeline")

def handle_item(item):
    if isinstance(item, ErrorRestartSignal):
        with tracer.start_as_current_span("retry") as span:
            item.record_as_span_event(span)
    return item

source.pipe(
    retry_with_signal(max_retries=5, delay_s=1.0),
    ops.map(handle_item),
).subscribe(process)
```

---

## Design Notes

- `redirect_to` enables separation of concerns without branching the subscription graph.
- `retry_with_signal` differs from RxPY's `retry` by surfacing retry events as stream items, enabling observability and logging.
