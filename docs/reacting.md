# reacting/1 source (`rxplus.reacting`)

Report a running graph — its elements, links, flow, backlog, latency and spans — to the Reacting viewer,
live through the hub or as a recording file. `rxplus.reacting` implements a *source* of the `reacting/1`
protocol (Reacting repository, `docs/spec/Reacting.md`). Design: `docs/proposals/2026-11-06-reacting-support.md`
(approved, D16).

The core uses only the standard library (D10): `import rxplus.reacting` loads neither reactivex nor
websockets nor OpenTelemetry. The Rx probe operators are in `rxplus.reacting.rx` (reactivex), and rxplus
components report themselves when given `reacting=` (see Components).

A complete example: `python -m tasks.task_reacting_pipeline` runs a WebSocket server, a few clients and
probes on the user's own stages, live into the Reacting hub (start it in the Reacting repository with
`npm run serve`; sources dial `ws://127.0.0.1:8787/source`), or into a file with `--record run.jsonl`.

## Example

```python
from rxplus.reacting import JsonlFileSink, Reporter, WebSocketSink

reporter = Reporter("ingest", WebSocketSink("ws://127.0.0.1:8770/sources"), label="Ingest")
# or: Reporter("ingest", JsonlFileSink("ingest.jsonl"))

app = reporter.element("App", kind="pipeline")
parse = app.child("Parse", kind="operator")
store = app.child("Store", kind="operator")
queue = reporter.link(parse, store, capacity=64)

token = queue.enter()          # an item enters the queue
queue.leave(token, bytes=120)  # ... and is delivered: flow, backlog, latency

with store.span("batch"):      # a unit of work; nested spans get it as parent
    ...

reporter.close()
```

## Arguments

`Reporter(source, sink, *, label=None, window=0.1, queue=10_000, clock=None, timer=True)`

| Name | Meaning |
|---|---|
| `source` | Source name: non-empty, no `/`. |
| `sink` | `JsonlFileSink`, `WebSocketSink` or `MemorySink`. One sink per reporter (D16). |
| `label` | Display name of the source, sent in snapshots. |
| `window` | The spec's `window` option, seconds. |
| `queue` | The spec's `queue` option: lines created but not yet written before Loss. |
| `clock` | `SystemClock()` by default; `ManualClock` for deterministic tests. |
| `timer` | `True`: a clock thread and a writer thread run in the background. `False`: nothing runs in the background and `poll()` does their work (tests). |

`reporter.element(label, *, kind=None, parent=None)`, `element.child(label, *, kind=None)`,
`reporter.link(src, dst, *, kind="data", capacity=None, depth=None)` (`src`/`dst`: elements, or a global
reference `"<source>/<id>"`), `reporter.remove(*handles)`, `handle.remove()`, `reporter.poll()`,
`reporter.close()`.

Hot path, on handles: `Link.deliver(n=1, bytes=None)`, `Link.drop(n=1)`, `Link.coalesce(n=1)`,
`Link.enter() -> token`, `Link.leave(token, bytes=None)`, `Link.leave_dropped(token)`, `with link.call():`
(call links), `Element.begin() -> token`, `Element.end(token)`, `Element.span(label, parent=None)`,
`Span.end(error=None)`, `current_span()`.

Sinks:

- `JsonlFileSink(path, *, overwrite=False)`: a recording, one session per file. Creates the file at
  construction (`FileExistsError` if it exists unless `overwrite`).
- `WebSocketSink(url, *, token=None, open_timeout=5.0, backoff=(0.5, 30.0), stable_after=10.0,
  send_timeout=10.0, ping_interval=20.0, ping_timeout=20.0, ssl_context=None)`: dials the observer (the
  hub, D9); each connection is a new session. Every reopen waits a delay that doubles up to `backoff[1]` and
  resets only after a connection that stayed up `stable_after` seconds, so an observer that accepts and
  then rejects (a duplicate source name) is not hammered. Any write error closes the connection. A send
  without progress for `send_timeout`, or a peer silent for `ping_interval + ping_timeout` (it is pinged
  when quiet), closes it too.
- `MemorySink()`: keeps the lines (`.lines`, `.signals()`); `disconnect()`/`connect()`, `hold()`/`release()`
  for tests.

## Model

- **State and sessions.** The reporter always holds the graph state: elements, links, open spans. A
  session opens when the sink connects; it starts with a snapshot (`seq` 0, `t = T`) and ends when the
  connection closes (a closed connection is noticed within a quarter second even when nothing is written).
  Without a session the hot path does nothing after its one check, and `Element.span` returns an unreported
  span. Open spans survive a reconnect: the next session's snapshot lists them
  (their `parent` only if that span is open too), and their `span.end` follows normally.
- **IDs.** `e<n>`, `l<n>`, `s<n>` in creation order, never reused. Labels and kinds are what you pass; use
  labels that are the same in every run for the same construction point (spec, Elements).
- **Windows.** Window `k` is `[T + k·w, T + (k+1)·w)`, boundaries computed as `T + k*w` (no drift). Each
  event is counted in the window of the instant it is recorded. A thread that read the clock just before a
  boundary and records just after the window was closed by another thread has its event counted in the next
  window; this is conformant, because the recording instant is the delivery instant (coordinator ruling R3).
  The bound: at most the events in flight across one boundary, per thread.
- **What each window reports.** Flow for every link with items, drops or coalescing (`bytes` only with
  items); latency for every element or link where a duration ended (`max ≤ sum ≤ count·max`); depth for
  every non-zero backlog at the boundary, and `0` once when it returns to zero. Every element and link is
  measured for its whole life (spec ruling "measured stays measured").
- **Backlog.** Counted by `enter`/`leave` and `begin`/`end`, starting from zero in each session (an item that
  entered before the session leaves without going below zero), or sampled with `link(depth=callable)`, read
  once per boundary (exact; nothing per item). A backlog above `capacity` is reported as `capacity`.
- **Order.** Each change is stamped with one clock read, under the reporter lock, so changes are in `t`
  order. The clock thread applies them in that order to its mirror of the graph. Before emitting a change it
  closes every window whose end `T + (k+1)·w` is at or before the change's `t`. So `t` never decreases, and
  a snapshot shows the state at its place in the stream.
- **Loss.** When more than `queue` lines are waiting (the batch being written counts), all queued lines are
  discarded and a snapshot is queued; the `seq` gap tells the observer how many were lost. If the discarded
  lines include the session's opening snapshot, a new session begins instead (spec fa173ab). Lines are
  created on schedule even while the sink is stalled, so `queue` bounds memory. The caller never waits.

- **Recovery snapshots carry backlogs.** A snapshot that follows a Loss lists every non-zero backlog at its
  time (counted now, or sampled) in `backlogs` (spec 5ad5cdb), so an observer does not show zero backlogs
  until the next depth signals. The depth rules continue from the listed value: one that drains reports
  `0` at the next window end.

- **Validation.** Labels, kinds, IDs and error strings are checked where you pass them, with or without a
  session: non-empty where required, no `/` in IDs, and encodable as UTF-8. A lone surrogate from
  `os.fsdecode` of a non-UTF-8 name raises `ValueError` at the call, never later.

## Probe operators (`rxplus.reacting.rx`)

```python
from rxplus.reacting import rx as rr

source.pipe(
    rr.count(recv_to_parse),                 # each item is delivered on the link (flow)
    ops.map(parse),
    rr.enter(parse_to_store),                # backlog +1 ...
    ops.observe_on(rr.ContextScheduler(pool)),
    rr.leave(parse_to_store),                # ... -1 on the far side: backlog and latency of the hop
    rr.work(store),                          # time until downstream on_next returns: latency of `store`
    ops.map(store_and_ack),
    rr.span(store, "batch"),                 # one span per item (coarse streams)
)
```

| Probe | Measures |
|---|---|
| `count(link, *, size=None)` | flow on `link` per item; `bytes` from `size(item)` |
| `enter(link)` / `leave(link, *, size=None)` | a FIFO pair around an asynchronous hop: counted backlog of `link`, latency (time in the hop), flow at `leave`. A token is kept for every item, with or without a session, so pairs stay aligned across a session gap; an item that entered before the session counts as flow only. At most 65 536 items in flight are tracked per link (`MAX_IN_FLIGHT`); for a hop that drops, call `Link.leave_dropped` from the dropping code |
| `work(element)` | per item, the time until the downstream `on_next` returns: latency and in-flight backlog of `element` |
| `span(element, label)` | one span per item around the downstream `on_next`; it is the current span there, so spans further down the same synchronous chain become its children; `error` (the exception's class) when downstream raises |

Every probe takes a handle or `None`; with `None` it is the identity operator (`rr.count(link if reporter
else None)` costs nothing without a reporter). With a handle it costs one check per item while no session
is open. Probes never change items, their order or timing, and never catch exceptions.

`ContextScheduler(inner)` wraps a scheduler so each action runs in a copy of the context taken when it was
scheduled; use it in `observe_on`/`subscribe_on` to keep `current_span()` (span parents) across a thread
hop. Without it a span on the far side has no parent. It is a periodic scheduler too, so `interval` and
`timer` accept it.

## Components

rxplus components take `reacting: Reporter | Element | None = None`, last in their signature (D2).
`None` installs nothing. A `Reporter` makes the component's element top-level; an `Element` makes it a
child of yours. `component.reacting_element` is its element and `component.reacting_port("send" | "recv")`
its ports, which your own links attach to (a component cannot see what it is subscribed to).

Kinds follow the convention shared with the Reacting viewer (Reacting DESIGN §4.1): ports have kind `port`
and are drawn on their parent's border; short-lived dynamic children have a kind ending in `.instance`
and are grouped.

| Component | Element (kind; label) | Children | Links and what they report | Spans |
|---|---|---|---|---|
| `RxWSServer` | `rxplus.ws.server`; `name`, else `RxWSServer` (never the port, which is often ephemeral) | ports `send`, `recv`; one `rxplus.ws.connection.instance` per open socket, `connection <path>` | `send → connection`: one item per frame sent, its bytes; `dropped`: frames discarded by the send-queue policy, the frame in flight when the socket failed, and the frames still queued when it closed (so items + dropped = frames queued); counted backlog (`capacity` = `send_queue`), latency from enqueue to sent. `connection → recv`: one item per message received, its bytes; a message that does not decode is `dropped`. Values for a path without clients have no link; `server.dropped` counts them | — |
| `RxWSClient` | `rxplus.ws.client`; `name`, else `RxWSClient <path>` | ports `send`, `recv`; `socket` (`rxplus.ws.socket.instance`) while connected | as the server, around the socket. Frames queued while offline enter the next socket's link | `connect` per attempt; `error` with the exception's class when it fails |
| `RxWSClientGroup` | `rxplus.ws.group`; `name`, else `RxWSClientGroup` | one `RxWSClient` element per path (`reacting_client(path)` gives the client) | those of the clients | — |
| `retry_with_signal(..., reacting=, label="retry_with_signal")` | `rxplus.operator`; `label`; exists while at least one subscription is active | — | — | `attempt` per subscription; `error` with the class of the underlying exception when it fails |

Labels come from construction arguments only (a request path for a connection, without its query string
and fragment), never from addresses, ports or anything random, so they are the same in every run. A component removes its element (with its children,
links and open spans) when it completes or fails. Dynamic children, and the component itself, are removed
one window after they end: a window's counts are reported at its end and only for links that still exist,
so a socket removed at once would lose the counts of its last window.

`GatewayNode` and the audio and screen components do not report yet.

## Limits

The clock thread closes each window in O(busy elements and links), at roughly 10–40 µs per busy one on
CPython (more when other threads keep the GIL busy). Above a few thousand busy links per window it may fall
behind. It never holds counts without bound; instead it turns the backlog into a **Loss**:

| Limit | Value | Enforced by | Above it |
|---|---|---|---|
| `queue` | your option (default 10 000) | the clock thread, at each line | Loss: queued lines discarded |
| lag | 64 windows | the clock thread, before each window | Loss |
| window totals held | 100 000 | the **hot path**, when a new total is created | the count is not kept; Loss at the clock thread's next turn |

The third limit is hard. Window totals live in 64 shards; an element or link belongs to one shard (by its
id), whose `windows[k][id]` hold its totals. Callers hold the shard's lock for their whole O(1) write; the
clock thread holds it to move the ended windows out (and processes them outside), and never reads a shard
without it, so it never iterates a structure a caller is changing. It takes each shard's lock as rarely as
it can, because under the GIL a lock held by a preempted caller stalls it for a switch interval (issue
#27): once per step to move every ended window out (an empty shard is skipped), once per shard and window
for the depths of the ids that have backlog activity, and never for an id with nothing to report (no
backlog, no sampler, no change in the window). Totals moved out but not yet closed count towards the
cap. A hot-path call
creates a total only for the first event of an element or link in a window, never per item; that is
where the cap is checked, so the totals held stay at or under 100 000 however the threads are scheduled
(the count is updated without a common lock, so a few concurrent creations can pass it). The backlog
counters keep counting at the cap. Each total costs about 160 bytes, so the counts held stay under about
16 MB.

A Loss gives up every window up to the current one in O(shards): each shard's windows are replaced by an
empty dict. Every change not yet emitted (structural changes, span starts and ends)
is applied to the graph without its own signal, and a snapshot taken now, which includes them, is sent. The
`seq` gap is at least the number of signals not sent, so observers see the loss; `t` never decreases.

Measured with 4 threads delivering on random links, window 0.1 s, totals sampled every 20 ms:

| Busy links | Duration | Peak totals held | Losses |
|---|---|---|---|
| 10 000 | 60 s | 39 604 | 0 |
| 20 000 | 60 s | 100 000 | 2 |
| 10 000 | 180 s | 69 503 | 0 |
| 20 000 | 180 s | 92 908 | 1 |

Before issue #27 (one lock per busy id and two per shard and window) the same runs had 30, 80, 91 and 267
Losses. With 200 links, 8 threads and a 10 ms window (CPython 3.13, default switch interval) the clock now
keeps up with and without counted backlogs (no Loss in 10 s), where it lost about half the counts.

The peak does not grow with run length. Before the cap moved into the hot path, the bound was checked only
on the clock thread: peaks of 150 000–330 000 were not a leak, but that thread getting less CPU than the
callers. A sink that keeps every line in memory makes it worse.

## Dataflow

```
your code / rxplus components
   │ element(), link(), remove(), spans        (reporter lock)
   │ deliver/enter/leave/begin/end/call         (per-handle lock; one check without a session)
   ▼
Reporter: graph state ── windows (Accumulator per id) ── encode ── bounded deque
                                                                     │ writer thread
                                                                     ▼
                                              JsonlFileSink │ WebSocketSink │ MemorySink
```

## Sink and stream data types

Lines are `reacting/1` signals, one JSON object per line, UTF-8, compact separators, no NaN or Infinity
(spec, Encoding). Signal types emitted: `snapshot`, `add`, `remove`, `flow`, `depth`, `latency`,
`span.start`, `span.end`; no `x-` types.

## Threads and cost

| Thread | Does | Never does |
|---|---|---|
| callers (the observed graph) | validate; update the registry and append a timestamped event under the reporter lock (O(1)); count into one accumulator under its shard's lock (O(1)) | close windows, encode JSON, call user code, touch the sink |
| clock (`rxplus-reacting-clock-<source>`) | apply events to the mirror; close windows on time, even when the process is idle and the sink stalled, at a cost of O(busy accumulators) per window (each closed window is moved out of its shard under the shard's lock, then processed); call `depth=` samplers outside every lock; encode; queue | wait for the sink |
| writer (`rxplus-reacting-writer-<source>`) | open the sink (always through its backoff when reopening); write batches; report a lost connection | build signals |

Both background threads are daemons. At interpreter exit, `close(timeout=1.0)` runs for every open reporter,
which flushes a file sink; `close()` first waits (within its timeout) for the writer's first `open()`, so a
process that exits right after creating a reporter still records its graph.

The clock thread never dies. A step that raises (a bug in rxplus) is logged with its traceback, counted in
`Reporter.clock_errors`, and recovered from: the clock rebuilds its graph from the reporter's registry,
ends the session and begins a new one at its next step (a new `seq` 0 snapshot); counts of the windows not
yet closed are lost. CI runs the reacting tests on free-threaded CPython 3.14t without the GIL, where
races between callers and the clock thread show up. `close(timeout)` is honoured even when the observer stops reading: after the
timeout the sink is closed under the blocked writer.

Measured (`benchmarks/bench_reacting_core.py`, i9-13900, CPython 3.13). Each figure is the cost over an empty
call:

| Call | Cost |
|---|---|
| hot-path call without a session (one check) | about 15 ns |
| `deliver`, live | about 0.35 µs |
| `enter` + `leave`, live | about 0.9 µs |

Memory with a stalled sink: 499 links, a 10 ms window and a held sink for 20 s (2000 windows):
- queued plus in-flight lines peaked at 9994 against `queue` 10 000;
- at most 3 open window totals per link;
- max RSS grew 2.8 MB;
- a span start plus end on the caller's thread took at most 24 µs.

## Log records

One, through the standard library's `logging` (the core has no other dependency): logger `rxplus.reacting`,
level ERROR, with the traceback, when a clock step raised (above); logged the 1st, 2nd, 4th, 8th, ... time,
so a step that keeps failing never floods the log. `WebSocketSink.last_error` holds the reason the last
connection attempt failed.

## Conformance

`tests/reacting/` runs every recording it produces — unit scenarios, twelve randomised sessions with Loss
and reconnects, threaded and WebSocket sessions — through the reference checker
(`check.py check --direct`) and compares the observer's fold with what the test did. The checker and the
corpus are vendored in `tests/conformance/`, pinned in `PIN` to a Reacting commit (D16);
`tests/conformance/update.sh <Reacting checkout> <commit>` re-vendors them, and `test_vendored.py` fails if
a file differs from its pin or the checker disagrees with a corpus expectation.

## Potential vulnerabilities

- **Content.** Signals must not carry data content (spec, Content). The API takes no item values; labels,
  kinds and error texts are yours, so keep data out of them. A failed span sends only the exception's
  class name, never `str(exception)`.
- **Transport.** Without a `token`, `WebSocketSink` dials only loopback addresses. It checks the very
  address it connects to, so a name that also resolves elsewhere is never dialled there. Prefer `wss://` off
  loopback. It never sends `Origin`. Incoming messages are
  discarded without being parsed; one larger than 1 MiB closes the connection (1009).
- **Resources.** Memory is bounded by `queue` lines per reporter. Removed elements and links release their
  counters.
- **Labels from peers.** A server connection is labelled by the request path its client chose, without the
  query string and fragment (which often carry tokens), cut to 80 characters. The path itself is treated as
  structure: if your paths carry data, do not report the server (or its clients).
