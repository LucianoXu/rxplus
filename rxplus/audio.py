"""Audio helpers built on top of ReactiveX."""

import asyncio
import math
import queue
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Literal, Optional

import numpy as np
import pyaudio
import reactivex as rx
import scipy
import soundfile as sf
from reactivex import Observable, Observer, Subject, create
from reactivex import operators as ops
from reactivex.disposable import CompositeDisposable, Disposable
from reactivex.scheduler import ThreadPoolScheduler
from reactivex.scheduler.eventloop import AsyncIOScheduler

from .mechanism import RxException
from .utils import TaggedData, get_full_error_info, get_short_error_info

PCMFormat = Literal["UInt8", "Int16", "Int24", "Int32", "Float32"]


def get_pyaudio_format(format: PCMFormat) -> int:
    """
    Get the pyaudio format from the PCMFormat.
    """
    if format == "UInt8":
        return pyaudio.paUInt8
    elif format == "Int16":
        return pyaudio.paInt16
    elif format == "Int24":
        return pyaudio.paInt24
    elif format == "Int32":
        return pyaudio.paInt32
    elif format == "Float32":
        return pyaudio.paFloat32
    else:
        raise ValueError(f"Unexpected PCMFormat: {format}")


def get_sf_format(format: PCMFormat) -> tuple[str, str]:
    """Return ``(numpy dtype, libsndfile subtype)`` for the given format."""
    if format == "UInt8":
        return ("uint8", "PCM_U8")
    elif format == "Int16":
        return ("int16", "PCM_16")
    elif format == "Int24":
        return ("int24", "PCM_24")
    elif format == "Int32":
        return ("int32", "PCM_32")
    elif format == "Float32":
        return ("float32", "FLOAT")
    else:
        raise ValueError(f"Unexpected PCMFormat: {format}")


def get_numpy_dtype(format: PCMFormat) -> np.dtype:
    """Return the numpy dtype for the given PCM format.
    
    Args:
        format: PCM format string ("UInt8", "Int16", "Int24", "Int32", "Float32")
        
    Returns:
        Corresponding numpy dtype
        
    Raises:
        ValueError: If format is not recognized
    """
    dtype_map: dict[PCMFormat, np.dtype] = {
        "UInt8": np.dtype(np.uint8),
        "Int16": np.dtype(np.int16),
        "Int24": np.dtype(np.int32),  # Int24 stored in int32
        "Int32": np.dtype(np.int32),
        "Float32": np.dtype(np.float32),
    }
    if format not in dtype_map:
        raise ValueError(f"Unexpected PCMFormat: {format}")
    return dtype_map[format]


def convert_audio_format(
    audio: np.ndarray,
    source_format: PCMFormat,
    target_format: PCMFormat,
) -> np.ndarray:
    """Convert audio data between different PCM formats.
    
    Handles proper scaling when converting between integer and floating-point
    representations:
    - Float32 uses range [-1.0, 1.0]
    - Int16 uses range [-32768, 32767]
    - Int32 uses full 32-bit range
    - UInt8 uses range [0, 255] with 128 as center
    
    Args:
        audio: Input audio array (any shape, will be treated as samples)
        source_format: Current format of the audio
        target_format: Desired output format
        
    Returns:
        Audio array converted to target format with proper scaling
        
    Example:
        >>> # Convert Float32 [-1.0, 1.0] to Int16 [-32768, 32767]
        >>> audio_int16 = convert_audio_format(audio_f32, "Float32", "Int16")
        >>> # Convert Int16 back to Float32
        >>> audio_f32 = convert_audio_format(audio_int16, "Int16", "Float32")
    """
    if source_format == target_format:
        return audio
    
    # First normalize to Float32 range [-1.0, 1.0]
    if source_format == "Float32":
        normalized = audio.astype(np.float32)
    elif source_format == "Int16":
        normalized = audio.astype(np.float32) / 32768.0
    elif source_format == "Int32":
        normalized = audio.astype(np.float32) / np.iinfo(np.int32).max
    elif source_format == "Int24":
        max24 = 2**23 - 1
        normalized = audio.astype(np.float32) / max24
    elif source_format == "UInt8":
        normalized = (audio.astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"Unexpected source format: {source_format}")
    
    # Clip to valid range
    normalized = np.clip(normalized, -1.0, 1.0)
    
    # Convert from normalized Float32 to target format
    if target_format == "Float32":
        return normalized
    elif target_format == "Int16":
        return (normalized * 32767).astype(np.int16)
    elif target_format == "Int32":
        return (normalized * np.iinfo(np.int32).max).astype(np.int32)
    elif target_format == "Int24":
        max24 = 2**23 - 1
        return (normalized * max24).astype(np.int32)
    elif target_format == "UInt8":
        return ((normalized * 127.5) + 128.0).astype(np.uint8)
    else:
        raise ValueError(f"Unexpected target format: {target_format}")


def resample_audio(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Resample audio to a new sample rate using ``scipy.signal.resample_poly``.

    .. note::

        This function processes each call independently and does **not**
        carry filter state between calls.  For chunk-by-chunk streaming
        use :class:`StreamingResampler` instead.
    """
    if orig_sr == target_sr:
        return audio

    up = target_sr
    down = orig_sr
    factor = math.gcd(up, down)
    up //= factor
    down //= factor

    resampled = scipy.signal.resample_poly(audio, up, down, axis=0)
    return resampled.astype(audio.dtype, copy=False)


class StreamingResampler:
    """Stateful rational resampler for chunk-by-chunk audio processing.

    Unlike :func:`resample_audio`, this class preserves the anti-aliasing
    filter state across successive calls to :meth:`process`, eliminating
    the click / buzz artefacts that occur at chunk boundaries when each
    chunk is resampled independently.

    Internally the resampler performs three steps:

    1. **Upsample** by factor *up* (zero-insertion with gain compensation).
    2. **Anti-alias lowpass filter** (Butterworth IIR in SOS form) with
       persistent ``zi`` state via :func:`scipy.signal.sosfilt`.
    3. **Decimate** by factor *down* with a phase counter that tracks the
       correct decimation offset across chunks.

    Args:
        orig_sr:  Source sample rate (Hz).
        target_sr: Target sample rate (Hz).
        filter_order: Order of the Butterworth anti-aliasing filter.
            Higher values give a sharper transition band at the cost of
            more computation and potential ringing.

    Example::

        resampler = StreamingResampler(48_000, 16_000)
        for chunk in mic_chunks:   # numpy int16 arrays
            out = resampler.process(chunk)
            send(out.tobytes())
    """

    def __init__(
        self,
        orig_sr: int,
        target_sr: int,
        filter_order: int = 8,
    ) -> None:
        self._orig_sr = orig_sr
        self._target_sr = target_sr

        if orig_sr == target_sr:
            self._passthrough = True
            return
        self._passthrough = False

        g = math.gcd(orig_sr, target_sr)
        self._up: int = target_sr // g
        self._down: int = orig_sr // g

        # Anti-aliasing Butterworth lowpass.
        # Cutoff at 90 % of the lower Nyquist rate (normalized to [0, 1]
        # where 1 == Nyquist of the *effective* sample rate after upsampling).
        max_rate = max(self._up, self._down)
        wn = 0.9 / max_rate
        self._sos: np.ndarray = scipy.signal.butter(
            filter_order, wn, btype="low", output="sos",
        )
        # Initial filter state — start from zero.
        self._zi: np.ndarray = scipy.signal.sosfilt_zi(self._sos) * 0.0

        # Phase counter for decimation (0 … down-1).
        self._decim_phase: int = 0

    # ------------------------------------------------------------------ #

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Resample one chunk and return the result.

        The returned array has the same dtype as *audio*.

        Args:
            audio: 1-D numpy array of PCM samples.

        Returns:
            Resampled 1-D numpy array.  Length ≈ ``len(audio) * up / down``.
        """
        if self._passthrough:
            return audio

        original_dtype = audio.dtype
        x = audio.astype(np.float64)

        # --- Upsample: zero-insertion with gain compensation ---
        if self._up > 1:
            up_len = len(x) * self._up
            upsampled = np.zeros(up_len, dtype=np.float64)
            upsampled[:: self._up] = x * self._up
            x = upsampled

        # --- Anti-aliasing filter (stateful IIR) ---
        x, self._zi = scipy.signal.sosfilt(self._sos, x, zi=self._zi)

        # --- Decimate with phase tracking ---
        if self._down > 1:
            start = (self._down - self._decim_phase) % self._down
            result = x[start :: self._down]
            # Advance phase so the next chunk picks up where we left off.
            self._decim_phase = (self._decim_phase + len(x)) % self._down
            x = result

        return x.astype(original_dtype, copy=False)

    def reset(self) -> None:
        """Reset the internal filter state and decimation phase."""
        if not self._passthrough:
            self._zi = scipy.signal.sosfilt_zi(self._sos) * 0.0
            self._decim_phase = 0


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
        return audio.mean(axis=1, keepdims=True).astype(audio.dtype)
    elif current_ch == 1 and target_ch == 2:
        # Mono to stereo: duplicate
        return np.repeat(audio, 2, axis=1)
    elif current_ch > target_ch:
        # Downmix: keep first (target-1) channels, average rest into last
        if target_ch == 1:
            return audio.mean(axis=1, keepdims=True).astype(audio.dtype)
        result = audio[:, : target_ch - 1]
        mixed = audio[:, target_ch - 1 :].mean(axis=1, keepdims=True).astype(audio.dtype)
        return np.concatenate([result, mixed], axis=1)
    else:
        # Upmix: duplicate last channel
        padding = np.repeat(audio[:, -1:], target_ch - current_ch, axis=1)
        return np.concatenate([audio, padding], axis=1)


def _load_wav_resample(
    path: str,
    target_format: PCMFormat = "Float32",
    target_sr: int = 48_000,
    target_ch: int = 1,
) -> np.ndarray:
    """
    load wave file, and resample to target_sr if necessary
    return audio. audio is float32 with shape [samples, channels]
    The returned array is also converted to **target_format**.
    """
    audio, orig_sr = sf.read(path, always_2d=True)
    audio = audio.astype(np.float32)

    # transform to target sample rate
    audio = resample_audio(audio, orig_sr, target_sr)

    # transform to target channel using proper mixing
    audio = _convert_channels(audio, target_ch)

    # ------------------------------------------------------------------ #
    #                   Convert to the target PCM dtype                  #
    # ------------------------------------------------------------------ #
    if target_format != "Float32":
        if target_format == "Int32":
            audio = np.clip(
                audio * np.iinfo(np.int32).max,
                np.iinfo(np.int32).min,
                np.iinfo(np.int32).max,
            ).astype(np.int32)

        elif target_format == "Int24":
            max24 = 2**23 - 1
            audio = np.clip(audio * max24, -max24 - 1, max24).astype(np.int32)

        elif target_format == "Int16":
            audio = np.clip(
                audio * np.iinfo(np.int16).max,
                np.iinfo(np.int16).min,
                np.iinfo(np.int16).max,
            ).astype(np.int16)

        elif target_format == "UInt8":
            audio = np.clip((audio * 127.5) + 127.5, 0, 255).astype(np.uint8)

        else:
            raise ValueError(f"Unexpected PCMFormat: {target_format}")

    return audio


def create_wavfile(
    wav_path: str,
    target_sample_rate: int = 48_000,
    target_channels: int = 1,
    target_format: PCMFormat = "Float32",
    frames_per_chunk: int = 1_024,
    scheduler: Optional[rx.abc.SchedulerBase] = None,
):
    """
    Create an Observable that loads a local WAV file and emits it chunk‑by‑chunk.

    The loader automatically:
      1. Resamples to *target_sample_rate* (Hz),
      2. Converts to *target_channels* interleaved channels, **and**
      3. Casts the samples to the requested *target_format* (UInt8 / Int16 /
         Int24 / Int32 / Float32).

    The emitted NumPy array is always 2‑D with shape ``[samples, channels]`` and
    uses the dtype implied by *target_format*.
    """

    def subscribe(
        observer: rx.abc.ObserverBase, scheduler_: Optional[rx.abc.SchedulerBase] = None
    ) -> rx.abc.DisposableBase:

        try:
            loop = asyncio.get_running_loop()
            running = loop.is_running()
        except RuntimeError:
            loop = None
            running = False

        _scheduler = (
            scheduler
            or scheduler_
            or (
                AsyncIOScheduler(loop)  # type: ignore[assignment]
                if running
                else ThreadPoolScheduler(1)
            )
        )

        audio = _load_wav_resample(
            wav_path,
            target_format=target_format,
            target_sr=target_sample_rate,
            target_ch=target_channels,
        )

        disposed = False

        def action(_: rx.abc.SchedulerBase, __: Any) -> None:
            nonlocal disposed

            try:
                count = 0
                while not disposed and count * frames_per_chunk <= len(audio):
                    # slice the wav and push
                    observer.on_next(
                        audio[count * frames_per_chunk : (count + 1) * frames_per_chunk]
                    )

                    count += 1

                observer.on_completed()

            except Exception as error:
                rx_exception = RxException(
                    error, note=f"Error while loading WAV file {wav_path}"
                )
                observer.on_error(rx_exception)

        def dispose() -> None:
            nonlocal disposed
            disposed = True

        disp = Disposable(dispose)
        return CompositeDisposable(_scheduler.schedule(action), disp)

    return Observable(subscribe)


class RxMicrophone(Subject):
    """
    A reactivex Subject that emits audio data from the microphone.

    This class is a Subject that emits audio data from the microphone. It can be used to create a stream of audio data.
    """

    def __init__(
        self,
        format: PCMFormat = "Float32",
        sample_rate: int = 48_000,
        channels: int = 1,
        frames_per_buffer: int = 1_024,
        device_index: Optional[int] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        super().__init__()

        # ---------- Audio config ------------------------------------------------
        self.sample_rate = sample_rate
        self.channels = channels
        self.format = format
        self.frames_per_buffer = frames_per_buffer
        self.device_index = device_index

        # ---------- PyAudio setup ----------------------------------------------
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            format=get_pyaudio_format(self.format),
            channels=self.channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=self.frames_per_buffer,
            stream_callback=self._pyaudio_callback,
        )
        self._stream.start_stream()

        # ---------- Stream-lifecycle watcher -----------------------------------
        self._loop = (
            loop
            if loop is not None
            else (
                asyncio.get_event_loop()
                if asyncio.get_event_loop().is_running()
                else None
            )
        )

        if self._loop and self._loop.is_running():
            self._watcher_task = self._loop.create_task(self._watch_stream())
        else:
            self._watcher_thread = threading.Thread(
                target=self._watch_stream_sync, daemon=True
            )
            self._watcher_thread.start()

    # --------------------------------------------------------------------- #
    #                           PyAudio callback                            #
    # --------------------------------------------------------------------- #
    def _pyaudio_callback(self, in_data, frame_count, time_info, status):
        """Forward audio frames from PyAudio to observers."""
        from reactivex.internal.exceptions import DisposedException

        try:
            super().on_next(in_data)

        except DisposedException:
            # Subject has been disposed - stop the stream gracefully
            return (None, pyaudio.paAbort)

        except Exception as exc:
            rx_exception = RxException(exc, note="Error in PyAudio callback")
            try:
                super().on_error(rx_exception)
            except DisposedException:
                # Subject disposed during error handling - just abort
                pass
            return (None, pyaudio.paAbort)

        return (None, pyaudio.paContinue)

    # --------------------------------------------------------------------- #
    #                       Async / thread-based watcher                    #
    # --------------------------------------------------------------------- #
    async def _watch_stream(self):
        """Watch the PyAudio stream from the asyncio event loop."""
        try:
            assert self._stream is not None, "Stream is not initialized."
            while self._stream.is_active():
                await asyncio.sleep(0.05)
        finally:
            self._shutdown()

    def _watch_stream_sync(self):
        """Synchronous watcher used when no running loop is available."""
        try:
            assert self._stream is not None, "Stream is not initialized."
            while self._stream.is_active():
                time.sleep(0.05)
        finally:
            self._shutdown()

    # --------------------------------------------------------------------- #
    #                          Cleanup / teardown                           #
    # --------------------------------------------------------------------- #
    def _shutdown(self):
        """Stop the PyAudio stream and notify observers of completion."""
        assert self._stream is not None, "Stream is not initialized."
        assert self._pa is not None, "PyAudio is not initialized."
        if getattr(self, "_stream", None):
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if getattr(self, "_pa", None):
            self._pa.terminate()
            self._pa = None
        try:
            super().on_completed()
        except Exception:
            pass


class RxSpeaker(Subject):
    """Play incoming audio chunks through the system sound device.

    ``on_next`` is **non-blocking**: audio data is placed on an internal
    ``queue.Queue`` and consumed by a dedicated playback thread so that the
    subscriber thread is freed immediately.

    Args:
        format: PCM sample format (default ``"Float32"``).
        sample_rate: Playback sample rate in Hz (default 48 000).
        channels: Number of audio channels (default 1).
        device_index: PyAudio output device index (``None`` = default device).
        playback_queue_maxsize: Maximum number of frames in the internal queue.
            A value of 200 × 1 920 B ≈ 384 kB ≈ 4 s of 20 ms frames.
            Frames are dropped (with a WARNING log) when the queue is full.
    """

    _SENTINEL = object()  # signals the playback thread to exit

    def __init__(
        self,
        format: PCMFormat = "Float32",
        sample_rate: int = 48_000,
        channels: int = 1,
        device_index: Optional[int] = None,
        playback_queue_maxsize: int = 200,
    ):

        super().__init__()

        # ---------- Audio config ------------------------------------------------
        self.sample_rate = sample_rate
        self.channels = channels
        self.format = format
        self.device_index = device_index

        # ---------- PyAudio setup ----------------------------------------------
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            format=get_pyaudio_format(self.format),
            channels=self.channels,
            rate=self.sample_rate,
            output=True,
            output_device_index=device_index,
        )
        self._stream.start_stream()

        # ---------- Playback queue + thread ------------------------------------
        self._queue: queue.Queue = queue.Queue(maxsize=playback_queue_maxsize)
        self._playback_thread = threading.Thread(
            target=self._playback_loop, daemon=True, name="RxSpeaker-playback"
        )
        self._playback_thread.start()

    # ------------------------------------------------------------------
    # Internal playback loop
    # ------------------------------------------------------------------

    def _playback_loop(self) -> None:
        """Consume frames from ``_queue`` and write them to PyAudio synchronously."""
        while True:
            item = self._queue.get()
            if item is self._SENTINEL:
                break
            try:
                if self._stream is not None:
                    self._stream.write(item)
            except Exception:
                pass  # stream may have been closed

    # ------------------------------------------------------------------
    # Subject interface overrides
    # ------------------------------------------------------------------

    def on_next(self, chunk: bytes) -> None:  # type: ignore[override]
        """Enqueue *chunk* for playback; returns immediately (non-blocking)."""
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            # Drop the frame rather than blocking the caller.
            pass

    def on_completed(self) -> None:  # type: ignore[override]
        """Signal the playback thread to finish and clean up resources."""
        self._queue.put_nowait(self._SENTINEL)
        self._playback_thread.join(timeout=5.0)
        self._teardown()
        super().on_completed()

    def dispose(self) -> None:  # type: ignore[override]
        """Dispose and clean up PyAudio resources."""
        try:
            self._queue.put_nowait(self._SENTINEL)
        except queue.Full:
            pass
        self._playback_thread.join(timeout=5.0)
        self._teardown()
        super().dispose()

    def _teardown(self) -> None:
        """Close the PyAudio stream and terminate PyAudio."""
        if getattr(self, "_stream", None):
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if getattr(self, "_pa", None):
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


# --------------------------------------------------------------------------
#                       WAV file saving observer
# --------------------------------------------------------------------------
class SaveWavFile(rx.abc.ObserverBase):
    """
    Observer that saves incoming audio byte chunks to a WAV file **using
    the `soundfile` library**.

    Each ``on_next`` call writes the raw PCM bytes directly to disk with
    ``SoundFile.buffer_write`` so it works well for streaming audio.

    Parameters
    ----------
    path
        Destination file path.
    format
        PyAudio sample format constant (defaults to ``pyaudio.paFloat32``).
    sample_rate
        Sampling rate in Hz.
    channels
        Number of interleaved channels.
    """

    # Mapping from PyAudio format constants to (numpy dtype, libsndfile subtype)

    def __init__(
        self,
        path: str,
        format: PCMFormat = "Float32",
        sample_rate: int = 48_000,
        channels: int = 1,
    ):
        # Resolve dtype / subtype from the format table (fallback to float32)
        dtype, subtype = get_sf_format(format)

        # Open file for streaming writes
        self._sf = sf.SoundFile(
            path,
            mode="w",
            samplerate=sample_rate,
            channels=channels,
            subtype=subtype,
            format="WAV",
        )
        self._dtype = dtype

    # ------------------------------------------------------------------ #
    #                  Observer interface implementation                  #
    # ------------------------------------------------------------------ #
    def on_next(self, chunk: bytes):
        """Write the next audio chunk."""
        self._sf.buffer_write(chunk, dtype=self._dtype)

    def on_completed(self):
        """Flush buffers and close the WAV file gracefully."""
        if getattr(self, "_sf", None):
            self._sf.flush()
            self._sf.close()

    def on_error(self, error: Exception):
        """Close the file on error and propagate."""
        if getattr(self, "_sf", None):
            self._sf.close()

        # raise the error because it is an observer
        raise error


def save_wavfile(
    path: str,
    format: PCMFormat = "Float32",
    sample_rate: int = 48_000,
    channels: int = 1,
):
    """
    Factory helper that returns a :class:`SaveWavFile` observer.

    Mirrors the naming of `create_wavfile` (observable) with a
    complementary *save* side (observer).
    """
    return SaveWavFile(
        path,
        format=format,
        sample_rate=sample_rate,
        channels=channels,
    )
