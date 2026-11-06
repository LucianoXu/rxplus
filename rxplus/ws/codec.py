"""Codecs for WebSocket frames, and a safe JSON codec for custom objects (D17).

:class:`WSCodec` is what turns values into frames and back. :class:`JsonCodec`
is the documented way to send custom objects: it encodes registered
dataclasses (or classes with explicit to/from functions) as JSON tagged with
a registered name. Decoding constructs only registered types, validates every
field against its annotation, enforces size and nesting limits, and never
imports, evaluates or calls anything named by the peer. Pickle
(``datatype="object"``) remains an opt-in for trusted links only.

Wire format of :class:`JsonCodec` (one text frame per value):

- ``None``, ``bool``, ``int``, ``float``, ``str``, lists and objects with
  string keys pass through as JSON;
- ``bytes`` is ``{"$bytes": "<base64>"}``;
- a registered object is ``{"$type": "<name>", "fields": {...}}``.
"""

import base64
import binascii
import dataclasses
import json
import types
import typing
from collections.abc import Callable
from typing import Literal, Protocol, cast, get_args, get_origin, runtime_checkable

type JsonValue = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)

_TYPE_KEY = "$type"
_BYTES_KEY = "$bytes"
_RESERVED = (_TYPE_KEY, _BYTES_KEY)


class CodecError(ValueError):
    """A value cannot be encoded, or a frame is not a valid encoding."""


@runtime_checkable
class WSCodec[T](Protocol):
    """Turns values into WebSocket frames and back.

    ``frame`` is the frame type the codec produces. ``decode`` must raise
    (preferably :class:`CodecError`) on anything it does not accept.
    """

    frame: Literal["text", "binary"]

    def encode(self, value: T) -> bytes | str: ...

    def decode(self, data: bytes | str) -> T: ...


# --------------------------------------------------------------------------
# Field validation from annotations
# --------------------------------------------------------------------------

type _Check = Callable[[object, "JsonCodec", int], object]


def _fail(expected: str, value: object) -> CodecError:
    return CodecError(f"expected {expected}, got {type(value).__name__}")


def _scalar(tp: type) -> _Check:
    def check(value: object, codec: "JsonCodec", depth: int) -> object:
        # bool is an int subclass; int is accepted where float is expected.
        if tp is float and isinstance(value, int) and not isinstance(value, bool):
            return float(value)
        if tp is int and isinstance(value, bool):
            raise _fail("int", value)
        if not isinstance(value, tp):
            raise _fail(tp.__name__, value)
        return value

    return check


def _none(value: object, codec: "JsonCodec", depth: int) -> object:
    if value is not None:
        raise _fail("None", value)
    return None


def _bytes(value: object, codec: "JsonCodec", depth: int) -> object:
    if isinstance(value, bytes):
        return value
    raise _fail("bytes", value)


class JsonCodec:
    """Text frames of JSON; custom objects only from its own registry.

    Args:
        max_size: Largest frame accepted by :meth:`decode` (and produced by
            :meth:`encode`), in bytes of UTF-8.
        max_depth: Deepest nesting of lists and objects accepted.
    """

    frame: Literal["text", "binary"] = "text"

    def __init__(self, *, max_size: int = 1 << 20, max_depth: int = 32) -> None:
        if max_size < 1 or max_depth < 1:
            raise ValueError("max_size and max_depth must be positive")
        self.max_size = max_size
        self.max_depth = max_depth
        self._by_name: dict[str, _Registered] = {}
        self._by_type: dict[type, _Registered] = {}

    # ------------------------------------------------------------- registry

    def register[D](self, cls: type[D], *, name: str | None = None) -> type[D]:
        """Register a dataclass; usable as a decorator. ``name`` defaults to
        the class's ``__qualname__``. Every field annotation must be
        supported (see :meth:`_check_for`), else ``TypeError``."""
        if not dataclasses.is_dataclass(cls):
            raise TypeError(f"{cls!r} is not a dataclass; use register_class")
        hints = typing.get_type_hints(cls)
        fields = {
            f.name: self._check_for(hints[f.name])
            for f in dataclasses.fields(cls)
            if f.init
        }

        def to_dict(obj: object) -> dict[str, object]:
            return {n: getattr(obj, n) for n in fields}

        def from_dict(values: dict[str, object]) -> object:
            return cls(**values)

        self._add(
            _Registered(name or cls.__qualname__, cls, fields, to_dict, from_dict)
        )
        return cls

    def register_class[C](
        self,
        cls: type[C],
        *,
        name: str,
        to_dict: Callable[[C], dict[str, JsonValue]],
        from_dict: Callable[[dict[str, JsonValue]], C],
    ) -> None:
        """Register a plain class with explicit conversions. Its fields are
        not validated by annotation; ``from_dict`` receives decoded JSON
        values (registered objects inside are already constructed)."""
        self._add(
            _Registered(
                name,
                cls,
                None,
                cast(Callable[[object], dict[str, object]], to_dict),
                cast(Callable[[dict[str, object]], object], from_dict),
            )
        )

    def _add(self, reg: "_Registered") -> None:
        if not reg.name or reg.name in self._by_name:
            raise ValueError(f"type name {reg.name!r} is empty or already registered")
        if reg.cls in self._by_type:
            raise ValueError(f"{reg.cls!r} is already registered")
        self._by_name[reg.name] = reg
        self._by_type[reg.cls] = reg

    def _check_for(self, tp: object) -> _Check:
        """A validator for one supported annotation."""
        if tp is type(None) or tp is None:
            return _none
        if tp is bytes:
            return _bytes
        if tp in (bool, int, float, str):
            return _scalar(cast(type, tp))
        origin = get_origin(tp)
        args = get_args(tp)
        if origin in (types.UnionType, typing.Union):
            options = [self._check_for(a) for a in args]

            def union(value: object, codec: "JsonCodec", depth: int) -> object:
                for option in options:
                    try:
                        return option(value, codec, depth)
                    except CodecError:
                        continue
                raise CodecError(f"value matches no member of {tp}")

            return union
        if origin is list and len(args) == 1:
            item = self._check_for(args[0])

            def list_(value: object, codec: "JsonCodec", depth: int) -> object:
                if not isinstance(value, list):
                    raise _fail("list", value)
                return [item(v, codec, depth + 1) for v in value]

            return list_
        if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
            item = self._check_for(args[0])

            def tuple_(value: object, codec: "JsonCodec", depth: int) -> object:
                if not isinstance(value, list):
                    raise _fail("list", value)
                return tuple(item(v, codec, depth + 1) for v in value)

            return tuple_
        if origin is dict and len(args) == 2 and args[0] is str:
            item = self._check_for(args[1])

            def dict_(value: object, codec: "JsonCodec", depth: int) -> object:
                if not isinstance(value, dict):
                    raise _fail("object", value)
                return {k: item(v, codec, depth + 1) for k, v in value.items()}

            return dict_
        if isinstance(tp, type):
            cls = tp

            def registered(value: object, codec: "JsonCodec", depth: int) -> object:
                # Resolved at use time, so classes may refer to each other.
                if type(value) is not cls:
                    raise _fail(cls.__qualname__, value)
                return value

            return registered
        raise TypeError(f"unsupported field annotation for JsonCodec: {tp!r}")

    # ------------------------------------------------------------ encoding

    def encode(self, value: object) -> str:
        text = json.dumps(
            self._to_json(value, 0),
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if len(text.encode("utf-8")) > self.max_size:
            raise CodecError(f"encoded value exceeds max_size={self.max_size}")
        return text

    def _to_json(self, value: object, depth: int) -> object:
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise CodecError("NaN and Infinity are not JSON")
            return value
        if isinstance(value, bytes):
            return {_BYTES_KEY: base64.b64encode(value).decode("ascii")}
        self._deeper(depth)  # every remaining case is a list or an object
        if isinstance(value, (list, tuple)):
            return [self._to_json(v, depth + 1) for v in value]
        if isinstance(value, dict):
            out: dict[str, object] = {}
            for k, v in value.items():
                if not isinstance(k, str):
                    raise CodecError("object keys must be strings")
                out[k] = self._to_json(v, depth + 1)
            if any(k in out for k in _RESERVED):
                raise CodecError(f"plain objects may not use the keys {_RESERVED}")
            return out
        reg = self._by_type.get(type(value))
        if reg is None:
            raise CodecError(f"{type(value).__qualname__} is not registered")
        fields = reg.to_dict(value)
        return {
            _TYPE_KEY: reg.name,
            "fields": {k: self._to_json(v, depth + 2) for k, v in fields.items()},
        }

    # ------------------------------------------------------------ decoding

    def decode(self, data: bytes | str) -> object:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        if len(raw) > self.max_size:
            raise CodecError(f"frame exceeds max_size={self.max_size}")
        try:
            parsed = json.loads(
                raw,
                object_pairs_hook=_no_duplicates,
                parse_constant=_no_constant,
            )
        except (ValueError, RecursionError) as e:
            raise CodecError(f"not valid JSON: {type(e).__name__}") from e
        return self._from_json(parsed, 0)

    def _deeper(self, depth: int) -> None:
        """A list or object at ``depth`` (0: top level) is nesting level
        ``depth + 1``."""
        if depth >= self.max_depth:
            raise CodecError(f"nesting deeper than max_depth={self.max_depth}")

    def _from_json(self, value: object, depth: int) -> object:
        if isinstance(value, (list, dict)):
            self._deeper(depth)
        if isinstance(value, list):
            return [self._from_json(v, depth + 1) for v in value]
        if not isinstance(value, dict):
            return value
        if _BYTES_KEY in value:
            if set(value) != {_BYTES_KEY} or not isinstance(value[_BYTES_KEY], str):
                raise CodecError("malformed $bytes")
            try:
                return base64.b64decode(value[_BYTES_KEY], validate=True)
            except (binascii.Error, ValueError) as e:
                raise CodecError("malformed $bytes") from e
        if _TYPE_KEY not in value:
            return {k: self._from_json(v, depth + 1) for k, v in value.items()}
        name = value[_TYPE_KEY]
        fields = value.get("fields")
        if set(value) != {_TYPE_KEY, "fields"} or not isinstance(fields, dict):
            raise CodecError("a tagged object is {$type, fields}")
        reg = self._by_name.get(name) if isinstance(name, str) else None
        if reg is None:
            raise CodecError(f"type {name!r} is not registered")
        decoded = {k: self._from_json(v, depth + 2) for k, v in fields.items()}
        if reg.fields is not None:
            if set(decoded) != set(reg.fields):
                missing = sorted(set(reg.fields) - set(decoded))
                extra = sorted(set(decoded) - set(reg.fields))
                raise CodecError(f"{name}: missing {missing}, unexpected {extra}")
            decoded = {
                k: check(decoded[k], self, depth + 2) for k, check in reg.fields.items()
            }
        try:
            return reg.from_dict(decoded)
        except CodecError:
            raise
        except Exception as e:
            raise CodecError(f"{name}: construction failed: {type(e).__name__}") from e


@dataclasses.dataclass(frozen=True, slots=True)
class _Registered:
    name: str
    cls: type
    fields: dict[str, _Check] | None  # None: register_class (no validation)
    to_dict: Callable[[object], dict[str, object]]
    from_dict: Callable[[dict[str, object]], object]


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in pairs:
        if k in out:
            raise CodecError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_constant(name: str) -> object:
    raise CodecError(f"{name} is not JSON")


__all__ = ["CodecError", "JsonCodec", "JsonValue", "WSCodec"]
