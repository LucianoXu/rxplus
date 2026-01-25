# RxWS Components Refinements Plan

## Current Context

- `RxWSServer`, `RxWSClient`, and `RxWSClientGroup` provide ReactiveX-compatible WebSocket communication
- Components wrap `websockets` library and run asyncio event loops in dedicated daemon threads
- Data serialization via `WSDatatype` adapters (`WSStr`, `WSBytes`, `WSObject`)
- Path-based routing using `TaggedData`
- Integrated OpenTelemetry-compatible logging

**Pain points identified during code review:**
- Thread safety issues in shared mutable state
- Silent message dropping when client not connected
- Resource cleanup gaps
- Missing input validation
- Inconsistent error handling patterns
- No graceful shutdown mechanism for client

## Requirements

### Functional Requirements

1. **Thread-safe operations** — All cross-thread data access must be properly synchronized
2. **Reliable message delivery** — Option to buffer messages during disconnection
3. **Graceful shutdown** — All components must cleanly release resources on completion
4. **Input validation** — Validate connection configuration parameters
5. **Consistent error handling** — Uniform error wrapping and propagation

### Non-Functional Requirements

- **Backwards compatibility** — Changes should not break existing API contracts
- **Performance** — Avoid unnecessary locking overhead in hot paths
- **Testability** — Enable unit testing of components in isolation

## Design Decisions

### 1. Thread Safety for `path_channels` (RxWSServer)

Will add `threading.Lock` for `path_channels` dictionary because:
- `_get_path_channels()` is called from both main thread (`on_next`) and asyncio thread (`handle_client`)
- Dictionary operations are not atomic when checking and inserting
- Trade-off: Minor lock contention vs. data corruption risk

### 2. Message Buffering Strategy (RxWSClient)

Will add optional `buffer_while_disconnected` parameter because:
- Current behavior silently drops messages when `_connected=False`
- Some use cases require guaranteed delivery after reconnection
- Default remains `False` for backwards compatibility
- Trade-off: Memory growth during extended disconnection vs. data loss

### 3. Graceful Shutdown Pattern

Will implement `_shutdown_client()` mirroring `_shutdown_server()` because:
- Current `on_completed()` only schedules async close, doesn't wait for thread
- Event loop continues running after `on_completed()`
- Thread resources never joined

## Technical Design

### 1. Bug Fixes

#### BUG-1: Race condition in `RxWSServer._get_path_channels()`

**Problem:** Non-atomic check-then-act on `path_channels` dict accessed from multiple threads.

**Location:** [rxplus/ws.py#L226-L234](rxplus/ws.py#L226-L234)

**Fix:**
```python
class RxWSServer(Subject):
    def __init__(self, ...):
        ...
        self._path_channels_lock = threading.Lock()
        self.path_channels: dict[str, WS_Channels] = {}
    
    def _get_path_channels(self, path: str) -> WS_Channels:
        with self._path_channels_lock:
            if path not in self.path_channels:
                datatype = self.datatype_func(path)
                self.path_channels[path] = WS_Channels(datatype=datatype)
            return self.path_channels[path]
```

#### BUG-2: Race condition in `RxWSClientGroup._ensure_client()`

**Problem:** Same pattern — check-then-act on `_clients` dict without synchronization.

**Location:** [rxplus/ws.py#L946-L958](rxplus/ws.py#L946-L958)

**Fix:** Add `threading.Lock` around client creation/lookup.

#### BUG-3: `RxWSClient` event loop not stopped on `on_completed()`

**Problem:** `async_completing()` closes WebSocket but doesn't stop the loop. The loop continues running `connect_client()` which will try to reconnect.

**Location:** [rxplus/ws.py#L609-L628](rxplus/ws.py#L609-L628)

**Fix:**
```python
async def async_completing(self):
    try:
        self._log("Closing...", "INFO")
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
        self._log("Closed.", "INFO")
        super().on_completed()
    except asyncio.CancelledError:
        self._log("Async completing cancelled.", "INFO")
        raise
    finally:
        # Stop the event loop to exit _run_loop()
        asyncio.get_running_loop().stop()
```

#### BUG-4: `RxWSClient.queue` created outside event loop

**Problem:** `asyncio.Queue()` created in `__init__` before the event loop exists. This can cause issues in some asyncio versions.

**Location:** [rxplus/ws.py#L556](rxplus/ws.py#L556)

**Fix:** Create queue lazily in `_run_loop()` after event loop is set.

#### BUG-5: Unpackage errors not caught in receivers

**Problem:** `adapter.unpackage(data)` can raise exceptions that are not caught, causing silent receiver task termination.

**Locations:**
- [rxplus/ws.py#L405-L407](rxplus/ws.py#L405-L407) (server receiver)
- [rxplus/ws.py#L795](rxplus/ws.py#L795) (client receiver)

**Fix:** Wrap unpackage in try-except and log/propagate errors.

#### BUG-6: `WS_Channels.queues` and `channels` modified concurrently

**Problem:** `handle_client()` adds/removes from sets while `on_next()` iterates over `queues`.

**Location:** [rxplus/ws.py#L299-L301](rxplus/ws.py#L299-L301) and [rxplus/ws.py#L277](rxplus/ws.py#L277)

**Fix:** Use `threading.Lock` or copy sets before iteration.

### 2. Improvements

#### IMP-1: Add connection configuration validation

**Problem:** Missing keys cause cryptic `KeyError` exceptions.

**Change:** Add validation in `__init__`:
```python
def _validate_conn_cfg(conn_cfg: dict, required_keys: list[str]) -> None:
    missing = [k for k in required_keys if k not in conn_cfg]
    if missing:
        raise ValueError(f"conn_cfg missing required keys: {missing}")
```

#### IMP-2: Add optional message buffering for `RxWSClient`

**Problem:** Messages sent while disconnected are silently dropped.

**Change:** Add `buffer_while_disconnected` parameter (default `False`):
```python
def on_next(self, value):
    if self._connected or self._buffer_while_disconnected:
        self.adapter.package_type_check(value)
        # ... enqueue logic
```

#### IMP-3: Add `close()` / `dispose()` method

**Problem:** No explicit resource cleanup method separate from `on_completed()`.

**Change:** Add `dispose()` method that:
1. Stops accepting new messages
2. Closes WebSocket connections
3. Stops event loop
4. Joins thread

#### IMP-4: Consistent error message in `WSBytes.package_type_check()`

**Problem:** Error message says "WSRawBytes" but class is `WSBytes`.

**Location:** [rxplus/ws.py#L87-L88](rxplus/ws.py#L87-L88)

**Fix:** Change error message to match class name.

#### IMP-5: Add connection state observable

**Problem:** Users cannot easily observe connection state changes.

**Change:** Add `connection_state` observable that emits `"connected"` / `"disconnected"` events.

#### IMP-6: Remove unused imports

**Problem:** `os`, `time`, `rx`, `create`, `ServerConnection`, `drop_log` imported but unused or only partially used.

**Location:** [rxplus/ws.py#L10-L27](rxplus/ws.py#L10-L27)

### 3. Files Changed

| File | Lines | Changes |
|------|-------|---------|
| `rxplus/ws.py` | 1-27 | Remove unused imports |
| `rxplus/ws.py` | 138-149 | Add lock to `WS_Channels` |
| `rxplus/ws.py` | 175-218 | Add `_path_channels_lock`, validate `conn_cfg` |
| `rxplus/ws.py` | 226-234 | Synchronized `_get_path_channels()` |
| `rxplus/ws.py` | 277 | Copy `queues` set before iteration |
| `rxplus/ws.py` | 299-301, 338-341 | Synchronized add/remove from sets |
| `rxplus/ws.py` | 405-407 | Wrap `unpackage()` in try-except |
| `rxplus/ws.py` | 540-575 | Add `buffer_while_disconnected` param, lazy queue creation |
| `rxplus/ws.py` | 581-604 | Update `on_next()` for buffering option |
| `rxplus/ws.py` | 609-628 | Stop event loop in `async_completing()` |
| `rxplus/ws.py` | 795 | Wrap `unpackage()` in try-except |
| `rxplus/ws.py` | 880-920 | Add lock to `RxWSClientGroup` |
| `rxplus/ws.py` | 946-958 | Synchronized `_ensure_client()` |
| `tests/test_ws.py` | 1-32 | Add tests for bug fixes and improvements |

## Implementation Plan

### Phase 1: Critical Bug Fixes (Thread Safety)

1. **Task 1.1:** Add `threading.Lock` to `RxWSServer` for `path_channels` access
2. **Task 1.2:** Add `threading.Lock` to `WS_Channels` for `channels`/`queues` access
3. **Task 1.3:** Add `threading.Lock` to `RxWSClientGroup` for `_clients` access
4. **Task 1.4:** Fix `asyncio.Queue` creation timing in `RxWSClient`

### Phase 2: Error Handling & Cleanup

1. **Task 2.1:** Wrap `adapter.unpackage()` calls in try-except blocks
2. **Task 2.2:** Fix `RxWSClient.async_completing()` to stop event loop
3. **Task 2.3:** Add `dispose()` method to all three components
4. **Task 2.4:** Fix inconsistent error message in `WSBytes`

### Phase 3: Improvements

1. **Task 3.1:** Add connection configuration validation
2. **Task 3.2:** Add `buffer_while_disconnected` parameter to `RxWSClient`
3. **Task 3.3:** Clean up unused imports
4. **Task 3.4:** Update documentation and docstrings

## Testing Strategy

### Unit Tests

| Test Case | Component | Description |
|-----------|-----------|-------------|
| `test_server_path_channels_thread_safety` | `RxWSServer` | Concurrent access to `_get_path_channels()` |
| `test_client_group_ensure_client_thread_safety` | `RxWSClientGroup` | Concurrent `_ensure_client()` calls |
| `test_client_buffering_disabled` | `RxWSClient` | Messages dropped when disconnected (default) |
| `test_client_buffering_enabled` | `RxWSClient` | Messages queued when disconnected |
| `test_conn_cfg_validation_missing_host` | All | `ValueError` raised for missing `host` |
| `test_conn_cfg_validation_missing_port` | All | `ValueError` raised for missing `port` |
| `test_unpackage_error_handling` | `RxWSServer` | Malformed data doesn't crash receiver |
| `test_client_on_completed_stops_loop` | `RxWSClient` | Event loop stops after `on_completed()` |
| `test_wsbytes_error_message` | `WSBytes` | Error message matches class name |
| `test_wsobject_type_check` | `WSObject` | `package_type_check` accepts any value |

### Integration Tests

| Test Case | Description |
|-----------|-------------|
| `test_server_client_roundtrip_string` | End-to-end with string datatype |
| `test_server_client_roundtrip_bytes` | End-to-end with bytes datatype |
| `test_server_client_roundtrip_object` | End-to-end with pickle datatype |
| `test_client_reconnect_after_server_restart` | Auto-reconnect behavior |
| `test_client_group_multiple_paths` | Multiple paths through group |
| `test_graceful_shutdown_server` | Server `on_completed()` cleans up |
| `test_graceful_shutdown_client` | Client `on_completed()` cleans up |

## Observability

### Logging

Existing logging is adequate. No changes needed.

### Metrics

Not applicable for this change.

## Future Considerations

### Potential Enhancements

- **SSL/TLS support:** Add `wss://` protocol support via `ssl` parameter
- **Connection pooling:** Reuse connections in `RxWSClientGroup`
- **Backpressure:** Implement queue size limits with configurable overflow behavior
- **Health checks:** Expose connection health metrics

### Known Limitations

- **Single event loop per component:** Each server/client runs its own loop; consolidation could reduce thread count
- **No message persistence:** Buffered messages lost if process crashes
- **Daemon threads:** Background threads may not complete pending sends on process exit

## Dependencies

- `websockets` — WebSocket protocol implementation
- `reactivex` — ReactiveX for Python
- `opentelemetry-api`, `opentelemetry-sdk` — Logging integration

## Security Considerations

- **Pickle deserialization (`WSObject`):** Remote code execution risk if receiving untrusted data. Document this clearly and consider adding warning.

## References

- Research document: [docs/research/rxws-components.md](docs/research/rxws-components.md)
- ADR: [ADR/2025-08-28-rxws-use-multi-thread.md](ADR/2025-08-28-rxws-use-multi-thread.md)
- Implementation: [rxplus/ws.py](rxplus/ws.py)
- Tests: [tests/test_ws.py](tests/test_ws.py)
