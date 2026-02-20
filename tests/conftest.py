"""Shared test fixtures for rxplus tests."""

from unittest.mock import MagicMock

import pytest

# Try to import optional dependencies
try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    np = None

import importlib.util

HAS_PYAUDIO = importlib.util.find_spec("pyaudio") is not None
HAS_MSS = importlib.util.find_spec("mss") is not None

# Combined feature flags
HAS_AUDIO = HAS_NUMPY and HAS_PYAUDIO
HAS_VIDEO = HAS_NUMPY and HAS_MSS


@pytest.fixture
def synthetic_rgb_frame():
    """Generate random RGB frame for video tests."""
    if not HAS_NUMPY:
        pytest.skip("numpy not available")
    return np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)


@pytest.fixture
def synthetic_audio_float32():
    """Generate sine wave audio chunk for testing."""
    if not HAS_NUMPY:
        pytest.skip("numpy not available")
    t = np.linspace(0, 0.1, 4800, dtype=np.float32)
    audio = 0.5 * np.sin(2 * np.pi * 440 * t)
    return audio.reshape(-1, 1)


@pytest.fixture
def mock_pyaudio(monkeypatch):
    """Mock PyAudio to avoid hardware dependency.

    Returns (mock_pa_instance, mock_stream) tuple.
    The stream's is_active() returns True by default to prevent immediate shutdown.
    Tests that need to verify shutdown behavior should set
    is_active.return_value = False.
    """
    if not HAS_AUDIO:
        pytest.skip("audio dependencies not available")
    mock_pa = MagicMock()
    mock_stream = MagicMock()
    mock_stream.is_active.return_value = True  # Keep active to allow testing
    mock_pa.open.return_value = mock_stream
    monkeypatch.setattr("rxplus.audio.pyaudio.PyAudio", lambda: mock_pa)
    return mock_pa, mock_stream


@pytest.fixture
def mock_mss(monkeypatch):
    """Mock mss for screen capture tests.

    Returns the mock_sct object that simulates mss.mss() context manager.
    The grab() method returns a 100x100 BGRA numpy array.
    """
    if not HAS_VIDEO:
        pytest.skip("video dependencies not available")
    # Create fake BGRA frame (mss returns BGRA format)
    fake_bgra = np.zeros((100, 100, 4), dtype=np.uint8)
    # Set some non-zero values to verify color conversion
    fake_bgra[:, :, 0] = 100  # Blue channel
    fake_bgra[:, :, 1] = 150  # Green channel
    fake_bgra[:, :, 2] = 200  # Red channel
    fake_bgra[:, :, 3] = 255  # Alpha channel

    mock_sct = MagicMock()
    mock_sct.monitors = [None, {"width": 100, "height": 100, "left": 0, "top": 0}]
    mock_sct.grab.return_value = fake_bgra

    # Make it work as a context manager
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_sct)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr("rxplus.graphic.mss.mss", lambda: mock_ctx)
    return mock_sct
