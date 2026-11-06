# reacting/1 source (`rxplus.reacting`)

Report a running graph — its elements, links, flow, backlog, latency and spans — to the Reacting viewer,
live through the hub or as a recording file. `rxplus.reacting` implements a *source* of the `reacting/1`
protocol (Reacting repository, `docs/spec/Reacting.md`). Design: `docs/proposals/2026-11-06-reacting-support.md`
(approved, D16).

The core uses only the standard library (D10): `import rxplus.reacting` loads neither reactivex nor
websockets nor OpenTelemetry. The Rx probe operators and the `reacting=` parameter of rxplus components come
in a later release step (X4).

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
| `timer` | `True`: a daemon thread opens the sink, closes windows on time and writes. `False`: nothing runs in the background and `poll()` does all three (tests). |

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
- `WebSocketSink(url, *, token=None, open_timeout=5.0, backoff=(0.5, 30.0), ssl_context=None)`: dials the
  observer (the hub, D9); reconnects with exponential backoff; each connection is a new session.
- `MemorySink()`: keeps the lines (`.lines`, `.signals()`); `disconnect()`/`connect()`, `hold()`/`release()`
  for tests.

## Model

- **State and sessions.** The reporter always holds the graph state: elements, links, open spans. A
  session opens when the sink connects; it starts with a snapshot (`seq` 0, `t = T`) and ends when the
  connection closes (a closed connection is noticed within one window even when nothing is written). No
  signal is built outside a session. Open spans survive a reconnect: the next session's snapshot lists them
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
- **Order.** Any structural or span change first closes the windows that have ended, so `t` never
  decreases; closing happens before the state changes, so a Loss during it never sends a change twice.
- **Loss.** When more than `queue` lines are waiting, all are discarded and a snapshot is queued; the `seq`
  gap tells the observer how many were lost. The caller never waits for the sink.

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

One lock per reporter (graph, session, queue, windows) and one per element/link (counters); lock order
reporter → handle; no user code runs under them except the `depth=` samplers, which must be cheap, never
block and never call the reporter. Threads: the writer (with `timer=True`) and, for `WebSocketSink`, a
reader that discards data frames and answers Ping/Close (RFC 6455; spec c7f1232).

Measured (`benchmarks/bench_reacting_core.py`, i9-13900, CPython 3.13): without a session a hot-path call
costs about 17 ns over an empty call (one attribute check); live, `deliver` about 0.30 µs, `enter`+`leave`
about 1.0 µs.

## Log records

None: the core is standard-library only and does not log. `WebSocketSink.last_error` holds the reason the
last connection attempt failed.

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
- **Transport.** `WebSocketSink` refuses to dial a non-loopback address without a `token`
  (`Authorization: Bearer`); prefer `wss://` off loopback. It never sends `Origin`. Incoming messages are
  discarded without being parsed; one larger than 1 MiB closes the connection (1009).
- **Resources.** Memory is bounded by `queue` lines per reporter. Removed elements and links release their
  counters.
