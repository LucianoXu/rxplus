# Video

Screen capture and image encoding utilities.

> **Optional dependency:** Install with `pip install rxplus[video]`

## Design Intention

Video capture is exposed as an Observable emitting frames at a target FPS. The stream self-adjusts timing to maintain consistent frame rates despite processing delays.

## API

```python
from rxplus import (
    create_screen_capture,
    rgb_ndarray_to_jpeg_bytes,
    jpeg_bytes_to_rgb_ndarray,
    rgb_ndarray_to_png_bytes,
    png_bytes_to_rgb_ndarray,
)
```

---

## Screen Capture

### `create_screen_capture(fps=10.0, monitor=None, scheduler=None)`

Observable emitting NumPy arrays `[H, W, 3]` (RGB, uint8) from screen capture.

```python
create_screen_capture(fps=30.0).pipe(
    ops.map(rgb_ndarray_to_jpeg_bytes),
).subscribe(send_frame)
```

**Parameters:**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `fps` | `float` | `10.0` | Target frames per second |
| `monitor` | `int \| dict \| None` | `None` | Monitor selection (see below) |
| `scheduler` | `SchedulerBase \| None` | `None` | Rx scheduler for timing |

**Monitor selection:**

| Value | Behavior |
|-------|----------|
| `None` | Primary monitor (default) |
| `0` | All monitors combined |
| `1` | Primary monitor |
| `2+` | Secondary monitors |
| `dict` | Custom region: `{"left": x, "top": y, "width": w, "height": h}` |

```python
# Capture specific region
create_screen_capture(fps=30.0, monitor={"left": 0, "top": 0, "width": 800, "height": 600})

# Capture secondary monitor
create_screen_capture(fps=30.0, monitor=2)
```

**Timing behavior:**

- Uses adaptive sleep to maintain target FPS
- Async-compatible: uses `asyncio.sleep` when an `AsyncIOScheduler` is provided
- Falls back to blocking `time.sleep` on thread-based schedulers

---

## Image Encoding

### `rgb_ndarray_to_jpeg_bytes(frame, quality=80)`

Encode RGB array to JPEG bytes.

```python
jpeg = rgb_ndarray_to_jpeg_bytes(frame, quality=90)
```

### `jpeg_bytes_to_rgb_ndarray(jpeg)`

Decode JPEG bytes to RGB array.

```python
frame = jpeg_bytes_to_rgb_ndarray(jpeg)  # [H, W, 3] uint8
```

### `rgb_ndarray_to_png_bytes(frame, compression=6)`

Encode RGB array to PNG bytes.

```python
png = rgb_ndarray_to_png_bytes(frame, compression=9)  # Max compression
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `frame` | `np.ndarray` | — | RGB array `[H, W, 3]` |
| `compression` | `int` | `6` | Compression level (0-9, 9 = max) |

### `png_bytes_to_rgb_ndarray(png)`

Decode PNG bytes to RGB array.

```python
frame = png_bytes_to_rgb_ndarray(png)  # [H, W, 3] uint8
```

---

## Example: Screen Streaming

```python
from rxplus import create_screen_capture, rgb_ndarray_to_jpeg_bytes, RxWSClient, TaggedData
from reactivex import operators as ops

client = RxWSClient({"host": "server", "port": 8765, "path": "/screen"}, datatype="bytes")

create_screen_capture(fps=15.0).pipe(
    ops.map(lambda f: rgb_ndarray_to_jpeg_bytes(f, quality=70)),
).subscribe(client)
```
