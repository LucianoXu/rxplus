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
)
```

---

## Screen Capture

### `create_screen_capture(fps=10.0, scheduler=None)`

Observable emitting NumPy arrays `[H, W, 3]` (RGB, uint8) from the primary monitor.

```python
create_screen_capture(fps=30.0).pipe(
    ops.map(rgb_ndarray_to_jpeg_bytes),
).subscribe(send_frame)
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
