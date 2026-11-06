"""Tests for rxplus.mechanism: RxException.

The custom trace-context API this file used to test (SpanContext,
TraceContext, start_span, ...) was removed in favour of OpenTelemetry; its
tests could no longer be collected.
"""

from rxplus import RxException

# =============================================================================
# Tests for RxException
# =============================================================================


def test_rx_exception_basic():
    """Verify RxException wraps another exception."""
    inner = ValueError("inner error")
    rx_exc = RxException(inner, source="TestSource", note="test note")

    assert rx_exc.exception == inner
    assert rx_exc.source == "TestSource"
    assert rx_exc.note == "test note"
    assert "TestSource" in str(rx_exc)
    assert "test note" in str(rx_exc)
    assert "inner error" in str(rx_exc)


def test_rx_exception_defaults():
    """Verify RxException default values."""
    inner = RuntimeError("test")
    rx_exc = RxException(inner)

    assert rx_exc.source == "Unknown"
    assert rx_exc.note == ""
