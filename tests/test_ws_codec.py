"""JsonCodec (D17): registered types only, validated fields, limits."""

import json
from dataclasses import dataclass, field

import pytest

from rxplus.ws import CodecError, JsonCodec, WSCodec


@dataclass
class Point:
    x: float
    y: float


@dataclass
class Shape:
    name: str
    points: list[Point]
    tags: dict[str, int] = field(default_factory=dict)
    blob: bytes | None = None
    pair: tuple[int, ...] = ()


class Money:
    """A plain class sent with explicit conversions."""

    def __init__(self, cents: int, currency: str) -> None:
        self.cents = cents
        self.currency = currency


def _codec() -> JsonCodec:
    codec = JsonCodec(max_size=4096, max_depth=8)
    codec.register(Point)
    codec.register(Shape, name="shape")
    codec.register_class(
        Money,
        name="money",
        to_dict=lambda m: {"cents": m.cents, "currency": m.currency},
        from_dict=lambda d: Money(int(str(d["cents"])), str(d["currency"])),
    )
    return codec


def test_is_a_ws_codec():
    assert isinstance(_codec(), WSCodec)
    assert _codec().frame == "text"


def test_round_trip_of_registered_and_plain_values():
    codec = _codec()
    shape = Shape("tri", [Point(0, 0), Point(1.5, 2)], {"a": 1}, b"\x00\xff", (1, 2))
    for value in (None, True, 3, 2.5, "s", [1, "a"], {"k": [1, 2]}, b"raw", shape):
        assert codec.decode(codec.encode(value)) == value
    money = codec.decode(codec.encode(Money(250, "EUR")))
    assert isinstance(money, Money) and (money.cents, money.currency) == (250, "EUR")


def test_wire_format():
    codec = _codec()
    assert json.loads(codec.encode(Point(1, 2))) == {
        "$type": "Point",
        "fields": {"x": 1, "y": 2},
    }
    assert json.loads(codec.encode(b"hi")) == {"$bytes": "aGk="}


def test_int_is_accepted_for_float_fields_but_not_bool_for_int():
    codec = _codec()
    p = codec.decode('{"$type":"Point","fields":{"x":1,"y":2}}')
    assert p == Point(1.0, 2.0) and isinstance(p.x, float)
    with pytest.raises(CodecError):
        codec.decode('{"$type":"money","fields":{"cents":true}}'.replace("true", '"x"'))


@pytest.mark.parametrize(
    "frame",
    [
        '{"$type":"os.system","fields":{"command":"echo"}}',  # not registered
        '{"$type":"Point","fields":{"x":1}}',  # missing field
        '{"$type":"Point","fields":{"x":1,"y":2,"z":3}}',  # extra field
        '{"$type":"Point","fields":{"x":"1","y":2}}',  # wrong type
        '{"$type":"Point","fields":{"x":1,"y":2},"more":1}',  # extra key
        '{"$type":"shape","fields":{"name":"n","points":[1],"tags":{},'
        '"blob":null,"pair":[]}}',  # list item of the wrong type
        '{"$bytes":"not base64!"}',
        '{"a":1,"a":2}',  # duplicate key
        "[NaN]",
        "not json",
    ],
)
def test_rejects_invalid_frames(frame):
    with pytest.raises(CodecError):
        _codec().decode(frame)


def test_limits():
    codec = _codec()
    with pytest.raises(CodecError):
        codec.decode("[" * 9 + "]" * 9)  # deeper than max_depth=8
    with pytest.raises(CodecError):
        codec.decode('"' + "x" * 5000 + '"')  # larger than max_size=4096
    with pytest.raises(CodecError):
        codec.decode("[" * 100_000 + "]" * 100_000)  # recursion inside json
    with pytest.raises(CodecError):
        codec.encode("x" * 5000)


def test_encode_errors():
    codec = _codec()
    with pytest.raises(CodecError):
        codec.encode(object())  # not registered
    with pytest.raises(CodecError):
        codec.encode({"$type": "Point", "fields": {}})  # reserved key
    with pytest.raises(CodecError):
        codec.encode(float("nan"))
    with pytest.raises(CodecError):
        codec.encode({1: "non-string key"})


def test_registration_errors():
    codec = JsonCodec()
    with pytest.raises(TypeError):
        codec.register(Money)  # not a dataclass

    @dataclass
    class Weird:
        f: set[int]

    with pytest.raises(TypeError):
        codec.register(Weird)  # unsupported annotation
    codec.register(Point)
    with pytest.raises(ValueError):
        codec.register(Point)


def test_decode_never_calls_unregistered_code():
    calls: list[object] = []

    class Trap:
        def __init__(self, *a: object, **k: object) -> None:
            calls.append((a, k))

    codec = _codec()
    for name in ("Trap", "builtins.eval", "__import__", "subprocess.Popen"):
        with pytest.raises(CodecError):
            codec.decode(json.dumps({"$type": name, "fields": {"x": "1"}}))
    assert calls == []


# --- review of #24 -----------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        "1e999",
        "-1e999",
        '{"$type":"Point","fields":{"x":1e999,"y":0}}',
        '{"$type":"Point","fields":{"x":%s,"y":0}}' % ("9" * 400),  # int -> float
        "9" * 5000,  # longer than Python's int-string limit
    ],
)
def test_non_finite_and_out_of_range_numbers_are_codec_errors(frame):
    with pytest.raises(CodecError):
        _codec().decode(frame)


@pytest.mark.parametrize("annotation", [list, dict, tuple, object, set[int]])
def test_bare_containers_are_refused_at_register(annotation):
    @dataclass
    class Loose:
        v: annotation  # type: ignore[valid-type]

    with pytest.raises(TypeError):
        JsonCodec().register(Loose)
