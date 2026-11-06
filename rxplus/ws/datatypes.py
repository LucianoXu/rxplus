"""WebSocket data types and connection configuration.

Provides the typed connection config dataclass, the abstract WSDatatype base
class, concrete implementations (WSStr, WSBytes, WSObject), the
wsdt_factory helper, and the resolution of a component's ``datatype``
argument to a codec per path (built-in names or any
:class:`~rxplus.ws.codec.WSCodec`).
"""

import pickle
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .codec import CodecError, WSCodec

WSDatatypeName = Literal["string", "bytes", "object"]

#: What a component's ``datatype`` takes: a built-in name, a codec, or a
#: function of the request path returning either.
type DatatypeSpec = (
    WSDatatypeName | WSCodec[object] | Callable[[str], WSDatatypeName | WSCodec[object]]
)


@dataclass(frozen=True)
class WSConnectionConfig:
    """Typed WebSocket connection configuration."""

    host: str
    port: int
    path: str = "/"


class WSDatatype(ABC):
    """A built-in codec: ``"string"``, ``"bytes"`` or ``"object"`` (pickle)."""

    frame: Literal["text", "binary"] = "binary"

    def encode(self, value: object) -> str | bytes:
        """:class:`WSCodec` interface: check, then package."""
        try:
            self.package_type_check(value)
            return self.package(value)
        except TypeError as e:
            raise CodecError(str(e)) from e

    def decode(self, data: str | bytes) -> object:
        """:class:`WSCodec` interface."""
        try:
            return self.unpackage(data)
        except TypeError as e:
            raise CodecError(str(e)) from e

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
    frame: Literal["text", "binary"] = "text"

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


class PickleNotAllowedError(ValueError):
    """``datatype="object"`` (pickle) was requested without ``allow_pickle``."""


def codec_for(
    choice: WSDatatypeName | WSCodec[object], *, allow_pickle: bool
) -> WSCodec[object]:
    """The codec for one resolved ``datatype`` choice.

    ``"object"`` unpickles whatever the peer sends, which runs the peer's
    code: it is refused unless ``allow_pickle=True`` (trusted links only).
    """
    if isinstance(choice, str):
        if choice == "object" and not allow_pickle:
            raise PickleNotAllowedError(
                'datatype="object" unpickles peer data (code execution);'
                " pass allow_pickle=True on trusted links only, or use"
                " rxplus.ws.JsonCodec for custom objects"
            )
        return wsdt_factory(choice)
    if isinstance(choice, WSObject) and not allow_pickle:
        # A WSObject instance is a WSCodec too: refuse it the same way.
        raise PickleNotAllowedError(
            "a pickle codec (WSObject) unpickles peer data (code execution);"
            " pass allow_pickle=True on trusted links only"
        )
    if isinstance(choice, WSCodec):
        return choice  # a codec you supply is trusted as written
    raise ValueError(f"Unsupported datatype {choice!r}")


def resolve_codecs(
    datatype: DatatypeSpec, *, allow_pickle: bool
) -> Callable[[str], WSCodec[object]]:
    """Per-path codec function for a component's ``datatype`` argument.

    A fixed choice is validated at once, so ``datatype="object"`` without
    ``allow_pickle`` fails at construction.
    """
    if isinstance(datatype, str) or isinstance(datatype, WSCodec):
        fixed = codec_for(datatype, allow_pickle=allow_pickle)
        return lambda _path: fixed
    if callable(datatype):
        per_path = datatype
        return lambda path: codec_for(per_path(path), allow_pickle=allow_pickle)
    raise ValueError(
        f"Unsupported datatype {datatype!r}. Expected 'string', 'bytes',"
        " 'object', a WSCodec, or a function of the path returning one."
    )
