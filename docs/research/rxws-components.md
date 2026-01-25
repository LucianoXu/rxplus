# Research: RxWS Series Components

## Summary

The RxWS series in `rxplus` provides ReactiveX-compatible WebSocket communication components: `RxWSServer`, `RxWSClient`, and `RxWSClientGroup`. These components wrap the `websockets` library and expose a `Subject`-based API, running asyncio event loops in dedicated daemon threads to allow synchronous code to use WebSocket communication without managing async context. The design supports multiple data serialization strategies (string, bytes, pickled objects) through a `WSDatatype` adapter pattern, path-based routing via `TaggedData`, automatic reconnection for clients, and integrated OpenTelemetry-compatible logging.

## Detailed Findings

### 1. Module Overview and Dependencies

**File:** [rxplus/ws.py](rxplus/ws.py)

**Core dependencies:**
- `asyncio` and `threading` for concurrent execution
- `websockets` library for WebSocket protocol (imports `ClientConnection`, `Server`, `ServerConnection`)
- `reactivex` for reactive stream primitives (`Observable`, `Observer`, `Subject`)
- `pickle` for object serialization

**Internal dependencies:**
- `.logging` — `LOG_LEVEL`, `create_log_record`, `drop_log`
- `.mechanism` — `RxException`
- `.utils` — `TaggedData`, `get_full_error_info`, `get_short_error_info`

**Public exports** (from [rxplus/__init__.py#L40](rxplus/__init__.py#L40)):
```python
from .ws import RxWSClient, RxWSClientGroup, RxWSServer, WSDatatype, WSStr
```

---

### 2. Data Type Adapters (WSDatatype Hierarchy)

**Code Reference:** [rxplus/ws.py#L53-L127](rxplus/ws.py#L53-L127)

The `WSDatatype` abstract base class defines the interface for payload serialization:

| Method | Purpose |
|--------|---------|
| `package_type_check(value)` | Validates value can be sent; raises `TypeError` if not |
| `package(value)` | Serializes value for transmission |
| `unpackage(value)` | Deserializes received value |

**Concrete implementations:**

| Class | Frame Type | Use Case | Validation |
|-------|------------|----------|------------|
| `WSStr` | Text | JSON, commands | Must be `str` |
| `WSBytes` | Binary | Audio, video, raw bytes | Must be `bytes` or `bytearray` |
| `WSObject` | Binary (pickle) | Arbitrary Python objects | Any pickleable object |

**Factory function** at [rxplus/ws.py#L117-L127](rxplus/ws.py#L117-L127):
```python
def wsdt_factory(datatype: Literal["string", "bytes", "object"]) -> WSDatatype:
```

---

### 3. Connection Configuration Pattern

All RxWS components accept a `conn_cfg` dictionary:

```python
conn_cfg = {
    "host": "localhost",  # Server bind address or client target
    "port": 8888,         # Port number (coerced to int)
    "path": "/",          # URI path (client/group only, optional)
}
```

---

### 4. WS_Channels Helper Class

**Code Reference:** [rxplus/ws.py#L138-L149](rxplus/ws.py#L138-L149)

Internal class that manages WebSocket connections for a specific path:

```python
class WS_Channels:
    def __init__(self, datatype: Literal["string", "bytes", "object"] = "string"):
        self.adapter: WSDatatype = wsdt_factory(datatype)
        self.channels: set[Any] = set()  # Active WebSocket connections
        self.queues: set[Any] = set()    # Per-connection asyncio.Queue instances
```

This enables broadcast to all clients connected to the same path.

---

### 5. RxWSServer

**Code Reference:** [rxplus/ws.py#L152-L481](rxplus/ws.py#L152-L481)

**Class hierarchy:** `Subject` → acts as both Observable (emits received data) and Observer (accepts data to send)

#### 5.1 Initialization Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `conn_cfg` | `dict` | required | `{host, port}` |
| `datatype` | `str` or `Callable[[str], str]` | `"string"` | Static type or path-based factory |
| `ping_interval` | `Optional[float]` | `30.0` | WebSocket heartbeat interval |
| `ping_timeout` | `Optional[float]` | `30.0` | WebSocket heartbeat timeout |
| `name` | `Optional[str]` | `None` | Custom source name for logging |

#### 5.2 Internal State

```python
self.path_channels: dict[str, WS_Channels]  # Per-path channel management
self._loop: Optional[asyncio.AbstractEventLoop]  # Background event loop
self._thread: Optional[threading.Thread]  # Daemon thread running the loop
self._loop_ready: threading.Event()  # Signals loop is ready
self._stop_flag: threading.Event()  # Signals shutdown
self.serve: Optional[Server]  # websockets Server instance
```

#### 5.3 Threading Model

- On `__init__`, spawns a daemon thread via `_start_server_thread()` at [rxplus/ws.py#L451-L453](rxplus/ws.py#L451-L453)
- Thread runs `_run_server_loop()` which creates a new asyncio event loop and calls `loop.run_forever()`
- Server task started via `loop.create_task(self.start_server())`

#### 5.4 Client Handling Flow

Each client connection spawns two concurrent tasks in `handle_client()` at [rxplus/ws.py#L292-L345](rxplus/ws.py#L292-L345):

```
┌─────────────────────────────────────────────────────────────┐
│                    handle_client(websocket)                 │
├─────────────────────────────────────────────────────────────┤
│  1. Extract path via _ws_path(websocket)                    │
│  2. Get/create WS_Channels for path                         │
│  3. Create per-connection asyncio.Queue                     │
│  4. Register websocket and queue in channels                │
│  5. Start parallel tasks:                                   │
│     ┌───────────────┐    ┌───────────────┐                 │
│     │ _server_sender │    │_server_receiver│                │
│     │ (queue→ws)     │    │ (ws→on_next)   │                │
│     └───────────────┘    └───────────────┘                 │
│  6. Wait for FIRST_COMPLETED, cancel pending               │
│  7. Cleanup: close ws, remove from channels                │
└─────────────────────────────────────────────────────────────┘
```

#### 5.5 Observer Interface (on_next)

**Code Reference:** [rxplus/ws.py#L233-L277](rxplus/ws.py#L233-L277)

- Expects `TaggedData` where `tag` = path, `data` = payload
- Non-TaggedData triggers `on_error` with `RxException`
- Type-checks data via adapter
- Pushes to all queues of matching path via `loop.call_soon_threadsafe(queue.put_nowait, data)`

#### 5.6 Observable Interface

- Received data from `_server_receiver()` is wrapped in `TaggedData(path, payload)` and emitted via `super().on_next()` at [rxplus/ws.py#L408-L409](rxplus/ws.py#L408-L409)
- Log records are also emitted via the same observable stream

#### 5.7 Shutdown

`on_completed()` calls `_shutdown_server()` at [rxplus/ws.py#L460-L481](rxplus/ws.py#L460-L481):
1. Logs "Closing..."
2. Schedules `serve.close(True)` on the event loop
3. Stops the loop
4. Joins the thread with 2.0s timeout
5. Calls `super().on_completed()`

---

### 6. RxWSClient

**Code Reference:** [rxplus/ws.py#L484-L820](rxplus/ws.py#L484-L820)

**Class hierarchy:** `Subject`

#### 6.1 Key Features

- **Auto-reconnect:** Retries connection every `conn_retry_timeout` seconds until successful
- **Back-pressure friendly:** Outbound messages queued in `asyncio.Queue` while disconnected
- **Connection state tracking:** `self._connected` flag controls whether `on_next` queues messages

#### 6.2 Initialization Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `conn_cfg` | `dict` | required | `{host, port, path}` |
| `datatype` | `Literal["string", "bytes", "object"]` | `"string"` | Payload serialization |
| `conn_retry_timeout` | `float` | `0.5` | Reconnection delay (seconds) |
| `ping_interval` | `Optional[float]` | `30.0` | Heartbeat interval |
| `ping_timeout` | `Optional[float]` | `30.0` | Heartbeat timeout |
| `name` | `Optional[str]` | `None` | Custom source for logging |

#### 6.3 Internal State

```python
self.queue: asyncio.Queue[Any]  # Outbound message queue
self.ws: Optional[ClientConnection]  # Active connection
self._connected: bool  # Connection state flag
self._loop, self._thread, self._loop_ready  # Same threading model as server
```

#### 6.4 Connection Loop

`connect_client()` at [rxplus/ws.py#L635-L743](rxplus/ws.py#L635-L743) implements:

```
┌─────────────────────────────────────────────────────────────┐
│                    connect_client()                         │
├─────────────────────────────────────────────────────────────┤
│  while True:  (outer reconnect loop)                        │
│    self._connected = False                                  │
│    while True:  (inner connection attempt loop)             │
│      try:                                                   │
│        ws = await websockets.connect(url, timeout=...)      │
│        break                                                │
│      except TimeoutError: continue                          │
│      except OSError: sleep, continue                        │
│      except InvalidHandshake: sleep, continue               │
│      except InvalidURI: on_error, return                    │
│    self._connected = True                                   │
│    sender_task = _client_sender(ws)                         │
│    receiver_task = _client_receiver(ws)                     │
│    await wait(FIRST_COMPLETED)                              │
│    cancel pending, cleanup                                  │
│    (loop back to reconnect)                                 │
└─────────────────────────────────────────────────────────────┘
```

#### 6.5 Observer Interface (on_next)

**Code Reference:** [rxplus/ws.py#L581-L604](rxplus/ws.py#L581-L604)

- Only queues messages when `self._connected` is `True`
- Messages sent before connection are **dropped** (not buffered)
- Type-checks via `adapter.package_type_check()`
- Thread-safe enqueue via `loop.call_soon_threadsafe(self.queue.put_nowait, value)`

#### 6.6 Observable Interface

- `_client_receiver()` emits received data via `super().on_next(adapter.unpackage(data))` at [rxplus/ws.py#L795](rxplus/ws.py#L795)
- Log records also flow through the observable

---

### 7. RxWSClientGroup

**Code Reference:** [rxplus/ws.py#L822-L959](rxplus/ws.py#L822-L959)

**Class hierarchy:** `Subject`

A multiplexing wrapper that manages multiple `RxWSClient` instances, one per path.

#### 7.1 Initialization Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `conn_cfg` | `dict` | required | `{host, port}` (no path) |
| `datatype` | `str` or `Callable[[str], str]` | `"string"` | Static or path-dependent |
| `conn_retry_timeout` | `float` | `0.5` | Per-client reconnection delay |
| `ping_interval` | `Optional[float]` | `30.0` | Per-client heartbeat |
| `ping_timeout` | `Optional[float]` | `30.0` | Per-client timeout |
| `name` | `Optional[str]` | `None` | Parent name; children get `"{name}:{path}"` |

#### 7.2 Internal Structure

```python
self._client_factory: Callable[[str], RxWSClient]  # Creates clients per path
self._clients: dict[str, RxWSClient]  # tag -> RxWSClient cache
self._bus: Subject  # Merged inbound stream from all clients
```

#### 7.3 Client Creation Flow

`_ensure_client(tag)` at [rxplus/ws.py#L946-L958](rxplus/ws.py#L946-L958):
1. Check if `tag` exists in `_clients`
2. If not, call `_client_factory(tag)` to create new `RxWSClient`
3. Subscribe client to `_bus` with `TaggedData` wrapping:
   ```python
   client.pipe(
       ops.map(keep_log(lambda data: TaggedData(tag, data)))
   ).subscribe(self._bus)
   ```
4. Store in `_clients[tag]`

#### 7.4 Observer Interface (on_next)

```python
def on_next(self, tagged: TaggedData):
    client = self._ensure_client(tagged.tag)
    client.on_next(tagged.data)
```

#### 7.5 Observable Interface

- Delegates to `self._bus.subscribe(observer)` in `_subscribe_core()`
- All client messages merged and wrapped with source path

#### 7.6 Utility Method

```python
def open_path(self, path: str) -> None:
    """Pre-create a client for the given path."""
    self._ensure_client(path)
```

---

### 8. Utility Functions

#### 8.1 `keep_log(func)`

**Code Reference:** [rxplus/ws.py#L30-L37](rxplus/ws.py#L30-L37)

Decorator that passes `LogRecord` instances through unchanged while applying `func` to other items. Used in `RxWSClientGroup` to preserve log records during `TaggedData` wrapping.

#### 8.2 `_ws_path(ws)`

**Code Reference:** [rxplus/ws.py#L40-L51](rxplus/ws.py#L40-L51)

Extracts the request path from a WebSocket connection object by probing:
1. `ws.request.path`
2. `ws.path`
3. Falls back to `""`

---

### 9. Logging Integration

All RxWS components emit log records as part of their observable stream:

**`_log()` method pattern:**
```python
def _log(self, body: str, level: LOG_LEVEL = "INFO") -> None:
    record = create_log_record(body, level, source=self._source)
    super().on_next(record)
```

**Source naming conventions:**
- `RxWSServer`: `"RxWSServer:{host}:{port}"` or custom `name`
- `RxWSClient`: `"RxWSClient:ws://{host}:{port}{path}"` or custom `name`
- `RxWSClientGroup` children: `"{parent_name}:{path}"` if parent has name

Log records include OpenTelemetry trace context when available (via `create_log_record`).

---

### 10. Error Handling

All components wrap errors in `RxException` from [rxplus/mechanism.py#L10-L20](rxplus/mechanism.py#L10-L20):

```python
class RxException(Exception):
    def __init__(self, exception: Exception, source: str = "Unknown", note: str = ""):
        self.exception = exception
        self.source = source
        self.note = note
```

Error scenarios:
- Server receives non-`TaggedData` in `on_next` → `RxException(ValueError)`
- Event loop not available → `RxException(RuntimeError)`
- WebSocket connection errors → logged as warnings, triggers reconnect
- Invalid URI (client) → `on_error` called, connection loop exits

---

### 11. Architectural Decision

**File:** [ADR/2025-08-28-rxws-use-multi-thread.md](ADR/2025-08-28-rxws-use-multi-thread.md)

> I decided to use multi-thread for websocket components. Two benefits:
> - Reduce potential blocking
> - Don't need to use asynchronized programming for rxws.

This explains why each component runs its own `asyncio.run_forever()` in a daemon thread rather than requiring callers to provide an event loop.

---

### 12. Usage Examples from Task Files

#### 12.1 Basic Server-Client Communication

**Server** ([tasks/task_wsserver.py](tasks/task_wsserver.py)):
```python
sender = RxWSServer(
    {'host': '::', 'port': 8888},
    datatype='string'
)
sender.subscribe(print)
sender.on_next(TaggedData("/", f"Hello {i}"))
```

**Client** ([tasks/task_wsclient.py](tasks/task_wsclient.py)):
```python
receiver = RxWSClient(
    {'host': 'localhost', 'port': 8888, 'path': '/'},
    datatype='string'
)
receiver.subscribe(print, on_error=print)
receiver.on_next(f"Ping {i}")
```

#### 12.2 Binary Audio Streaming

**Server** ([tasks/task_mic_server.py](tasks/task_mic_server.py)):
```python
sender = RxWSServer({'host': '::', 'port': 8888}, datatype='bytes')
mic = RxMicrophone(format='Float32', sample_rate=48000, channels=1)
mic.pipe(tag("/")).subscribe(sender)
```

#### 12.3 Object (Pickle) Streaming

**Server** ([tasks/task_np_matrix_server.py](tasks/task_np_matrix_server.py)):
```python
sender = RxWSServer({'host': '::', 'port': 8888}, datatype='object')
sender.on_next(TaggedData("/matrix", numpy_array))
```

**Client** ([tasks/task_np_matrix_client.py](tasks/task_np_matrix_client.py)):
```python
client = RxWSClient(
    {'host': 'localhost', 'port': 8888, 'path': '/matrix'},
    datatype='object'
)
```

#### 12.4 Client Group Multiplexing

**Code** ([tasks/task_wsclient_group.py](tasks/task_wsclient_group.py)):
```python
receiver = RxWSClientGroup({'host': 'localhost', 'port': 8888}, datatype='string')
receiver.subscribe(print, on_error=print)
receiver.on_next(TaggedData(path, f"Ping {i}"))  # Creates client for 'path' lazily
```

---

### 13. Cross-Component Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          RxWSServer                                     │
│  ┌─────────────┐    ┌─────────────────────────────────────────────┐   │
│  │ on_next()   │───▶│ path_channels[tag].queues                   │   │
│  │ (TaggedData)│    │   │                                         │   │
│  └─────────────┘    │   ▼ (per client queue)                      │   │
│                     │ _server_sender() → websocket.send()         │   │
│                     └─────────────────────────────────────────────┘   │
│                                                                        │
│                     ┌─────────────────────────────────────────────┐   │
│                     │ websocket.recv() → _server_receiver()       │   │
│                     │   │                                         │   │
│                     │   ▼                                         │   │
│  ┌─────────────┐◀───│ TaggedData(path, payload)                   │   │
│  │ Observable  │    │   → super().on_next()                       │   │
│  └─────────────┘    └─────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                          RxWSClient                                     │
│  ┌─────────────┐    ┌─────────────────────────────────────────────┐   │
│  │ on_next()   │───▶│ self.queue (if _connected)                  │   │
│  │ (payload)   │    │   │                                         │   │
│  └─────────────┘    │   ▼                                         │   │
│                     │ _client_sender() → websocket.send()         │   │
│                     └─────────────────────────────────────────────┘   │
│                                                                        │
│                     ┌─────────────────────────────────────────────┐   │
│                     │ websocket.recv() → _client_receiver()       │   │
│                     │   │                                         │   │
│                     │   ▼                                         │   │
│  ┌─────────────┐◀───│ adapter.unpackage(data)                     │   │
│  │ Observable  │    │   → super().on_next()                       │   │
│  └─────────────┘    └─────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                        RxWSClientGroup                                  │
│  ┌─────────────┐    ┌─────────────────────────────────────────────┐   │
│  │ on_next()   │───▶│ _ensure_client(tag)                         │   │
│  │ (TaggedData)│    │   │                                         │   │
│  └─────────────┘    │   ▼                                         │   │
│                     │ client = _clients[tag]                      │   │
│                     │ client.on_next(tagged.data)                 │   │
│                     └─────────────────────────────────────────────┘   │
│                                                                        │
│                     ┌─────────────────────────────────────────────┐   │
│                     │ client.pipe(map(→TaggedData))               │   │
│                     │   │                                         │   │
│                     │   ▼                                         │   │
│  ┌─────────────┐◀───│ _bus.on_next(TaggedData(tag, data))         │   │
│  │ Observable  │    │                                             │   │
│  └─────────────┘    └─────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### 14. Test Coverage

**File:** [tests/test_ws.py](tests/test_ws.py)

Current tests cover only the datatype adapters:
- `test_wsdt_factory_valid()` — factory returns correct types
- `test_wsdt_factory_invalid()` — raises `ValueError` for unknown type
- `test_wsstr_and_wsbytes()` — basic package/unpackage operations

**Note from test file:**
```python
# TODO: more sophisticated tests for WSServer and WSClient are needed here.
```

Integration tests for `RxWSServer`, `RxWSClient`, and `RxWSClientGroup` are not present.
