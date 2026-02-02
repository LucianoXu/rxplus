# Solution Proposals: Audio and Video Component Enhancements

**Date:** 2026-02-01  
**Status:** Proposal 1 ✅ Implemented | Proposal 2 Ready for Implementation

## Context

- **Request:** Enhance audio and video components and build comprehensive tests
- **Research Source:** [docs/research/2026-02-01-video-audio-components.md](../research/2026-02-01-video-audio-components.md) — Sections 1-8

### Key Findings from Research

| Gap | Location | Status | Impact |
|-----|----------|--------|--------|
| No tests for `RxMicrophone`, `RxSpeaker`, `SaveWavFile` | `tests/test_audio.py` | ✅ Resolved | Hardware components now tested with mocks |
| No tests for graphic module | `tests/test_graphic.py` | ✅ Resolved | JPEG codec + screen capture tests added |
| Simplistic channel conversion | `rxplus/audio.py:90-91` | 🔶 Open | Audio quality degradation for multi-channel sources |
| Hardcoded primary monitor | `rxplus/graphic.py:68` | 🔶 Open | Cannot capture secondary monitors or regions |
| macOS device hot-swap issue | `tasks/task_mic_server.py:12-13` | 🔶 Open | Runtime errors when audio device changes |

---

## Proposal 1 — Test Infrastructure First ✅ IMPLEMENTED

### Overview

~~Prioritize building a robust test suite for both modules before adding features. This approach uses mocking to test hardware components without requiring physical devices, ensures CI compatibility, and establishes patterns for future development.~~

**Status: COMPLETED** — The test infrastructure has been successfully implemented:

- ✅ `tests/conftest.py`: Shared fixtures (`mock_pyaudio`, `mock_mss`, `synthetic_rgb_frame`, `synthetic_audio_float32`)
- ✅ `tests/test_audio.py`: Tests for `RxMicrophone`, `RxSpeaker`, `SaveWavFile`, format functions
- ✅ `tests/test_graphic.py`: Tests for JPEG codec and `create_screen_capture`

### Implemented Tests

| Component | Tests | Coverage |
|-----------|-------|----------|
| `RxMicrophone` | `test_emits_on_callback`, `test_shutdown_on_stream_stop`, `test_format_configuration` | PyAudio callback flow |
| `RxSpeaker` | `test_writes_to_stream`, `test_format_configuration` | Output stream handling |
| `SaveWavFile` | `test_writes_chunks` | File I/O |
| JPEG Codec | 6 tests covering roundtrip, quality, edge cases | Full coverage |
| Screen Capture | `test_emits_rgb_arrays`, `test_bgra_to_rgb_conversion`, `test_disposal_stops_loop`, `test_fps_timing`, `test_uses_thread_scheduler_outside_loop` | Core functionality |

---

### Key Changes

| Component | Change | Files |
|-----------|--------|-------|
| Audio unit tests | Mock PyAudio/soundfile for `RxMicrophone`, `RxSpeaker`, `SaveWavFile` | `tests/test_audio.py` |
| Video unit tests | Mock `mss` for `create_screen_capture`, test JPEG codecs with synthetic data | New `tests/test_graphic.py` |
| Test fixtures | Create reusable audio/video test data generators | `tests/conftest.py` |
| CI compatibility | Ensure all tests run without hardware | `pyproject.toml` (dev deps) |

### Detailed Implementation

#### 1.1 Audio Test Expansion

**Target:** `tests/test_audio.py`

```python
# New tests to add:

# RxMicrophone tests (mocked)
- test_rx_microphone_emits_on_callback()      # Mock PyAudio callback
- test_rx_microphone_shutdown_on_stream_stop()
- test_rx_microphone_wraps_errors_in_rx_exception()

# RxSpeaker tests (mocked)
- test_rx_speaker_writes_to_stream()
- test_rx_speaker_format_configuration()

# SaveWavFile tests
- test_save_wavfile_writes_chunks()
- test_save_wavfile_closes_on_completed()
- test_save_wavfile_closes_on_error()

# Integration-style tests (file-based, no hardware)
- test_wavfile_roundtrip()  # create_wavfile → save_wavfile
```

**Mocking Strategy:**
```python
@pytest.fixture
def mock_pyaudio(monkeypatch):
    """Mock PyAudio to avoid hardware dependency."""
    mock_pa = MagicMock()
    mock_stream = MagicMock()
    mock_stream.is_active.return_value = True
    mock_pa.open.return_value = mock_stream
    monkeypatch.setattr("pyaudio.PyAudio", lambda: mock_pa)
    return mock_pa, mock_stream
```

#### 1.2 Video Test Creation

**Target:** New `tests/test_graphic.py`

```python
# Tests to create:

# Screen capture tests (mocked)
- test_create_screen_capture_emits_rgb_arrays()
- test_create_screen_capture_respects_fps()
- test_create_screen_capture_disposal_stops_loop()
- test_create_screen_capture_async_scheduler()
- test_create_screen_capture_thread_scheduler()

# JPEG codec tests (no mocking needed)
- test_rgb_to_jpeg_produces_valid_jpeg()
- test_jpeg_to_rgb_recovers_dimensions()
- test_jpeg_roundtrip_preserves_shape()
- test_jpeg_quality_affects_size()
```

**Mocking Strategy:**
```python
@pytest.fixture
def mock_mss(monkeypatch):
    """Mock mss for screen capture tests."""
    fake_frame = np.zeros((100, 100, 4), dtype=np.uint8)  # BGRA
    mock_sct = MagicMock()
    mock_sct.monitors = [None, {"width": 100, "height": 100}]
    mock_sct.grab.return_value = fake_frame
    # ... monkeypatch mss.mss context manager
```

#### 1.3 Shared Fixtures

**Target:** `tests/conftest.py`

```python
@pytest.fixture
def synthetic_audio_chunk():
    """Generate sine wave audio chunk for testing."""
    ...

@pytest.fixture  
def synthetic_rgb_frame():
    """Generate random RGB frame for testing."""
    return np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
```

### Trade-offs

| Benefit | Risk |
|---------|------|
| CI-compatible (no hardware needed) | Mocks may not catch real hardware edge cases |
| Fast test execution | Initial setup time for mock infrastructure |
| Catches regressions early | Does not validate actual audio/video quality |
| Clear separation of unit vs integration | May need separate manual integration tests |

### Validation

1. **Unit test coverage:** Aim for >80% line coverage on `audio.py` and `graphic.py`
2. **CI green:** All tests pass in GitHub Actions without hardware
3. **Mutation testing (optional):** Verify tests catch introduced bugs

### Open Questions

- Should we add optional hardware integration tests gated by environment variable (e.g., `RXPLUS_HW_TESTS=1`)?
- Do we need to test specific PCM format conversions beyond the existing `test_get_pyaudio_format_and_sf_format`?

---

## Proposal 2 — Feature Enhancements with Targeted Tests (Recommended Next Step)

### Overview

Add new capabilities to both modules alongside tests for new features. This approach prioritizes user-facing improvements: multi-monitor support, proper channel mixing, and PNG codec support for lossless video. **Now that test infrastructure is in place, this proposal can proceed safely.**

### Key Changes

| Component | Enhancement | Files |
|-----------|-------------|-------|
| Screen capture | Multi-monitor + region selection | `rxplus/graphic.py` |
| Channel conversion | Proper stereo↔mono mixing | `rxplus/audio.py` |
| Image codecs | Add PNG support alongside JPEG | `rxplus/graphic.py` |
| Device selection | Allow specifying audio device index | `rxplus/audio.py` |

### Detailed Implementation

#### 2.1 Multi-Monitor Screen Capture

**Target:** `rxplus/graphic.py:18-120`

```python
def create_screen_capture(
    fps: float = 10.0,
    monitor: int | dict | None = None,  # NEW: None=primary, int=index, dict=region
    scheduler: Optional[rx.abc.SchedulerBase] = None,
) -> Observable[np.ndarray]:
    """
    Args:
        monitor: Monitor selection:
            - None: Primary monitor (default, backwards compatible)
            - int: Monitor index (0=all, 1=primary, 2+=secondary)
            - dict: Region {"left": x, "top": y, "width": w, "height": h}
    """
```

**Implementation change at line 68:**
```python
# Current:
monitor = sct.monitors[1]  # primary monitor

# Proposed:
if monitor is None:
    _monitor = sct.monitors[1]
elif isinstance(monitor, int):
    _monitor = sct.monitors[monitor]
else:
    _monitor = monitor  # dict region
```

#### 2.2 Proper Audio Channel Mixing

**Target:** `rxplus/audio.py:86-95`

```python
# Current (simplistic):
if current_ch > target_ch:
    audio = audio[:, :target_ch]
elif current_ch < target_ch:
    audio = np.concatenate((audio[:, 0],) * target_ch, 1)

# Proposed (proper mixing):
def _convert_channels(audio: np.ndarray, target_ch: int) -> np.ndarray:
    """Convert audio channels with proper mixing."""
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
        # Downmix: average excess channels into last target channel
        result = audio[:, :target_ch-1]
        mixed = audio[:, target_ch-1:].mean(axis=1, keepdims=True)
        return np.concatenate([result, mixed], axis=1)
    else:
        # Upmix: duplicate last channel
        padding = np.repeat(audio[:, -1:], target_ch - current_ch, axis=1)
        return np.concatenate([audio, padding], axis=1)
```

#### 2.3 PNG Codec Support

**Target:** `rxplus/graphic.py` (new functions)

```python
def rgb_ndarray_to_png_bytes(frame: np.ndarray, compression: int = 6) -> bytes:
    """Encode RGB array to lossless PNG bytes."""
    img = Image.frombytes("RGB", (frame.shape[1], frame.shape[0]), frame.tobytes())
    with BytesIO() as output:
        img.save(output, format="PNG", compress_level=compression)
        return output.getvalue()

def png_bytes_to_rgb_ndarray(png: bytes) -> np.ndarray:
    """Decode PNG bytes to RGB array."""
    with Image.open(BytesIO(png)) as im:
        return np.asarray(im.convert("RGB"))
```

#### 2.4 Audio Device Selection

**Target:** `rxplus/audio.py:186-216`

```python
class RxMicrophone(Subject):
    def __init__(
        self,
        format: PCMFormat = "Float32",
        sample_rate: int = 48_000,
        channels: int = 1,
        frames_per_buffer: int = 1_024,
        device_index: int | None = None,  # NEW
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        ...
        self._stream = self._pa.open(
            ...
            input_device_index=device_index,  # NEW
        )
```

Similarly for `RxSpeaker`:
```python
class RxSpeaker(Subject):
    def __init__(
        self,
        ...
        device_index: int | None = None,  # NEW
    ):
```

### Trade-offs

| Benefit | Risk |
|---------|------|
| Immediate user-facing value | Less test coverage overall |
| Solves documented TODOs | New features may introduce bugs |
| Backwards compatible (defaults unchanged) | More code surface to maintain |
| Addresses multi-monitor use case | Region capture untested on all platforms |

### Validation

1. **Feature tests:** Write targeted tests for each new feature
2. **Manual testing:** Verify multi-monitor on macOS/Windows/Linux
3. **Backwards compatibility:** Existing task files should work unchanged

### Open Questions

- Should PNG support be a separate optional dependency to avoid increasing `pillow` requirements?
- For channel mixing, should we support arbitrary channel layouts (e.g., 5.1 surround)?
- Should device selection support device names in addition to indices?

---

## Recommendation

**~~Start with Proposal 1~~** ✅ **COMPLETED**

**Proceed with Proposal 2** (Feature Enhancements) because:

1. ✅ Test infrastructure is now in place with mock fixtures
2. ✅ CI-compatible tests exist for both audio and video modules
3. The documented TODOs (channel conversion, device hot-swap) can now be addressed safely
4. New features can be added incrementally with accompanying tests

**Suggested implementation order:**
1. ~~Implement Proposal 1 (1-2 days)~~ ✅ Done
2. **Next:** Proper channel mixing (`_convert_channels` function) with tests
3. **Next:** Multi-monitor support for `create_screen_capture`
4. **Optional:** PNG codec support and audio device selection

### Priority Features (Proposal 2)

| Feature | Effort | Value | Priority |
|---------|--------|-------|----------|
| Proper stereo↔mono mixing | Low | High | 🔴 P1 |
| Multi-monitor capture | Medium | Medium | 🟡 P2 |
| Audio device selection | Low | Medium | 🟡 P2 |
| PNG codec support | Low | Low | 🟢 P3 |

---

## Appendix: Implementation Status

### Test Files (Proposal 1 — ✅ Implemented)

| File | Status | Description |
|------|--------|-------------|
| `tests/conftest.py` | ✅ Complete | Shared fixtures: `mock_pyaudio`, `mock_mss`, `synthetic_rgb_frame`, `synthetic_audio_float32` |
| `tests/test_graphic.py` | ✅ Complete | JPEG codec tests + screen capture tests with mocking |
| `tests/test_audio.py` | ✅ Extended | Added `RxMicrophone`, `RxSpeaker`, `SaveWavFile` tests |

### Feature Files (Proposal 2 — Ready for Implementation)

| File | Enhancement | Status |
|------|-------------|--------|
| `rxplus/audio.py:86-95` | `_convert_channels()` function for proper mixing | 🔶 Pending |
| `rxplus/audio.py:186-216` | `device_index` parameter for `RxMicrophone` | 🔶 Pending |
| `rxplus/audio.py:323-366` | `device_index` parameter for `RxSpeaker` | 🔶 Pending |
| `rxplus/graphic.py:18-120` | `monitor` parameter for region/multi-monitor | 🔶 Pending |
| `rxplus/graphic.py` (new) | `rgb_ndarray_to_png_bytes()`, `png_bytes_to_rgb_ndarray()` | 🔶 Pending |
