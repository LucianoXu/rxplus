# Audio and Video Test Infrastructure Design Document

## Current Context

- **rxplus** provides audio (`rxplus/audio.py`) and video (`rxplus/graphic.py`) components as optional feature modules
- Audio module wraps PyAudio for hardware I/O (`RxMicrophone`, `RxSpeaker`) and soundfile for file I/O (`create_wavfile`, `save_wavfile`)
- Video module wraps `mss` for screen capture and PIL for JPEG encoding/decoding
- Current test coverage is minimal: 4 tests in `tests/test_audio.py`, zero tests for graphic module
- Hardware-dependent components (`RxMicrophone`, `RxSpeaker`, `create_screen_capture`) have no tests
- CI cannot run without mocking hardware dependencies

## Requirements

### Functional Requirements

- Test all public functions and classes in `rxplus/audio.py`
- Test all public functions and classes in `rxplus/graphic.py`
- Tests must run without physical audio/video hardware
- Maintain backwards compatibility with existing test patterns

### Non-Functional Requirements

- **CI compatibility:** All tests pass in GitHub Actions without hardware
- **Execution speed:** Test suite completes in <30 seconds
- **Coverage target:** >80% line coverage for both modules
- **Isolation:** Tests do not interfere with each other (no shared mutable state)

## Design Decisions

### 1. Mocking Strategy for Hardware Components

Will use `unittest.mock.MagicMock` with `monkeypatch` because:
- Pytest's `monkeypatch` provides clean setup/teardown
- `MagicMock` handles PyAudio's callback-based API well
- Avoids complex test doubles while maintaining realistic behavior

Trade-offs:
- Mocks may not catch real hardware edge cases
- Requires understanding of internal implementation details

### 2. JPEG Codec Tests Without Mocking

Will test JPEG functions with synthetic NumPy data because:
- PIL operations are fast and deterministic
- No external dependencies beyond installed packages
- Can verify actual encoding/decoding correctness

### 3. Screen Capture Mocking Approach

Will mock `mss.mss()` context manager because:
- `mss` is the only hardware dependency in graphic module
- Can control frame data shape and content
- Allows testing adaptive FPS logic with controlled timing

## Technical Design

### 1. Core Test Fixtures

```python
# tests/conftest.py

@pytest.fixture
def synthetic_rgb_frame():
    """Generate random RGB frame for video tests."""
    return np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)

@pytest.fixture
def synthetic_audio_float32():
    """Generate sine wave audio chunk for testing."""
    t = np.linspace(0, 0.1, 4800, dtype=np.float32)
    audio = 0.5 * np.sin(2 * np.pi * 440 * t)
    return audio.reshape(-1, 1)

@pytest.fixture
def mock_pyaudio(monkeypatch):
    """Mock PyAudio to avoid hardware dependency."""
    mock_pa = MagicMock()
    mock_stream = MagicMock()
    mock_stream.is_active.return_value = False  # Prevent infinite loops
    mock_pa.open.return_value = mock_stream
    monkeypatch.setattr("rxplus.audio.pyaudio.PyAudio", lambda: mock_pa)
    return mock_pa, mock_stream

@pytest.fixture
def mock_mss(monkeypatch):
    """Mock mss for screen capture tests."""
    fake_bgra = np.zeros((100, 100, 4), dtype=np.uint8)
    mock_sct = MagicMock()
    mock_sct.monitors = [None, {"width": 100, "height": 100}]
    mock_sct.grab.return_value = fake_bgra
    mock_sct.__enter__ = MagicMock(return_value=mock_sct)
    mock_sct.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr("rxplus.graphic.mss.mss", lambda: mock_sct)
    return mock_sct
```

### 2. Test Class Structure

```python
# tests/test_audio.py - additions

class TestRxMicrophone:
    """Tests for RxMicrophone Subject."""
    def test_emits_on_callback(self, mock_pyaudio): ...
    def test_shutdown_on_stream_stop(self, mock_pyaudio): ...
    def test_wraps_errors_in_rx_exception(self, mock_pyaudio): ...

class TestRxSpeaker:
    """Tests for RxSpeaker Subject."""
    def test_writes_to_stream(self, mock_pyaudio): ...
    def test_format_configuration(self, mock_pyaudio): ...

class TestSaveWavFile:
    """Tests for SaveWavFile Observer."""
    def test_writes_chunks(self, tmp_path): ...
    def test_closes_on_completed(self, tmp_path): ...
    def test_closes_on_error(self, tmp_path): ...
```

```python
# tests/test_graphic.py - new file

class TestJPEGCodec:
    """Tests for JPEG encoding/decoding."""
    def test_rgb_to_jpeg_produces_valid_jpeg(self): ...
    def test_jpeg_to_rgb_recovers_dimensions(self): ...
    def test_jpeg_roundtrip_preserves_shape(self): ...
    def test_jpeg_quality_affects_size(self): ...

class TestScreenCapture:
    """Tests for create_screen_capture Observable."""
    def test_emits_rgb_arrays(self, mock_mss): ...
    def test_disposal_stops_loop(self, mock_mss): ...
    def test_bgra_to_rgb_conversion(self, mock_mss): ...
```

### 3. Files Changed

| File | Action | Lines |
|------|--------|-------|
| `tests/test_audio.py` | Extend | Add ~80 lines after line 68 |
| `tests/test_graphic.py` | Create | New file, ~100 lines |
| `tests/conftest.py` | Create | New file, ~50 lines |

**Detailed line references:**
- `tests/test_audio.py` (existing tests at lines 1-68, append new tests)
- `rxplus/audio.py:186-295` — `RxMicrophone` implementation to test
- `rxplus/audio.py:323-366` — `RxSpeaker` implementation to test
- `rxplus/audio.py:373-443` — `SaveWavFile` implementation to test
- `rxplus/graphic.py:18-120` — `create_screen_capture` implementation to test
- `rxplus/graphic.py:123-167` — JPEG codec functions to test

## Implementation Plan

### Phase 1: Test Fixtures Setup
1. Create `tests/conftest.py` with shared fixtures
2. Add `synthetic_rgb_frame` fixture
3. Add `synthetic_audio_float32` fixture
4. Add `mock_pyaudio` fixture
5. Add `mock_mss` fixture

### Phase 2: Audio Test Expansion
1. Add `TestRxMicrophone` class to `tests/test_audio.py`
   - `test_emits_on_callback`
   - `test_shutdown_on_stream_stop`
   - `test_wraps_errors_in_rx_exception`
2. Add `TestRxSpeaker` class
   - `test_writes_to_stream`
   - `test_format_configuration`
3. Add `TestSaveWavFile` class
   - `test_writes_chunks`
   - `test_closes_on_completed`
   - `test_closes_on_error`
4. Add `test_wavfile_roundtrip` integration test

### Phase 3: Video Test Creation
1. Create `tests/test_graphic.py`
2. Add `TestJPEGCodec` class
   - `test_rgb_to_jpeg_produces_valid_jpeg`
   - `test_jpeg_to_rgb_recovers_dimensions`
   - `test_jpeg_roundtrip_preserves_shape`
   - `test_jpeg_quality_affects_size`
3. Add `TestScreenCapture` class
   - `test_emits_rgb_arrays`
   - `test_disposal_stops_loop`
   - `test_bgra_to_rgb_conversion`

### Phase 4: Validation
1. Run full test suite locally
2. Verify coverage meets >80% target
3. Confirm no hardware dependencies in CI path

## Testing Strategy

### Unit Tests

**Audio module (`tests/test_audio.py`):**

| Test Case | Description | Mock Required |
|-----------|-------------|---------------|
| `test_rx_microphone_emits_on_callback` | Verify callback triggers `on_next` | `mock_pyaudio` |
| `test_rx_microphone_shutdown_on_stream_stop` | Verify cleanup when stream stops | `mock_pyaudio` |
| `test_rx_microphone_wraps_errors_in_rx_exception` | Verify error wrapping | `mock_pyaudio` |
| `test_rx_speaker_writes_to_stream` | Verify `on_next` writes to PyAudio | `mock_pyaudio` |
| `test_rx_speaker_format_configuration` | Verify format passed to PyAudio | `mock_pyaudio` |
| `test_save_wavfile_writes_chunks` | Verify chunks written to file | None (file I/O) |
| `test_save_wavfile_closes_on_completed` | Verify file closed on completion | None |
| `test_save_wavfile_closes_on_error` | Verify file closed on error | None |
| `test_wavfile_roundtrip` | Verify create → save produces valid WAV | None |

**Video module (`tests/test_graphic.py`):**

| Test Case | Description | Mock Required |
|-----------|-------------|---------------|
| `test_rgb_to_jpeg_produces_valid_jpeg` | Check JPEG magic bytes `\xff\xd8` | None |
| `test_jpeg_to_rgb_recovers_dimensions` | Verify shape preserved | None |
| `test_jpeg_roundtrip_preserves_shape` | End-to-end codec test | None |
| `test_jpeg_quality_affects_size` | Higher quality → larger file | None |
| `test_screen_capture_emits_rgb_arrays` | Verify output shape `[H,W,3]` | `mock_mss` |
| `test_screen_capture_disposal_stops_loop` | Verify disposal flag honored | `mock_mss` |
| `test_screen_capture_bgra_to_rgb_conversion` | Verify color channel order | `mock_mss` |

### Integration Tests

None required — file-based roundtrip tests cover integration scenarios.

## Observability

Not applicable for test infrastructure.

## Future Considerations

### Potential Enhancements
- Add optional hardware integration tests gated by `RXPLUS_HW_TESTS=1` environment variable
- Add mutation testing with `mutmut` to verify test quality
- Add coverage reporting to CI pipeline

### Known Limitations
- Mocked tests cannot verify actual audio/video quality
- Timing-sensitive tests (FPS accuracy) may be flaky with mocks
- PyAudio callback threading behavior not fully simulated

## Dependencies

- `pytest>=7.0.0` (already in dev dependencies)
- `numpy` (already required by audio/video modules)
- No new dependencies required

## Security Considerations

Not applicable for test infrastructure.

## Rollout Strategy

Not applicable — tests run locally and in CI.

## References

- Proposal document: `docs/proposals/2026-02-01-audio-video-enhancements.md`
- Research document: `docs/research/2026-02-01-video-audio-components.md`
- Key code references:
  - `rxplus/audio.py:186-295` — RxMicrophone
  - `rxplus/audio.py:323-366` — RxSpeaker
  - `rxplus/audio.py:373-443` — SaveWavFile
  - `rxplus/graphic.py:18-120` — create_screen_capture
  - `rxplus/graphic.py:123-167` — JPEG codecs
  - `tests/test_audio.py:1-68` — existing tests
