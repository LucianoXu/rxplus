"""Signal encoding and name validation for reacting/1 (standard library only)."""

import json

#: One reacting/1 signal before encoding: a JSON object.
type Signal = dict[str, object]

PROTOCOL = "reacting/1"


def encode(signal: Signal) -> str:
    """Encode one signal as one JSON line (without the newline).

    Compact separators, UTF-8 text (no ASCII escaping), and NaN/Infinity
    refused: reacting/1 signals are RFC 8259 JSON.
    """
    return json.dumps(
        signal, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def check_id(value: str, what: str) -> str:
    """Validate a source name, element/link/span ID: non-empty, no ``/``."""
    if not isinstance(value, str) or not value or "/" in value:
        raise ValueError(f"{what} must be a non-empty string without '/': {value!r}")
    return value


def check_label(value: str, what: str = "label") -> str:
    """Validate a label: a non-empty string."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string: {value!r}")
    return value


def check_kind(value: str | None) -> str | None:
    """Validate an optional element kind."""
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"kind must be a non-empty string or None: {value!r}")
    return value


def check_reference(value: str, own_source: str) -> str:
    """Validate a global element reference ``<source>/<id>`` of another source."""
    source, sep, ident = value.partition("/")
    if not sep or not source or not ident or "/" in ident:
        raise ValueError(f"a global reference has the form '<source>/<id>': {value!r}")
    if source == own_source:
        raise ValueError(f"a global reference must name another source: {value!r}")
    return value
