"""Convenience exports for the :mod:`rxplus` package.

This package supports optional dependencies for audio and video features.
Install with:
    - `pip install rxplus` for basic features
    - `pip install rxplus[audio]` for audio features
    - `pip install rxplus[video]` for video features
    - `pip install rxplus[all]` for all features
"""

# =============================================================================
# Basic exports (always available)
# =============================================================================
from .cli import from_cli  # noqa: F401
from .duplex import Duplex, connect_adapter, make_duplex  # noqa: F401
from .logging import (  # noqa: F401
    LOG_LEVEL,
    SEVERITY_MAP,
    Logger,
    create_log_record,
    configure_otel_logging,
    format_log_record,
    drop_log,
    log_filter,
    log_redirect_to,
)
# Re-export LogRecord from OpenTelemetry for convenience
from opentelemetry.sdk._logs._internal import LogRecord  # noqa: F401
from .mechanism import (  # noqa: F401
    RxException,
    SpanContext,
    TraceContext,
    generate_trace_id,
    generate_span_id,
    get_current_span,
    set_current_span,
    start_span,
)
from .opt import redirect_to, stream_print_out, ErrorRestartSignal, retry_with_signal, error_restart_signal_to_logitem  # noqa: F401
from .utils import TaggedData, tag, tag_filter, untag, FPSMonitor, BandwidthMonitor  # noqa: F401
from .ws import RxWSClient, RxWSClientGroup, RxWSServer, WSDatatype, WSStr  # noqa: F401


# =============================================================================
# Audio exports (optional - requires rxplus[audio])
# =============================================================================
try:
    from .audio import (  # noqa: F401
        PCMFormat,
        RxMicrophone,
        RxSpeaker,
        create_wavfile,
        save_wavfile,
    )
    _HAS_AUDIO = True
except ImportError:
    _HAS_AUDIO = False


# =============================================================================
# Video exports (optional - requires rxplus[video])
# =============================================================================
try:
    from .graphic import (  # noqa: F401
        create_screen_capture,
        rgb_ndarray_to_jpeg_bytes,
        jpeg_bytes_to_rgb_ndarray,
    )
    _HAS_VIDEO = True
except ImportError:
    _HAS_VIDEO = False


# =============================================================================
# __all__ definition
# =============================================================================
__all__ = [
    # Core
    "RxException",
    "TaggedData",
    "tag",
    "tag_filter",
    "untag",
    "FPSMonitor",
    "BandwidthMonitor",

    # Trace Context
    "SpanContext",
    "TraceContext",
    "generate_trace_id",
    "generate_span_id",
    "get_current_span",
    "set_current_span",
    "start_span",

    # Logging (OpenTelemetry-based)
    "LogRecord",
    "LOG_LEVEL",
    "SEVERITY_MAP",
    "create_log_record",
    "format_log_record",
    "configure_otel_logging",
    "log_filter",
    "drop_log",
    "log_redirect_to",
    "Logger",
    "stream_print_out",
    "redirect_to",
    "ErrorRestartSignal",
    "retry_with_signal",
    "error_restart_signal_to_logitem",

    # WebSocket
    "WSDatatype",
    "WSStr",
    "RxWSServer",
    "RxWSClient",
    "RxWSClientGroup",

    # Duplex
    "Duplex",
    "make_duplex",
    "connect_adapter",

    # CLI
    "from_cli",
]

# Add audio exports to __all__ if available
if _HAS_AUDIO:
    __all__.extend([
        "PCMFormat",
        "create_wavfile",
        "RxMicrophone",
        "RxSpeaker",
        "save_wavfile",
    ])

# Add video exports to __all__ if available
if _HAS_VIDEO:
    __all__.extend([
        "create_screen_capture",
        "rgb_ndarray_to_jpeg_bytes",
        "jpeg_bytes_to_rgb_ndarray",
    ])
