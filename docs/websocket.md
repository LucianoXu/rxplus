# WebSocket

Reactive WebSocket server and client with auto-reconnect and multi-path support.

## Design Intention

`rxplus` WebSocket components wrap the `websockets` library and expose a `Subject`-based API. Internally, each component runs its own asyncio event loop in a background thread, so you can use them from synchronous code without managing async context.

Values are turned into frames by a codec: `"string"`, `"bytes"`, a `WSCodec` of your own, or `JsonCodec` for custom objects (the safe way, see [Codecs](#codecs-and-custom-objects)). Pickle (`"object"`) is available only with `allow_pickle=True`, for trusted links.

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
| `datatype` | `"string"` | `"string"`, `"bytes"`, `"object"` (needs `allow_pickle`), a `WSCodec`, or `Callable[[path], one of these]` |
| `ping_interval` | `30.0` | Heartbeat interval (seconds) |
| `ping_timeout` | `30.0` | Heartbeat timeout (seconds) |
| `name` | `None` | Custom name for log source identification |
| `reacting` | `None` | Report to a `Reporter` or under an `Element` (`rxplus.reacting`): elements, ports, per-socket flow, backlog and latency; see `docs/reacting.md`, Components. `None` installs nothing |

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
| `datatype` | `"string"` | `"string"`, `"bytes"`, `"object"` (needs `allow_pickle`), or a `WSCodec` |
| `retry_policy` | `RetryPolicy()` | Backoff between connection attempts: `max_retries` (`None` = forever), `base_delay` `0.5`, `max_delay` `30.0`, `backoff_factor` `2.0`, `jitter` `0.1` |
| `ping_interval` | `30.0` | Heartbeat interval (seconds) |
| `ping_timeout` | `30.0` | Heartbeat timeout (seconds) |
| `name` | `None` | Custom name for log source identification |
| `reacting` | `None` | Report to a `Reporter` or under an `Element` (`rxplus.reacting`): elements, ports, per-socket flow, backlog and latency; see `docs/reacting.md`, Components. `None` installs nothing |
| `buffer_while_disconnected` | `False` | If `True`, queue messages while disconnected for delivery after reconnection. If `False`, messages are dropped when not connected. A frame already taken for a socket that closed before it was handed over goes back to the front of the queue; one whose send failed part-way is not retried (no duplicates). |
| `greeting` | `None` | Called on every new connection; the wire frame it returns (`str` or `bytes`, not encoded by `datatype`) is sent first, ahead of any queued frame (the gateway sends its HELLO this way). |

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
| `datatype` | `"string"` | as `RxWSServer`; resolved per path |
| `retry_policy` | `RetryPolicy()` | Passed to child clients |
| `ping_interval` | `30.0` | Heartbeat interval (seconds) |
| `ping_timeout` | `30.0` | Heartbeat timeout (seconds) |
| `name` | `None` | Custom name for log source; child clients use `"{name}:{path}"` |
| `reacting` | `None` | Report to a `Reporter` or under an `Element` (`rxplus.reacting`): elements, ports, per-socket flow, backlog and latency; see `docs/reacting.md`, Components. `None` installs nothing |
| `buffer_while_disconnected` | `False` | If `True`, queue messages while disconnected. Passed to child clients. |

All three components also take these keyword-only parameters (0.2):

| Name | Default | Description |
|------|---------|-------------|
| `allow_pickle` | `False` | Allow `datatype="object"` or a `WSObject()` codec, however supplied (fixed, per path, or from `wsdt_factory("object")`). Unpickling runs the peer's code: trusted links only. |
| `max_size` | `1 MiB` | Largest incoming message in bytes; a larger one closes the connection (1009). `None`: unlimited. |
| `send_queue` | `1024` | Frames queued per connection (per client for the server) before `overflow` applies. `None`: unbounded. It counts frames, not bytes: the worst case is `send_queue` times your largest frame per connection. |
| `send_queue_bytes` | `None` | Optional bound on the queued bytes per connection; `overflow` applies when either bound is reached. |
| `overflow` | `"drop_old"` | Full queue: `"drop_old"` (discard the oldest), `"drop_new"` (discard the new frame), `"disconnect"` (close that connection; a client reconnects with an empty queue). While a client is offline (`buffer_while_disconnected=True`) its full buffer always drops the oldest frame, whatever `overflow` says: there is no connection to drop. Drops are logged (the first, then every thousandth) and counted. |

### Data Types

| Type | Frame | Use Case |
|------|-------|----------|
| `"string"` | Text | Text, commands |
| `"bytes"` | Binary | Audio, video, raw data |
| `JsonCodec()` | Text | Custom objects (registered dataclasses or classes), JSON values |
| any `WSCodec` | as it declares | Your own format |
| `"object"` | Binary (pickle) | Arbitrary Python objects, **trusted links only** (`allow_pickle=True`) |

### Codecs and custom objects

A codec implements `WSCodec[T]`: a `frame` attribute (`"text"` or `"binary"`), `encode(value) -> str | bytes` and `decode(data) -> T` (raising `CodecError` on what it refuses). `JsonCodec` is the documented way to send your own objects:

```python
from dataclasses import dataclass
from rxplus import RxWSClient, RxWSServer, WSConnectionConfig
from rxplus.ws import JsonCodec

codec = JsonCodec(max_size=1 << 20, max_depth=32)

@codec.register  # name defaults to the class's __qualname__
@dataclass
class Reading:
    sensor: str
    value: float

server = RxWSServer(WSConnectionConfig("127.0.0.1", 8765), datatype=codec)
client = RxWSClient(WSConnectionConfig("127.0.0.1", 8765, "/r"), datatype=codec)
client.on_next(Reading("t1", 21.5))  # arrives as TaggedData("/r", Reading("t1", 21.5))
```

A class that is not a dataclass is registered with explicit conversions: `codec.register_class(Money, name="money", to_dict=..., from_dict=...)`. On the wire a registered object is `{"$type": "<name>", "fields": {...}}` and `bytes` is `{"$bytes": "<base64>"}`; other JSON values pass through.

Decoding checks the size before parsing and rejects duplicate keys, NaN and Infinity (also `1e999`), and nesting deeper than `max_depth`. It constructs only types in the codec's registry, with exactly their fields, each validated against the field's annotation. Supported annotations: `None`, `bool`, `int`, `float`, `str`, `bytes`, registered classes, `list[X]`, `tuple[X, ...]`, `dict[str, X]`, and unions of these. A bare `list`, `dict`, `tuple` or `object` is refused at `register` (it would admit unvalidated content). It never imports, evaluates or calls anything named by the peer.

What *does* run on the peer's input is your own code for the registered types: a dataclass's `__init__` and `__post_init__`, and the `from_dict` of `register_class`. Keep them free of side effects you would not run on untrusted data, and validate in `from_dict`. A codec you write yourself is trusted as written.

### Errors

A problem with one item never ends a component (0.2):

- a value that is not `TaggedData` (server), that the codec cannot encode, or a frame that does not decode: logged at `ERROR`, dropped;
- a subscriber that raises while handling a received value: logged, the connection goes on;
- a connection that fails unexpectedly or overflows under `overflow="disconnect"`: that connection is closed, the others go on.

Every dropped item is counted: `server.dropped`, `client.dropped` and `group.dropped` (values the group refused itself) give the totals. When components report to a Reacting observer (X4), the same counts are sent as `dropped` on their links.

Only lifecycle failures reach `on_error`: the server cannot bind its port, a client exhausts `max_retries` or has an invalid URI. An `on_error` received from upstream shuts the component down (like `on_completed`) and forwards the error to its subscribers. A subscriber of `server.connections` that raises is logged and does not affect the connection.

`RxWSClientGroup.on_next` logs and drops a value that is not `TaggedData`, or one for a path whose datatype is refused. A fixed `datatype="object"` without `allow_pickle` is refused at construction; a per-path function can only be checked when the path is first used.

`RxWSClientGroup` removes a child that ends with an error and emits `(path, error)` on `group.child_errors`; the group and its other children go on, and the next value for that path starts a new child. The group's own `on_completed` or `on_error` completes every child (children are shut down quietly, not errored), then ends the group's stream; values sent afterwards are ignored.

### Per-connection API

`server.received` emits `WSReceived(connection_id, path, value)` for every received value (the main stream
carries `TaggedData(path, value)`). `server.send_to(connection_id, value)` sends to that one connection and
returns `False` if it is unknown or closed. `server.close_connection(connection_id)` closes it. `GatewayNode`
uses these to keep one connection per socket.

### Connection events

`server.connections` emits `WSConnectionEvent(kind, connection_id, path)` with `kind` `"opened"` or `"closed"` for every client connection (on the server's loop thread). `connection_id` is unique for the server's lifetime. The stream completes when the server shuts down.

## Threading Model

Each `RxWSServer` / `RxWSClient` spawns a daemon thread running `asyncio.run_forever()`. All WebSocket I/O happens on that thread; Rx callbacks are invoked from there. Use schedulers if you need to marshal data to a specific thread.

## Reconnection

`RxWSClient` (and each child of `RxWSClientGroup`) retries according to its `RetryPolicy`:

- One attempt (TCP connect plus handshake) may take at most `open_timeout` seconds (default `10.0`; `None` waits indefinitely). It is independent of the retry delays.
- A failed attempt waits `RetryPolicy.get_delay(attempt)` (exponential backoff with jitter) before the next one.
- A connection that stayed open at least `RetryPolicy.stable_after` seconds (default `1.0`) resets the backoff and is re-established at once when it drops. A connection that closes sooner counts as a failed attempt, so a server that accepts and immediately closes is retried with backoff rather than in a tight loop.
- Every failed attempt counts toward `RetryPolicy.max_retries`, and so does every connection that closes within `stable_after`: with `max_retries=3`, a server that keeps accepting and closing at once ends the client with an error after three such drops. A stable connection that drops sets the count to one, not zero (the drop itself is an attempt), as in 0.1: with `max_retries=1` the client stops after any drop.
