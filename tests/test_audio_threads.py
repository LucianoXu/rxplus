"""create_wavfile must not leave a worker thread behind per subscription."""

import threading
import time

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pyaudio")
pytest.importorskip("soundfile")

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from rxplus.audio import create_wavfile  # noqa: E402


def test_create_wavfile_does_not_leak_threads(tmp_path):
    """Each subscription outside a loop must not leave a worker thread behind."""
    wav_path = tmp_path / "short.wav"
    sf.write(wav_path, np.zeros((256, 1), dtype=np.float32), samplerate=48000)
    before = threading.active_count()
    for _ in range(20):
        done = threading.Event()
        create_wavfile(str(wav_path)).subscribe(on_completed=done.set)
        assert done.wait(2.0)
    time.sleep(0.2)
    assert threading.active_count() - before <= 1


def test_script_whose_last_line_subscribes_emits_all_chunks(tmp_path):
    """The emitting thread is non-daemon: the interpreter waits for it."""
    import subprocess
    import sys
    import textwrap

    wav_path = tmp_path / "four.wav"
    sf.write(wav_path, np.zeros((4000, 1), dtype=np.float32), samplerate=48000)
    script = tmp_path / "last_line_subscribes.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from rxplus.audio import create_wavfile

            create_wavfile({str(wav_path)!r}, frames_per_chunk=1024).subscribe(
                on_next=lambda chunk: print(len(chunk), flush=True),
                on_completed=lambda: print("done", flush=True),
            )
            """
        )
    )
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["1024", "1024", "1024", "928", "done"]
