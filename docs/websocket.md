# WebSocket

Reactive WebSocket server and client with auto-reconnect and multi-path support.

## Design Intention

`rxplus` WebSocket components wrap the `websockets` library and expose a `Subject`-based API. Internally, each component runs its own asyncio event loop in a background thread, so you can use them from synchronous code without managing async context.

Data framing is abstracted via `WSDatatype` adapters supporting strings, raw bytes, or pickled Python objects.

## API

```python
from rxplus import (
    RxWSClient,
    RxWSClientGroup,
    RxWSServer,
    TaggedData,
    WSConnectionConfig,
)
from rxplus.ws import RetryPolicy
```

### Connection Configuration

All components take a frozen `WSConnectionConfig` as their first argument:

```python
config = WSConnectionConfig(host="localhost", port=8765, path="/chat")
```

`path` defaults to `"/"`. The server ignores `path` (it serves every path); `RxWSClientGroup` ignores it too and takes the path of each `TaggedData` instead.

### `RxWSServer`

Multi-client WebSocket server. Messages are wrapped in `TaggedData(path, payload)`.

```python
server = RxWSServer(config, datatype="string")

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
client = RxWSClient(config, datatype="string", retry_policy=RetryPolicy(base_delay=0.5))

# Send messages
client.on_next("hello")

# Receive messages
client.subscribe(print)

# Close
client.on_completed()
```

The client automatically retries connection until the server is available.

**Parameters:**

| Name | Default | Description |
|------|---------|-------------|
| `datatype` | `"string"` | `"string"`, `"bytes"`, or `"object"` |
| `retry_policy` | `RetryPolicy()` | Backoff between connection attempts: `max_retries` (`None` = forever), `base_delay` `0.5`, `max_delay` `30.0`, `backoff_factor` `2.0`, `jitter` `0.1` |
| `ping_interval` | `30.0` | Heartbeat interval (seconds) |
| `ping_timeout` | `30.0` | Heartbeat timeout (seconds) |
| `name` | `None` | Custom name for log source identification |
| `buffer_while_disconnected` | `False` | If `True`, queue messages while disconnected for delivery after reconnection. If `False`, messages are dropped when not connected. |

### `RxWSClientGroup`

Manages multiple `RxWSClient` instances behind a single interface, creating clients lazily per path.

```python
group = RxWSClientGroup(WSConnectionConfig(host="localhost", port=8765))

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
| `retry_policy` | `RetryPolicy()` | Passed to child clients |
| `ping_interval` | `30.0` | Heartbeat interval (seconds) |
| `ping_timeout` | `30.0` | Heartbeat timeout (seconds) |
| `name` | `None` | Custom name for log source; child clients use `"{name}:{path}"` |
| `buffer_while_disconnected` | `False` | If `True`, queue messages while disconnected. Passed to child clients. |

### Data Types

| Type | Frame | Use Case |
|------|-------|----------|
| `"string"` | Text | JSON, commands |
| `"bytes"` | Binary | Audio, video, raw data |
| `"object"` | Binary (pickle) | Arbitrary Python objects |

## Threading Model

Each `RxWSServer` / `RxWSClient` spawns a daemon thread running `asyncio.run_forever()`. All WebSocket I/O happens on that thread; Rx callbacks are invoked from there. Use schedulers if you need to marshal data to a specific thread.

## Reconnection

`RxWSClient` (and each child of `RxWSClientGroup`) retries according to its `RetryPolicy`:

- One attempt (TCP connect plus handshake) may take at most `open_timeout` seconds (default `10.0`; `None` waits indefinitely). It is independent of the retry delays.
- A failed attempt waits `RetryPolicy.get_delay(attempt)` (exponential backoff with jitter) before the next one.
- A connection that stayed open at least `RetryPolicy.stable_after` seconds (default `1.0`) resets the backoff and is re-established at once when it drops. A connection that closes sooner counts as a failed attempt, so a server that accepts and immediately closes is retried with backoff rather than in a tight loop.
- Every failed attempt counts toward `RetryPolicy.max_retries`, and so does every connection that closes within `stable_after`: with `max_retries=3`, a server that keeps accepting and closing at once ends the client with an error after three such drops. A stable connection that drops sets the count to one, not zero (the drop itself is an attempt), as in 0.1: with `max_retries=1` the client stops after any drop.
