# WebSocket

Reactive WebSocket server and client with auto-reconnect and multi-path support.

## Design Intention

`rxplus` WebSocket components wrap the `websockets` library and expose a `Subject`-based API. Internally, each component runs its own asyncio event loop in a background thread, so you can use them from synchronous code without managing async context.

Data framing is abstracted via `WSDatatype` adapters supporting strings, raw bytes, or pickled Python objects.

## API

```python
from rxplus import RxWSServer, RxWSClient, RxWSClientGroup, TaggedData, WSStr
```

### Connection Configuration

All components accept a `conn_cfg` dictionary:

```python
conn_cfg = {"host": "localhost", "port": 8765, "path": "/chat"}
```

### `RxWSServer`

Multi-client WebSocket server. Messages are wrapped in `TaggedData(path, payload)`.

```python
server = RxWSServer(conn_cfg, datatype="string")

# Receive from clients
server.subscribe(lambda td: print(f"[{td.tag}] {td.data}"))

# Send to clients on a specific path
server.on_next(TaggedData("/chat", "broadcast message"))

# Shutdown
server.on_completed()
```

**Parameters:**

| Name | Default | Description |
|------|---------|-------------|
| `datatype` | `"string"` | `"string"`, `"bytes"`, `"object"`, or `Callable[[path], dtype]` |
| `ping_interval` | `30.0` | Heartbeat interval (seconds) |
| `ping_timeout` | `30.0` | Heartbeat timeout (seconds) |
| `name` | `None` | Custom name for log source identification |

### `RxWSClient`

Auto-reconnecting WebSocket client.

```python
client = RxWSClient(conn_cfg, datatype="string", conn_retry_timeout=0.5)

# Send messages
client.on_next("hello")

# Receive messages
client.subscribe(print)

# Close
client.on_completed()
```

The client automatically retries connection until the server is available. Outbound messages are queued while disconnected.

**Parameters:**

| Name | Default | Description |
|------|---------|-------------|
| `datatype` | `"string"` | `"string"`, `"bytes"`, or `"object"` |
| `conn_retry_timeout` | `0.5` | Delay between reconnection attempts (seconds) |
| `ping_interval` | `30.0` | Heartbeat interval (seconds) |
| `ping_timeout` | `30.0` | Heartbeat timeout (seconds) |
| `name` | `None` | Custom name for log source identification |

### `RxWSClientGroup`

Manages multiple `RxWSClient` instances behind a single interface, creating clients lazily per path.

```python
group = RxWSClientGroup({"host": "localhost", "port": 8765})

# Opens /audio and /video paths automatically
group.on_next(TaggedData("/audio", audio_data))
group.on_next(TaggedData("/video", video_data))

# Receive from all paths
group.subscribe(lambda td: print(f"[{td.tag}] received"))
```

**Parameters:**

| Name | Default | Description |
|------|---------|-------------|
| `datatype` | `"string"` | `"string"`, `"bytes"`, `"object"`, or `Callable[[path], dtype]` |
| `conn_retry_timeout` | `0.5` | Delay between reconnection attempts (seconds) |
| `ping_interval` | `30.0` | Heartbeat interval (seconds) |
| `ping_timeout` | `30.0` | Heartbeat timeout (seconds) |
| `name` | `None` | Custom name for log source; child clients use `"{name}:{path}"` |

### Data Types

| Type | Frame | Use Case |
|------|-------|----------|
| `"string"` | Text | JSON, commands |
| `"bytes"` | Binary | Audio, video, raw data |
| `"object"` | Binary (pickle) | Arbitrary Python objects |

## Threading Model

Each `RxWSServer` / `RxWSClient` spawns a daemon thread running `asyncio.run_forever()`. All WebSocket I/O happens on that thread; Rx callbacks are invoked from there. Use schedulers if you need to marshal data to a specific thread.
