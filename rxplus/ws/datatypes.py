"""WebSocket data types and connection configuration.

Provides the typed connection config dataclass, the abstract WSDatatype base
class, concrete implementations (WSStr, WSBytes, WSObject), and the
wsdt_factory helper.
"""

import pickle
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class WSConnectionConfig:
    """Typed WebSocket connection configuration."""

    host: str
    port: int
    path: str = "/"


class WSDatatype(ABC):
    @abstractmethod
    def package_type_check(self, value: str | bytes | object) -> None:
        """Check whether the value can be sent through this datatype.

        If not, an error will be raised.
        """
        ...

    @abstractmethod
    def package(self, value: str | bytes | object) -> str | bytes: ...

    @abstractmethod
    def unpackage(self, value: str | bytes) -> str | bytes | object: ...


class WSStr(WSDatatype):
    def package_type_check(self, value: str | bytes | object) -> None:
        # Ensure text frames are actual strings to avoid implicit coercion
        # and unexpected payload formats at send time.
        if not isinstance(value, str):
            raise TypeError("WSStr expects a string payload")

    def package(self, value: str | bytes | object) -> str:
        return str(value)

    def unpackage(self, value: str | bytes) -> str:
        if isinstance(value, bytes):
            return value.decode()
        return value


class WSBytes(WSDatatype):
    def package_type_check(self, value: str | bytes | object) -> None:
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("WSBytes expects a bytes-like object")

    def package(self, value: str | bytes | object) -> bytes:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        raise TypeError("WSBytes expects a bytes-like object")

    def unpackage(self, value: str | bytes) -> bytes:
        # websockets binary frame -> bytes; text frame -> str
        if isinstance(value, str):
            raise TypeError(f"WSBytes expects a bytes-like object, got str '{value}'")
        return bytes(value)


class WSObject(WSDatatype):
    """WebSocket datatype for arbitrary Python objects using pickle serialization.

    Note: Standard OTel LogRecords may have issues with pickle serialization
    due to their context objects. Custom data classes are recommended for
    reliable serialization over WebSocket.
    """

    def package_type_check(self, value: str | bytes | object) -> None:
        # Accept any pickleable object; validation occurs in package().
        pass

    def package(self, value: str | bytes | object) -> bytes:
        try:
            return pickle.dumps(value)
        except Exception as e:
            raise TypeError(
                f"WSObject cannot pickle value of type {type(value)}: {e}"
            ) from e

    def unpackage(self, value: str | bytes) -> object:
        if isinstance(value, str):
            raise TypeError(
                f"WSObject expects binary frame (bytes); got text frame: {value!r}"
            )
        try:
            return pickle.loads(value)  # noqa: S301
        except Exception as e:
            # Include exception type and repr for better debugging
            error_msg = str(e) or repr(e)
            raise TypeError(
                f"WSObject failed to unpickle payload: {type(e).__name__}: {error_msg}"
            ) from e


def wsdt_factory(datatype: Literal["string", "bytes", "object"]) -> WSDatatype:
    """Create a WSDatatype instance based on the datatype parameter."""
    if datatype == "string":
        return WSStr()
    elif datatype == "bytes":
        return WSBytes()
    elif datatype == "object":
        return WSObject()
    else:
        raise ValueError(f"Unsupported datatype '{datatype}'.")
