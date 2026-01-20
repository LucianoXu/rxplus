import os
import tempfile
import time
from datetime import timedelta

import reactivex as rx

from rxplus.logging import LogItem, Logger, log_filter, drop_log, log_redirect_to


class Collector:
    def __init__(self):
        self.items = []

    def on_next(self, value):
        self.items.append(value)

    def on_error(self, error):
        raise error

    def on_completed(self):
        pass


def test_log_filter():
    logs = [LogItem('a', 'INFO'), LogItem('b', 'DEBUG'), 'x']
    collected = []
    rx.from_(logs).pipe(log_filter({'DEBUG'})).subscribe(collected.append)

    assert len(collected) == 1
    log = collected[0]
    assert isinstance(log, LogItem)
    assert log.level == 'DEBUG'
    assert log.msg == 'b'


def test_drop_log():
    items = [LogItem('a'), 'keep']
    collected = []
    rx.from_(items).pipe(drop_log()).subscribe(collected.append)

    assert collected == ['keep']


def test_log_redirect_to():
    target = Collector()
    output = []
    source = [LogItem('x', 'INFO'), LogItem('y', 'DEBUG'), 1]
    rx.from_(source).pipe(log_redirect_to(target, {'INFO'})).subscribe(output.append)

    assert output == [1]
    assert len(target.items) == 1
    assert isinstance(target.items[0], LogItem)
    assert target.items[0].msg == 'x'


def test_logger_creates_timestamped_file():
    """Test that Logger creates a file with timestamp postfix."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")
        logger = Logger(logfile=logfile)

        logger.on_next(LogItem("test message", "INFO", "Test"))

        # Check that a timestamped file was created (filter out lock files)
        files = [f for f in os.listdir(tmpdir) if f.endswith(".log")]
        assert len(files) == 1
        assert files[0].startswith("test_")
        assert files[0].endswith(".log")
        # Format: test_YYYYMMDDTHHmmss.log
        assert len(files[0]) == len("test_20250120T103045.log")


def test_logger_rotation_by_record_count():
    """Test that Logger rotates files based on record count."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")
        logger = Logger(logfile=logfile, rotate_interval=3)

        # Write 3 records (should stay in first file)
        for i in range(3):
            logger.on_next(LogItem(f"message {i}", "INFO", "Test"))

        first_file = logger.logfile
        assert first_file is not None

        # Write one more record (should trigger rotation)
        time.sleep(1)  # Ensure different timestamp
        logger.on_next(LogItem("message 3", "INFO", "Test"))

        second_file = logger.logfile
        assert second_file is not None
        assert first_file != second_file

        # Check that two files exist
        files = [f for f in os.listdir(tmpdir) if f.endswith(".log")]
        assert len(files) == 2


def test_logger_rotation_by_time():
    """Test that Logger rotates files based on time interval."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")
        # Rotate after 1 second (timestamp resolution is 1 second)
        logger = Logger(logfile=logfile, rotate_interval=timedelta(seconds=1))

        logger.on_next(LogItem("message 1", "INFO", "Test"))
        first_file = logger.logfile

        # Wait for rotation interval (a bit more than 1 second)
        time.sleep(1.1)

        logger.on_next(LogItem("message 2", "INFO", "Test"))
        second_file = logger.logfile

        # Files should be different due to time-based rotation
        assert first_file != second_file

        files = [f for f in os.listdir(tmpdir) if f.endswith(".log")]
        assert len(files) == 2


def test_logger_cleanup_old_logs():
    """Test that Logger cleans up old log files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")

        # Create an "old" log file manually
        old_file = os.path.join(tmpdir, "test_20200101T000000.log")
        with open(old_file, "w") as f:
            f.write("old log")

        # Create logger with 1-day max age (the old file is from 2020)
        logger = Logger(
            logfile=logfile,
            rotate_interval=1,  # Rotate after 1 record
            max_log_age=timedelta(days=1),
        )

        # Write a record to trigger file creation and cleanup
        logger.on_next(LogItem("message", "INFO", "Test"))

        # The old file should have been deleted
        files = [f for f in os.listdir(tmpdir) if f.endswith(".log")]
        assert len(files) == 1
        assert "20200101" not in files[0]


def test_logger_no_rotation_without_interval():
    """Test that Logger doesn't rotate when rotate_interval is None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")
        logger = Logger(logfile=logfile)  # No rotate_interval

        # Write many records
        for i in range(10):
            logger.on_next(LogItem(f"message {i}", "INFO", "Test"))

        # Should still be only one file
        files = [f for f in os.listdir(tmpdir) if f.endswith(".log")]
        assert len(files) == 1


def test_logger_backward_compatible_logfile_property():
    """Test that the logfile property returns the current file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")
        logger = Logger(logfile=logfile)

        # Before any writes, logfile is None
        assert logger.logfile is None

        logger.on_next(LogItem("test", "INFO", "Test"))

        # After write, logfile should be the actual timestamped path
        assert logger.logfile is not None
        assert logger.logfile.startswith(os.path.join(tmpdir, "test_"))
        assert logger.logfile.endswith(".log")
