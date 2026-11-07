# rxplus 0.2.0: `reacting/1` support and the breaking redesign batch

**Date:** 2026-11-06
**Status:** Approved by the owner (D16, 2026-11-06); amended with D17 (codec layer)
**Breaking:** Yes (0.2.0, decision D3)

## Goal

1. `rxplus.reacting`: a source of the `reacting/1` protocol (Reacting repository,
   `docs/spec/Reacting.md`, including the rulings of commit 68d9a54), so that rxplus pipelines can be
   watched live in the Reacting viewer or recorded to a file. Its core uses the standard library only
   (D10).
2. Opt-in reporting by rxplus components through one construction parameter (D2), and explicit probe
   operators for user pipelines.
3. The breaking fixes found by the audit (`AgentDepartment/Reactive/Audit.md`, findings `F<n>`),
   grouped into three pull requests with one migration table.

Decisions cited: D2 (construction parameter), D3 (0.2.0 may break), D9 (the source dials the hub
over WebSocket; a JSON-lines file sink), D10 (stdlib-only core).

Non-goals: OpenTelemetry stays as it is (D1); no monkey-patching of reactivex; no global switch;
no cross-process trace propagation; no listening (server-side) source mode in 0.2.0.

## 1. `rxplus.reacting`

### 1.1 Layout

| Module | Contents | Imports |
|---|---|---|
| `rxplus/reacting/__init__.py` | re-exports of the core | stdlib |
| `rxplus/reacting/reporter.py` | `Reporter`, `Element`, `Link`, `Span`, sessions, `seq`, queue, Loss | stdlib |
| `rxplus/reacting/_window.py` | per-window accumulators, the window clock | stdlib |
| `rxplus/reacting/_encode.py` | signal dicts → JSON lines | stdlib (`json`) |
| `rxplus/reacting/sinks.py` | `Sink`, `JsonlFileSink`, `WebSocketSink`, `MemorySink` | stdlib (`socket`, `ssl`, `hashlib`, `base64`) |
| `rxplus/reacting/rx.py` | probe operators, `ContextScheduler` | reactivex |

`import rxplus.reacting` must not import reactivex, websockets or OpenTelemetry. This needs
`rxplus/__init__.py` to export lazily (PEP 562 `__getattr__`), which is also audit F19
(`import rxplus` costs 133 ms today); `from rxplus import X` keeps working. A test imports
`rxplus.reacting` in a subprocess and asserts that `reactivex` is not in `sys.modules`.

### 1.2 Public API (core)

```python
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from typing import Literal, Protocol

LinkKind = Literal["data", "call"]


class Clock(Protocol):
    def wall(self) -> float: ...        # Unix seconds (time.time)
    def monotonic(self) -> float: ...   # seconds (time.monotonic)


class Reporter:
    """One reacting/1 source. Thread-safe."""

    def __init__(
        self,
        source: str,                      # source name: non-empty, no "/"
        sink: "Sink",
        *,
        label: str | None = None,         # snapshot label
        window: float = 0.1,              # spec option `window`, seconds
        queue: int = 10_000,              # spec option `queue`
        clock: Clock | None = None,       # tests inject a manual clock
        timer: bool = True,               # False: windows close only in poll()
    ) -> None: ...

    @property
    def source(self) -> str: ...
    @property
    def live(self) -> bool: ...           # a session is open on the sink

    def element(
        self, label: str, *, kind: str | None = None, parent: "Element | None" = None
    ) -> "Element": ...
    def link(
        self,
        src: "Element | str",             # str: a global reference "<source>/<id>"
        dst: "Element | str",
        *,
        kind: LinkKind = "data",
        capacity: int | None = None,      # reported with every depth signal
        depth: Callable[[], int] | None = None,  # sampled backlog (see 1.5)
    ) -> "Link": ...
    def remove(self, *handles: "Element | Link") -> None: ...
    def poll(self) -> None: ...           # close every window that has ended
    def close(self, timeout: float = 1.0) -> None: ...
    def __enter__(self) -> "Reporter": ...
    def __exit__(self, *exc: object) -> None: ...


class Element:
    id: str
    label: str
    reporter: Reporter

    def child(self, label: str, *, kind: str | None = None) -> "Element": ...
    def begin(self) -> float: ...                      # item received; returns a token
    def end(self, token: float) -> None: ...           # item finished: backlog -1, latency
    def span(self, label: str, *, parent: "Span | None" = None) -> "Span": ...
    def remove(self) -> None: ...


class Link:
    id: str
    kind: LinkKind

    def deliver(self, n: int = 1, *, bytes: int | None = None) -> None: ...
    def drop(self, n: int = 1) -> None: ...
    def coalesce(self, n: int = 1) -> None: ...
    def enter(self) -> float: ...                      # item enters: backlog +1; token
    def leave(self, token: float, *, bytes: int | None = None) -> None: ...  # delivered
    def leave_dropped(self, token: float) -> None: ...
    def call(self) -> AbstractContextManager[None]: ...  # kind "call": one call
    def remove(self) -> None: ...


class Span(AbstractContextManager["Span"]):
    id: str                               # "s<n>", unique per session
    def end(self, error: BaseException | str | None = None) -> None: ...


def current_span() -> Span | None: ...    # the ContextVar of 1.6
```

All handle methods are no-ops after the handle is removed or the reporter is closed. IDs are
`e<n>` (elements), `l<n>` (links) and `s<n>` (spans), allocated per reporter in creation order and
never reused; the spec does not require them to be stable, but a deterministic construction order
gives the same IDs in every run.

### 1.3 Sessions and `seq`

- The reporter holds the graph state (elements, links, open spans) at all times; structural changes
  are rare, so keeping it costs nothing per item. It builds **no signal** while no session is open
  (spec: "must not build or queue signals").
- A session opens when the sink reports a connection (the file sink: once, when opened; the
  WebSocket sink: on every successful handshake). The reporter then takes `T = clock.wall()`,
  emits the snapshot with `seq` 0 and `t = T`, and sets `live`.
- Every signal gets the next `seq` of the session when it is created, under the reporter lock,
  whether or not it is later written (spec, Generic signal).
- Times: `t = T + (clock.monotonic() − M)` where `M` is the monotonic time at `T`. `t` therefore
  never decreases within a session, even if the wall clock steps.
- The session ends when the sink reports the connection lost or `close()` is called: `live` is
  cleared, the queue is discarded, accumulators are reset. A span that started in an earlier session
  ends silently (its `span.end` is not emitted: the observer never saw it open).
- No `x-` signal types are emitted (ruling 1 of 68d9a54 would require them to take a `seq`;
  rxplus has no use for them).

### 1.4 Signals and their order

Each signal is built as a `dict` and encoded by
`json.dumps(d, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`; integers stay `int`,
optional attributes are omitted, never `null`. Validation at the API boundary: labels non-empty,
IDs and the source name without `/`; a `ValueError` is raised at construction, never on the hot
path.

`t` never decreases within a session, but window signals carry the window's end, which can lie
before "now". So every emission of a structural or span signal first closes every window that has
ended (the check is one comparison against the next boundary). The timer thread does the same at
each boundary. Hence signals are created in `t` order.

### 1.5 Windows and aggregation

Window `k` is `[T + k·w, T + (k+1)·w)`; boundaries are computed as `T + k*w` (no accumulated
sums), so `t − t0 = w` within the checker's 1 µs tolerance.

Each measured link or element has one accumulator guarded by its own lock:

| Quantity | Hot-path call | Kept per window |
|---|---|---|
| flow | `deliver`, `drop`, `coalesce`, `leave*`, `call` | `items`, `bytes` (if any item had a size), `dropped`, `coalesced` |
| latency | `end`, `leave`, end of `call` | `count`, `sum`, `max` of durations that ended |
| depth (counted) | `enter`/`leave*`, `begin`/`end`, `call` | net change, so the backlog *at the boundary* is `current − change since the boundary` |
| depth (sampled) | none: `depth=` callable | read once per window end by the clock |

The window index of an event is `int((now − T) / w)`, taken when it is counted, so items land in the
window in which they were delivered. An event whose window was already closed by a concurrent
flush is added to the current window. Coordinator ruling (R3): the instant the source records an
item is its delivery instant, so this is conformant, with no grace period; `docs/reacting.md` states
the bound (at most the events of threads that read the clock before a boundary and record after it).

At each window end the reporter emits, in this order: flow for each link with
`items + dropped + coalesced > 0` (`bytes` only when `items > 0`, rule `flow-bytes`); latency for
each id with `count ≥ 1`; depth with `value ≠ 0` for each id whose backlog at the boundary is
non-zero; depth `value: 0` for each id whose backlog at the previous boundary was non-zero and is
now zero. `capacity` is added to every depth of a link or element that has one. A measured element
or link stays measured while it exists (ruling 2): its depth rules are evaluated at every window end
until it is removed. A window with nothing to say emits nothing.

Counted backlog starts at zero with each session: an item that entered before the session and
leaves during it is not subtracted below zero (clamped), and has no latency. Components that own a
queue use the sampled form instead, which is exact and costs nothing per item.

### 1.6 Spans and parent propagation

- `element.span(label)` returns an open `Span`; as a context manager it ends with `ok`, or with
  `error` and the exception's **class name** when the block raises. `Span.end(error=...)` takes a
  string or an exception; for exceptions only `type(e).__name__` is sent (spec, Content: error
  descriptions must not carry data; `str(e)` often does).
- The parent is `parent=` if given, else `current_span()`, a `contextvars.ContextVar`. A span used
  as a context manager sets the variable for its block; the `rx.span` operator sets it around the
  downstream `on_next`, so spans of operators further down the same synchronous chain become its
  children.
- Threads: a `ContextVar` follows asyncio tasks (copied at creation) but not reactivex scheduler
  hops. `rxplus.reacting.rx.ContextScheduler(inner)` wraps `schedule`, `schedule_relative` and
  `schedule_absolute` so the action runs in `contextvars.copy_context()` taken at scheduling time;
  use it in `observe_on`/`subscribe_on` to keep parents across threads. Without it, a span on the
  far side of a hop simply has no parent.
- A parent that is not open (ended, or opened in an earlier session) is omitted, never sent
  (rule `span-parent`).
- Removing an element ends its open spans without `span.end` (spec, Span signals).
- A snapshot lists open spans parent-first; `spans` is omitted when empty (rule `spans-empty`).

### 1.7 Bounded queue and Loss

The reporter keeps one `collections.deque` of encoded lines, appended under the reporter lock and
drained by one writer thread per reporter that calls `sink.write(batch)`. Lines count as written
when `write` returns. When appending makes the count exceed `queue`, the reporter discards every
queued line, creates a snapshot of the current state (next `seq`, `t` = now) and queues it alone;
so the next line written is a snapshot and the `seq` gap tells the observer how many were lost
(spec, Loss). The emitting thread never waits: appending is O(1), overflow is O(graph) at most once
per overflow, and nothing ever blocks on the sink.

### 1.8 Sinks

```python
class Sink(Protocol):
    def open(self) -> bool: ...                 # writer thread; True: a new session may start
    def write(self, lines: Sequence[str]) -> None: ...  # raises SinkDisconnected
    def retry_delay(self) -> float | None: ...  # None: never reopen
    def close(self) -> None: ...


class SinkDisconnected(Exception): ...


class JsonlFileSink:
    def __init__(self, path: str | os.PathLike[str], *, overwrite: bool = False) -> None: ...


class WebSocketSink:
    def __init__(
        self,
        url: str,                                # ws://host:port/path or wss://...
        *,
        token: str | None = None,                # sent as Authorization: Bearer
        open_timeout: float = 5.0,
        backoff: tuple[float, float] = (0.5, 30.0),  # first and largest retry delay
        ssl_context: ssl.SSLContext | None = None,
    ) -> None: ...


class MemorySink:                                # tests
    lines: list[str]
    def signals(self) -> list[dict[str, object]]: ...
    def disconnect(self) -> None: ...            # next write raises SinkDisconnected
    def hold(self) -> None: ...                  # writes block until release(): Loss tests
    def release(self) -> None: ...
```

- **File** (D9): one session per file, one JSON object per line, `\n`, UTF-8 without BOM; opened
  with mode `"x"` unless `overwrite`; flushed after each batch. A write error ends the session for
  good. One reporter per file, so the recording-order rule holds trivially.
- **WebSocket** (D9, spec Transport): a minimal RFC 6455 client on `socket`/`ssl` (stdlib only,
  D10): HTTP/1.1 upgrade, `Sec-WebSocket-Key`/`Accept` check, one masked text frame per signal, no
  extensions, no `Origin` header. If the resolved address is not loopback (`127.0.0.0/8`, `::1`),
  `token` is required and sent as `Authorization: Bearer <token>`; without one the sink refuses to
  connect (logged once) instead of sending unauthenticated. Incoming data frames are read and
  discarded and control frames handled as RFC 6455 requires (Ping → Pong, Close → Close), as the
  spec now says (Reacting c7f1232: "A source ignores every data frame it receives and handles
  control frames as RFC 6455 requires"). Each successful handshake starts a new session;
  a lost connection ends it and the writer reopens after `backoff`.
- **Memory**: in-process, for tests; `hold()` simulates a slow sink to exercise Loss.

A reporter has exactly one sink (sessions, `seq` and windows are per connection). To record and
watch at once, connect to the hub, which records (Reacting DESIGN §5). There is no `TeeSink` (D16).

### 1.9 Thread-safety model

- One reporter lock guards the graph state, the session, `seq`, the queue and window closing.
- One lock per accumulator guards its counters. Lock order is reporter → accumulator; the hot path
  takes only its accumulator lock. No user code and no I/O runs under either lock.
- `live` is a plain `bool` written under the reporter lock and read without it on the hot path. The
  race is benign: an event counted just after a session ended is discarded with the accumulators.
- Two daemon threads per reporter at most: the writer (also the window timer: it waits on the
  reporter's condition with a timeout until the next boundary) and, for `WebSocketSink`, a reader
  that drains incoming frames.
- Correct on free-threaded CPython: no shared counter is updated outside a lock.

### 1.10 "No observer: at most one check per item"

Every hot-path method and every probe operator starts with `if not state.live: return` (one
attribute load and one branch), where `state` is bound at construction. A component given
`reacting=None` installs nothing at all: no probe in its pipeline, no branch. Measured targets are in
§4.3.

### 1.11 Probe operators (`rxplus.reacting.rx`)

Every probe accepts `None` and then returns the identity operator, so user code can write
`count(link if reporter else None)` and pay nothing without a reporter.

```python
Operator = Callable[[Observable[T]], Observable[T]]

def count(link: Link | None, *, size: Callable[[T], int] | None = None) -> Operator[T]
    # each item passing is delivered on `link` (flow; bytes if `size`)
def enter(link: Link | None) -> Operator[T]
def leave(link: Link | None, *, size: Callable[[T], int] | None = None) -> Operator[T]
    # a FIFO pair around an asynchronous hop (observe_on, a queue): backlog and latency of `link`
def work(element: Element | None) -> Operator[T]
    # time until the downstream on_next returns: latency and in-flight backlog of `element`
def span(element: Element | None, label: str) -> Operator[T]
    # one span per item around the downstream on_next; for coarse streams only
class ContextScheduler(SchedulerBase):
    def __init__(self, inner: SchedulerBase) -> None: ...
```

Probes never change items, order or timing; they do not catch exceptions (an exception from
downstream ends the span with `error` and propagates unchanged). `enter`/`leave` assume FIFO order
between them; for a hop that drops, call `Link.leave_dropped` from the dropping code.

## 2. Opt-in by components (D2)

### 2.1 The parameter

Every reporting component gains one keyword parameter, last in its signature:

```python
ReportTo = Reporter | Element
reacting: ReportTo | None = None
```

`None` (the default): nothing is reported and nothing is installed. A `Reporter`: the component's
element is top-level. An `Element`: the component's element is its child, so components can be
placed inside the user's own hierarchy. The component exposes its handles as
`component.reacting_element: Element | None` and `component.reacting_port(name) -> Element | None`,
so user probes can link to its ports. On `on_completed`, `on_error` or `close()` the component
removes its element (and with it its children, links and open spans).

### 2.2 Labels stable across runs

The element label is `name` when the component takes one, else a default built only from
construction arguments: class name plus port or path (never an address, never anything random):
`RxWSServer :8888`, `RxWSClient /chat`, `GatewayNode :8765`. Dynamic children are labelled by
what identifies them at their construction point: a server connection by its request path, a
gateway connection by direction and order (`outbound 1`, `inbound 2`), never by a UUID.

### 2.3 What each component reports

Kinds are prefixed `rxplus.`; ports have kind `port`.

| Component | Elements | Links (kind) | Flow / depth / latency | Spans |
|---|---|---|---|---|
| `RxWSServer` | server; ports `send`, `recv`; one `rxplus.ws.connection` child per open socket | `send → conn` (data), `conn → recv` (data) | `send → conn`: items when `send` returns, bytes = frame size, dropped by the send-queue policy, sampled depth = queue size, capacity = `send_queue`, latency enqueue → sent. `conn → recv`: items, bytes | `handshake` on the connection |
| `RxWSClient` | client; ports `send`, `recv`; child `socket` while connected | `send → socket`, `socket → recv` (data) | as the server; `dropped` also counts values dropped while disconnected | `connect` per attempt (`error` = exception class) |
| `RxWSClientGroup` | group; one `RxWSClient` element per path as children | those of the children | — | — |
| `GatewayNode` | node; its server and clients as children; one `rxplus.gateway.connection` per connection | `node → connection` and `connection → node` (data) | frames sent/received per connection; dropped = frames refused because not `READY` | `handshake` from `HANDSHAKING` to `READY`/`CLOSED` |
| `retry_with_signal` (`reacting=`, `label=`) | operator element | — | — | `attempt` per subscription, `error` when it fails and is retried |
| `RxMicrophone`, `create_wavfile`, `create_screen_capture` | source element; port `out` | — | — | — |
| `RxSpeaker`, `SaveWavFile` | sink element; port `in`; for `RxSpeaker` child `device` | `in → device` (data) | items played, dropped when the queue is full, sampled depth, capacity = `playback_queue_maxsize` | — |

Links between components are the user's to declare (a component cannot see what it is subscribed
to). Cross-process links (global references between a client and the server it talks to) are left
until after 0.2.0 (D16).

### 2.4 Example

```python
import reactivex.operators as ops
from rxplus import RxWSServer, TaggedData, WSConnectionConfig
from rxplus.reacting import Reporter, WebSocketSink
from rxplus.reacting import rx as rr

reporter = Reporter(
    "ingest", WebSocketSink("ws://127.0.0.1:8770/sources"), label="Ingest service"
)
server = RxWSServer(WSConnectionConfig("::", 8888), name="Frontend", reacting=reporter)

app = reporter.element("App", kind="pipeline")
parse = app.child("Parse", kind="operator")
store = app.child("Store", kind="operator")
recv_to_parse = reporter.link(server.reacting_port("recv"), parse)
parse_to_store = reporter.link(parse, store)
store_to_send = reporter.link(store, server.reacting_port("send"))

server.pipe(
    rr.count(recv_to_parse),
    ops.map(lambda td: TaggedData(td.tag, parse_message(td.data))),
    ops.filter(lambda td: td.data is not None),
    rr.count(parse_to_store),
    rr.work(store),
    ops.map(store_and_ack),
    rr.count(store_to_send),
).subscribe(server)
```

A client connecting on `/chat` and sending 12 messages in the first window, 11 of which parse,
yields this session (checked with `check.py check --direct`: valid):

```
{"source":"ingest","seq":0,"t":1762430400.0,"type":"snapshot","protocol":"reacting/1","label":"Ingest service","elements":[{"id":"e1","label":"Frontend","kind":"rxplus.ws.server"},{"id":"e2","label":"send","parent":"e1","kind":"port"},{"id":"e3","label":"recv","parent":"e1","kind":"port"},{"id":"e4","label":"App","kind":"pipeline"},{"id":"e5","label":"Parse","parent":"e4","kind":"operator"},{"id":"e6","label":"Store","parent":"e4","kind":"operator"}],"links":[{"id":"l1","from":"e3","to":"e5","kind":"data"},{"id":"l2","from":"e5","to":"e6","kind":"data"},{"id":"l3","from":"e6","to":"e2","kind":"data"}]}
{"source":"ingest","seq":1,"t":1762430400.031,"type":"add","elements":[{"id":"e7","label":"connection /chat","parent":"e1","kind":"rxplus.ws.connection"}],"links":[{"id":"l4","from":"e2","to":"e7","kind":"data"},{"id":"l5","from":"e7","to":"e3","kind":"data"}]}
{"source":"ingest","seq":2,"t":1762430400.032,"type":"span.start","span":"s1","id":"e7","label":"handshake"}
{"source":"ingest","seq":3,"t":1762430400.034,"type":"span.end","span":"s1","status":"ok"}
{"source":"ingest","seq":4,"t":1762430400.1,"type":"flow","link":"l5","t0":1762430400.0,"items":12,"bytes":480,"dropped":0,"coalesced":0}
{"source":"ingest","seq":5,"t":1762430400.1,"type":"flow","link":"l1","t0":1762430400.0,"items":12,"dropped":0,"coalesced":0}
{"source":"ingest","seq":6,"t":1762430400.1,"type":"flow","link":"l2","t0":1762430400.0,"items":11,"dropped":0,"coalesced":0}
{"source":"ingest","seq":7,"t":1762430400.1,"type":"flow","link":"l3","t0":1762430400.0,"items":11,"dropped":0,"coalesced":0}
{"source":"ingest","seq":8,"t":1762430400.1,"type":"latency","id":"e6","t0":1762430400.0,"count":11,"sum":0.0022,"max":0.0004}
{"source":"ingest","seq":9,"t":1762430400.1,"type":"flow","link":"l4","t0":1762430400.0,"items":9,"bytes":180,"dropped":0,"coalesced":0}
{"source":"ingest","seq":10,"t":1762430400.1,"type":"latency","id":"l4","t0":1762430400.0,"count":9,"sum":0.0018,"max":0.0003}
{"source":"ingest","seq":11,"t":1762430400.1,"type":"depth","id":"l4","value":2,"capacity":1024}
{"source":"ingest","seq":12,"t":1762430400.2,"type":"flow","link":"l4","t0":1762430400.1,"items":2,"bytes":40,"dropped":0,"coalesced":0}
{"source":"ingest","seq":13,"t":1762430400.2,"type":"latency","id":"l4","t0":1762430400.1,"count":2,"sum":0.0005,"max":0.0003}
{"source":"ingest","seq":14,"t":1762430400.2,"type":"depth","id":"l4","value":0,"capacity":1024}
{"source":"ingest","seq":15,"t":1762430400.27,"type":"remove","ids":["e7"]}
```

The snapshot is taken before any client connects; `e7` and its links come and go with the socket;
two acknowledgements are still queued at the end of window 0 (`depth` 2), and the queue drains in
window 1 (`depth` 0). `e5` has no latency because no `work` probe measures it; the filtered item
appears as the difference between `l1` and `l2`.

## 3. The breaking redesign batch (D3)

Three pull requests, each titled `refactor!(…)` with a "Breaking" section, plus additive ones that
need no owner review (the `reacting=` parameters, lazy `__init__`, the WS hot path). Every row
cites the audit.

**PR-A `refactor!(ws)`: codecs, resource bounds and error semantics**
- D17, the codec layer (3.1 below): `datatype` also takes a `WSCodec[T]`; `JsonCodec` is the
  documented way to send custom objects.
- F6: `datatype="object"` needs `allow_pickle=True` (default off, D16) and is documented for trusted
  links only; default `max_size=1 MiB` (parameter) for every datatype.
- F7: `send_queue: int = 1024` per connection with `overflow: OverflowStrategy = "drop_old"`
  (`"drop_new"`, `"disconnect"`); `buffer_while_disconnected` bounded by the same limit.
- F11: per-item failures (non-`TaggedData`, wrong payload type, a subscriber raising) are logged and
  dropped (counted as `dropped` with a reporter); only lifecycle failures reach `on_error`.
  `on_error` received from upstream shuts the component down like `on_completed`.
- F10: `RxWSClientGroup`: a child's terminal error removes it and is emitted on
  `group.child_errors: Observable[tuple[str, Exception]]`; the group ends only on its own
  `on_completed`/`on_error`.
- F20 lands in an additive PR before PR-A (batched hand-off, one wake-up per non-empty
  transition, package once per message per path), with before/after numbers.

### 3.1 Codec layer (D17, part of PR-A)

```python
from typing import Literal, Protocol

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class CodecError(ValueError): ...


class WSCodec[T](Protocol):
    frame: Literal["text", "binary"]
    def encode(self, value: T) -> bytes | str: ...
    def decode(self, data: bytes | str) -> T: ...      # raises CodecError


class JsonCodec:
    """Text frames of JSON; custom objects only from its own registry."""

    frame: Literal["text"]

    def __init__(self, *, max_size: int = 1 << 20, max_depth: int = 32) -> None: ...
    def register[D](self, cls: type[D], *, name: str | None = None) -> type[D]: ...
        # a dataclass; usable as a decorator; name defaults to cls.__qualname__
    def register_class[C](
        self,
        cls: type[C],
        *,
        name: str,
        to_dict: Callable[[C], dict[str, JsonValue]],
        from_dict: Callable[[dict[str, JsonValue]], C],
    ) -> None: ...
    def encode(self, value: object) -> str: ...
    def decode(self, data: bytes | str) -> object: ...
```

- Wire format: plain JSON values pass through; a registered object is
  `{"$type": "<name>", "fields": {...}}`; `bytes` is `{"$bytes": "<base64>"}`. Encoding a plain
  `dict` that has a `"$type"` or `"$bytes"` key, or an unregistered object, raises `CodecError`.
- Decoding checks `len(data) ≤ max_size` before parsing, rejects duplicate keys, NaN and Infinity,
  and nesting deeper than `max_depth`; it constructs only types in the registry, with exactly their
  fields (missing or extra fields are errors), each validated against the field's annotation.
  Supported annotations: `None`, `bool`, `int`, `float`, `str`, `bytes`, registered types,
  `list[X]`, `tuple[X, ...]`, `dict[str, X]`, `X | None`, unions of those; anything else is refused
  at `register` time. It never imports, evaluates or calls anything named by the peer;
  `from_dict` runs only for a name the user registered.
- Errors: a frame that fails to decode is logged and dropped (counted as `dropped` with a
  reporter), like every per-item failure in PR-A; the connection stays open.
- `datatype` accepts `"string" | "bytes" | "object" | WSCodec[T]`, or a per-path callable returning
  one of these. `docs/websocket.md` presents `JsonCodec` as the way to send custom objects and
  `"object"` (pickle) as an opt-in for trusted links.

**PR-B `refactor!(gateway)`: connections per socket**
- F8: `RxWSServer.connections: Observable[WSConnectionEvent]` (additive, PR-A); `GatewayNode` keys
  inbound connections by socket, closes them on disconnect, emits `on_disconnection`.
- F21: the four overflow classes become one `BoundedBuffer[T](limit, strategy)`, which serves the
  gateway; `pop()` raises `IndexError` on empty instead of returning `None`. The WebSocket send path keeps
  its own `SendQueue` (PR-A): it is used on the loop thread only and has a byte bound, needs a thread-safe
  general buffer does not have (ruling on #26).
- `GatewayNode` with no `logger_provider` logs nothing (no implicit console exporter).
- `gateway.ConnectionState` is renamed `GatewayConnectionState`.

**PR-C `refactor!(core)`: operators, typing, package surface**
- F12: `retry_with_signal` waits with `scheduler.schedule_relative` (default `TimeoutScheduler`)
  instead of `time.sleep`; it retries every `Exception` (`should_retry` decides); errors keep being
  wrapped in `RxException`.
- `ErrorRestartSignal` uses `dataclasses` (drops the `attrs` dependency).
- `FPSMonitor`, `BandwidthMonitor` report through a callback (default: OTel log) instead of `print`.
- Typing (F22): `RxWSServer(Subject[TaggedData[str, P]])`, `RxWSClient(Subject[P])`,
  `GatewayNode.sink: Observable[TaggedFrame[F]]`, typed `Duplex[T_in, T_out]`, a generic
  `Framing[F]` protocol (today no concrete framing class satisfies `Framing`); target
  `mypy --strict rxplus` clean except the audio module.
- Aliases removed: `rxplus.ws.ConnectionState`, `rxplus.ws.WS_Channels`.

### Migration table (to become `docs/migration-0.2.md`)

| 0.1.x | 0.2.0 | PR |
|---|---|---|
| `RxWSServer(cfg, datatype="object")` | `datatype=JsonCodec()` with registered types; or `datatype="object", allow_pickle=True` on trusted links | A |
| a value that cannot be packaged ended the component via `on_error` (0.1.x) | logged at ERROR and dropped (already in 0.1.x `main` since #11) | #11 |
| frames of any size | `max_size=1 MiB`; pass `max_size=None` for the old behaviour | A |
| unbounded per-client queue | `send_queue=1024`, `overflow="drop_old"`; `send_queue=None` restores | A |
| non-`TaggedData` to `RxWSServer.on_next` → `on_error` | logged and dropped | A |
| `package_type_check` raised in the caller of `on_next` | logged and dropped | A |
| upstream `on_error` left the WS thread running | shuts the component down | A |
| a child error ended `RxWSClientGroup` | `group.child_errors`; group continues | A |
| inbound gateway connections keyed by path, never closed | keyed by socket; closed on disconnect | B |
| `DropOld`, `DropNew`, `BufferK`, `Disconnect` | `BoundedBuffer(limit, strategy)` | B |
| `buffer.pop()` → `None` when empty | raises `IndexError` | B |
| `GatewayNode()` logs to the console by default | silent unless `logger_provider` | B |
| `rxplus.ConnectionState` (gateway) | `rxplus.GatewayConnectionState` | B |
| `retry_with_signal` sleeps on the error's thread | schedules on `scheduler` | C |
| non-`RxException` errors not retried | retried; `should_retry` decides | C |
| `FPSMonitor`/`BandwidthMonitor` print | callback, default OTel log | C |
| `rxplus.ws.ConnectionState`, `WS_Channels` | `WSConnectionState`, `WSChannels` | C |
| — | `reacting=` on components; `rxplus.reacting` | additive |

## 4. Testing

### 4.1 Unit tests (`tests/reacting/`)

With `MemorySink`, `timer=False` and a manual clock, so every test is deterministic:
- session: snapshot first with `seq` 0, consecutive `seq`, a new session after `disconnect()`,
  spans of the old session not ended in the new one;
- windows: boundaries `T + k·w` over 10⁶ windows (no drift), events landing in the right window,
  the four depth rules incl. "stays measured", no flow with all-zero counts, no `bytes` without
  items, latency `max ≤ sum ≤ count·max`;
- ordering: a structural signal after a boundary closes the window first (`t` never decreases);
- Loss: `hold()` the sink, exceed `queue`, check one snapshot follows and the gap equals the
  discarded count;
- spans: nesting through the `ContextVar`, through `ContextScheduler`, parents never sent when not
  open, removal ending spans, error text is the class name;
- content: no item value reaches any signal (items are sentinels whose `repr` must not appear);
- threads: 8 threads counting on one link, totals exact; free-threaded build in CI if available;
- import: `import rxplus.reacting` does not load reactivex, websockets or OpenTelemetry;
- WebSocket sink: against a local `websockets` server: handshake, Bearer rules (refuses a
  non-loopback address without token), no `Origin`, ignores data frames, answers Ping, reconnect
  starts a new session.

### 4.2 Conformance

Every test that produces a session writes it as a recording and runs the reference checker on it:
`python3 conformance/check.py check <file> --direct` (it also checks window alignment against the
session's first snapshot). Since observers ignore malformed signals silently (68d9a54), the checker
is the only guard, so this is part of every recording test and of a long randomised test (random
graphs, structural changes, spans, overflows, reconnects; 60 s simulated). The tests also fold the
recording with `check.py fold --direct` and compare with what the test knows it did (e.g. the sum of
`items` on a link equals the items sent), which the checker cannot verify ("completeness and
truth"). Finally each corpus case in `conformance/valid/` that a source could produce is replayed
through the `Reporter` API and must yield a recording that folds to the same state.

The checker comes from the Reacting repository, which is private, so rxplus vendors it (D16):
`tests/conformance/check.py` and the corpus (`valid/`, `invalid/`) copied from a pinned Reacting
commit recorded in `tests/conformance/PIN` (commit hash plus the SHA-256 of every vendored file).
A test fails if a vendored file's hash disagrees with `PIN`; `tests/conformance/update.sh <commit>`
re-vendors. A further test runs the vendored checker over the vendored corpus and compares its
output with each case's `.expect.json`, so a copy that is internally inconsistent fails in rxplus
CI too.

### 4.3 Overhead benchmarks (`benchmarks/`)

`benchmarks/bench_ws.py` (in this PR) is the audit baseline. `benchmarks/bench_reacting.py` will
measure, per item, a `Subject → map → subscriber` pipeline in four configurations; targets:

| Configuration | Target |
|---|---|
| no reporter (`reacting=None`, probes with `None`) | equal to baseline (nothing installed) |
| reporter, no session (sink disconnected) | ≤ baseline + 0.1 µs (one check) |
| reporter live, `count` probe, `MemorySink` | ≤ baseline + 1.0 µs |
| `RxWSServer` with reporter live, 16-byte messages | ≥ 90 % of its throughput without |

Baseline today (audit): `Subject → subscriber` 0.32 µs, `→ do_action(noop) →` 0.39 µs per item; WS
46 k msg/s. Numbers before/after go into each PR.

## 5. Decisions on the open questions (D16)

1. One sink per reporter; no `TeeSink`. Record through the hub to watch and record at once.
2. Pickle stays only behind `allow_pickle=True`, default off; the codec layer (D17, 3.1) is the
   documented way to send custom objects.
3. rxplus CI vendors `check.py` and the corpus pinned to a Reacting commit (4.2).
4. Cross-process links are deferred until after 0.2.0.

## Files

| File | Change |
|---|---|
| `docs/proposals/2026-11-06-reacting-support.md` | this proposal |
| `benchmarks/bench_ws.py` | the audit's WS/gateway/Rx baseline benchmark |
| later (X3) `rxplus/reacting/*`, `tests/reacting/*`, `tests/conformance/*`, `docs/reacting.md`, lazy `rxplus/__init__.py` | the core and sinks |
| later (X4) components, `rxplus/reacting/rx.py`, `benchmarks/bench_reacting.py` | opt-in, probes |
| later (X5) PR-A (incl. `rxplus/ws/codec.py`), PR-B, PR-C, `docs/migration-0.2.md` | the breaking batch |
