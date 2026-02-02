# WebSocket Connection State Observable + Retry Policy Design Document

## Current Context
- `RxWSClient` in [rxplus/ws.py](../rxplus/ws.py) manages WebSocket connections with auto-reconnect
- Connection state (`_connected`) is a private boolean with no external visibility
- Reconnection uses constant delay (`conn_retry_timeout`) with infinite retries
- Consumers cannot react to connection state changes or control retry behavior

## Requirements

### Functional Requirements
- Expose connection state as an observable stream (`ConnectionState` enum)
- Emit state on every reconnect attempt (not just state changes)
- Support configurable retry policy with exponential backoff
- Call `on_error` when max retries exhausted
- Maintain backward compatibility with existing API

### Non-Functional Requirements
- Minimal overhead when connection state observable is not subscribed
- Thread-safe state transitions
- No breaking changes to existing consumers

## Design Decisions

### 1. Connection State as BehaviorSubject
Will use `BehaviorSubject` for `_connection_state` because:
- New subscribers immediately receive current state
- Fits ReactiveX pattern already used in the codebase
- Natural integration with existing `Subject` base class

### 2. Emit on Every Attempt
Will emit state on every reconnect attempt because:
- User explicitly requested this behavior
- Enables retry count tracking in downstream observers
- More granular observability for health dashboards

### 3. RetryPolicy as Dataclass
Will use a `@dataclass` for `RetryPolicy` because:
- Clean, immutable configuration object
- Easy to construct with defaults
- Self-documenting via type hints

### 4. on_error for Max Retries Exhaustion
Will call `on_error` (not `on_completed`) when max retries exhausted because:
- User explicitly requested this behavior
- Semantically, exhausted retries is an error condition
- Allows consumers to handle failure distinctly from graceful shutdown

## Technical Design

### 1. Core Components

```python
from enum import Enum
from dataclasses import dataclass
import random

class ConnectionState(Enum):
    """Observable states for RxWSClient connection lifecycle."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"  # terminal state after on_completed


@dataclass
class RetryPolicy:
    """Configurable retry behavior for WebSocket reconnection."""
    max_retries: int | None = None  # None = infinite
    base_delay: float = 0.5
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    jitter: float = 0.1  # randomization factor (0.0-1.0)

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number (0-indexed)."""
        delay = min(self.base_delay * (self.backoff_factor ** attempt), self.max_delay)
        jitter_range = delay * self.jitter
        return delay + random.uniform(-jitter_range, jitter_range)
```

### 2. Updated RxWSClient Interface

```python
from reactivex.subject import BehaviorSubject

class RxWSClient(Subject):
    def __init__(
        self,
        conn_cfg: dict,
        datatype: Literal["string", "bytes", "object"] = "string",
        conn_retry_timeout: float = 0.5,  # DEPRECATED, use retry_policy
        ping_interval: Optional[float] = 30.0,
        ping_timeout: Optional[float] = 30.0,
        name: Optional[str] = None,
        buffer_while_disconnected: bool = False,
        tracer_provider: TracerProvider | None = None,
        logger_provider: LoggerProvider | None = None,
        retry_policy: RetryPolicy | None = None,  # NEW
    ):
        ...
        # Connection state observable
        self._connection_state_subject = BehaviorSubject(ConnectionState.DISCONNECTED)
    
    @property
    def connection_state(self) -> Observable:
        """Observable stream of connection state changes.
        
        Emits on every state transition including each reconnect attempt.
        New subscribers immediately receive the current state.
        """
        return self._connection_state_subject.pipe(ops.share())
    
    def _set_connection_state(self, state: ConnectionState) -> None:
        """Thread-safe state transition with logging."""
        self._log(f"Connection state: {state.value}", "DEBUG")
        self._connection_state_subject.on_next(state)
```

### 3. Updated connect_client Logic

```python
async def connect_client(self):
    url = f"ws://{self.host}:{self.port}{self.path}"
    remote_desc = f"[{url}]"
    attempt = 0

    try:
        while not self._shutdown_requested:
            self._set_connection_state(
                ConnectionState.RECONNECTING if attempt > 0 else ConnectionState.CONNECTING
            )

            # Check max retries
            if self._retry_policy.max_retries is not None and attempt >= self._retry_policy.max_retries:
                self._log(f"Max retries ({self._retry_policy.max_retries}) exhausted", "ERROR")
                self._set_connection_state(ConnectionState.DISCONNECTED)
                super().on_error(RxException(
                    ConnectionError(f"Max retries exhausted connecting to {url}"),
                    source=self._name,
                    note="RxWSClient.connect_client",
                ))
                return

            try:
                self.ws = await asyncio.wait_for(
                    websockets.connect(url, ...),
                    timeout=self._retry_policy.base_delay,  # use base_delay as connect timeout
                )
                attempt = 0  # reset on successful connection
                self._set_connection_state(ConnectionState.CONNECTED)
                # ... rest of connection handling
                
            except (asyncio.TimeoutError, OSError, websockets.InvalidHandshake):
                attempt += 1
                delay = self._retry_policy.get_delay(attempt - 1)
                self._log(f"Retry {attempt}, waiting {delay:.2f}s", "WARN")
                await asyncio.sleep(delay)

    finally:
        self._set_connection_state(ConnectionState.CLOSED)
```

### 4. Files Changed

- `rxplus/ws.py` (lines 1-950)
  - Add imports: `from enum import Enum`, `from dataclasses import dataclass`, `import random`
  - Add `ConnectionState` enum after line 30
  - Add `RetryPolicy` dataclass after `ConnectionState`
  - Modify `RxWSClient.__init__` (lines 602-663) to add `retry_policy` parameter
  - Add `_connection_state_subject`, `connection_state` property, `_set_connection_state` method
  - Modify `connect_client` (lines 755-867) for retry policy and state emissions
  - Modify `_shutdown_client` (lines 731-752) to emit `CLOSED` state
  - Update `on_completed` to set `CLOSED` state

- `tests/test_ws.py` (new test cases to add)
  - Test `ConnectionState` enum values
  - Test `RetryPolicy.get_delay` with backoff
  - Test connection state observable emissions
  - Test max_retries exhaustion triggers on_error

## Implementation Plan

1. Phase 1 — Core Types
   - Add `ConnectionState` enum to `ws.py`
   - Add `RetryPolicy` dataclass to `ws.py`
   - Add unit tests for `RetryPolicy.get_delay`

2. Phase 2 — Client Integration
   - Add `_connection_state_subject` to `RxWSClient.__init__`
   - Add `connection_state` property
   - Add `_set_connection_state` helper method
   - Wire state emissions in `connect_client`
   - Emit `CLOSED` in `_shutdown_client` and `on_completed`

3. Phase 3 — Retry Policy Integration
   - Add `retry_policy` parameter to `__init__`
   - Implement backoff logic in `connect_client`
   - Add max_retries check with `on_error` emission
   - Add deprecation warning for `conn_retry_timeout` when `retry_policy` is also provided

4. Phase 4 — Testing & Documentation
   - Add integration tests for state transitions
   - Update docstrings
   - Update `docs/websocket.md` if it exists

## Testing Strategy

### Unit Tests
- `test_retry_policy_default_values` — verify default dataclass values
- `test_retry_policy_get_delay_exponential` — verify backoff calculation
- `test_retry_policy_get_delay_max_cap` — verify delay doesn't exceed max_delay
- `test_retry_policy_get_delay_jitter` — verify jitter is applied within bounds
- `test_connection_state_enum_values` — verify all states have expected string values

### Integration Tests
- `test_client_emits_connecting_on_start` — verify initial state emission
- `test_client_emits_connected_on_success` — verify connected state after handshake
- `test_client_emits_reconnecting_on_retry` — verify reconnecting state on subsequent attempts
- `test_client_emits_closed_on_shutdown` — verify closed state on `on_completed`
- `test_client_on_error_when_max_retries_exhausted` — verify `on_error` called after max retries
- `test_connection_state_observable_replays_current` — verify BehaviorSubject replays to new subscribers

## Observability

### Logging
- Log state transitions at DEBUG level via existing `_log` method
- Log retry attempts with delay at WARN level
- Log max retries exhaustion at ERROR level

### Metrics
- Existing OTel integration will capture state transition logs
- Consumers can derive metrics from `connection_state` observable

## Future Considerations

### Potential Enhancements
- Add `pause_reconnect()` / `resume_reconnect()` methods for manual control
- Expose retry attempt count in state emissions (e.g., `ConnectionState` becomes a dataclass with `attempt` field)
- Add `BufferPolicy` from Proposal 2 for bounded message queuing

### Known Limitations
- State observable adds memory overhead (~1 BehaviorSubject per client)
- Backoff resets on successful connection, not on first message sent
- No circuit breaker pattern (always retries up to max)

## Dependencies
- `reactivex` — already in use, provides `BehaviorSubject`
- No new external dependencies required

## Security Considerations
Not applicable — no security-sensitive changes.

## Rollout Strategy
Not applicable — library change, consumers opt-in to new features.

## References
- [docs/proposals/2026-02-02-ws-disconnection-behavior-refinement.md](proposals/2026-02-02-ws-disconnection-behavior-refinement.md)
- `rxplus/ws.py:602-663` — `RxWSClient.__init__`
- `rxplus/ws.py:755-867` — `RxWSClient.connect_client`
- `rxplus/ws.py:731-752` — `RxWSClient._shutdown_client`
