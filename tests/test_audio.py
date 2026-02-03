"""Tests for rxplus.audio module."""

import asyncio
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

# Skip entire module if audio dependencies are not available
pytest.importorskip("numpy")
pytest.importorskip("pyaudio")
pytest.importorskip("soundfile")

import numpy as np
import pyaudio
import soundfile as sf

from rxplus.audio import (
    RxMicrophone,
    RxSpeaker,
    SaveWavFile,
    create_wavfile,
    get_pyaudio_format,
    get_sf_format,
    resample_audio,
    save_wavfile,
)
from rxplus.mechanism import RxException


def _make_wav(path):
    data = np.zeros((10, 1), dtype=np.float32)
    sf.write(path, data, samplerate=48000)


def test_create_wavfile_outside_loop(tmp_path):
    wav_path = tmp_path / "test.wav"
    _make_wav(wav_path)
    thread_ids = []

    obs = create_wavfile(str(wav_path), frames_per_chunk=1)
    obs.subscribe(lambda _: thread_ids.append(threading.get_ident()))

    # give scheduler time to emit
    import time

    time.sleep(0.1)

    assert thread_ids, "no data emitted"
    assert all(tid != threading.get_ident() for tid in thread_ids)


async def _collect_inside_loop(wav_path):
    thread_ids = []
    obs = create_wavfile(str(wav_path), frames_per_chunk=1)
    obs.subscribe(lambda _: thread_ids.append(threading.get_ident()))
    await asyncio.sleep(0.1)
    return thread_ids


def test_create_wavfile_inside_loop(tmp_path):
    wav_path = tmp_path / "test.wav"
    _make_wav(wav_path)

    thread_ids = asyncio.run(_collect_inside_loop(wav_path))

    assert thread_ids, "no data emitted"
    assert all(tid == threading.get_ident() for tid in thread_ids)


def test_resample_audio_changes_rate():
    audio = np.arange(8, dtype=np.float32).reshape(-1, 1)
    res = resample_audio(audio, 8, 4)
    assert res.shape[0] == 4


def test_get_pyaudio_format_and_sf_format():
    assert get_pyaudio_format("Float32") == pyaudio.paFloat32
    assert get_sf_format("Int16") == ("int16", "PCM_16")
    with pytest.raises(ValueError):
        get_pyaudio_format("bad")
    with pytest.raises(ValueError):
        get_sf_format("bad")


# =============================================================================
# Audio Format Conversion Tests
# =============================================================================


class TestGetNumpyDtype:
    """Tests for get_numpy_dtype function."""
    
    def test_int16(self):
        from rxplus.audio import get_numpy_dtype
        assert get_numpy_dtype("Int16") == np.dtype(np.int16)
    
    def test_float32(self):
        from rxplus.audio import get_numpy_dtype
        assert get_numpy_dtype("Float32") == np.dtype(np.float32)
    
    def test_int32(self):
        from rxplus.audio import get_numpy_dtype
        assert get_numpy_dtype("Int32") == np.dtype(np.int32)
    
    def test_uint8(self):
        from rxplus.audio import get_numpy_dtype
        assert get_numpy_dtype("UInt8") == np.dtype(np.uint8)
    
    def test_int24_stored_as_int32(self):
        from rxplus.audio import get_numpy_dtype
        # Int24 is stored in int32 container
        assert get_numpy_dtype("Int24") == np.dtype(np.int32)
    
    def test_invalid_format_raises(self):
        from rxplus.audio import get_numpy_dtype
        with pytest.raises(ValueError):
            get_numpy_dtype("invalid")


class TestConvertAudioFormat:
    """Tests for convert_audio_format function."""
    
    def test_same_format_noop(self):
        from rxplus.audio import convert_audio_format
        audio = np.array([1, 2, 3], dtype=np.int16)
        result = convert_audio_format(audio, "Int16", "Int16")
        assert np.array_equal(result, audio)
    
    def test_float32_to_int16(self):
        from rxplus.audio import convert_audio_format
        f32 = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
        int16 = convert_audio_format(f32, "Float32", "Int16")
        
        assert int16.dtype == np.int16
        assert int16[0] == 0
        assert int16[1] == 16383  # 0.5 * 32767
        assert int16[2] == -16383  # -0.5 * 32767
        assert int16[3] == 32767  # 1.0 clamped
        assert int16[4] == -32767  # -1.0 clamped
    
    def test_int16_to_float32(self):
        from rxplus.audio import convert_audio_format
        int16 = np.array([0, 16384, -16384, 32767, -32768], dtype=np.int16)
        f32 = convert_audio_format(int16, "Int16", "Float32")
        
        assert f32.dtype == np.float32
        assert f32[0] == 0.0
        assert 0.4 < f32[1] < 0.6  # ~0.5
        assert -0.6 < f32[2] < -0.4  # ~-0.5
    
    def test_int16_to_uint8(self):
        from rxplus.audio import convert_audio_format
        int16 = np.array([0, 16384, -16384], dtype=np.int16)
        uint8 = convert_audio_format(int16, "Int16", "UInt8")
        
        assert uint8.dtype == np.uint8
        assert uint8[0] == 128  # zero point at 128
        assert uint8[1] > 128  # positive
        assert uint8[2] < 128  # negative
    
    def test_uint8_to_float32(self):
        from rxplus.audio import convert_audio_format
        uint8 = np.array([0, 128, 255], dtype=np.uint8)
        f32 = convert_audio_format(uint8, "UInt8", "Float32")
        
        assert f32.dtype == np.float32
        assert f32[0] == -1.0  # 0 maps to -1.0
        assert f32[1] == 0.0  # 128 maps to 0.0
        assert f32[2] == pytest.approx(1.0, abs=0.01)  # 255 maps to ~1.0
    
    def test_clipping_on_overflow(self):
        from rxplus.audio import convert_audio_format
        # Values outside [-1.0, 1.0] should be clipped
        f32 = np.array([-2.0, 2.0], dtype=np.float32)
        int16 = convert_audio_format(f32, "Float32", "Int16")
        
        assert int16[0] == -32767  # clipped to min
        assert int16[1] == 32767  # clipped to max


# =============================================================================
# RxMicrophone Tests
# =============================================================================


class TestRxMicrophone:
    """Tests for RxMicrophone Subject."""

    @pytest.fixture(autouse=True)
    def setup_event_loop(self):
        """Create an event loop for tests since Python 3.12+ requires it."""
        # Create a new event loop for this test
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        yield loop
        loop.close()
        asyncio.set_event_loop(None)

    def test_emits_on_callback(self, mock_pyaudio, setup_event_loop):
        """Verify that PyAudio callback triggers on_next to observers."""
        mock_pa, mock_stream = mock_pyaudio
        
        # Subscribe to capture emitted data BEFORE creating mic
        # because callback might fire during initialization
        received = []
        
        # Capture the callback function passed to PyAudio.open()
        mic = RxMicrophone(format="Float32", sample_rate=48000, channels=1)
        
        # Subscribe to the mic subject
        mic.subscribe(on_next=lambda x: received.append(x))
        
        # Get the callback that was passed to open()
        call_kwargs = mock_pa.open.call_args
        callback = call_kwargs.kwargs.get("stream_callback") or call_kwargs[1].get("stream_callback")
        
        # Simulate PyAudio calling the callback with audio data
        test_data = b"\x00\x01\x02\x03"
        result = callback(test_data, 1024, None, 0)
        
        assert result == (None, pyaudio.paContinue)
        assert test_data in received

    def test_shutdown_on_stream_stop(self, mock_pyaudio, setup_event_loop):
        """Verify cleanup when stream stops (is_active returns False)."""
        mock_pa, mock_stream = mock_pyaudio
        # Start with active stream
        mock_stream.is_active.return_value = True
        
        completed = []
        mic = RxMicrophone(format="Float32", sample_rate=48000, channels=1)
        mic.subscribe(on_completed=lambda: completed.append(True))
        
        # Now simulate stream stopping
        mock_stream.is_active.return_value = False
        
        # Give watcher thread time to detect stream stopped
        time.sleep(0.15)
        
        # Verify stream was stopped and closed
        assert mock_stream.stop_stream.called or mock_stream.close.called

    def test_format_configuration(self, mock_pyaudio, setup_event_loop):
        """Verify format is correctly passed to PyAudio."""
        mock_pa, mock_stream = mock_pyaudio
        
        mic = RxMicrophone(format="Int16", sample_rate=44100, channels=2)
        
        call_kwargs = mock_pa.open.call_args
        # Check kwargs or positional args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        if "format" in kwargs:
            assert kwargs["format"] == pyaudio.paInt16
        assert kwargs.get("rate") == 44100 or call_kwargs[1].get("rate") == 44100
        assert kwargs.get("channels") == 2 or call_kwargs[1].get("channels") == 2

    def test_device_index_passed_to_pyaudio(self, mock_pyaudio, setup_event_loop):
        """Verify device_index is correctly passed to PyAudio.open()."""
        mock_pa, mock_stream = mock_pyaudio
        
        mic = RxMicrophone(format="Float32", sample_rate=48000, channels=1, device_index=3)
        
        call_kwargs = mock_pa.open.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        assert kwargs.get("input_device_index") == 3

    def test_device_index_none_by_default(self, mock_pyaudio, setup_event_loop):
        """Verify device_index is None when not specified."""
        mock_pa, mock_stream = mock_pyaudio
        
        mic = RxMicrophone(format="Float32", sample_rate=48000, channels=1)
        
        call_kwargs = mock_pa.open.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        assert kwargs.get("input_device_index") is None


# =============================================================================
# RxSpeaker Tests
# =============================================================================


class TestRxSpeaker:
    """Tests for RxSpeaker Subject."""

    def test_writes_to_stream(self, mock_pyaudio):
        """Verify on_next writes audio data to PyAudio stream."""
        mock_pa, mock_stream = mock_pyaudio
        
        speaker = RxSpeaker(format="Float32", sample_rate=48000, channels=1)
        
        test_data = b"\x00\x01\x02\x03"
        speaker.on_next(test_data)
        
        mock_stream.write.assert_called_once_with(test_data)

    def test_format_configuration(self, mock_pyaudio):
        """Verify format is correctly passed to PyAudio."""
        mock_pa, mock_stream = mock_pyaudio
        
        speaker = RxSpeaker(format="Int16", sample_rate=44100, channels=2)
        
        call_kwargs = mock_pa.open.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        if "format" in kwargs:
            assert kwargs["format"] == pyaudio.paInt16
        # Verify output mode
        assert kwargs.get("output") is True or call_kwargs[1].get("output") is True

    def test_device_index_passed_to_pyaudio(self, mock_pyaudio):
        """Verify device_index is correctly passed to PyAudio.open()."""
        mock_pa, mock_stream = mock_pyaudio
        
        speaker = RxSpeaker(format="Float32", sample_rate=48000, channels=1, device_index=2)
        
        call_kwargs = mock_pa.open.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        assert kwargs.get("output_device_index") == 2

    def test_device_index_none_by_default(self, mock_pyaudio):
        """Verify device_index is None when not specified."""
        mock_pa, mock_stream = mock_pyaudio
        
        speaker = RxSpeaker(format="Float32", sample_rate=48000, channels=1)
        
        call_kwargs = mock_pa.open.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        assert kwargs.get("output_device_index") is None


# =============================================================================
# SaveWavFile Tests
# =============================================================================


class TestSaveWavFile:
    """Tests for SaveWavFile Observer."""

    def test_writes_chunks(self, tmp_path):
        """Verify chunks are written to the WAV file."""
        wav_path = tmp_path / "output.wav"
        
        observer = SaveWavFile(
            str(wav_path),
            format="Float32",
            sample_rate=48000,
            channels=1,
        )
        
        # Write some audio chunks
        chunk = np.zeros(1024, dtype=np.float32).tobytes()
        observer.on_next(chunk)
        observer.on_next(chunk)
        observer.on_completed()
        
        # Verify file exists and has content
        assert wav_path.exists()
        data, sr = sf.read(str(wav_path))
        assert sr == 48000
        assert len(data) == 2048  # Two chunks of 1024 samples

    def test_closes_on_completed(self, tmp_path):
        """Verify file is properly closed on completion."""
        wav_path = tmp_path / "output.wav"
        
        observer = SaveWavFile(str(wav_path), format="Float32", sample_rate=48000, channels=1)
        
        chunk = np.zeros(1024, dtype=np.float32).tobytes()
        observer.on_next(chunk)
        observer.on_completed()
        
        # File should be readable (properly closed)
        data, sr = sf.read(str(wav_path))
        assert len(data) == 1024

    def test_closes_on_error(self, tmp_path):
        """Verify file is closed even on error."""
        wav_path = tmp_path / "output.wav"
        
        observer = SaveWavFile(str(wav_path), format="Float32", sample_rate=48000, channels=1)
        
        chunk = np.zeros(1024, dtype=np.float32).tobytes()
        observer.on_next(chunk)
        
        # Simulate error
        with pytest.raises(ValueError):
            observer.on_error(ValueError("test error"))
        
        # File should still exist (was written before error)
        assert wav_path.exists()


# =============================================================================
# Wavfile Roundtrip Test
# =============================================================================


def test_wavfile_roundtrip(tmp_path):
    """Verify create_wavfile -> save_wavfile produces valid WAV."""
    # Create source WAV file
    source_path = tmp_path / "source.wav"
    source_data = np.sin(np.linspace(0, 2 * np.pi, 4800)).astype(np.float32).reshape(-1, 1)
    sf.write(str(source_path), source_data, samplerate=48000)
    
    # Create destination path
    dest_path = tmp_path / "dest.wav"
    
    # Create the roundtrip pipeline
    dest_observer = save_wavfile(str(dest_path), format="Float32", sample_rate=48000, channels=1)
    
    # Collect chunks and write them
    chunks = []
    
    def on_next(chunk):
        chunks.append(chunk)
        dest_observer.on_next(chunk.tobytes())
    
    obs = create_wavfile(str(source_path), frames_per_chunk=1024)
    obs.subscribe(on_next=on_next, on_completed=lambda: dest_observer.on_completed())
    
    # Wait for async processing
    time.sleep(0.2)
    
    # Verify output file
    assert dest_path.exists()
    dest_data, dest_sr = sf.read(str(dest_path))
    assert dest_sr == 48000
    assert len(dest_data) == len(source_data)


# =============================================================================
# Channel Conversion Tests
# =============================================================================


class TestConvertChannels:
    """Tests for _convert_channels function."""

    def test_stereo_to_mono_averages(self):
        """Verify stereo to mono uses channel averaging."""
        from rxplus.audio import _convert_channels

        # Create stereo audio with different values per channel
        stereo = np.array([[1.0, 0.0], [0.5, 0.5], [0.0, 1.0]], dtype=np.float32)
        mono = _convert_channels(stereo, 1)

        assert mono.shape == (3, 1)
        np.testing.assert_array_almost_equal(mono[:, 0], [0.5, 0.5, 0.5])

    def test_mono_to_stereo_duplicates(self):
        """Verify mono to stereo duplicates the channel."""
        from rxplus.audio import _convert_channels

        mono = np.array([[0.5], [0.7], [0.3]], dtype=np.float32)
        stereo = _convert_channels(mono, 2)

        assert stereo.shape == (3, 2)
        np.testing.assert_array_equal(stereo[:, 0], stereo[:, 1])
        np.testing.assert_array_equal(stereo[:, 0], mono[:, 0])

    def test_same_channel_count_noop(self):
        """Verify same channel count returns unchanged array."""
        from rxplus.audio import _convert_channels

        audio = np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32)
        result = _convert_channels(audio, 2)

        assert result is audio  # Should be same object

    def test_downmix_multichannel(self):
        """Verify 4-channel to 2-channel downmix."""
        from rxplus.audio import _convert_channels

        # 4-channel audio
        quad = np.array(
            [[1.0, 2.0, 3.0, 4.0], [0.0, 0.0, 0.0, 0.0]], dtype=np.float32
        )
        stereo = _convert_channels(quad, 2)

        assert stereo.shape == (2, 2)
        # First channel preserved, rest averaged into second
        assert stereo[0, 0] == 1.0
        assert stereo[0, 1] == (2.0 + 3.0 + 4.0) / 3  # Average of channels 1,2,3

    def test_upmix_multichannel(self):
        """Verify 2-channel to 4-channel upmix."""
        from rxplus.audio import _convert_channels

        stereo = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
        quad = _convert_channels(stereo, 4)

        assert quad.shape == (2, 4)
        # Original channels preserved
        assert quad[0, 0] == 1.0
        assert quad[0, 1] == 2.0
        # Last channel duplicated
        assert quad[0, 2] == 2.0
        assert quad[0, 3] == 2.0

    def test_multichannel_to_mono(self):
        """Verify multi-channel to mono averages all channels."""
        from rxplus.audio import _convert_channels

        quad = np.array([[1.0, 2.0, 3.0, 4.0]], dtype=np.float32)
        mono = _convert_channels(quad, 1)

        assert mono.shape == (1, 1)
        assert mono[0, 0] == (1.0 + 2.0 + 3.0 + 4.0) / 4
