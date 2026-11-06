"""Locking of FileLogRecordExporter."""

import time

import pytest

from rxplus.telemetry import FileLogRecordExporter


def test_file_exporter_lock_propagates_errors_without_waiting(tmp_path):
    """An error inside the locked block surfaces at once and unchanged."""
    exporter = FileLogRecordExporter(str(tmp_path / "a.log"), lock_timeout=2.0)
    exporter._ensure_file_open()
    start = time.monotonic()
    with pytest.raises(ValueError, match="write failed"):
        with exporter._acquire_lock():
            raise ValueError("write failed")
    assert time.monotonic() - start < 0.5
    with exporter._acquire_lock():  # the lock is usable afterwards
        pass
    exporter.shutdown()
