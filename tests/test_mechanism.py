"""Tests for rxplus.mechanism module - trace context and ID generation."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from rxplus import (
    RxException,
    SpanContext,
    TraceContext,
    generate_span_id,
    generate_trace_id,
    get_current_span,
    set_current_span,
    start_span,
)

# =============================================================================
# Tests for generate_trace_id
# =============================================================================


def test_generate_trace_id_length():
    """Verify trace ID is 32 hex characters (128 bits)."""
    trace_id = generate_trace_id()
    assert len(trace_id) == 32


def test_generate_trace_id_uniqueness():
    """Verify multiple calls produce different IDs."""
    ids = {generate_trace_id() for _ in range(100)}
    assert len(ids) == 100  # All unique


def test_generate_trace_id_hex_valid():
    """Verify output is valid hexadecimal."""
    trace_id = generate_trace_id()
    # Should not raise ValueError
    int(trace_id, 16)


def test_generate_trace_id_lowercase():
    """Verify trace ID uses lowercase hex characters."""
    trace_id = generate_trace_id()
    assert trace_id == trace_id.lower()


# =============================================================================
# Tests for generate_span_id
# =============================================================================


def test_generate_span_id_length():
    """Verify span ID is 16 hex characters (64 bits)."""
    span_id = generate_span_id()
    assert len(span_id) == 16


def test_generate_span_id_uniqueness():
    """Verify multiple calls produce different IDs."""
    ids = {generate_span_id() for _ in range(100)}
    assert len(ids) == 100  # All unique


def test_generate_span_id_hex_valid():
    """Verify output is valid hexadecimal."""
    span_id = generate_span_id()
    # Should not raise ValueError
    int(span_id, 16)


def test_generate_span_id_lowercase():
    """Verify span ID uses lowercase hex characters."""
    span_id = generate_span_id()
    assert span_id == span_id.lower()


# =============================================================================
# Tests for SpanContext
# =============================================================================


def test_span_context_immutable():
    """Verify SpanContext is a frozen dataclass."""
    ctx = SpanContext(
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id=None,
    )
    with pytest.raises(AttributeError):
        ctx.trace_id = "c" * 32  # type: ignore


def test_span_context_fields():
    """Verify SpanContext stores all fields correctly."""
    ctx = SpanContext(
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id="c" * 16,
    )
    assert ctx.trace_id == "a" * 32
    assert ctx.span_id == "b" * 16
    assert ctx.parent_span_id == "c" * 16


def test_span_context_optional_parent():
    """Verify parent_span_id defaults to None."""
    ctx = SpanContext(trace_id="a" * 32, span_id="b" * 16)
    assert ctx.parent_span_id is None


# =============================================================================
# Tests for get_current_span / set_current_span
# =============================================================================


def test_get_current_span_default_none():
    """Verify get_current_span returns None when no span is active."""
    # Reset context first
    set_current_span(None)
    assert get_current_span() is None


def test_set_current_span():
    """Verify set_current_span sets the context."""
    ctx = SpanContext(trace_id="a" * 32, span_id="b" * 16)
    set_current_span(ctx)
    try:
        assert get_current_span() == ctx
    finally:
        set_current_span(None)


def test_current_span_thread_isolation():
    """Verify span context is isolated between threads."""
    results = {}

    def thread_fn(thread_id: int):
        # Each thread sets its own context
        ctx = SpanContext(trace_id=f"{thread_id:032d}", span_id=f"{thread_id:016d}")
        set_current_span(ctx)
        # Small delay to increase chance of race condition if isolation fails
        import time

        time.sleep(0.01)
        results[thread_id] = get_current_span()

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(thread_fn, i) for i in range(5)]
        for f in futures:
            f.result()

    # Each thread should have gotten its own context back
    for thread_id, ctx in results.items():
        assert ctx is not None
        assert ctx.trace_id == f"{thread_id:032d}"


# =============================================================================
# Tests for start_span
# =============================================================================


def test_start_span_creates_new_trace():
    """Verify start_span creates a new trace when no parent exists."""
    set_current_span(None)

    with start_span() as ctx:
        assert ctx.trace_id is not None
        assert len(ctx.trace_id) == 32
        assert ctx.span_id is not None
        assert len(ctx.span_id) == 16
        assert ctx.parent_span_id is None

    # Context is cleared after exiting
    assert get_current_span() is None


def test_start_span_inherits_trace():
    """Verify child span inherits trace ID from parent."""
    with start_span() as root:
        with start_span() as child:
            assert child.trace_id == root.trace_id
            assert child.span_id != root.span_id


def test_start_span_sets_parent():
    """Verify child span has parent_span_id set correctly."""
    with start_span() as root:
        with start_span() as child:
            assert child.parent_span_id == root.span_id


def test_start_span_context_cleanup():
    """Verify context is restored after span exits."""
    with start_span() as root:
        root_ctx = get_current_span()
        assert root_ctx == root

        with start_span() as child:
            child_ctx = get_current_span()
            assert child_ctx == child

        # After child exits, should be back to root
        assert get_current_span() == root

    # After root exits, should be None
    assert get_current_span() is None


def test_start_span_with_explicit_trace_id():
    """Verify start_span uses provided trace_id."""
    explicit_trace = "x" * 32

    with start_span(trace_id=explicit_trace) as ctx:
        assert ctx.trace_id == explicit_trace


def test_start_span_with_explicit_parent():
    """Verify start_span uses provided parent_span_id."""
    explicit_parent = "y" * 16

    with start_span(parent_span_id=explicit_parent) as ctx:
        assert ctx.parent_span_id == explicit_parent


def test_nested_spans_three_levels():
    """Verify multiple nested spans work correctly."""
    with start_span() as level1:
        assert level1.parent_span_id is None

        with start_span() as level2:
            assert level2.trace_id == level1.trace_id
            assert level2.parent_span_id == level1.span_id

            with start_span() as level3:
                assert level3.trace_id == level1.trace_id
                assert level3.parent_span_id == level2.span_id


def test_start_span_exception_cleanup():
    """Verify context is cleaned up even if exception occurs."""
    set_current_span(None)

    with pytest.raises(ValueError):
        with start_span() as ctx:
            assert get_current_span() == ctx
            raise ValueError("test error")

    # Context should be cleaned up despite exception
    assert get_current_span() is None


# =============================================================================
# Tests for TraceContext
# =============================================================================


def test_trace_context_generates_id():
    """Verify TraceContext creates trace ID when not provided."""
    trace = TraceContext()
    assert trace.trace_id is not None
    assert len(trace.trace_id) == 32


def test_trace_context_accepts_id():
    """Verify TraceContext uses provided trace ID."""
    explicit_trace = "z" * 32
    trace = TraceContext(trace_id=explicit_trace)
    assert trace.trace_id == explicit_trace


def test_trace_context_start_root_span():
    """Verify start_root_span creates and sets root span."""
    set_current_span(None)
    trace = TraceContext()

    root = trace.start_root_span()
    assert root.trace_id == trace.trace_id
    assert root.parent_span_id is None
    assert get_current_span() == root

    # Cleanup
    set_current_span(None)


def test_trace_context_span():
    """Verify TraceContext.span() creates child spans."""
    set_current_span(None)
    trace = TraceContext()
    root = trace.start_root_span()

    with trace.span() as child:
        assert child.trace_id == trace.trace_id
        assert child.parent_span_id == root.span_id

    # Cleanup
    set_current_span(None)


def test_trace_context_multiple_spans():
    """Verify multiple spans within same trace context."""
    set_current_span(None)
    trace = TraceContext()
    trace.start_root_span()

    span_ids = set()
    for _ in range(5):
        with trace.span() as span:
            span_ids.add(span.span_id)
            assert span.trace_id == trace.trace_id

    # All spans should have unique IDs
    assert len(span_ids) == 5

    # Cleanup
    set_current_span(None)


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
