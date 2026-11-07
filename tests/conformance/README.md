# Conformance for `reacting/1`

The canonical test material for the `reacting/1` protocol (`docs/spec/Reacting.md`, decision D8):
recordings that are valid or invalid, the state an observer must hold at the end of each, and a
reference checker written from the spec text alone (Python ≥ 3.12, standard library only).

A source must only produce recordings that `check.py check` accepts. An observer must fold every
recording here to the `state` of its `.expect.json`, which means it applies every valid signal
and ignores what the spec says to ignore.

## Layout

```
conformance/
  check.py              the reference checker (subcommands check and fold)
  test_check.py         runs every case through both subcommands
  valid/NNN-Name.jsonl          a recording that conforming sources and relays could produce
  valid/NNN-Name.expect.json    its expected result
  invalid/NNN-Name.jsonl        a recording that breaks exactly one rule
  invalid/NNN-Name.expect.json
```

Case numbers are stable and never reused: `invalid/005` became `valid/013` when the spec allowed
sources to interleave freely (71c62db).

Each `.expect.json` is

```json
{ "valid": true, "violations": [{ "line": 3, "rule": "seq-order" }], "state": { "sources": { } } }
```

- `valid`: whether `check` accepts the recording (exit 0).
- `violations`: what `check` reports, in order: the line number (1-based, counting every line,
  empty ones included) and the rule identifier. Messages are not part of the contract.
- `state`: the result of `fold`, in the format fixed in
  `AgentDepartment/Coordination/ConformanceState.md`. Compare parsed values (deep equality).

Invalid recordings carry a `state` too: it is what an observer holds after ignoring what it must
ignore, so observers are tested on invalid input as well.

## Usage

```sh
python3 conformance/check.py check my.jsonl        # exit 0: valid; 1: violations listed on stdout
python3 conformance/check.py fold my.jsonl         # the observer's final state as JSON
python3 -m unittest discover -s conformance -p 'test_*.py'
```

Options (both subcommands):

- `--window W`: the window length in seconds for every source. By default it is inferred per
  session from its flow and latency signals (I1). A session with neither gets a note on stderr,
  and its window rules are not checked.
- `--direct`: the recording was taken from its sources directly, not through a relay. Windows
  are then anchored at the `t` of each seq-0 snapshot (`T` in the spec), and the depth rules are
  checked from the start of each session. Hub recordings are direct: they hold the signals as
  received from the sources. Use it for them and for source file-sink recordings.
- `--tolerance S`: the absolute floor, in seconds, of the tolerance for window boundaries (default
  `1e-6`). The tolerance is the larger of this and 10⁻⁴ of the window length.
- `--schema` (`check` only): if the `jsonschema` module is importable, also validates each signal
  against `docs/spec/Reacting.schema.json` and prints `line <n>: schema-disagreement: …` wherever
  the schema and the spec text disagree on the shape of one signal. These lines do not change the
  exit status. There are two known disagreements. JSON Schema counts `1.0` as an integer, which the spec
  forbids (`invalid/031`). The checker also rejects a time too large for a double, which the
  schema accepts (`invalid/053`). The spec sets no upper bound on times, so this is a limit of
  the checker: such a value cannot be compared or folded.

Warnings (unknown attributes or signal types whose names do not start with `x-`, which breaks a
"should") go to stderr and do not change the exit status. So do notes.

Exit status: `0` the recording is valid (`check`) or was folded (`fold`); `1` it has violations
(`check`); `2` the checker could not decide: a usage error, an unreadable file, or an internal
error, which is a bug in the checker and is reported on stderr. Malformed input never causes
status 2. Input too deeply nested, integers too large, or lone surrogates are violations.

**For sources:** record a session (e.g. with the rxplus file sink) and run `check`; add `--direct`
when the recording did not pass through a relay. **For observers:** feed each `*.jsonl` here to
your fold, convert the result to the conformance state, and compare it with `state`.

## Rules

`check` prints `line <n>: <rule>: <message>`. The identifiers are stable.

| Rule | Broken when |
|---|---|
| `encoding` | a line is not UTF-8, or carries a byte order mark |
| `json` | a non-empty line is not one JSON object (NaN and Infinity are not JSON) |
| `duplicate-key` | an object of a signal has a duplicate key |
| `attr-missing` | a required attribute is missing |
| `attr-type` | an attribute has the wrong JSON type |
| `attr-value` | a link `kind` or span `status` is not one of its values |
| `null-attr` | an attribute has the value `null` |
| `int-format` | an integer is written with a fraction or exponent |
| `int-range` | an integer is outside `0 .. 2^53 - 1` |
| `time-negative` | a time or duration is negative |
| `label-empty` | a label is the empty string |
| `id-invalid` | a source name, ID or reference is empty, or an ID contains `/` |
| `ref-global` | a reference that may only be local contains `/` |
| `ref-invalid` | a global reference is not of the form `<source>/<id>` |
| `ref-global-self` | a global reference names the signal's own source |
| `protocol` | a snapshot's `protocol` is not `reacting/1` |
| `t0-after-t` | a `t0` is greater than the `t` of its signal |
| `add-empty` | an add has neither a non-empty `elements` nor a non-empty `links` |
| `remove-empty` | a remove has an empty `ids` |
| `spans-empty` | a snapshot has `spans` present but empty |
| `span-error-attr` | a `span.end` with status `ok` has an `error` |
| `flow-zero` | a flow counts no item (`items`, `dropped` and `coalesced` all 0) |
| `flow-bytes` | a flow has `bytes` > 0 but `items` = 0 |
| `depth-capacity` | a depth `value` exceeds its `capacity` |
| `latency-count` | a latency `count` is less than 1 |
| `latency-stats` | `max` > `sum`, or `sum` > `count` · `max` |
| `snapshot-first` | the first signal of a source is not a snapshot with `seq` 0 |
| `seq-order` | `seq` does not strictly increase within a session |
| `t-order` | `t` decreases within a session |
| `loss-snapshot` | a `seq` gap is not followed by a snapshot as the next signal |
| `snapshot-no-gap-change` | a snapshot with no `seq` gap lists other elements, links or open spans than those held (attributes included), unless it is empty |
| `ref-missing` | a local reference names no existing element or link, or no open span |
| `ref-kind` | a reference names a link where an element is required, or the reverse |
| `parent-order` | an element or span is listed before its parent in the same array |
| `id-duplicate` | an element or link ID is already in use (one namespace for both) |
| `span-duplicate` | a snapshot lists the same span twice |
| `span-reuse` | a span ID is used for a second span in one session |
| `span-not-open` | a `span.end` names a span that is not open |
| `span-parent` | a `span.start` `parent` names a span that is not open |
| `window-bounds` | flow/latency `t - t0` is not the window length, or a `t0`/`t` is not on a window boundary |
| `window-duplicate` | two flow, latency or depth signals for one ID in one window |
| `depth-zero` | a depth of `value` 0 without a non-zero depth at the previous window end |
| `depth-missing` | a non-zero backlog without a depth signal at the next window end |

Some violations do not make the observer ignore the signal. `check` still reports them, and
`fold` applies the signal anyway:

- `span-parent`: the signal is applied with the parent treated as omitted (spec, Receivers);
- `snapshot-no-gap-change`: a snapshot replaces the state whatever it lists;
- `depth-missing`: the violation is a signal that never came;
- the rules of Windows (`window-bounds`, `window-duplicate`, `flow-zero`, `depth-zero`,
  `depth-missing`): "An observer need not check the rules of Windows" (spec, Receivers). See
  `invalid/049`.

Every other violation makes the observer ignore the signal as a whole. `check` runs as the same
observer. A signal with a violation is not applied, so later violations may follow from an earlier
one.

### What the checker cannot verify

These requirements cannot be checked from a recording; `check` accepts recordings that break them:

- Non-interference: a source does not change, block, delay or reorder the graph, builds no signals
  while no observer is connected, and has at most one check per item of overhead.
- Completeness and truth. A snapshot lists every element, link and open span that exists. Every
  change is reported by an add or remove. Counts, bytes, backlogs and durations are correct. A
  flow, latency or depth signal that should have been emitted was emitted. The one exception is
  `depth-missing`, where an earlier depth signal shows that the next one is due.
- What the source decides: which parts are elements, which are measured, which work is a span,
  what an item's size is and when an item is finished.
- Labels and kinds staying the same across runs (a "should"), and IDs (which need not).
- Content: whether labels, kinds, IDs or error texts carry data content.
- Loss mechanics: that discarding happened because the queue exceeded `queue`. Only the
  consequence (a snapshot next after a gap) is visible.
- Relays: that a relay-built snapshot has the `t` of the last signal received, that a relay sends
  a seq-0 snapshot on connect, and that the empty snapshot follows a closed source connection.
  A recording does not show connections.
- The windows' absolute origin `T` for sessions forwarded by a relay, and the window length when it
  cannot be inferred (I1).
- Clock semantics: that `t` is Unix time on the source's clock.
- Transport: WebSocket framing, `Authorization`, `Origin` rejection, and one connection per source
  name at the observer.
- The observer's behaviour on a `protocol` mismatch beyond this recording (closing a connection).

## Rules taken from the spec text

These were decided on the conformance requests (`AgentDepartment/Coordination/Status.md`, C1-R1 to
R8) and are now in the spec or in ConformanceState.md:

- **An ignored signal is still received.** "A signal that an observer ignores, but whose `source`
  and `seq` are valid, still counts as received in Loss: its `seq` is not lost" (Receivers). Both
  subcommands track `seq` and `t` over every such signal. A broken signal is not counted in `lost`,
  and the observer does not wait for a snapshot after it (`invalid/045`). A signal ignored for
  breaking Loss itself still counts too, so the gap before it is in `lost` (`invalid/003`). A seq-0
  snapshot that is ignored still begins a session's numbering. Signals before a source's first
  seq-0 snapshot belong to no session and are not counted.
- **A session's signals wait for one of its snapshots.** "An observer ignores a signal other than a
  snapshot unless it has applied a snapshot of the same session" (Receivers). Such a signal breaks
  no rule. `check` does not report it, and neither subcommand applies it, but its `seq` counts as
  received. If a session-opening snapshot is ignored, nothing of that session is applied until a
  later snapshot of it is (`invalid/039`, where the source never gets an entry, and `invalid/050`,
  where a recovery snapshot opens it).
- **Extension signal types take a `seq`.** "A signal of a type not defined by this spec has the
  generic attributes and takes a `seq` like any other signal" (Generic signal, `type`). Such a
  signal is checked for `source`, `seq` and `t` and takes part in every order rule. Apart from
  that it is ignored (`valid/011`, `invalid/048`). A line with an `x-` type and no `source` is a
  recording extension line (Encoding) and is skipped entirely.
- **Observers need not check Windows** (Receivers). `fold` applies signals that break the rules
  of Windows (`invalid/049`); `check` reports them.
- **Measured stays measured.** "an element or link it measures stays measured while it exists"
  (Windows). This is what makes `depth-missing` checkable (`invalid/021`).
- **Protocol mismatch.** "A reader of a recording stops reading at that line" (Receivers). `fold`
  stops there; `check` reports `protocol` and stops (`invalid/028`).
- **Empty `spans`.** "Omitted when no span is open" (Snapshot), and the schema has `minItems: 1`
  (`invalid/040`).
- **Lines of different sources interleave freely.** "Lines of different sources may interleave in
  any order" (Encoding). Only the order within a source is checked (`valid/013`).
- **The observer judges `t` against the last signal it applied.** "Its `t` is not compared with
  the `t` of later signals" (Receivers). `fold` compares `t` with the last applied signal of the
  session. `check` compares it with the last received one, because the source broke `t` order
  even if the observer ignored the earlier signal (`test_check.py`, probe t1).
- **A snapshot without a gap restates the state** (coordinator ruling on the review). A snapshot
  whose `seq` follows the previous signal with no gap must list exactly the elements, links and
  open spans the observer holds, with the same attributes. Backlogs are not compared. The
  exception is a snapshot with empty `elements` and `links` and no `spans`, which is a relay's
  disconnect snapshot. Otherwise `snapshot-no-gap-change` (`valid/014`, `invalid/051`). The
  check runs only once a snapshot of the session has been applied, and `fold` still replaces the
  state.
- **Conformance state.** Spans ended by one removal are listed in `span` order (`valid/002`).
  Removing an element or link drops its backlog entry, so the same ID added again starts at zero
  (`valid/007`). `capacity` is that of the last depth signal. `lost` counts gaps between received
  signals.

## Interpretations

Where the spec is still silent, the checker takes the most literal reading:

- **I1. Window length and origin.** `window` is not transmitted (ruling C1-R3: no new
  attributes). Per session, the first estimate `d` is the median `t - t0` of the flow and latency
  signals. Every `t0` and `t` is a window boundary, so for each boundary `b`, the value
  `(b - b0) / round((b - b0) / d)` estimates the length, with an error that shrinks with distance.
  `w` is the median of those estimates within 0.1 % of `d`. This holds for lengths such as 1/3 or
  1/60 s written with a few decimals (`valid/015`). A session without flow or latency signals has
  no inferable `w`. Its window rules are skipped with a note unless `--window` is given
  (`valid/016`, `valid/017`). A relay forwards "the windows of the session the original source
  emits", and the observer may never see that session's seq-0 snapshot. So by default, all window
  boundaries of a session must share one phase, set by the first applied flow, latency or depth
  signal. `--direct` anchors them at `T`; hub recordings are direct (ruling C1-R3). Boundaries
  match within max(`--tolerance`, 10⁻⁴ · `w`).
- **I2. The depth rules are checked only where they are observable.** A depth of `0` at window end
  `E` is a violation only if window `E - w` ended after the latest snapshot of the session (or the
  session start, with `--direct`). Otherwise the non-zero depth may have been lost, or emitted
  before a relay's observer connected (`valid/008`). `depth-missing` is reported at the next signal
  of the session that has `t > E + w` and no `seq` gap.
- **I3. Span parents.** A snapshot may list a span whose parent is not listed (the order rule
  applies "if the parent is open"). The parent is then omitted (`valid/006`). A `span.start` whose
  parent is not open breaks "a parent span, which is open when the child starts" (`span-parent`).
  It is still applied with the parent omitted (`invalid/016`). A child keeps its parent after the
  parent ends (`valid/005`), until a snapshot that does not list the parent replaces the state.
- **I4. A span ID in a recovery snapshot** that was used earlier in the session is the same span
  only if that span is still open with the same `id`, `label` and `t0`. Its `parent` must equal
  either the one the source sent or the one the observer kept, which is omitted when the sent parent
  was not open (coordinator ruling). A relay builds its snapshots from what it holds, so it omits
  such a parent. The span then keeps the parent its source sent, so a later snapshot may list either
  form (`valid/018`, `invalid/066`). Otherwise it is `span-reuse`. The same comparison decides
  `snapshot-no-gap-change` for open spans. A span that silently disappears from a snapshot is not
  ended, and its ID stays used.
- **I5. Empty arrays in add.** An add with one empty array next to a non-empty one is valid
  (`valid/003`).
- **I6. Ordering rules.** A signal whose `t` is before that of the last applied signal of its
  session breaks a rule, so the observer ignores it, though it still counts as received. Recording
  extension lines take no part in any order.
- **I7. Unknown attributes are not checked**, not even for `null`. The fold drops them from the
  state (`valid/011`). A signal whose `type` is not recognised and does not start with `x-` is
  treated like an `x-` signal, with a warning on stderr.
- **I8. Consistency rules derived from the definitions.** These follow from the attribute
  definitions rather than from explicit "must" sentences: `flow-zero` (a flow exists only when an
  item was counted, i.e. delivered, dropped or coalesced), `flow-bytes`, `latency-stats` and
  `depth-capacity`. `flow-zero` restates a Windows condition, so the fold does not enforce it. The
  other three are about one signal's attributes, so the fold does.
- **I9. Lines.** An empty line is one with no bytes at all. A line of only spaces is not a JSON
  object (`json`). A `\r` before `\n` is JSON whitespace and is accepted.
- **I10. Relay disconnect snapshot.** The spec does not say whether it carries the source's
  `label`. The corpus one omits it, so the source's state has no `label` (`valid/009`).
