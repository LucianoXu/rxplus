# Migrating to rxplus 0.2

rxplus 0.2 is a breaking release (decision D3 of the Reacting project; design in
`docs/proposals/2026-11-06-reacting-support.md`, section 3). This page lists every change you may have to act
on, grouped by component. It grows with each pull request of the 0.2 batch; the PR that introduced each change
is in the last column.

## WebSocket (`rxplus.ws`)

| 0.1 | 0.2 | What to do | PR |
|---|---|---|---|
| `datatype="object"` (pickle) by default available | refused unless `allow_pickle=True` (`PickleNotAllowedError` at construction; a per-path function returning `"object"` refuses that path's connections) | send custom objects with `datatype=JsonCodec()` and registered types; keep pickle only on trusted links: `allow_pickle=True` | A |
| custom objects only via pickle | `WSCodec[T]` protocol and `JsonCodec` (registered dataclasses / classes, validated, size and depth limits) | `codec = JsonCodec(); codec.register(MyType)`; pass `datatype=codec` | A |
| incoming messages of any size (`max_size=None`) | `max_size=1 MiB`; larger messages close the connection (1009) | pass `max_size=None` (or a larger limit) where you need big frames | A |
| unbounded per-client send queue (server), unbounded reconnect buffer (client) | `send_queue=1024` frames per connection, `overflow="drop_old"` | tune `send_queue`/`overflow` (`"drop_new"`, `"disconnect"`); `send_queue=None` restores unbounded | A |
| `RxWSServer.on_next` with a non-`TaggedData` value → `on_error`, the server ended | logged at ERROR and dropped | nothing, unless you relied on the error | A |
| `package_type_check` raised `TypeError` in the caller of `on_next` (e.g. a non-`str` to `"string"`) | logged at ERROR and dropped; values are encoded in the caller's `on_next` | if you caught that `TypeError`, validate before sending instead | A |
| a frame that did not decode was logged and dropped | unchanged (decoding errors are `CodecError`) | — | A |
| a subscriber raising in `on_next` ended the server (`on_error`) | logged; the connection goes on | — | A |
| an unexpected error in one client's handler ended the whole server | closes that client's connection only | — | A |
| upstream `on_error` into `RxWSServer`/`RxWSClient` forwarded the error (client: wrapped in `RxException`) and left the thread running | shuts the component down and forwards the error **unwrapped** | if you matched `RxException` from a client's `on_error`, match the original error | A |
| one child of `RxWSClientGroup` failing (or completing) ended the group's stream | the child is removed, `(path, error)` is emitted on `group.child_errors`; the group ends only on its own `on_completed`/`on_error` | subscribe to `child_errors` if you need to know | A |
| — | `RxWSServer.connections`: `WSConnectionEvent("opened" \| "closed", connection_id, path)` | optional | A |
| a `WSObject()` instance (or `wsdt_factory("object")`) as `datatype` | refused unless `allow_pickle=True`, like `"object"` | pass `allow_pickle=True` (trusted links only) | A |
| `WSChannels("object")` | raises `PickleNotAllowedError`; `WSChannels(..., allow_pickle=True)` | pass `allow_pickle=True` | A |
| `WSChannels.queues` held `asyncio.Queue`s | holds the server's per-client records (each with a bounded send queue) | do not use it to send; use `on_next` / `send_to` | A |
| `RxWSClient.queue` was an `asyncio.Queue` of values | a `SendQueue` of encoded frames (loop thread only) | do not touch it; use `on_next` | A |
| `RxWSClient.adapter` was a `WSDatatype` | a `WSCodec` (`encode`/`decode`) | use `encode`/`decode` | A |
| a subscriber of `RxWSClient` raising ended the client | logged, counted in `dropped`; the connection goes on | — | A |
| `RxWSClientGroup.on_error`/`on_completed` errored or completed each child | completes each child (shut down quietly), then ends the group | — | A |
| `RxWSClientGroup.on_next` after the group ended reached its children | ignored | — | A |
| `RxWSClientGroup.on_next` raised on a non-`TaggedData` value | logged and dropped (`group.dropped`) | — | A |
| — | `dropped` counters on server, client and group; `send_queue_bytes` | optional | A |
| — | `server.connections` completes when the server shuts down | — | A |
| `RxWSServer(...).datatype_func` attribute | removed (codecs are resolved per path internally) | use the `datatype` you passed | A |
| `WSChannels.adapter` was a `WSDatatype` | a `WSCodec` (`WSDatatype` subclasses implement it: `encode`/`decode`, `frame`) | use `encode`/`decode` | A |
| `RxWSClient` handshake bounded by `RetryPolicy.base_delay` | `open_timeout` (default 10 s) | pass `open_timeout=` if you relied on a short handshake timeout | #10 |
| connections that closed at once were retried without delay | they count as failed attempts (backoff), also toward `max_retries` | raise `max_retries` if a flapping server now ends your client | #10 |
| a value that could not be packaged ended the server/client via `on_error` | logged at ERROR and dropped | — | #11 |
| a terminal error (retries exhausted, bind failure) left the loop thread running | the thread ends | — | #12 |

## Gateway (`rxplus.gateway`)

| 0.1 | 0.2 | What to do | PR |
|---|---|---|---|
| inbound connections keyed by request path, never closed; two peers on one path merged | one connection per server socket; closed (state `CLOSED`, `on_disconnection`) when the socket closes | nothing, unless you relied on the merging | B |
| the inbound side sent HELLO when a path was first seen | it sends HELLO in reply to the peer's HELLO, then HELLO_ACK | custom peers must send HELLO first (as `GatewayNode` always did) | B |
| `GatewayNode()` without `logger_provider` logged to the console | logs nothing | pass a `logger_provider` to see logs | B |
| `ConnectionState` (`rxplus.ConnectionState`, `rxplus.gateway.ConnectionState`) | `GatewayConnectionState` | rename | B |
| `DropOld`, `DropNew`, `BufferK`, `Disconnect`, `OverflowPolicy` | `BoundedBuffer(limit, strategy)` | `DropOld(n)` → `BoundedBuffer(n, "drop_old")`, etc. | B |
| `buffer.pop()` returned `None` when empty | raises `IndexError` | check `len(buffer)` or catch `IndexError` | B |
| `OverflowStrategy` defined in `gateway.stream` | defined in `gateway.overflow` (still exported by `rxplus.gateway`) | import from `rxplus.gateway` | B |
| — | `RxWSServer.received` (values with their `connection_id`), `send_to(connection_id, value)`, `close_connection(connection_id)` | optional | B |

## Notes

- "Logged" means an OTel log record on the component's `logger_provider` (nothing without one).
- Everything new is keyword-only, so positional calls of 0.1 constructors keep their meaning.
