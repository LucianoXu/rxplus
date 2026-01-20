# Duplex

Bidirectional communication channel for connecting reactive components.

## Design Intention

When building reactive pipelines, you often need components that both receive and emit data. A `Subject` can do this, but conflates input and output into a single stream. `Duplex` separates these concerns with distinct `sink` (inbound) and `stream` (outbound) subjects, making data flow direction explicit.

## API

```python
from rxplus import Duplex, make_duplex, connect_adapter
```

### `Duplex`

A frozen dataclass with two fields:

| Field | Type | Direction |
|-------|------|-----------|
| `sink` | `Subject` | Inbound — push data **into** the component |
| `stream` | `Subject` | Outbound — subscribe to data **from** the component |

### `make_duplex(sink=None, stream=None)`

Factory that creates a `Duplex`. If either argument is `None`, a new `Subject` is created.

```python
adapter = make_duplex()
adapter.stream.subscribe(print)  # listen to outbound
adapter.sink.on_next("hello")    # push inbound
```

### `connect_adapter(adapterA, adapterB, A_to_B_pipeline=None, B_to_A_pipeline=None)`

Connects two adapters bidirectionally:

- `adapterA.stream` → `adapterB.sink`
- `adapterB.stream` → `adapterA.sink`

Returns a `CompositeDisposable` to disconnect.

```python
left = make_duplex()
right = make_duplex()

# Add transformation pipelines
disposable = connect_adapter(
    left, right,
    A_to_B_pipeline=(ops.map(str.upper),),
    B_to_A_pipeline=(ops.filter(lambda x: len(x) > 0),),
)

# Later: disconnect
disposable.dispose()
```

## Use Cases

- **Protocol adapters** — Wrap a lower-level transport (e.g., WebSocket) behind a `Duplex` interface.
- **Component isolation** — Connect modules without tight coupling.
- **Pipeline injection** — Insert operators between two components via the `*_pipeline` arguments.
