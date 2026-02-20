"""WebSocket package for bi-directional ReactiveX communication.

Re-exports all public names for backward compatibility.
"""

from ._otel_mixin import OTelLoggingMixin
from .client import RetryPolicy, RxWSClient, WSConnectionState
from .client_group import RxWSClientGroup
from .datatypes import (
    WSBytes,
    WSConnectionConfig,
    WSDatatype,
    WSObject,
    WSStr,
    wsdt_factory,
)
from .server import RxWSServer, WSChannels

# Backward-compatible aliases for names that have been renamed.
# These allow downstream code (e.g., gateway/node.py) to continue importing
# the old names until they are updated in a separate task.
ConnectionState = WSConnectionState
WS_Channels = WSChannels

__all__ = [
    # datatypes
    "WSConnectionConfig",
    "WSDatatype",
    "WSStr",
    "WSBytes",
    "WSObject",
    "wsdt_factory",
    # mixin
    "OTelLoggingMixin",
    # server
    "WSChannels",
    "RxWSServer",
    # client
    "WSConnectionState",
    "RetryPolicy",
    "RxWSClient",
    # client group
    "RxWSClientGroup",
]
