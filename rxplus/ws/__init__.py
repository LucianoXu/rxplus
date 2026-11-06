"""WebSocket package for bi-directional ReactiveX communication.

Re-exports all public names.
"""

from ._otel_mixin import OTelLoggingMixin
from ._sendqueue import SendOverflow
from .client import RetryPolicy, RxWSClient, WSConnectionState
from .client_group import RxWSClientGroup
from .codec import CodecError, JsonCodec, JsonValue, WSCodec
from .datatypes import (
    DatatypeSpec,
    PickleNotAllowedError,
    WSBytes,
    WSConnectionConfig,
    WSDatatype,
    WSObject,
    WSStr,
    wsdt_factory,
)
from .server import RxWSServer, WSChannels, WSConnectionEvent

# Backward-compatible aliases for names that have been renamed (removed in
# PR-C of the 0.2 batch).
ConnectionState = WSConnectionState
WS_Channels = WSChannels

__all__ = [
    # datatypes and codecs
    "WSConnectionConfig",
    "WSDatatype",
    "WSStr",
    "WSBytes",
    "WSObject",
    "wsdt_factory",
    "DatatypeSpec",
    "PickleNotAllowedError",
    "WSCodec",
    "JsonCodec",
    "JsonValue",
    "CodecError",
    "SendOverflow",
    # mixin
    "OTelLoggingMixin",
    # server
    "WSChannels",
    "WSConnectionEvent",
    "RxWSServer",
    # client
    "WSConnectionState",
    "RetryPolicy",
    "RxWSClient",
    # client group
    "RxWSClientGroup",
]
