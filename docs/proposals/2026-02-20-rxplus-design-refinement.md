# rxplus Design Refinement Proposal

**Date:** 2026-02-20
**Status:** Draft
**Breaking:** Yes (major version bump)

## Goal

Restructure `rxplus` to enforce the ~700 LOC file limit, eliminate `Any` types, resolve the `ConnectionState` naming conflict, and extract duplicated OTel logging -- while preserving the library's power and expressiveness for internal use.

## Design

### 1. Split `ws.py` (1360 LOC) into focused modules

The current monolithic `ws.py` becomes a `ws/` package with five files:

| New file | Contents | Est. LOC |
|---|---|---|
| `ws/__init__.py` | Re-exports (backward-compatible public API) | ~30 |
| `ws/datatypes.py` | `WSDatatype` (ABC), `WSStr`, `WSBytes`, `WSObject`, `wsdt_factory` | ~120 |
| `ws/server.py` | `WSChannels`, `RxWSServer` | ~400 |
| `ws/client.py` | `RxWSClient`, `WSConnectionState`, `RetryPolicy` | ~550 |
| `ws/client_group.py` | `RxWSClientGroup` | ~160 |

Shared helper `_ws_path` goes into `ws/server.py` (server-only usage).

### 2. Remove `_validate_conn_cfg` (dead code)

With the introduction of `WSConnectionConfig` (a typed dataclass), `_validate_conn_cfg` is no longer needed. It is explicitly removed -- not moved to `_util.py`. The `ws/_util.py` file is not created since `_ws_path` goes directly into `server.py`.

Tests in `tests/test_ws.py` that import `_validate_conn_cfg` (lines 13, 101-121) must be deleted and replaced with `WSConnectionConfig` construction tests.

### 3. Remove `_schedule_on_loop` (dead code)

`_schedule_on_loop` at `ws.py` line 1198 is never called anywhere in the codebase. Remove it during the split.

### 4. Resolve `ConnectionState` naming conflict

Currently `gateway/node.py` imports `ws.ConnectionState as WSConnectionState` to work around the collision. Make the names self-documenting:

- **Rename** `ws.ConnectionState` to `WSConnectionState` (canonical name, not an alias).
- **Keep** `gateway.ConnectionState` as-is (it has different semantics: CONNECTING/HANDSHAKING/READY/CLOSED).
- The top-level `__init__.py` already exports gateway's `ConnectionState`; add `WSConnectionState` to exports.

This is a breaking change for any code importing `from rxplus.ws import ConnectionState`.

### 5. Rename `WS_Channels` to `WSChannels`

Since this is a breaking-change release, rename `WS_Channels` to `WSChannels` for PEP 8 compliance. The old name `WS_Channels` is not re-exported; callers must update. Tests in `test_ws.py` that reference `WS_Channels` (line 9, 125+) must be updated.

### 6. Extract shared `_log` helper into a mixin

`RxWSServer._log` and `RxWSClient._log` are identical 12-line methods. Extract into a reusable mixin:

```python
# rxplus/ws/_otel_mixin.py  (~40 LOC)

class OTelLoggingMixin:
    """Mixin providing _log() for WS components with OTel integration."""
    _logger: Logger | None
    _name: str

    def _log(self, body: str, level: str = "INFO") -> None:
        if self._logger is None:
            return
        severity_map: dict[str, SeverityNumber] = {
            "DEBUG": SeverityNumber.DEBUG,
            "INFO": SeverityNumber.INFO,
            "WARN": SeverityNumber.WARN,
            "ERROR": SeverityNumber.ERROR,
        }
        record = OTelLogRecord(
            timestamp=time.time_ns(),
            body=body,
            severity_text=level,
            severity_number=severity_map.get(level, SeverityNumber.INFO),
            attributes={"component": self._name},
        )
        self._logger.emit(record)
```

Both `RxWSServer` and `RxWSClient` inherit from `(Subject, OTelLoggingMixin)`.

### 7. Replace `conn_cfg: dict` with a typed dataclass

```python
# rxplus/ws/datatypes.py

@dataclass(frozen=True)
class WSConnectionConfig:
    """Typed WebSocket connection configuration."""
    host: str
    port: int
    path: str = "/"
```

This replaces all `conn_cfg: dict` parameters. Existing dict-based call sites use `WSConnectionConfig(**cfg)` or keyword arguments directly.

### 8. Eliminate `Any` types

Specific replacements:

| Location | Current | Proposed |
|---|---|---|
| `WSDatatype.package` return | `Any` | `str \| bytes` (concrete union) |
| `WSDatatype.unpackage` return | `Any` | `str \| bytes \| object` (concrete union) |
| `WSChannels.channels` | `set[Any]` | `set[ClientConnection]` |
| `WSChannels.queues` | `set[Any]` | `set[asyncio.Queue[str \| bytes]]` |
| `_ws_path(ws: Any)` | `Any` | `ClientConnection \| ServerConnection` (see note below) |
| `Connection._duplex` | `Any \| None` | `Duplex \| None` |
| `Connection._ws_client` | `Any \| None` | `RxWSClient \| None` |
| `opt.py` `redirect_to` / `retry_with_signal` | `Any` in several spots | Proper generics `T` |

**`_ws_path` type note:** `ServerConnection` is available as `from websockets import ServerConnection` in websockets >= 15.0 (the project pins `>=15.0.1`). The codebase currently imports only `ClientConnection` and `Server`; the implementer must add `ServerConnection` to the import line at `ws.py` line 27.

**Decision on `WSDatatype` generics (was Open Q1):** `WSDatatype` stays **non-generic**. The `package` method returns `str | bytes` and `unpackage` returns `str | bytes | object`. This avoids complicating `wsdt_factory`'s return type (which would need a union of generics that mypy handles poorly). The concrete subclasses (`WSStr`, `WSBytes`, `WSObject`) already narrow these types via their implementations, and callers always know which subclass they use.

```python
class WSDatatype(ABC):
    @abstractmethod
    def package_type_check(self, value: str | bytes | object) -> None: ...
    @abstractmethod
    def package(self, value: str | bytes | object) -> str | bytes: ...
    @abstractmethod
    def unpackage(self, value: str | bytes) -> str | bytes | object: ...

class WSStr(WSDatatype): ...
class WSBytes(WSDatatype): ...
class WSObject(WSDatatype): ...
```

### 9. Split `telemetry.py` (836 LOC) preemptively

Split into a `telemetry/` package:

| New file | Contents | Est. LOC |
|---|---|---|
| `telemetry/__init__.py` | Re-exports | ~30 |
| `telemetry/config.py` | `configure_telemetry`, `configure_metrics`, `get_default_providers` | ~150 |
| `telemetry/logger.py` | `OTelLogger`, `LogContext` (move from standalone file) | ~250 |
| `telemetry/exporters.py` | `ConsoleLogRecordExporter`, `FileLogRecordExporter` | ~300 |
| `telemetry/metrics.py` | `MetricsHelper` | ~80 |

`log_context.py` (49 LOC) is absorbed into `telemetry/logger.py` since `LogContext` is only used by `OTelLogger`.

### 10. No transport abstraction for Gateway

Per requirements, `GatewayNode` remains directly coupled to `RxWSServer`/`RxWSClient`. No `Transport` ABC is introduced. The gateway package structure stays as-is (`node.py`, `connection.py`, `stream.py`, `framing.py`, `overflow.py`).

The gateway changes are:
- Update imports to use canonical names from the new `ws/` package (handled by `ws/__init__.py` re-exports, so no functional change).
- **`gateway/node.py` lines 176-184 and 385-386**: Currently constructs `RxWSServer` and `RxWSClient` with `dict` config (e.g., `{"host": host, "port": port}`). These must be migrated to use `WSConnectionConfig(host=host, port=port)` and `WSConnectionConfig(host=host, port=port, path=unique_path)`.

## Files to Create/Modify

### New files

| File | Purpose |
|---|---|
| `rxplus/ws/__init__.py` | Package init, re-exports all public names |
| `rxplus/ws/datatypes.py` | `WSConnectionConfig`, `WSDatatype`, `WSStr`, `WSBytes`, `WSObject`, `wsdt_factory` |
| `rxplus/ws/_otel_mixin.py` | `OTelLoggingMixin` with shared `_log` |
| `rxplus/ws/server.py` | `WSChannels`, `RxWSServer`, `_ws_path` |
| `rxplus/ws/client.py` | `WSConnectionState`, `RetryPolicy`, `RxWSClient` |
| `rxplus/ws/client_group.py` | `RxWSClientGroup` |
| `rxplus/telemetry/__init__.py` | Package init, re-exports |
| `rxplus/telemetry/config.py` | `configure_telemetry`, `configure_metrics`, `get_default_providers` |
| `rxplus/telemetry/logger.py` | `OTelLogger`, `LogContext` |
| `rxplus/telemetry/exporters.py` | `ConsoleLogRecordExporter`, `FileLogRecordExporter` |
| `rxplus/telemetry/metrics.py` | `MetricsHelper` |

### Files to delete

| File | Reason |
|---|---|
| `rxplus/ws.py` | Replaced by `rxplus/ws/` package |
| `rxplus/telemetry.py` | Replaced by `rxplus/telemetry/` package |
| `rxplus/log_context.py` | Absorbed into `rxplus/telemetry/logger.py` |

### Dead code to remove during split

| Code | Location | Reason |
|---|---|---|
| `_validate_conn_cfg` | `ws.py` line 191 | Replaced by `WSConnectionConfig` dataclass |
| `_schedule_on_loop` | `ws.py` line 1198 | Never called anywhere in the codebase |

### Files to modify

| File | Change |
|---|---|
| `rxplus/__init__.py` | Update import paths; add `WSConnectionState`, `WSConnectionConfig`, `WSChannels` to exports; remove `log_context` import |
| `rxplus/gateway/node.py` | (1) Change `from ..ws import ConnectionState as WSConnectionState` to `from ..ws import WSConnectionState`. (2) Migrate dict configs at lines 176-184 and 385-386 to `WSConnectionConfig`. |
| `rxplus/gateway/connection.py` | Type `_duplex` as `Duplex \| None`, `_ws_client` as `RxWSClient \| None` (requires conditional import or `TYPE_CHECKING`) |
| `rxplus/opt.py` | Replace `Any` with generics `T` in `redirect_to` and `retry_with_signal` |
| `tests/test_ws.py` | (1) Remove `_validate_conn_cfg` import and its tests (lines 13, 101-121). (2) Add `WSConnectionConfig` construction/validation tests. (3) Update `WS_Channels` references to `WSChannels`. (4) Update any `ConnectionState` references to `WSConnectionState`. |
| `tests/test_telemetry.py` | Update imports (should be transparent via re-exports) |

## Interface Contracts

### `WSConnectionConfig`

```python
@dataclass(frozen=True)
class WSConnectionConfig:
    host: str
    port: int
    path: str = "/"
```

### `WSConnectionState` (renamed from `ws.ConnectionState`)

```python
class WSConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"
```

### `WSDatatype` (non-generic)

```python
class WSDatatype(ABC):
    @abstractmethod
    def package_type_check(self, value: str | bytes | object) -> None: ...
    @abstractmethod
    def package(self, value: str | bytes | object) -> str | bytes: ...
    @abstractmethod
    def unpackage(self, value: str | bytes) -> str | bytes | object: ...

class WSStr(WSDatatype): ...
class WSBytes(WSDatatype): ...
class WSObject(WSDatatype): ...
```

### `OTelLoggingMixin`

```python
class OTelLoggingMixin:
    _logger: Logger | None
    _name: str

    def _log(self, body: str, level: str = "INFO") -> None: ...
```

### `WSChannels` (renamed from `WS_Channels`)

```python
class WSChannels:
    channels: set[ClientConnection]
    queues: set[asyncio.Queue[str | bytes]]
    ...
```

### `RxWSServer.__init__` (updated signature)

```python
def __init__(
    self,
    config: WSConnectionConfig,       # was conn_cfg: dict
    datatype: Callable[[str], Literal["string", "bytes", "object"]]
        | Literal["string", "bytes", "object"] = "string",
    ping_interval: float | None = 30.0,
    ping_timeout: float | None = 30.0,
    name: str | None = None,
    tracer_provider: TracerProvider | None = None,
    logger_provider: LoggerProvider | None = None,
) -> None: ...
```

### `RxWSClient.__init__` (updated signature)

```python
def __init__(
    self,
    config: WSConnectionConfig,       # was conn_cfg: dict
    datatype: Literal["string", "bytes", "object"] = "string",
    retry_policy: RetryPolicy | None = None,
    ping_interval: float | None = 30.0,
    ping_timeout: float | None = 30.0,
    name: str | None = None,
    buffer_while_disconnected: bool = False,
    tracer_provider: TracerProvider | None = None,
    logger_provider: LoggerProvider | None = None,
) -> None: ...
```

Note: `conn_retry_timeout` is removed entirely. `RxWSClient` uses `retry_policy` exclusively. If no `retry_policy` is provided, it defaults to `RetryPolicy()`.

### `RxWSClientGroup.__init__` (updated signature)

```python
def __init__(
    self,
    config: WSConnectionConfig,       # was conn_cfg: dict
    datatype: Callable[[str], Literal["string", "bytes", "object"]]
        | Literal["string", "bytes", "object"] = "string",
    retry_policy: RetryPolicy | None = None,
    ping_interval: float | None = 30.0,
    ping_timeout: float | None = 30.0,
    name: str | None = None,
    buffer_while_disconnected: bool = False,
    tracer_provider: TracerProvider | None = None,
    logger_provider: LoggerProvider | None = None,
) -> None: ...
```

**`RxWSClientGroup` must forward `retry_policy` to child clients.** The `make_client` closure must pass `retry_policy` (not the removed `conn_retry_timeout`) when constructing `RxWSClient` instances:

```python
def make_client(path: str) -> RxWSClient:
    child_config = WSConnectionConfig(
        host=config.host,
        port=config.port,
        path=path,
    )
    child_name = f"{name}:{path}" if name else None
    return RxWSClient(
        config=child_config,
        datatype=self.datatype_func(path),
        retry_policy=retry_policy,          # forwarded from group
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        name=child_name,
        buffer_while_disconnected=buffer_while_disconnected,
        tracer_provider=tracer_provider,
        logger_provider=logger_provider,
    )
```

## Test Plan

1. **All existing tests pass.** Run `pytest --cov=rxplus --cov-report=term-missing` after every split operation to ensure re-exports maintain backward compatibility.

2. **Import compatibility.** Verify that `from rxplus import RxWSServer, RxWSClient, WSConnectionState` works, and `from rxplus.ws import RxWSServer` also works.

3. **`WSConnectionConfig` validation.** Unit tests for:
   - Valid construction: `WSConnectionConfig(host="localhost", port=8080)`
   - Default path is `"/"`
   - Frozen (immutable): assigning to `.host` raises `FrozenInstanceError`

4. **`_validate_conn_cfg` removal.** Verify the function no longer exists in the codebase. Verify `tests/test_ws.py` no longer imports it (tests `test_validate_conn_cfg_*` are removed and replaced with `WSConnectionConfig` tests).

5. **`_schedule_on_loop` removal.** Verify the method no longer exists in the codebase.

6. **`WSConnectionState` rename.** Grep the test suite and codebase for any reference to `ws.ConnectionState` (not `gateway.ConnectionState`) and confirm they are updated.

7. **`WSChannels` rename.** Grep for `WS_Channels` and confirm zero references remain (including tests).

8. **`RxWSClientGroup` retry forwarding.** Write a test (or verify existing tests) that `RxWSClientGroup` passes `retry_policy` to child `RxWSClient` instances. Specifically:
   - Create a group with a custom `RetryPolicy(base_delay=5.0)`
   - Trigger client creation for a path
   - Assert the child client's `_retry_policy.base_delay == 5.0`

9. **`OTelLoggingMixin`.** Test that both `RxWSServer` and `RxWSClient` emit OTel log records through the mixin. Verify the `_log` method is not duplicated (only defined in the mixin).

10. **Type checking.** Run `mypy rxplus` and confirm zero `Any`-related errors in the modified files. Specifically verify:
    - `WSChannels.channels` is `set[ClientConnection]`
    - `Connection._duplex` is `Duplex | None`
    - `_ws_path` parameter is typed as `ClientConnection | ServerConnection`

11. **Lint.** Run `ruff check .` and `ruff format --check .` to confirm compliance.

12. **LOC check.** Verify no file in `rxplus/` exceeds ~700 LOC (use `wc -l rxplus/**/*.py`).

13. **Gateway integration.** Run any gateway-related tests to confirm `GatewayNode` still works with `WSConnectionConfig` and the restructured `ws/` package. Verify `gateway/node.py` constructs `WSConnectionConfig` objects instead of dicts at lines 176-184 and 385-386.

## Open Questions

1. **`log_context.py` absorption.** If `LogContext` is used outside `OTelLogger` by downstream consumers who import `from rxplus import LogContext`, keep the re-export in `telemetry/__init__.py` and `rxplus/__init__.py`. The implementer should verify no circular import arises from merging it into `telemetry/logger.py`.
