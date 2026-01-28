import pytest

from rxplus.ws import (
    WSBytes,
    WSStr,
    WSObject,
    wsdt_factory,
    _validate_conn_cfg,
    WS_Channels,
    RxWSServer,
    RxWSClient,
    RxWSClientGroup,
)


def test_wsdt_factory_valid():
    assert isinstance(wsdt_factory("string"), WSStr)
    assert isinstance(wsdt_factory("bytes"), WSBytes)
    assert isinstance(wsdt_factory("object"), WSObject)


def test_wsdt_factory_invalid():
    with pytest.raises(ValueError):
        wsdt_factory("unknown")


def test_wsstr_type_check():
    s = WSStr()
    s.package_type_check("valid")
    with pytest.raises(TypeError, match="WSStr expects a string payload"):
        s.package_type_check(123)


def test_wsstr_and_wsbytes():
    s = WSStr()
    assert s.package(123) == "123"
    assert s.unpackage("abc") == "abc"

    b = WSBytes()
    data = b"abc"
    assert b.package(data) == data
    assert b.unpackage(data) == data
    with pytest.raises(TypeError, match="WSBytes expects"):
        b.package_type_check("x")
    with pytest.raises(TypeError, match="WSBytes expects"):
        b.unpackage("x")


def test_wsobject():
    """Test WSObject pickle serialization."""
    obj = WSObject()
    # package_type_check accepts any value
    obj.package_type_check(None)
    obj.package_type_check({"key": "value"})
    obj.package_type_check([1, 2, 3])

    # package and unpackage roundtrip
    test_data = {"nested": {"data": [1, 2, 3]}}
    packaged = obj.package(test_data)
    assert isinstance(packaged, bytes)
    assert obj.unpackage(packaged) == test_data

    # unpackage rejects text frames
    with pytest.raises(TypeError, match="expects binary frame"):
        obj.unpackage("text frame")


def test_wsobject_tagged_data_serialization():
    """Test WSObject handles TaggedData serialization transparently."""
    from rxplus import TaggedData
    
    obj = WSObject()
    
    # Test: TaggedData containing string data
    tagged = TaggedData("/test/path", "Hello, World!")
    packaged = obj.package(tagged)
    restored = obj.unpackage(packaged)
    
    assert isinstance(restored, TaggedData)
    assert restored.tag == "/test/path"
    assert restored.data == "Hello, World!"

    # Test: TaggedData containing dict data
    data = {"message": "test", "count": 42}
    tagged = TaggedData("/api", data)
    packaged = obj.package(tagged)
    restored = obj.unpackage(packaged)
    
    assert isinstance(restored, TaggedData)
    assert restored.tag == "/api"
    assert restored.data == data


# =============================================================================
# Connection configuration validation tests
# =============================================================================


def test_validate_conn_cfg_valid():
    """Valid config should not raise."""
    _validate_conn_cfg({"host": "localhost", "port": 8888}, ["host", "port"])


def test_validate_conn_cfg_missing_host():
    """Missing host should raise ValueError."""
    with pytest.raises(ValueError, match="missing required keys.*host"):
        _validate_conn_cfg({"port": 8888}, ["host", "port"])


def test_validate_conn_cfg_missing_port():
    """Missing port should raise ValueError."""
    with pytest.raises(ValueError, match="missing required keys.*port"):
        _validate_conn_cfg({"host": "localhost"}, ["host", "port"])


def test_validate_conn_cfg_missing_multiple():
    """Missing multiple keys should list all."""
    with pytest.raises(ValueError, match="missing required keys"):
        _validate_conn_cfg({}, ["host", "port"])


# =============================================================================
# WS_Channels thread safety tests
# =============================================================================


def test_ws_channels_add_remove_client():
    """Test thread-safe client registration."""
    channels = WS_Channels(datatype="string")

    mock_ws = object()
    mock_queue = object()

    channels.add_client(mock_ws, mock_queue)
    assert mock_ws in channels.channels
    assert mock_queue in channels.queues

    channels.remove_client(mock_ws, mock_queue)
    assert mock_ws not in channels.channels
    assert mock_queue not in channels.queues


def test_ws_channels_get_queues_snapshot():
    """Test snapshot returns a copy for safe iteration."""
    channels = WS_Channels(datatype="string")

    q1, q2 = object(), object()
    channels.add_client(object(), q1)
    channels.add_client(object(), q2)

    snapshot = channels.get_queues_snapshot()
    assert isinstance(snapshot, list)
    assert len(snapshot) == 2
    assert q1 in snapshot
    assert q2 in snapshot


# =============================================================================
# RxWSServer initialization tests
# =============================================================================


def test_server_requires_host_and_port():
    """Server should validate conn_cfg."""
    with pytest.raises(ValueError, match="missing required keys"):
        RxWSServer({"host": "localhost"})  # missing port

    with pytest.raises(ValueError, match="missing required keys"):
        RxWSServer({"port": 8888})  # missing host


def test_server_accepts_valid_config():
    """Server should accept valid config."""
    server = RxWSServer({"host": "localhost", "port": 0}, name="test-server")
    assert server.host == "localhost"
    assert server._name == "test-server"
    server.on_completed()


# =============================================================================
# RxWSClient initialization tests
# =============================================================================


def test_client_requires_host_and_port():
    """Client should validate conn_cfg."""
    with pytest.raises(ValueError, match="missing required keys"):
        RxWSClient({"host": "localhost"})  # missing port


def test_client_buffer_while_disconnected_default():
    """Default buffer_while_disconnected should be False."""
    client = RxWSClient({"host": "localhost", "port": 9999})
    assert client._buffer_while_disconnected is False
    client.on_completed()


def test_client_buffer_while_disconnected_enabled():
    """buffer_while_disconnected can be enabled."""
    client = RxWSClient(
        {"host": "localhost", "port": 9999},
        buffer_while_disconnected=True,
    )
    assert client._buffer_while_disconnected is True
    client.on_completed()


# =============================================================================
# RxWSClientGroup initialization tests
# =============================================================================


def test_client_group_requires_host_and_port():
    """ClientGroup should validate conn_cfg."""
    with pytest.raises(ValueError, match="missing required keys"):
        RxWSClientGroup({"host": "localhost"})  # missing port


def test_client_group_passes_buffer_while_disconnected():
    """ClientGroup should pass buffer_while_disconnected to child clients."""
    group = RxWSClientGroup(
        {"host": "localhost", "port": 9999},
        buffer_while_disconnected=True,
    )
    assert group._buffer_while_disconnected is True
    group.on_completed()