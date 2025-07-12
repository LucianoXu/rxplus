import asyncio
import threading
import sys
import types

import numpy as np
import soundfile as sf

sys.modules.setdefault(
    "pyaudio",
    types.SimpleNamespace(
        PyAudio=lambda *a, **k: None,
        paFloat32=0,
        paInt32=0,
        paInt24=0,
        paInt16=0,
        paUInt8=0,
    ),
)

from rxplus.audio import create_wavfile


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
