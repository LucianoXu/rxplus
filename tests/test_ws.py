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
    ConnectionState,
    RetryPolicy,
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


# =============================================================================
# ConnectionState enum tests
# =============================================================================


def test_connection_state_enum_values():
    """Verify all ConnectionState values are defined correctly."""
    assert ConnectionState.DISCONNECTED.value == "disconnected"
    assert ConnectionState.CONNECTING.value == "connecting"
    assert ConnectionState.CONNECTED.value == "connected"
    assert ConnectionState.RECONNECTING.value == "reconnecting"
    assert ConnectionState.CLOSED.value == "closed"


def test_connection_state_is_enum():
    """Verify ConnectionState members are proper enum instances."""
    assert isinstance(ConnectionState.DISCONNECTED, ConnectionState)
    assert ConnectionState.DISCONNECTED is ConnectionState.DISCONNECTED


# =============================================================================
# RetryPolicy tests
# =============================================================================


def test_retry_policy_default_values():
    """Verify default RetryPolicy values."""
    policy = RetryPolicy()
    assert policy.max_retries is None
    assert policy.base_delay == 0.5
    assert policy.max_delay == 30.0
    assert policy.backoff_factor == 2.0
    assert policy.jitter == 0.1


def test_retry_policy_get_delay_exponential():
    """Verify exponential backoff calculation (without jitter)."""
    policy = RetryPolicy(base_delay=1.0, backoff_factor=2.0, jitter=0.0)
    
    # attempt 0: 1.0 * 2^0 = 1.0
    assert policy.get_delay(0) == 1.0
    # attempt 1: 1.0 * 2^1 = 2.0
    assert policy.get_delay(1) == 2.0
    # attempt 2: 1.0 * 2^2 = 4.0
    assert policy.get_delay(2) == 4.0
    # attempt 3: 1.0 * 2^3 = 8.0
    assert policy.get_delay(3) == 8.0


def test_retry_policy_get_delay_max_cap():
    """Verify delay is capped at max_delay."""
    policy = RetryPolicy(base_delay=1.0, max_delay=5.0, backoff_factor=2.0, jitter=0.0)
    
    # attempt 10: would be 1.0 * 2^10 = 1024.0, but capped at 5.0
    assert policy.get_delay(10) == 5.0
    assert policy.get_delay(100) == 5.0


def test_retry_policy_get_delay_jitter():
    """Verify jitter is applied within expected bounds."""
    policy = RetryPolicy(base_delay=10.0, backoff_factor=1.0, jitter=0.1)
    
    # With jitter=0.1 and base_delay=10.0, delay should be 10.0 ± 1.0
    delays = [policy.get_delay(0) for _ in range(100)]
    
    assert all(9.0 <= d <= 11.0 for d in delays), "All delays should be within jitter bounds"
    # Check that jitter actually varies the values (not all the same)
    assert len(set(delays)) > 1, "Jitter should produce varying delays"


def test_retry_policy_custom_values():
    """Verify custom RetryPolicy values are respected."""
    policy = RetryPolicy(
        max_retries=5,
        base_delay=0.1,
        max_delay=10.0,
        backoff_factor=3.0,
        jitter=0.2,
    )
    assert policy.max_retries == 5
    assert policy.base_delay == 0.1
    assert policy.max_delay == 10.0
    assert policy.backoff_factor == 3.0
    assert policy.jitter == 0.2


# =============================================================================
# RxWSClient connection state tests
# =============================================================================


def test_client_has_connection_state_observable():
    """Client should expose connection_state property."""
    client = RxWSClient({"host": "localhost", "port": 9999})
    
    # Should be able to subscribe to connection_state
    states = []
    client.connection_state.subscribe(lambda s: states.append(s))
    
    # Should have received initial state (DISCONNECTED or CONNECTING depending on timing)
    # Give a moment for the thread to start
    import time
    time.sleep(0.1)
    
    assert len(states) >= 1
    assert all(isinstance(s, ConnectionState) for s in states)
    
    client.on_completed()


def test_client_retry_policy_default():
    """Client should use default RetryPolicy when not provided."""
    client = RxWSClient({"host": "localhost", "port": 9999})
    
    assert isinstance(client._retry_policy, RetryPolicy)
    # Default should use conn_retry_timeout as base_delay
    assert client._retry_policy.base_delay == client.conn_retry_timeout
    
    client.on_completed()


def test_client_retry_policy_custom():
    """Client should use custom RetryPolicy when provided."""
    custom_policy = RetryPolicy(max_retries=3, base_delay=1.0)
    client = RxWSClient(
        {"host": "localhost", "port": 9999},
        retry_policy=custom_policy,
    )
    
    assert client._retry_policy is custom_policy
    assert client._retry_policy.max_retries == 3
    assert client._retry_policy.base_delay == 1.0
    
    client.on_completed()


def test_client_emits_closed_on_completed():
    """Client should emit CLOSED state on on_completed."""
    client = RxWSClient({"host": "localhost", "port": 9999})
    
    states = []
    client.connection_state.subscribe(lambda s: states.append(s))
    
    import time
    time.sleep(0.1)  # Let connection attempt start
    
    client.on_completed()
    time.sleep(0.1)  # Let shutdown complete
    
    # CLOSED should be in the state history
    assert ConnectionState.CLOSED in states