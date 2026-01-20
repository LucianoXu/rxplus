# Operators

Custom RxPY operators for stream control and error handling.

## API

```python
from rxplus import (
    redirect_to, stream_print_out,
    retry_with_signal, ErrorRestartSignal, error_restart_signal_to_logitem,
)
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

### `stream_print_out(prompt="")`

Debug operator that prints and forwards every item.

```python
stream.pipe(stream_print_out("[DEBUG] ")).subscribe(...)
```

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

### `error_restart_signal_to_logitem(log_source)`

Converts `ErrorRestartSignal` items into `LogItem` for unified logging.

```python
source.pipe(
    retry_with_signal(max_retries=5),
    error_restart_signal_to_logitem("MyComponent"),
    log_redirect_to(logger),
).subscribe(process)
```

## Design Notes

- `redirect_to` enables separation of concerns without branching the subscription graph.
- `retry_with_signal` differs from RxPY's `retry` by surfacing retry events as stream items, enabling observability and logging.
