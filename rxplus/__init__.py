"""Convenience exports for the :mod:`rxplus` package.

Exports are loaded lazily (PEP 562): ``import rxplus`` imports nothing
else, and ``from rxplus import X`` imports only the submodule that defines
``X``. This keeps ``import rxplus.reacting`` standard-library only (D10)
and ``import rxplus`` cheap.

This package supports optional dependencies for audio and video features.
Install with:
    - `pip install rxplus` for basic features
    - `pip install rxplus[audio]` for audio features
    - `pip install rxplus[video]` for video features
    - `pip install rxplus[all]` for all features
"""

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # what type checkers and IDEs see
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
    from .cli import (  # noqa: F401
        from_cli,
        to_cli,
    )
    from .duplex import (  # noqa: F401
        Duplex,
        connect_adapter,
        make_duplex,
    )
    from .gateway import (  # noqa: F401
        BufferK,
        Connection,
        ConnectionState,
        Disconnect,
        DisconnectError,
        DropNew,
        DropOld,
        Framing,
        GatewayNode,
        HelloPayload,
        OverflowPolicy,
        OverflowStrategy,
        StreamInfo,
        StreamState,
        StreamTable,
        TaggedFrame,
        create_overflow_policy,
    )
    from .graphic import (  # noqa: F401
        create_screen_capture,
        jpeg_bytes_to_rgb_ndarray,
        png_bytes_to_rgb_ndarray,
        rgb_ndarray_to_jpeg_bytes,
        rgb_ndarray_to_png_bytes,
    )
    from .mechanism import (  # noqa: F401
        RxException,
    )
    from .opt import (  # noqa: F401
        ErrorRestartSignal,
        redirect_to,
        retry_with_signal,
        stream_print_out,
    )
    from .telemetry import (  # noqa: F401
        ConsoleLogRecordExporter,
        FileLogRecordExporter,
        LogContext,
        MetricsHelper,
        OTelLogger,
        configure_metrics,
        configure_telemetry,
    )
    from .utils import (  # noqa: F401
        BandwidthMonitor,
        FPSMonitor,
        TaggedData,
        tag,
        tag_filter,
        untag,
    )
    from .ws import (  # noqa: F401
        RxWSClient,
        RxWSClientGroup,
        RxWSServer,
        WSChannels,
        WSConnectionConfig,
        WSConnectionState,
        WSDatatype,
        WSStr,
    )

# name -> submodule that defines it
_EXPORTS: dict[str, str] = {
    "from_cli": "cli",
    "to_cli": "cli",
    "Duplex": "duplex",
    "connect_adapter": "duplex",
    "make_duplex": "duplex",
    "RxException": "mechanism",
    "ErrorRestartSignal": "opt",
    "redirect_to": "opt",
    "retry_with_signal": "opt",
    "stream_print_out": "opt",
    "ConsoleLogRecordExporter": "telemetry",
    "FileLogRecordExporter": "telemetry",
    "LogContext": "telemetry",
    "MetricsHelper": "telemetry",
    "OTelLogger": "telemetry",
    "configure_metrics": "telemetry",
    "configure_telemetry": "telemetry",
    "BandwidthMonitor": "utils",
    "FPSMonitor": "utils",
    "TaggedData": "utils",
    "tag": "utils",
    "tag_filter": "utils",
    "untag": "utils",
    "RxWSClient": "ws",
    "RxWSClientGroup": "ws",
    "RxWSServer": "ws",
    "WSChannels": "ws",
    "WSConnectionConfig": "ws",
    "WSConnectionState": "ws",
    "WSDatatype": "ws",
    "WSStr": "ws",
    "BufferK": "gateway",
    "Connection": "gateway",
    "ConnectionState": "gateway",
    "Disconnect": "gateway",
    "DisconnectError": "gateway",
    "DropNew": "gateway",
    "DropOld": "gateway",
    "Framing": "gateway",
    "GatewayNode": "gateway",
    "HelloPayload": "gateway",
    "OverflowPolicy": "gateway",
    "OverflowStrategy": "gateway",
    "StreamInfo": "gateway",
    "StreamState": "gateway",
    "StreamTable": "gateway",
    "TaggedFrame": "gateway",
    "create_overflow_policy": "gateway",
}
_AUDIO_EXPORTS: dict[str, str] = {
    "PCMFormat": "audio",
    "RxMicrophone": "audio",
    "RxSpeaker": "audio",
    "StreamingResampler": "audio",
    "convert_audio_format": "audio",
    "create_wavfile": "audio",
    "get_numpy_dtype": "audio",
    "resample_audio": "audio",
    "save_wavfile": "audio",
}
_VIDEO_EXPORTS: dict[str, str] = {
    "create_screen_capture": "graphic",
    "jpeg_bytes_to_rgb_ndarray": "graphic",
    "png_bytes_to_rgb_ndarray": "graphic",
    "rgb_ndarray_to_jpeg_bytes": "graphic",
    "rgb_ndarray_to_png_bytes": "graphic",
}

_BASIC_ALL = [
    "RxException",
    "TaggedData",
    "tag",
    "tag_filter",
    "untag",
    "FPSMonitor",
    "BandwidthMonitor",
    "LogContext",
    "configure_telemetry",
    "configure_metrics",
    "FileLogRecordExporter",
    "ConsoleLogRecordExporter",
    "OTelLogger",
    "MetricsHelper",
    "stream_print_out",
    "redirect_to",
    "ErrorRestartSignal",
    "retry_with_signal",
    "WSConnectionConfig",
    "WSConnectionState",
    "WSChannels",
    "WSDatatype",
    "WSStr",
    "RxWSServer",
    "RxWSClient",
    "RxWSClientGroup",
    "Duplex",
    "make_duplex",
    "connect_adapter",
    "from_cli",
    "to_cli",
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
_AUDIO_ALL = [
    "PCMFormat",
    "convert_audio_format",
    "create_wavfile",
    "get_numpy_dtype",
    "resample_audio",
    "RxMicrophone",
    "RxSpeaker",
    "save_wavfile",
]
_VIDEO_ALL = [
    "create_screen_capture",
    "rgb_ndarray_to_jpeg_bytes",
    "jpeg_bytes_to_rgb_ndarray",
    "rgb_ndarray_to_png_bytes",
    "png_bytes_to_rgb_ndarray",
]

_optional: dict[str, bool] = {}


def _available(module: str) -> bool:
    """Whether an optional submodule imports (its dependencies are present)."""
    if module not in _optional:
        try:
            importlib.import_module(f".{module}", __name__)
            _optional[module] = True
        except ImportError:
            _optional[module] = False
    return _optional[module]


def __getattr__(name: str) -> object:
    if name in _EXPORTS:
        value = getattr(importlib.import_module(f".{_EXPORTS[name]}", __name__), name)
    elif name in _AUDIO_EXPORTS or name in _VIDEO_EXPORTS:
        module = _AUDIO_EXPORTS.get(name) or _VIDEO_EXPORTS[name]
        if not _available(module):
            raise AttributeError(
                f"rxplus.{name} needs the optional dependencies of rxplus.{module}"
                f" (pip install 'rxplus[{'audio' if module == 'audio' else 'video'}]')"
            )
        value = getattr(importlib.import_module(f".{module}", __name__), name)
    elif name == "_HAS_AUDIO":
        return _available("audio")
    elif name == "_HAS_VIDEO":
        return _available("graphic")
    elif name == "__all__":
        names = list(_BASIC_ALL)
        if _available("audio"):
            names += _AUDIO_ALL
        if _available("graphic"):
            names += _VIDEO_ALL
        return names
    else:
        raise AttributeError(f"module 'rxplus' has no attribute {name!r}")
    globals()[name] = value  # cache: later lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(
        set(globals()) | set(_EXPORTS) | set(_AUDIO_EXPORTS) | set(_VIDEO_EXPORTS)
    )
