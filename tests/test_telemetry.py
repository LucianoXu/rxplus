"""Tests for OpenTelemetry logging integration.

Tests the OTel telemetry utilities:
- ConsoleLogRecordExporter for CLI-friendly output
- OTelLogger wrapper for convenient log emission
- get_default_providers() lazy initialization
- Custom provider override behavior
"""

import io
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor, LogRecordExportResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry._logs import LogRecord, SeverityNumber

from rxplus.telemetry import (
    ConsoleLogRecordExporter,
    OTelLogger,
    get_default_providers,
    configure_telemetry,
)


class TestConsoleLogRecordExporter:
    """Tests for ConsoleLogRecordExporter."""

    def test_export_formats_correctly(self):
        """Verify output format matches expected CLI format."""
        exporter = ConsoleLogRecordExporter()
        
        # Create a mock ReadableLogRecord
        record = LogRecord(
            timestamp=int(time.time() * 1e9),
            body="Test message",
            severity_text="INFO",
            severity_number=SeverityNumber.INFO,
            attributes={"log.source": "TestSource"},
        )
        
        # Create a mock ReadableLogRecord wrapper
        class MockReadableLogRecord:
            def __init__(self, log_record):
                self.log_record = log_record
        
        readable_record = MockReadableLogRecord(record)
        
        # Capture stderr
        captured = io.StringIO()
        with patch.object(sys, 'stderr', captured):
            result = exporter.export([readable_record])
        
        output = captured.getvalue()
        
        assert result == LogRecordExportResult.SUCCESS
        assert "[INFO]" in output
        assert "TestSource" in output
        assert "Test message" in output

    def test_export_handles_empty_batch(self):
        """Verify empty batch returns success."""
        exporter = ConsoleLogRecordExporter()
        
        captured = io.StringIO()
        with patch.object(sys, 'stderr', captured):
            result = exporter.export([])
        
        assert result == LogRecordExportResult.SUCCESS
        assert captured.getvalue() == ""

    def test_shutdown_is_noop(self):
        """Verify shutdown doesn't raise."""
        exporter = ConsoleLogRecordExporter()
        exporter.shutdown()  # Should not raise

    def test_force_flush_returns_true(self):
        """Verify force_flush returns True."""
        exporter = ConsoleLogRecordExporter()
        assert exporter.force_flush() is True


class TestOTelLogger:
    """Tests for OTelLogger wrapper."""

    def test_severity_levels_map_correctly(self):
        """Verify INFO/DEBUG/WARN/ERROR map to correct severity numbers."""
        mock_logger = MagicMock()
        otel_logger = OTelLogger(mock_logger, source="TestSource")
        
        # Test each level
        otel_logger.info("info message")
        otel_logger.debug("debug message")
        otel_logger.warning("warning message")
        otel_logger.error("error message")
        
        assert mock_logger.emit.call_count == 4
        
        # Verify severity numbers
        calls = mock_logger.emit.call_args_list
        assert calls[0][0][0].severity_number == SeverityNumber.INFO
        assert calls[1][0][0].severity_number == SeverityNumber.DEBUG
        assert calls[2][0][0].severity_number == SeverityNumber.WARN
        assert calls[3][0][0].severity_number == SeverityNumber.ERROR

    def test_severity_text_set_correctly(self):
        """Verify severity text is set correctly."""
        mock_logger = MagicMock()
        otel_logger = OTelLogger(mock_logger, source="TestSource")
        
        otel_logger.info("test")
        otel_logger.debug("test")
        otel_logger.warning("test")
        otel_logger.error("test")
        
        calls = mock_logger.emit.call_args_list
        assert calls[0][0][0].severity_text == "INFO"
        assert calls[1][0][0].severity_text == "DEBUG"
        assert calls[2][0][0].severity_text == "WARN"
        assert calls[3][0][0].severity_text == "ERROR"

    def test_source_attribute_included(self):
        """Verify log.source attribute is set from source parameter."""
        mock_logger = MagicMock()
        otel_logger = OTelLogger(mock_logger, source="MyComponent")
        
        otel_logger.info("test message")
        
        record = mock_logger.emit.call_args[0][0]
        assert record.attributes["log.source"] == "MyComponent"

    def test_custom_attributes_included(self):
        """Verify additional keyword arguments are included as attributes."""
        mock_logger = MagicMock()
        otel_logger = OTelLogger(mock_logger, source="TestSource")
        
        otel_logger.info("test message", peer_id="abc123", port=8765)
        
        record = mock_logger.emit.call_args[0][0]
        assert record.attributes["peer_id"] == "abc123"
        assert record.attributes["port"] == 8765

    def test_message_body_set(self):
        """Verify message is set as log record body."""
        mock_logger = MagicMock()
        otel_logger = OTelLogger(mock_logger, source="TestSource")
        
        otel_logger.info("Hello, world!")
        
        record = mock_logger.emit.call_args[0][0]
        assert record.body == "Hello, world!"

    def test_timestamp_is_set(self):
        """Verify timestamp is set to current time."""
        mock_logger = MagicMock()
        otel_logger = OTelLogger(mock_logger, source="TestSource")
        
        before = time.time_ns()
        otel_logger.info("test")
        after = time.time_ns()
        
        record = mock_logger.emit.call_args[0][0]
        assert before <= record.timestamp <= after


class TestGetDefaultProviders:
    """Tests for get_default_providers() function."""

    def test_returns_valid_providers(self):
        """Verify returns TracerProvider and LoggerProvider."""
        # Note: This modifies global state, so we need to be careful
        # In a real test suite, we'd want to reset the globals
        tracer_provider, logger_provider = get_default_providers("test-service")
        
        assert isinstance(tracer_provider, TracerProvider)
        assert isinstance(logger_provider, LoggerProvider)

    def test_singleton_returns_same_providers(self):
        """Verify lazy initialization returns same providers on subsequent calls."""
        tracer1, logger1 = get_default_providers("service1")
        tracer2, logger2 = get_default_providers("service2")  # Different name, same providers
        
        # Should return the same instances (singleton pattern)
        assert tracer1 is tracer2
        assert logger1 is logger2


class TestConfigureTelemetry:
    """Tests for configure_telemetry() function."""

    def test_returns_providers(self):
        """Verify returns TracerProvider and LoggerProvider."""
        tracer_provider, logger_provider = configure_telemetry(
            service_name="test-app",
        )
        
        assert isinstance(tracer_provider, TracerProvider)
        assert isinstance(logger_provider, LoggerProvider)

    def test_custom_exporter_is_used(self):
        """Verify custom log exporter receives logs."""
        mock_exporter = MagicMock()
        mock_exporter.export.return_value = LogRecordExportResult.SUCCESS
        
        tracer_provider, logger_provider = configure_telemetry(
            service_name="test-app",
            log_exporter=mock_exporter,
            batch_logs=False,  # Use SimpleLogRecordProcessor for immediate export
        )
        
        # Get a logger and emit a log
        logger = logger_provider.get_logger("test")
        record = LogRecord(
            timestamp=time.time_ns(),
            body="Test message",
            severity_text="INFO",
            severity_number=SeverityNumber.INFO,
        )
        logger.emit(record)
        
        # Verify exporter was called
        assert mock_exporter.export.called


class TestIntegration:
    """Integration tests for the telemetry stack."""

    def test_end_to_end_logging(self):
        """Test complete flow: OTelLogger -> LoggerProvider -> ConsoleExporter."""
        # Create providers with console exporter
        tracer_provider, logger_provider = configure_telemetry(
            service_name="integration-test",
            log_exporter=ConsoleLogRecordExporter(),
            batch_logs=False,
        )
        
        # Create OTelLogger
        otel_logger = OTelLogger(
            logger_provider.get_logger("integration.test"),
            source="IntegrationTest",
        )
        
        # Capture stderr and emit logs
        captured = io.StringIO()
        with patch.object(sys, 'stderr', captured):
            otel_logger.info("Integration test message", key="value")
        
        output = captured.getvalue()
        
        # Verify output contains expected parts
        assert "[INFO]" in output
        assert "IntegrationTest" in output
        assert "Integration test message" in output
