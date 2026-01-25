# Utilities

Helper classes for tagging, monitoring, and error handling.

## API

```python
from rxplus import (
    TaggedData, tag, untag, tag_filter,
    FPSMonitor, BandwidthMonitor,
    RxException,
)
```

---

## Tagged Data

Wrap items with a tag for multiplexing multiple logical channels through a single stream.

### `TaggedData[TagT, InnerDataT]`

```python
td = TaggedData("/audio", b"\x00\x01\x02")
print(td.tag)   # "/audio"
print(td.data)  # b'\x00\x01\x02'
```

### Operators

| Operator | Description |
|----------|-------------|
| `tag(t)` | Wrap items: `x → TaggedData(t, x)` |
| `untag()` | Unwrap: `TaggedData → data` |
| `tag_filter(t)` | Keep only items where `tag == t` |

```python
audio_stream.pipe(tag("/audio"))  # → TaggedData("/audio", chunk)

mixed_stream.pipe(
    tag_filter("/audio"),
    untag()
).subscribe(process_audio)
```

---

## Monitoring

Callable objects for use with `ops.do_action` or `ops.map`.

### `FPSMonitor(interval=1.0)`

Prints frames-per-second every `interval` seconds.

```python
stream.pipe(ops.do_action(FPSMonitor(1.0))).subscribe(...)
# Output: [FPS] 30.2
```

### `BandwidthMonitor(interval=1.0, scale=1.0, unit="B/s")`

Measures throughput for items supporting `len()`.

```python
stream.pipe(
    ops.do_action(BandwidthMonitor(1.0, scale=1/1024, unit="KiB/s"))
).subscribe(...)
# Output: [Bandwidth] 512.3 KiB/s
```

---

## Exceptions

### `RxException`

Enriched exception for reactive pipelines:

```python
try:
    ...
except Exception as e:
    raise RxException(e, source="MyComponent", note="during initialization")
```

Attributes:

| Name | Description |
|------|-------------|
| `exception` | Original exception |
| `source` | Component name |
| `note` | Additional context |

Used for consistent error wrapping in reactive components.
