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
from .cli import from_cli, to_cli  # noqa: F401
from .duplex import Duplex, connect_adapter, make_duplex  # noqa: F401
from .mechanism import RxException  # noqa: F401
from .telemetry import configure_telemetry, FileLogRecordExporter, ConsoleLogRecordExporter, OTelLogger  # noqa: F401
from .opt import redirect_to, stream_print_out, ErrorRestartSignal, retry_with_signal  # noqa: F401
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
        StreamingResampler,
        convert_audio_format,
        create_wavfile,
        get_numpy_dtype,
        resample_audio,
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
        rgb_ndarray_to_png_bytes,
        png_bytes_to_rgb_ndarray,
    )
    _HAS_VIDEO = True
except ImportError:
    _HAS_VIDEO = False


# =============================================================================
# Gateway exports (always available)
# =============================================================================
from .gateway import (  # noqa: F401
    # Overflow strategies
    OverflowPolicy,
    DropOld,
    DropNew,
    BufferK,
    Disconnect,
    DisconnectError,
    create_overflow_policy,
    # Stream management
    OverflowStrategy,
    StreamState,
    StreamInfo,
    StreamTable,
    # Connection management
    ConnectionState,
    Connection,
    HelloPayload,
    # Framing abstraction
    Framing,
    TaggedFrame,
    # Gateway node
    GatewayNode,
)


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

    # Telemetry
    "configure_telemetry",
    "FileLogRecordExporter",
    "ConsoleLogRecordExporter",
    "OTelLogger",

    # Operators
    "stream_print_out",
    "redirect_to",
    "ErrorRestartSignal",
    "retry_with_signal",

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
    "to_cli",

    # Gateway
    "OverflowPolicy",
    "DropOld",
    "DropNew",
    "BufferK",
    "Disconnect",
    "DisconnectError",
    "create_overflow_policy",
    "OverflowStrategy",
    "StreamState",
    "StreamInfo",
    "StreamTable",
    "ConnectionState",
    "Connection",
    "HelloPayload",
    "Framing",
    "TaggedFrame",
    "GatewayNode",
]

# Add audio exports to __all__ if available
if _HAS_AUDIO:
    __all__.extend([
        "PCMFormat",
        "convert_audio_format",
        "create_wavfile",
        "get_numpy_dtype",
        "resample_audio",
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
        "rgb_ndarray_to_png_bytes",
        "png_bytes_to_rgb_ndarray",
    ])
