import pytest

from rxplus.ws import wsdt_factory, WSStr, WSBytes


def test_wsdt_factory_valid():
    assert isinstance(wsdt_factory('string'), WSStr)
    assert isinstance(wsdt_factory('byte'), WSBytes)


def test_wsdt_factory_invalid():
    with pytest.raises(ValueError):
        wsdt_factory('unknown')
