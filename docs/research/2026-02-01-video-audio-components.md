# Research: Video and Audio Components

**Date:** 2026-02-01  
**Scope:** Audio and video/graphic reactive components in rxplus

## Summary

The rxplus library provides audio and video components as **optional feature modules** that integrate hardware I/O with ReactiveX streams. Audio components (`rxplus/audio.py`) expose microphone input as an Observable (`RxMicrophone`), speaker output as an Observer (`RxSpeaker`), and WAV file I/O as both Observable (`create_wavfile`) and Observer (`save_wavfile`). Video components (`rxplus/graphic.py`) provide screen capture as an Observable (`create_screen_capture`) with adaptive FPS timing, plus JPEG encoding/decoding utilities. Both modules wrap their functionality in ReactiveX patterns, allowing users to compose complex media pipelines using standard Rx operators. Installation is modular via optional dependencies (`rxplus[audio]`, `rxplus[video]`, or `rxplus[all]`).

---

## 1. Audio Module

**File:** [rxplus/audio.py](../../rxplus/audio.py)

### 1.1 Overview

The audio module provides reactive wrappers around PyAudio for hardware I/O and soundfile for file I/O. All components operate on raw PCM data with configurable formats.

### 1.2 PCM Format System

**Location:** [rxplus/audio.py: 23-53](../../rxplus/audio.py#L23-L53)

```python
PCMFormat = Literal["UInt8", "Int16", "Int24", "Int32", "Float32"]
```

Two helper functions map this type to library-specific formats:

| Function | Purpose | Returns |
|----------|---------|---------|
| `get_pyaudio_format(format)` | Maps to PyAudio constants | `pyaudio.paXXX` constant |
| `get_sf_format(format)` | Maps to soundfile types | `(numpy_dtype, libsndfile_subtype)` tuple |

### 1.3 Resampling Utility

**Location:** [rxplus/audio.py: 56-69](../../rxplus/audio.py#L56-L69)

```python
def resample_audio(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
```

Uses `scipy.signal.resample_poly` for efficient integer-ratio resampling. Computes GCD of sample rates to minimize the up/down factors.

### 1.4 WAV File Loading (Internal)

**Location:** [rxplus/audio.py: 72-117](../../rxplus/audio.py#L72-L117)

```python
def _load_wav_resample(path, target_format, target_sr, target_ch) -> np.ndarray:
```

Internal helper that:
1. Reads WAV via `soundfile.read()` (always 2D)
2. Converts to float32 for processing
3. Resamples to target sample rate
4. Adjusts channel count (truncation or duplication)
5. Converts to target PCM format with proper scaling

**Format conversion scaling:**
- `Int32`: `audio * np.iinfo(np.int32).max`
- `Int24`: `audio * (2^23 - 1)`
- `Int16`: `audio * np.iinfo(np.int16).max`
- `UInt8`: `(audio * 127.5) + 127.5`

### 1.5 `create_wavfile` Observable

**Location:** [rxplus/audio.py: 120-183](../../rxplus/audio.py#L120-L183)

```python
def create_wavfile(
    wav_path: str,
    target_sample_rate: int = 48_000,
    target_channels: int = 1,
    target_format: PCMFormat = "Float32",
    frames_per_chunk: int = 1_024,
    scheduler: Optional[rx.abc.SchedulerBase] = None,
) -> Observable[np.ndarray]:
```

**Behavior:**
- Creates a cold Observable that loads and emits WAV data chunk-by-chunk
- Output shape: `[samples, channels]` as 2D NumPy array
- Auto-selects scheduler: `AsyncIOScheduler` if event loop running, else `ThreadPoolScheduler(1)`
- Emits `on_completed()` after all chunks, wraps errors in `RxException`

**Scheduler Selection Logic:** [rxplus/audio.py: 136-152](../../rxplus/audio.py#L136-L152)
```python
_scheduler = scheduler or scheduler_ or (
    AsyncIOScheduler(loop) if running else ThreadPoolScheduler(1)
)
```

### 1.6 `RxMicrophone` Subject

**Location:** [rxplus/audio.py: 186-295](../../rxplus/audio.py#L186-L295)

```python
class RxMicrophone(Subject):
    def __init__(
        self,
        format: PCMFormat = "Float32",
        sample_rate: int = 48_000,
        channels: int = 1,
        frames_per_buffer: int = 1_024,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
```

**Architecture:**

1. **PyAudio Setup** ([rxplus/audio.py: 206-216](../../rxplus/audio.py#L206-L216)):
   - Opens input stream with callback mode
   - Callback forwards raw bytes via `super().on_next(in_data)`

2. **Stream Watcher** ([rxplus/audio.py: 218-234](../../rxplus/audio.py#L218-L234)):
   - Async watcher (`_watch_stream`) if event loop available
   - Threaded watcher (`_watch_stream_sync`) otherwise
   - Monitors `stream.is_active()` every 50ms
   - Calls `_shutdown()` when stream stops

3. **PyAudio Callback** ([rxplus/audio.py: 239-251](../../rxplus/audio.py#L239-L251)):
   ```python
   def _pyaudio_callback(self, in_data, frame_count, time_info, status):
       super().on_next(in_data)
       return (None, pyaudio.paContinue)
   ```

4. **Shutdown** ([rxplus/audio.py: 303-320](../../rxplus/audio.py#L303-L320)):
   - Stops and closes PyAudio stream
   - Terminates PyAudio instance
   - Calls `on_completed()` on observers

**Data Flow:**
```
PyAudio Hardware → Callback → Subject.on_next(bytes) → Observers
```

### 1.7 `RxSpeaker` Subject

**Location:** [rxplus/audio.py: 323-366](../../rxplus/audio.py#L323-L366)

```python
class RxSpeaker(Subject):
    def __init__(
        self,
        format: PCMFormat = "Float32",
        sample_rate: int = 48_000,
        channels: int = 1,
    ):
```

**Architecture:**
- Opens PyAudio output stream (non-callback mode)
- Overrides `on_next` to write bytes to stream via `_play_to_soundcard()`
- Simpler than `RxMicrophone` (no watchers needed)

**Data Flow:**
```
Observable → RxSpeaker.on_next(bytes) → PyAudio write → Speaker
```

### 1.8 `SaveWavFile` Observer

**Location:** [rxplus/audio.py: 373-425](../../rxplus/audio.py#L373-L425)

```python
class SaveWavFile(rx.abc.ObserverBase):
    def __init__(
        self,
        path: str,
        format: PCMFormat = "Float32",
        sample_rate: int = 48_000,
        channels: int = 1,
    ):
```

**Implementation:**
- Opens `soundfile.SoundFile` in write mode
- Uses `buffer_write()` for streaming writes on each `on_next`
- Flushes and closes on `on_completed()` or `on_error()`

**Factory Function:** [rxplus/audio.py: 428-443](../../rxplus/audio.py#L428-L443)
```python
def save_wavfile(path, format, sample_rate, channels) -> SaveWavFile:
```

### 1.9 Dependencies

**From [pyproject.toml: 37-43](../../pyproject.toml#L37-L43):**
```toml
audio = [
    "pyaudio>=0.2.14",
    "soundfile>=0.12.1",
    "scipy>=1.13.1",
    "numpy",
]
```

**System requirement:** `portaudio` library (see [README.md: 35-52](../../README.md#L35-L52))

---

## 2. Video/Graphic Module

**File:** [rxplus/graphic.py](../../rxplus/graphic.py)

### 2.1 Overview

The graphic module provides screen capture as a reactive Observable and JPEG codec utilities. It uses `mss` for cross-platform screen capture and `PIL` for image encoding.

### 2.2 `create_screen_capture` Observable

**Location:** [rxplus/graphic.py: 18-120](../../rxplus/graphic.py#L18-L120)

```python
def create_screen_capture(
    fps: float = 10.0,
    scheduler: Optional[rx.abc.SchedulerBase] = None,
) -> Observable[np.ndarray]:
```

**Output:** RGB NumPy arrays of shape `[H, W, 3]`, dtype `uint8`

**Adaptive Timing Algorithm:**

The function uses a feedback loop to maintain consistent FPS despite processing delays:

```python
sleep_time += 0.5 * (interval - elapsed)
```

This adjusts the sleep duration based on the difference between target interval and actual elapsed time.

**Dual Implementation:**

1. **Async Loop** ([rxplus/graphic.py: 60-80](../../rxplus/graphic.py#L60-L80)):
   - Uses `asyncio.sleep()` for non-blocking delay
   - Runs when `AsyncIOScheduler` is provided

2. **Sync Loop** ([rxplus/graphic.py: 82-102](../../rxplus/graphic.py#L82-L102)):
   - Uses `time.sleep()` blocking delay
   - Runs in worker thread via `ThreadPoolScheduler`

**Color Conversion:** [rxplus/graphic.py: 71](../../rxplus/graphic.py#L71)
```python
rgb = np.array(sct.grab(monitor))[:, :, :3][:, :, ::-1]  # BGRA -> RGB
```
- `mss` returns BGRA format
- Slices to remove alpha channel
- Reverses channel order to RGB

**Monitor Selection:**
```python
monitor = sct.monitors[1]  # primary monitor
```

### 2.3 JPEG Encoding

**Location:** [rxplus/graphic.py: 123-148](../../rxplus/graphic.py#L123-L148)

```python
def rgb_ndarray_to_jpeg_bytes(frame: np.ndarray, quality: int = 80) -> bytes:
```

**Implementation:**
1. Creates PIL Image from raw bytes: `Image.frombytes("RGB", (W, H), frame.tobytes())`
2. Saves to BytesIO buffer with JPEG format
3. Returns raw bytes

### 2.4 JPEG Decoding

**Location:** [rxplus/graphic.py: 150-167](../../rxplus/graphic.py#L150-L167)

```python
def jpeg_bytes_to_rgb_ndarray(jpeg: bytes) -> np.ndarray:
```

**Implementation:**
1. Opens JPEG from BytesIO via `Image.open()`
2. Converts to RGB mode (ensures 3 channels)
3. Returns NumPy array of shape `(H, W, 3)`, dtype `uint8`

### 2.5 Dependencies

**From [pyproject.toml: 45-50](../../pyproject.toml#L45-L50):**
```toml
video = [
    "pillow>=11.3.0",
    "mss>=10.0.0",
    "numpy",
]
```

---

## 3. Monitoring Utilities

**File:** [rxplus/utils.py](../../rxplus/utils.py)

### 3.1 `FPSMonitor`

**Location:** [rxplus/utils.py: 77-103](../../rxplus/utils.py#L77-L103)

```python
class FPSMonitor:
    def __init__(self, interval: float = 1.0) -> None:
    def __call__(self, item):  # Side-effect: print FPS, pass item through
```

**Usage:** Callable class for use with `ops.map()` or `ops.do_action()`

### 3.2 `BandwidthMonitor`

**Location:** [rxplus/utils.py: 104-150](../../rxplus/utils.py#L104-L150)

```python
class BandwidthMonitor:
    def __init__(self, interval: float = 1.0, scale: float = 1.0, unit: str = "B/s") -> None:
    def __call__(self, item):  # Side-effect: print bandwidth, pass item through
```

**Parameters:**
- `scale`: Conversion factor (e.g., `1/1024` for KB/s)
- `unit`: Display unit string

---

## 4. Module Exports

**File:** [rxplus/__init__.py](../../rxplus/__init__.py)

### 4.1 Optional Import Pattern

**Location:** [rxplus/__init__.py: 23-49](../../rxplus/__init__.py#L23-L49)

```python
# Audio (conditional)
try:
    from .audio import (PCMFormat, RxMicrophone, RxSpeaker, create_wavfile, save_wavfile)
    _HAS_AUDIO = True
except ImportError:
    _HAS_AUDIO = False

# Video (conditional)
try:
    from .graphic import (create_screen_capture, rgb_ndarray_to_jpeg_bytes, jpeg_bytes_to_rgb_ndarray)
    _HAS_VIDEO = True
except ImportError:
    _HAS_VIDEO = False
```

This pattern allows graceful degradation when optional dependencies are not installed.

### 4.2 Exported Symbols

**Audio exports:** `PCMFormat`, `RxMicrophone`, `RxSpeaker`, `create_wavfile`, `save_wavfile`

**Video exports:** `create_screen_capture`, `rgb_ndarray_to_jpeg_bytes`, `jpeg_bytes_to_rgb_ndarray`

---

## 5. Task Examples

### 5.1 Audio Tasks

| Task File | Description | Data Flow |
|-----------|-------------|-----------|
| [tasks/task_mic_server.py](../../tasks/task_mic_server.py) | Microphone → WebSocket server | `RxMicrophone → tag("/") → RxWSServer` |
| [tasks/task_speaker_client.py](../../tasks/task_speaker_client.py) | WebSocket client → Speaker | `RxWSClient → RxSpeaker` |
| [tasks/task_wavfile_server.py](../../tasks/task_wavfile_server.py) | WAV file → WebSocket server | `create_wavfile → tobytes → repeat → tag → RxWSServer` |
| [tasks/task_wavfile_client.py](../../tasks/task_wavfile_client.py) | WebSocket client → WAV file | `RxWSClient → save_wavfile` |

### 5.2 Video Tasks

| Task File | Description | Data Flow |
|-----------|-------------|-----------|
| [tasks/task_screen_capture_server.py](../../tasks/task_screen_capture_server.py) | Screen capture → JPEG → WebSocket | `create_screen_capture → FPSMonitor → JPEG encode → BandwidthMonitor → RxWSServer` |
| [tasks/task_jpeg_client.py](../../tasks/task_jpeg_client.py) | WebSocket → JPEG decode | `RxWSClient → jpeg_bytes_to_rgb_ndarray` |

### 5.3 Example Pipeline (Screen Capture Server)

**From [tasks/task_screen_capture_server.py: 32-44](../../tasks/task_screen_capture_server.py#L32-L44):**

```python
source.pipe(
    ops.do_action(lambda frame: print(f"Captured frame of shape: {frame.shape}")),
    ops.map(fps_monitor),
    ops.observe_on(ThreadPoolScheduler(1)),
    ops.map(lambda frame: rgb_ndarray_to_jpeg_bytes(frame, quality=80)),
    ops.map(bandwidth_monitor),
    tag("/"),
).subscribe(sender)
```

This demonstrates:
- FPS monitoring on raw frames
- Thread hop for encoding (`observe_on`)
- JPEG compression
- Bandwidth monitoring on compressed data
- Path tagging for WebSocket routing

---

## 6. Testing

**File:** [tests/test_audio.py](../../tests/test_audio.py)

### 6.1 Test Coverage

| Test | Description |
|------|-------------|
| `test_create_wavfile_outside_loop` | Verifies `ThreadPoolScheduler` usage outside async context |
| `test_create_wavfile_inside_loop` | Verifies `AsyncIOScheduler` usage inside async context |
| `test_resample_audio_changes_rate` | Validates resampling produces correct sample count |
| `test_get_pyaudio_format_and_sf_format` | Tests format mapping functions and error handling |

### 6.2 Test Methodology

**Scheduler verification pattern:** [tests/test_audio.py: 22-40](../../tests/test_audio.py#L22-L40)

Tests capture thread IDs during emission and compare against main thread to verify correct scheduler selection:
- Outside loop: thread IDs should differ from main thread
- Inside loop: thread IDs should match main thread (async scheduler)

---

## 7. Cross-Component Connections

### 7.1 Integration with WebSocket

Both audio and video components are designed to integrate seamlessly with `RxWSServer`/`RxWSClient`:

```
Audio: RxMicrophone → bytes → tag() → RxWSServer
Video: create_screen_capture → ndarray → JPEG → tag() → RxWSServer
```

### 7.2 Shared Patterns

1. **Scheduler Selection:** Both `create_wavfile` and `create_screen_capture` use identical logic to auto-select between `AsyncIOScheduler` and `ThreadPoolScheduler`

2. **Error Wrapping:** Both wrap exceptions in `RxException` with contextual notes

3. **Disposal Pattern:** Both use `disposed` flag checked in loops for clean shutdown

### 7.3 Data Format Conventions

| Component | Input | Output |
|-----------|-------|--------|
| `RxMicrophone` | N/A | `bytes` (raw PCM) |
| `RxSpeaker` | `bytes` (raw PCM) | N/A |
| `create_wavfile` | N/A | `np.ndarray [samples, channels]` |
| `save_wavfile` | `bytes` (raw PCM) | N/A |
| `create_screen_capture` | N/A | `np.ndarray [H, W, 3]` RGB uint8 |
| `rgb_ndarray_to_jpeg_bytes` | `np.ndarray [H, W, 3]` | `bytes` (JPEG) |
| `jpeg_bytes_to_rgb_ndarray` | `bytes` (JPEG) | `np.ndarray [H, W, 3]` |

---

## 8. Known Issues / TODOs

**From [tasks/task_mic_server.py: 12-13](../../tasks/task_mic_server.py#L12-L13):**
```python
# TODO: problem encounted when changing the microphone device. The following code observed:
# ||PaMacCore (AUHAL)|| Error on line 2523: err='-50', msg=Unknown Error
```

This indicates a known issue with hot-swapping audio devices on macOS.

**From [rxplus/audio.py: 90-91](../../rxplus/audio.py#L90-L91):**
```python
# TODO: there should be a protocol to transform the wav array between different channel numbers.
```

Current channel conversion is simplistic (truncate or duplicate first channel).
