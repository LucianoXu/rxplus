import pytest

from rxplus.ws import WSBytes, WSStr, wsdt_factory


def test_wsdt_factory_valid():
    assert isinstance(wsdt_factory("string"), WSStr)
    assert isinstance(wsdt_factory("byte"), WSBytes)


def test_wsdt_factory_invalid():
    with pytest.raises(ValueError):
        wsdt_factory("unknown")


def test_wsstr_and_wsbytes():
    s = WSStr()
    assert s.package(123) == "123"
    assert s.unpackage("abc") == "abc"

    b = WSBytes()
    data = b"abc"
    assert b.package(data) == data
    assert b.unpackage(data) == data
    with pytest.raises(TypeError):
        b.package_type_check("x")
    with pytest.raises(TypeError):
        b.unpackage("x")

# TODO: more sophisticated tests for WSServer and WSClient are needed here.