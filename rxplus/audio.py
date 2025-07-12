
from typing import Any, Callable, Literal, Optional
import scipy
import soundfile as sf
import pyaudio
import asyncio
import threading
import time
import numpy as np

from abc import ABC, abstractmethod

import reactivex as rx
from reactivex import Observable, Observer, Subject, create, operators as ops
from reactivex.disposable import Disposable, CompositeDisposable
from reactivex.scheduler import ThreadPoolScheduler

from .logging import *
from .utils import TaggedData, get_short_error_info, get_full_error_info

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
    '''
    Mapping from PyAudio format constants to (numpy dtype, libsndfile subtype)
    '''
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



def _load_wav_resample(path: str,
                      target_sr: int,
                      target_ch: int) -> np.ndarray:
    """
    load wave file, and resample to target_sr if necessary
    return audio. audio is float32 with shape [samples, channels]
    """
    audio, orig_sr = sf.read(path, always_2d=True)        # 保留多声道
    audio = audio.astype(np.float32)

    # transform to target sample rate
    if orig_sr != target_sr:
        n_target = int(round(len(audio) * target_sr / orig_sr)) # TODO: this resampling is very primitive. There should be the algorithm that support arbitrary rate.
        audio = scipy.signal.resample(audio, n_target, axis=0)

    # transform to target channel
    # TODO: there should be a protocol to transform the wav array between different channel numbers.
    current_ch = audio.shape[1]
    if current_ch > target_ch:
        audio = audio[:, :target_ch]
    elif current_ch < target_ch:
        audio = np.concatenate((audio[:, 0],)*target_ch, 1)

    return audio

def create_wavfile(
    wav_path: str,
    target_sample_rate: int = 48_000,
    target_channels: int = 1,
    frames_per_chunk: int = 1_024,
    scheduler: Optional[rx.abc.SchedulerBase] = None,
):
    """
    Create the observable that loads the local wav file and push the np array. 
    The wav will be automatically transformed to the target sampling rate and channel.
    The np array will always be 2D of shape [sample, channel], in float32 form.
    """
    def subscribe(
        observer: rx.abc.ObserverBase, 
        scheduler_: Optional[rx.abc.SchedulerBase] = None
    ) -> rx.abc.DisposableBase:
        
        # TODO: check whether it is the best choice to use ThreadPoolScheduler as default here
        _scheduler = scheduler or scheduler_ or ThreadPoolScheduler(1)

        audio = _load_wav_resample(wav_path, target_sample_rate, target_ch=target_channels)

        disposed = False

        def action(_: rx.abc.SchedulerBase, __: Any) -> None:
            nonlocal disposed

            try:
                count = 0
                while not disposed and count * frames_per_chunk <= len(audio):
                    # slice the wav and push
                    observer.on_next(audio[count * frames_per_chunk: (count+1) * frames_per_chunk])

                    count += 1

                observer.on_completed()
            
            except Exception as error:
                observer.on_error(error)

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
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        super().__init__()

        # ---------- Audio config ------------------------------------------------
        self.sample_rate = sample_rate
        self.channels = channels
        self.format = format
        self.frames_per_buffer = frames_per_buffer

        # ---------- PyAudio setup ----------------------------------------------
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            format=get_pyaudio_format(self.format),
            channels=self.channels,
            rate=self.sample_rate,
            input=True,
            frames_per_buffer=self.frames_per_buffer,
            stream_callback=self._pyaudio_callback,
        )
        self._stream.start_stream()

        # ---------- Stream-lifecycle watcher -----------------------------------
        self._loop = (
            loop
            if loop is not None
            else (asyncio.get_event_loop() if asyncio.get_event_loop().is_running() else None)
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
        try:
            super().on_next(in_data)
        except Exception as exc:
            super().on_error(exc)
            return (None, pyaudio.paAbort)
        return (None, pyaudio.paContinue)

    # --------------------------------------------------------------------- #
    #                       Async / thread-based watcher                    #
    # --------------------------------------------------------------------- #
    async def _watch_stream(self):
        try:
            assert self._stream is not None, "Stream is not initialized."
            while self._stream.is_active():
                await asyncio.sleep(0.05)
        finally:
            self._shutdown()

    def _watch_stream_sync(self):
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
    '''
    '''
    def __init__(self, 
        format: PCMFormat = "Float32",
        sample_rate: int = 48_000,
        channels: int = 1,
        ):

        super().__init__()

        # ---------- Audio config ------------------------------------------------
        self.sample_rate = sample_rate
        self.channels = channels
        self.format = format

        # ---------- PyAudio setup ----------------------------------------------
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            format=get_pyaudio_format(self.format),
            channels=self.channels,
            rate=self.sample_rate,
            output=True
        )
        self._stream.start_stream()

    def _play_to_soundcard(self, chunk: bytes) -> None:
        """
        Send audio chunk to sound card using PyAudio.
        Requires: pip install pyaudio
        
        Parameters:
            chunk: Audio data, shape [samples, channels]
        """
        
        # Play the chunk
        self._stream.write(chunk)
        
        # Print status like the demo function
        # print(f"Playing {len(chunk)} samples @ {self.sample_rate} Hz through sound card")

    def on_next(self, chunk):
        self._play_to_soundcard(chunk=chunk)


# --------------------------------------------------------------------------
#                       WAV file saving observer
# --------------------------------------------------------------------------
class SaveWavFile:
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