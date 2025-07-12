import asyncio
import sys
import threading
import types

import numpy as np
import pyaudio
import pytest
import soundfile as sf

from rxplus.audio import (
    create_wavfile,
    get_pyaudio_format,
    get_sf_format,
    resample_audio,
)


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
