# Solution Proposals: WebSocket Disconnection Behavior Refinement

## Context
- **Request**: Evaluate the current disconnection behavior in `RxWSClient` and related components for flexibility, and propose improvements.
- **Source**: [rxplus/ws.py](rxplus/ws.py) — particularly `RxWSClient`, `connect_client`, and message handling logic.

## Current Behavior Analysis

The current `RxWSClient` disconnection handling has the following characteristics:

### 1. Binary Buffering Strategy (`buffer_while_disconnected`)
- **True**: Messages queue indefinitely during disconnection
- **False**: Messages are silently dropped

**Issues:**
- No middle ground (e.g., bounded buffer, time-based expiration)
- No callback/notification when messages are dropped
- Unbounded queue can cause memory issues in prolonged disconnections

### 2. Infinite Auto-Reconnect
The client loops forever in `connect_client()` until `_shutdown_requested` is set.

**Issues:**
- No configurable max retry count
- No exponential backoff (constant `conn_retry_timeout`)
- No way to be notified of connection state changes
- No way to programmatically pause/resume reconnection

### 3. Silent State Changes
Connection state (`_connected`) is internal with no observable way to react.

**Issues:**
- Consumers cannot distinguish between "not yet connected" vs "disconnected"
- No reactive signal for connection state changes
- Difficult to build UI indicators or health checks

### 4. Lack of Graceful Degradation
When disconnected, there's no policy for:
- Prioritizing certain messages
- Applying backpressure to upstream
- Selective retry of failed sends

---

## Proposal 1 — Connection State Observable + Configurable Retry Policy

### Overview
Expose connection state as an observable stream and introduce a configurable retry policy with backoff support. This preserves backward compatibility while enabling sophisticated monitoring and control.

### Key Changes

**1. Connection State Enum and Observable**
```python
class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"  # terminal state after on_completed

class RxWSClient(Subject):
    def __init__(self, ...):
        ...
        self._connection_state = BehaviorSubject(ConnectionState.DISCONNECTED)
    
    @property
    def connection_state(self) -> Observable:
        """Observable of connection state changes."""
        return self._connection_state.pipe(ops.distinct_until_changed())
```

**2. Configurable Retry Policy**
```python
@dataclass
class RetryPolicy:
    max_retries: int | None = None  # None = infinite
    base_delay: float = 0.5
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    jitter: float = 0.1  # randomization factor
    
    def get_delay(self, attempt: int) -> float:
        delay = min(self.base_delay * (self.backoff_factor ** attempt), self.max_delay)
        jitter_range = delay * self.jitter
        return delay + random.uniform(-jitter_range, jitter_range)
```

**3. Updated Constructor**
```python
def __init__(
    self,
    conn_cfg: dict,
    ...,
    retry_policy: RetryPolicy | None = None,  # None uses default infinite retry
    on_disconnect: Callable[[], None] | None = None,  # callback hook
):
```

### Trade-offs
| Pros | Cons |
|------|------|
| Fully backward compatible | Adds complexity to internal state management |
| Observable pattern fits RxPy ecosystem | Additional overhead for state subject |
| Enables health dashboards and UI bindings | More parameters to document |
| Flexible backoff prevents server hammering | |

### Validation
- Unit test state transitions for each scenario
- Integration test with mock server that drops connections
- Measure memory overhead of state subject

### Open Questions
- Should `connection_state` emit on every reconnect attempt, or only on actual state changes?
- Should max_retries exhaustion call `on_error` or `on_completed`?

---

## Proposal 2 — Bounded Buffer with Drop Policy + Disconnect Callbacks

### Overview
Focus on message handling during disconnection with bounded buffers and configurable drop policies. Simpler than Proposal 1 but addresses the most practical pain point: message loss visibility.

### Key Changes

**1. Buffer Configuration**
```python
@dataclass
class BufferPolicy:
    max_size: int | None = None  # None = unbounded (current behavior when buffer_while_disconnected=True)
    drop_strategy: Literal["oldest", "newest", "none"] = "oldest"
    on_drop: Callable[[Any], None] | None = None  # callback when message dropped
    
class RxWSClient(Subject):
    def __init__(
        self,
        ...,
        buffer_policy: BufferPolicy | None = None,  # None = drop all when disconnected
    ):
```

**2. Smart Queue Implementation**
```python
class BoundedQueue:
    def __init__(self, policy: BufferPolicy):
        self._policy = policy
        self._queue = collections.deque(maxlen=policy.max_size)
    
    def put(self, item):
        if self._policy.max_size and len(self._queue) >= self._policy.max_size:
            if self._policy.drop_strategy == "oldest":
                dropped = self._queue.popleft()
            elif self._policy.drop_strategy == "newest":
                dropped = item
                if self._policy.on_drop:
                    self._policy.on_drop(dropped)
                return
            
            if self._policy.on_drop:
                self._policy.on_drop(dropped)
        
        self._queue.append(item)
```

**3. Disconnect Event Hooks**
```python
def __init__(
    self,
    ...,
    on_connected: Callable[[], None] | None = None,
    on_disconnected: Callable[[], None] | None = None,
):
```

### Trade-offs
| Pros | Cons |
|------|------|
| Prevents OOM from unbounded queues | Less "reactive" than Proposal 1 |
| Drop callbacks enable logging/metrics | Callbacks are imperative, not composable |
| Minimal API surface change | Doesn't address reconnect policy |
| Easy to understand and configure | |

### Validation
- Test bounded queue with various max_size values
- Verify drop callback is invoked correctly
- Load test with high message volume during disconnection

### Open Questions
- Should dropped messages be available as an observable stream instead of callbacks?
- Is "oldest" the right default drop strategy for most use cases?

---

## Recommendation

**Start with Proposal 2** for immediate practical benefit (memory safety, drop visibility), then layer in **Proposal 1's connection state observable** as a follow-up enhancement.

Combined implementation order:
1. `BufferPolicy` with bounded queue (addresses OOM risk)
2. Simple `on_connected`/`on_disconnected` callbacks (low-hanging fruit)
3. `RetryPolicy` with backoff (prevents server hammering)
4. `connection_state` observable (full reactive experience)

This incremental approach allows validation at each step and maintains backward compatibility throughout.

---

## Migration Path

Both proposals maintain backward compatibility:
- Default parameters preserve existing behavior
- New features are opt-in
- Deprecation warnings can be added to `buffer_while_disconnected` parameter pointing to `BufferPolicy`
