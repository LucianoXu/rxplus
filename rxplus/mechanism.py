"""Core error types and trace context for :mod:`rxplus`."""

import secrets
from contextvars import ContextVar, Token
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Generator


class RxException(Exception):
    """Base class for all RxPlus exceptions."""

    def __init__(self, exception: Exception, source: str = "Unknown", note: str = ""):
        super().__init__(f"<{source}> {note}: {exception}")
        self.exception = exception
        self.source = source
        self.note = note

    def __str__(self):
        return f"<{self.source}> {self.note}: {self.exception}"


# =============================================================================
# Trace Context
# =============================================================================


@dataclass(frozen=True)
class SpanContext:
    """
    Immutable span context with trace and span IDs.

    Follows OpenTelemetry conventions:
    - trace_id: 128-bit identifier (32 hex characters)
    - span_id: 64-bit identifier (16 hex characters)
    - parent_span_id: Optional parent span ID for nested spans

    Example:
        >>> ctx = SpanContext(
        ...     trace_id="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        ...     span_id="1234567890abcdef",
        ...     parent_span_id=None,
        ... )
    """

    trace_id: str  # 32-char hex (128-bit)
    span_id: str  # 16-char hex (64-bit)
    parent_span_id: Optional[str] = None  # 16-char hex or None


# Context variable for current span (thread-safe and asyncio-safe)
_current_span: ContextVar[Optional[SpanContext]] = ContextVar(
    "current_span", default=None
)


def generate_trace_id() -> str:
    """
    Generate a 128-bit cryptographically secure trace ID.

    Uses Python's `secrets` module (CSPRNG) to generate random bytes,
    returned as a 32-character hexadecimal string.

    Returns:
        32-character hex string representing a 128-bit trace ID.

    Example:
        >>> trace_id = generate_trace_id()
        >>> len(trace_id)
        32
        >>> int(trace_id, 16)  # Valid hex
        ...
    """
    return secrets.token_hex(16)


def generate_span_id() -> str:
    """
    Generate a 64-bit cryptographically secure span ID.

    Uses Python's `secrets` module (CSPRNG) to generate random bytes,
    returned as a 16-character hexadecimal string.

    Returns:
        16-character hex string representing a 64-bit span ID.

    Example:
        >>> span_id = generate_span_id()
        >>> len(span_id)
        16
        >>> int(span_id, 16)  # Valid hex
        ...
    """
    return secrets.token_hex(8)


def get_current_span() -> Optional[SpanContext]:
    """
    Get the current span context from contextvars.

    Returns:
        The current SpanContext if within a span, None otherwise.

    Example:
        >>> with start_span() as ctx:
        ...     current = get_current_span()
        ...     assert current == ctx
        >>> get_current_span() is None
        True
    """
    return _current_span.get()


def set_current_span(ctx: Optional[SpanContext]) -> Token[Optional[SpanContext]]:
    """
    Set the current span context.

    Args:
        ctx: The SpanContext to set, or None to clear.

    Returns:
        A token that can be used to reset to the previous state.

    Note:
        Prefer using `start_span()` context manager for automatic cleanup.
    """
    return _current_span.set(ctx)


@contextmanager
def start_span(
    trace_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
) -> Generator[SpanContext, None, None]:
    """
    Context manager to create a new span.

    If no trace_id is provided and no current span exists, creates a new trace.
    If no trace_id is provided but a current span exists, inherits the trace_id.

    Args:
        trace_id: Optional trace ID (generates new if not provided and no parent).
        parent_span_id: Optional parent span ID (uses current span if not provided).

    Yields:
        SpanContext with the new span information.

    Example:
        >>> with start_span() as root:
        ...     print(f"Root trace: {root.trace_id}")
        ...     with start_span() as child:
        ...         assert child.trace_id == root.trace_id
        ...         assert child.parent_span_id == root.span_id
    """
    current = _current_span.get()

    # Determine trace_id
    if trace_id is None:
        trace_id = current.trace_id if current else generate_trace_id()

    # Determine parent_span_id
    if parent_span_id is None and current is not None:
        parent_span_id = current.span_id

    # Create new span
    new_span = SpanContext(
        trace_id=trace_id,
        span_id=generate_span_id(),
        parent_span_id=parent_span_id,
    )

    token = _current_span.set(new_span)
    try:
        yield new_span
    finally:
        _current_span.reset(token)


class TraceContext:
    """
    A component that manages trace context for a logical operation.

    Use this to create a trace that spans multiple operations, or attach
    to an existing trace for distributed tracing.

    Example:
        >>> trace = TraceContext()
        >>> trace.start_root_span()
        >>> with trace.span() as child:
        ...     print(f"Child span: {child.span_id}")
    """

    def __init__(self, trace_id: Optional[str] = None):
        """
        Initialize a trace context.

        Args:
            trace_id: Optional trace ID. If None, generates a new one.
        """
        self.trace_id = trace_id or generate_trace_id()
        self._root_span_id: Optional[str] = None

    def start_root_span(self) -> SpanContext:
        """
        Start the root span for this trace.

        Returns:
            The root SpanContext for this trace.
        """
        self._root_span_id = generate_span_id()
        ctx = SpanContext(
            trace_id=self.trace_id,
            span_id=self._root_span_id,
            parent_span_id=None,
        )
        set_current_span(ctx)
        return ctx

    @contextmanager
    def span(self) -> Generator[SpanContext, None, None]:
        """
        Create a child span within this trace.

        Yields:
            SpanContext for the new child span.
        """
        with start_span(trace_id=self.trace_id) as ctx:
            yield ctx
