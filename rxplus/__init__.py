"""Convenience exports for the :mod:`rxplus` package."""

from .audio import (  # noqa: F401
    PCMFormat,
    RxMicrophone,
    RxSpeaker,
    create_wavfile,
    save_wavfile,
)
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
from .opt import redirect_to, stream_print_out  # noqa: F401
from .utils import TaggedData, tag, tag_filter, untag  # noqa: F401
from .ws import RxWSClient, RxWSClientGroup, RxWSServer, WSDatatype, WSStr  # noqa: F401

__all__ = [
    "RxException",
    "TaggedData",
    "tag",
    "tag_filter",
    "untag",
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
    "WSDatatype",
    "WSStr",
    "RxWSServer",
    "RxWSClient",
    "RxWSClientGroup",
    "Duplex",
    "make_duplex",
    "connect_adapter",
    "from_cli",
    "PCMFormat",
    "create_wavfile",
    "RxMicrophone",
    "RxSpeaker",
    "save_wavfile",
]
