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
    EmptyLogComp,
    LogComp,
    Logger,
    LogItem,
    NamedLogComp,
    drop_log,
    keep_log,
    log_filter,
    log_redirect_to,
)
from .mechanism import RxException  # noqa: F401
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

    # Logging
    "LogItem",
    "LOG_LEVEL",
    "keep_log",
    "log_filter",
    "drop_log",
    "log_redirect_to",
    "LogComp",
    "EmptyLogComp",
    "NamedLogComp",
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
