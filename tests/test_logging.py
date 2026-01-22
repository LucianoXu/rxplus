"""
Tests for OpenTelemetry-based logging in rxplus.
"""

import os
import tempfile
import time
from datetime import timedelta

import reactivex as rx
from opentelemetry._logs import SeverityNumber, LogRecord

from rxplus.logging import (
    LOG_LEVEL,
    SEVERITY_MAP,
    Logger,
    NamedLogComp,
    EmptyLogComp,
    create_log_record,
    format_log_record,
    log_filter,
    drop_log,
    log_redirect_to,
    configure_otel_logging,
)


class Collector:
    """Helper class to collect items from a reactive stream."""

    def __init__(self):
        self.items = []

    def on_next(self, value):
        self.items.append(value)

    def on_error(self, error):
        raise error

    def on_completed(self):
        pass


# =============================================================================
# Tests for create_log_record
# =============================================================================

def test_create_log_record_default():
    """Verify factory creates LogRecord with INFO severity by default."""
    record = create_log_record("test message")

    assert isinstance(record, LogRecord)
    assert record.body == "test message"
    assert record.severity_number == SeverityNumber.INFO
    assert record.severity_text == "INFO"
    assert record.attributes["log.source"] == "Unknown"
    assert record.timestamp is not None
    assert record.timestamp > 0


def test_create_log_record_all_levels():
    """Verify each LOG_LEVEL maps to correct SeverityNumber."""
    test_cases = [
        ("DEBUG", SeverityNumber.DEBUG),
        ("INFO", SeverityNumber.INFO),
        ("WARN", SeverityNumber.WARN),
        ("ERROR", SeverityNumber.ERROR),
        ("FATAL", SeverityNumber.FATAL),
    ]

    for level, expected_severity in test_cases:
        record = create_log_record("msg", level=level)
        assert record.severity_number == expected_severity, f"Failed for {level}"
        assert record.severity_text == level


def test_create_log_record_source():
    """Verify source is stored in log.source attribute."""
    record = create_log_record("msg", source="MyComponent")
    assert record.attributes["log.source"] == "MyComponent"


def test_create_log_record_attributes():
    """Verify custom attributes merge with log.source."""
    record = create_log_record(
        "msg",
        source="API",
        attributes={"request_id": "abc123", "duration_ms": 42},
    )

    assert record.attributes["log.source"] == "API"
    assert record.attributes["request_id"] == "abc123"
    assert record.attributes["duration_ms"] == 42


def test_create_log_record_timestamp():
    """Verify timestamp is populated in nanoseconds."""
    before = time.time_ns()
    record = create_log_record("msg")
    after = time.time_ns()

    assert before <= record.timestamp <= after


def test_severity_map_completeness():
    """Verify SEVERITY_MAP contains all LOG_LEVEL values."""
    expected_levels = {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"}
    assert set(SEVERITY_MAP.keys()) == expected_levels


# =============================================================================
# Tests for format_log_record
# =============================================================================

def test_format_log_record():
    """Verify string output format matches expected pattern."""
    record = create_log_record("Hello world", "INFO", source="Test")
    formatted = format_log_record(record)

    assert formatted.startswith("[INFO]")
    assert "Test" in formatted
    assert "Hello world" in formatted
    assert formatted.endswith("\n")
    # Check timestamp format (ISO 8601)
    assert "T" in formatted
    assert "Z" in formatted


# =============================================================================
# Tests for log_filter operator
# =============================================================================

def test_log_filter_by_level():
    """Verify filtering by severity_number works."""
    records = [
        create_log_record("a", "INFO"),
        create_log_record("b", "DEBUG"),
        create_log_record("c", "ERROR"),
    ]
    collected = []
    rx.from_(records).pipe(log_filter({"DEBUG"})).subscribe(collected.append)

    assert len(collected) == 1
    assert collected[0].body == "b"
    assert collected[0].severity_number == SeverityNumber.DEBUG


def test_log_filter_multiple_levels():
    """Verify filtering with multiple levels."""
    records = [
        create_log_record("a", "INFO"),
        create_log_record("b", "DEBUG"),
        create_log_record("c", "ERROR"),
        create_log_record("d", "WARN"),
    ]
    collected = []
    rx.from_(records).pipe(log_filter({"ERROR", "WARN"})).subscribe(collected.append)

    assert len(collected) == 2
    bodies = {r.body for r in collected}
    assert bodies == {"c", "d"}


def test_log_filter_drops_non_log_items():
    """Verify non-LogRecord items are dropped by log_filter."""
    items = [create_log_record("a", "INFO"), "not a log", 123]
    collected = []
    rx.from_(items).pipe(log_filter({"INFO"})).subscribe(collected.append)

    assert len(collected) == 1
    assert isinstance(collected[0], LogRecord)


# =============================================================================
# Tests for drop_log operator
# =============================================================================

def test_drop_log_removes_records():
    """Verify LogRecords are dropped, other items pass."""
    items = [create_log_record("a"), "keep", 123]
    collected = []
    rx.from_(items).pipe(drop_log()).subscribe(collected.append)

    assert collected == ["keep", 123]


# =============================================================================
# Tests for log_redirect_to operator
# =============================================================================

def test_log_redirect_to_observer():
    """Verify LogRecords redirect to target observer."""
    target = Collector()
    output = []
    source = [create_log_record("x", "INFO"), create_log_record("y", "DEBUG"), 1]
    rx.from_(source).pipe(log_redirect_to(target, {"INFO"})).subscribe(output.append)

    assert output == [1]
    assert len(target.items) == 1
    assert isinstance(target.items[0], LogRecord)
    assert target.items[0].body == "x"


def test_log_redirect_to_callable():
    """Verify LogRecords redirect to callable."""
    captured = []
    output = []
    source = [create_log_record("msg", "ERROR"), "data"]
    rx.from_(source).pipe(log_redirect_to(captured.append, {"ERROR"})).subscribe(
        output.append
    )

    assert output == ["data"]
    assert len(captured) == 1
    assert captured[0].body == "msg"


# =============================================================================
# Tests for Logger
# =============================================================================

def test_logger_creates_timestamped_file():
    """Test that Logger creates a file with timestamp postfix."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")
        logger = Logger(logfile=logfile)

        logger.on_next(create_log_record("test message", "INFO", source="Test"))

        # Check that a timestamped file was created (filter out lock files)
        files = [f for f in os.listdir(tmpdir) if f.endswith(".log")]
        assert len(files) == 1
        assert files[0].startswith("test_")
        assert files[0].endswith(".log")
        # Format: test_YYYYMMDDTHHmmss.log
        assert len(files[0]) == len("test_20250120T103045.log")


def test_logger_writes_to_file():
    """Verify Logger writes formatted output to file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")
        logger = Logger(logfile=logfile)

        logger.on_next(create_log_record("hello", "INFO", source="Test"))

        # Read the file
        with open(logger.logfile, "r") as f:
            content = f.read()

        assert "[INFO]" in content
        assert "Test" in content
        assert "hello" in content


def test_logger_file_rotation():
    """Verify file rotation by count."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")
        logger = Logger(logfile=logfile, rotate_interval=3)

        # Write 3 records (should stay in first file)
        for i in range(3):
            logger.on_next(create_log_record(f"message {i}", "INFO", source="Test"))

        first_file = logger.logfile
        assert first_file is not None

        # Write one more record (should trigger rotation)
        time.sleep(1)  # Ensure different timestamp
        logger.on_next(create_log_record("message 3", "INFO", source="Test"))

        second_file = logger.logfile
        assert second_file is not None
        assert first_file != second_file

        # Check that two files exist
        files = [f for f in os.listdir(tmpdir) if f.endswith(".log")]
        assert len(files) == 2


def test_logger_file_rotation_by_time():
    """Verify file rotation by timedelta."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")
        logger = Logger(logfile=logfile, rotate_interval=timedelta(seconds=1))

        logger.on_next(create_log_record("message 1", "INFO", source="Test"))
        first_file = logger.logfile

        # Wait for rotation interval
        time.sleep(1.1)

        logger.on_next(create_log_record("message 2", "INFO", source="Test"))
        second_file = logger.logfile

        assert first_file != second_file

        files = [f for f in os.listdir(tmpdir) if f.endswith(".log")]
        assert len(files) == 2


def test_logger_max_log_age_cleanup():
    """Verify old log cleanup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")

        # Create an "old" log file manually
        old_file = os.path.join(tmpdir, "test_20200101T000000.log")
        with open(old_file, "w") as f:
            f.write("old log")

        logger = Logger(
            logfile=logfile,
            rotate_interval=1,
            max_log_age=timedelta(days=1),
        )

        logger.on_next(create_log_record("message", "INFO", source="Test"))

        # The old file should have been deleted
        files = [f for f in os.listdir(tmpdir) if f.endswith(".log")]
        assert len(files) == 1
        assert "20200101" not in files[0]


def test_logger_no_rotation_without_interval():
    """Test that Logger doesn't rotate when rotate_interval is None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")
        logger = Logger(logfile=logfile)

        for i in range(10):
            logger.on_next(create_log_record(f"message {i}", "INFO", source="Test"))

        files = [f for f in os.listdir(tmpdir) if f.endswith(".log")]
        assert len(files) == 1


def test_logger_logfile_property():
    """Test that the logfile property returns the current file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        logfile = os.path.join(tmpdir, "test.log")
        logger = Logger(logfile=logfile)

        # Before any writes, logfile is None
        assert logger.logfile is None

        logger.on_next(create_log_record("test", "INFO", source="Test"))

        # After write, logfile should be the actual timestamped path
        assert logger.logfile is not None
        assert logger.logfile.startswith(os.path.join(tmpdir, "test_"))
        assert logger.logfile.endswith(".log")


def test_logger_forwards_to_subscribers():
    """Verify Logger forwards LogRecords to subscribers."""
    logger = Logger()
    collected = []
    logger.subscribe(collected.append)

    record = create_log_record("msg", "INFO", source="Test")
    logger.on_next(record)

    assert len(collected) == 1
    assert collected[0] is record


# =============================================================================
# Tests for NamedLogComp
# =============================================================================

def test_named_log_comp_creates_record():
    """Verify NamedLogComp creates LogRecord with source."""
    comp = NamedLogComp(name="MyComp")
    collected = []
    comp.set_super(collected.append)

    comp.log("hello", "INFO")

    assert len(collected) == 1
    assert isinstance(collected[0], LogRecord)
    assert collected[0].body == "hello"
    assert collected[0].attributes["log.source"] == "MyComp"


def test_named_log_comp_attributes():
    """Verify NamedLogComp passes attributes."""
    comp = NamedLogComp(name="API")
    collected = []
    comp.set_super(collected.append)

    comp.log("request", "DEBUG", attributes={"request_id": "123"})

    assert collected[0].attributes["request_id"] == "123"
    assert collected[0].attributes["log.source"] == "API"


def test_named_log_comp_raises_without_super():
    """Verify NamedLogComp raises if set_super not called."""
    comp = NamedLogComp(name="Test")

    try:
        comp.log("msg")
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "set_super" in str(e)


def test_empty_log_comp():
    """Verify EmptyLogComp is a no-op."""
    comp = EmptyLogComp()
    comp.set_super(lambda x: None)
    comp.log("ignored")  # Should not raise


# =============================================================================
# Tests for configure_otel_logging
# =============================================================================

def test_configure_otel_logging_console():
    """Verify helper configures console exporter."""
    provider = configure_otel_logging(
        service_name="test-service",
        console_export=True,
    )

    # Check provider was created
    assert provider is not None
    # Check we can get a logger
    logger = provider.get_logger("test")
    assert logger is not None


def test_configure_otel_logging_resource():
    """Verify helper sets resource attributes."""
    provider = configure_otel_logging(
        service_name="my-service",
        service_version="1.0.0",
    )

    # The provider should be created with the resource
    assert provider is not None


# =============================================================================
# Tests for reactive pipeline integration
# =============================================================================

def test_reactive_pipeline_with_log_operators():
    """Verify operators work in rx pipeline."""
    logger_collector = Collector()
    data_collector = []

    # Create a mixed stream
    source = rx.from_([
        create_log_record("log1", "INFO"),
        "data1",
        create_log_record("log2", "ERROR"),
        "data2",
    ])

    # Redirect logs to logger, process data
    source.pipe(
        log_redirect_to(logger_collector, {"INFO", "ERROR"})
    ).subscribe(data_collector.append)

    assert data_collector == ["data1", "data2"]
    assert len(logger_collector.items) == 2
    assert logger_collector.items[0].body == "log1"
    assert logger_collector.items[1].body == "log2"
