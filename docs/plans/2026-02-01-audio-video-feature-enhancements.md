# Audio and Video Feature Enhancements Design Document

## Current Context

- **rxplus** provides reactive wrappers for audio (`rxplus/audio.py`) and video (`rxplus/graphic.py`) I/O
- Audio module: `RxMicrophone`, `RxSpeaker`, `create_wavfile`, `save_wavfile` with PCM format support
- Video module: `create_screen_capture` with adaptive FPS, JPEG codec utilities
- Test infrastructure is now in place with mock fixtures for hardware-independent testing
- Several documented TODOs and gaps remain unaddressed

### Pain Points Being Addressed

| Gap | Location | Impact |
|-----|----------|--------|
| Simplistic channel conversion | `rxplus/audio.py:86-95` | Audio quality degradation for multi-channel sources |
| Hardcoded primary monitor | `rxplus/graphic.py:68` | Cannot capture secondary monitors or regions |
| No device selection | `RxMicrophone`/`RxSpeaker` | Cannot specify which audio device to use |
| No lossless image codec | `rxplus/graphic.py` | JPEG-only limits use cases requiring pixel-perfect capture |

---

## Requirements

### Functional Requirements

1. **Proper Channel Mixing**
   - Stereo-to-mono conversion via channel averaging (not truncation)
   - Mono-to-stereo conversion via duplication
   - Generic downmix/upmix for arbitrary channel counts

2. **Multi-Monitor Screen Capture**
   - Select specific monitor by index
   - Capture all monitors combined (index 0)
   - Capture arbitrary region via dict specification
   - Backwards compatible: `None` defaults to primary monitor

3. **Audio Device Selection**
   - `RxMicrophone` accepts optional `device_index` parameter
   - `RxSpeaker` accepts optional `device_index` parameter
   - `None` uses system default (backwards compatible)

4. **PNG Codec Support**
   - `rgb_ndarray_to_png_bytes()` for lossless encoding
   - `png_bytes_to_rgb_ndarray()` for decoding
   - Configurable compression level

### Non-Functional Requirements

- **Backwards Compatibility:** All existing code must work unchanged
- **Performance:** Channel mixing should not introduce measurable latency
- **Testability:** All new features must have unit tests using existing mock infrastructure

---

## Design Decisions

### 1. Channel Conversion Strategy

Will implement **averaging-based mixing** because:
- Industry standard for stereo→mono conversion
- Preserves audio energy better than truncation
- Simple to implement with NumPy operations

Trade-offs:
- Does not handle advanced layouts (5.1, 7.1) with proper speaker mapping
- Will use simple average for downmix, duplication for upmix

### 2. Monitor Selection API

Will use **union type `int | dict | None`** because:
- `None` = primary monitor (backwards compatible)
- `int` = monitor index (0=all, 1=primary, 2+=secondary) — matches `mss` convention
- `dict` = region specification — matches `mss` grab() API directly

Alternative considered: Separate `region` parameter — rejected to keep API simple.

### 3. PNG vs Other Lossless Formats

Will implement **PNG only** because:
- Already have PIL/Pillow as dependency
- Widely supported, good compression
- WebP would require additional complexity

---

## Technical Design

### 1. Core Components

#### 1.1 Channel Conversion Function

```python
def _convert_channels(audio: np.ndarray, target_ch: int) -> np.ndarray:
    """Convert audio channels with proper mixing.
    
    Args:
        audio: Input audio array with shape [samples, channels]
        target_ch: Target number of channels
        
    Returns:
        Audio array with shape [samples, target_ch]
    """
    current_ch = audio.shape[1]
    if current_ch == target_ch:
        return audio
    
    if current_ch == 2 and target_ch == 1:
        # Stereo to mono: average channels
        return audio.mean(axis=1, keepdims=True)
    elif current_ch == 1 and target_ch == 2:
        # Mono to stereo: duplicate
        return np.repeat(audio, 2, axis=1)
    elif current_ch > target_ch:
        # Downmix: keep first (target-1) channels, average rest into last
        result = audio[:, :target_ch-1]
        mixed = audio[:, target_ch-1:].mean(axis=1, keepdims=True)
        return np.concatenate([result, mixed], axis=1)
    else:
        # Upmix: duplicate last channel
        padding = np.repeat(audio[:, -1:], target_ch - current_ch, axis=1)
        return np.concatenate([audio, padding], axis=1)
```

#### 1.2 Multi-Monitor Screen Capture

```python
def create_screen_capture(
    fps: float = 10.0,
    monitor: int | dict | None = None,  # NEW parameter
    scheduler: Optional[rx.abc.SchedulerBase] = None,
) -> Observable[np.ndarray]:
    """
    Args:
        fps: Target frames per second
        monitor: Monitor selection:
            - None: Primary monitor (default)
            - int: Monitor index (0=all, 1=primary, 2+=secondary)
            - dict: Region {"left": x, "top": y, "width": w, "height": h}
        scheduler: Optional scheduler override
    """
```

#### 1.3 PNG Codec Functions

```python
def rgb_ndarray_to_png_bytes(frame: np.ndarray, compression: int = 6) -> bytes:
    """Encode RGB array to lossless PNG bytes."""

def png_bytes_to_rgb_ndarray(png: bytes) -> np.ndarray:
    """Decode PNG bytes to RGB array."""
```

### 2. Files Changed

| File | Changes | Lines |
|------|---------|-------|
| `rxplus/audio.py` | Add `_convert_channels()`, update `_load_wav_resample()`, add `device_index` to `RxMicrophone`/`RxSpeaker` | 86-95, 186-216, 323-366 |
| `rxplus/graphic.py` | Add `monitor` param to `create_screen_capture()`, add PNG codec functions | 18-120 (capture), 167+ (new PNG functions) |
| `rxplus/__init__.py` | Export new PNG functions | 23-49 |
| `tests/test_audio.py` | Add channel conversion tests | New tests |
| `tests/test_graphic.py` | Add monitor selection tests, PNG codec tests | New tests |

---

## Implementation Plan

### Phase 1: Audio Channel Mixing (P1 Priority)

1. Add `_convert_channels()` function in `rxplus/audio.py`
2. Replace inline channel conversion in `_load_wav_resample()` with call to `_convert_channels()`
3. Remove TODO comment at line 90-91
4. Add unit tests for all conversion cases:
   - Stereo → Mono
   - Mono → Stereo  
   - Multi-channel downmix
   - Multi-channel upmix
   - Same channel count (no-op)

### Phase 2: Multi-Monitor Capture (P2 Priority)

1. Add `monitor` parameter to `create_screen_capture()` signature
2. Update monitor selection logic in both async and sync loops
3. Add tests:
   - Default behavior (None → primary)
   - Integer index selection
   - Dict region selection
4. Update docstring with examples

### Phase 3: Audio Device Selection (P2 Priority)

1. Add `device_index: int | None = None` to `RxMicrophone.__init__()`
2. Pass `input_device_index=device_index` to `self._pa.open()`
3. Add `device_index: int | None = None` to `RxSpeaker.__init__()`
4. Pass `output_device_index=device_index` to `self._pa.open()`
5. Add tests verifying parameter is passed to PyAudio

### Phase 4: PNG Codec (P3 Priority)

1. Add `rgb_ndarray_to_png_bytes()` function
2. Add `png_bytes_to_rgb_ndarray()` function
3. Export from `rxplus/__init__.py`
4. Add tests:
   - PNG magic bytes verification
   - Roundtrip dimension preservation
   - Compression level affects size

---

## Testing Strategy

### Unit Tests

All tests use existing mock infrastructure from `tests/conftest.py`.

#### Audio Tests (`tests/test_audio.py`)

| Test | Description |
|------|-------------|
| `test_convert_channels_stereo_to_mono` | Verify averaging produces correct mono output |
| `test_convert_channels_mono_to_stereo` | Verify duplication produces identical channels |
| `test_convert_channels_downmix_multichannel` | Verify 4ch→2ch mixes correctly |
| `test_convert_channels_upmix_multichannel` | Verify 2ch→4ch duplicates last channel |
| `test_convert_channels_same_count_noop` | Verify same channel count returns unchanged |
| `test_rx_microphone_device_index_passed` | Verify device_index reaches PyAudio.open() |
| `test_rx_speaker_device_index_passed` | Verify device_index reaches PyAudio.open() |

#### Video Tests (`tests/test_graphic.py`)

| Test | Description |
|------|-------------|
| `test_screen_capture_default_monitor` | Verify None uses monitors[1] |
| `test_screen_capture_monitor_index` | Verify int index selects correct monitor |
| `test_screen_capture_region_dict` | Verify dict region passed to grab() |
| `test_png_produces_valid_header` | Verify PNG magic bytes (89 50 4E 47) |
| `test_png_roundtrip_dimensions` | Verify encode→decode preserves shape |
| `test_png_compression_affects_size` | Verify compression=1 > compression=9 |
| `test_png_lossless_roundtrip` | Verify exact pixel values preserved |

### Mock Strategies

- **Channel conversion:** Pure function, no mocking needed
- **Device selection:** Use existing `mock_pyaudio` fixture, assert `call_args`
- **Monitor selection:** Use existing `mock_mss` fixture, verify `grab()` argument
- **PNG codec:** No mocking needed, test with synthetic frames

---

## Observability

Not applicable — these are stateless utility functions and hardware wrappers without logging requirements.

---

## Future Considerations

### Potential Enhancements

- Support device selection by name (requires PyAudio device enumeration)
- Add 5.1/7.1 surround sound channel mapping
- WebP codec support for better compression
- Video recording to file (MP4/WebM)
- Camera capture in addition to screen capture

### Known Limitations

- Channel mixing uses simple averaging, not proper surround sound downmix
- Monitor selection depends on `mss` monitor ordering (OS-dependent)
- Device hot-swap issue on macOS remains unaddressed (separate investigation needed)

---

## Dependencies

- **numpy** — Array operations for channel mixing
- **pillow** — PNG encoding/decoding (already required for JPEG)
- **pyaudio** — Device index parameter (existing dependency)
- **mss** — Monitor selection (existing dependency)

No new dependencies required.

---

## References

- Proposal document: [docs/proposals/2026-02-01-audio-video-enhancements.md](../proposals/2026-02-01-audio-video-enhancements.md)
- Research document: [docs/research/2026-02-01-video-audio-components.md](../research/2026-02-01-video-audio-components.md)
- Audio module: `rxplus/audio.py:86-95` (channel conversion), `rxplus/audio.py:186-216` (RxMicrophone), `rxplus/audio.py:323-366` (RxSpeaker)
- Video module: `rxplus/graphic.py:18-120` (screen capture), `rxplus/graphic.py:123-167` (JPEG codec)
- Test fixtures: `tests/conftest.py:1-70`
