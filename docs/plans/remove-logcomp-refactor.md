# [REFACTOR] Remove LogComp Interface and Simplify Logging

**Status: COMPLETED**

## Current Context
- `LogComp` is an abstract base class that components use to emit logs
- `NamedLogComp` and `EmptyLogComp` are concrete implementations
- Components like `RxWSServer`, `RxWSClient`, `RxWSClientGroup` accept `logcomp` parameter
- `LogComp` requires `set_super()` call to connect to a parent observer before use
- The interface adds indirection: components call `self.logcomp.log()` instead of directly creating log records
- With trace context now available via `contextvars`, the `LogComp` pattern is no longer necessary
- `create_log_record()` already handles trace/span injection automatically

## Requirements

### Functional Requirements
- Remove `LogComp`, `NamedLogComp`, `EmptyLogComp` classes from `logging.py`
- Update `RxWSServer`, `RxWSClient`, `RxWSClientGroup` to use `create_log_record()` directly
- Components should emit log records via `super().on_next()` directly
- Preserve all existing logging behavior (messages, levels, trace context)
- Error wrapping should use `RxException` directly instead of `logcomp.get_rx_exception()`

### Non-Functional Requirements
- Simpler API surface - fewer abstractions to learn
- Better discoverability - `create_log_record()` is the single entry point for logging
- No functional regression in WebSocket components

## Design Decisions

### 1. Direct Logging vs. Logging Component
Will remove `LogComp` and use `create_log_record()` directly because:
- Trace context is now propagated via `contextvars`, not through component hierarchy
- `create_log_record()` already includes trace/span IDs automatically
- Eliminates need for `set_super()` ceremony
- Reduces cognitive overhead for users
- One less abstraction layer between components and logging

### 2. Error Handling Pattern
Will use `RxException` directly because:
- `logcomp.get_rx_exception()` was just a wrapper around `RxException()`
- Components can construct `RxException` directly with `source` and `note`
- Clearer and more explicit error creation

### 3. Component Source Name
Will add a `_source` attribute to WS components for log source identification:
- `RxWSServer` → `"RxWSServer:{host}:{port}"`
- `RxWSClient` → `"RxWSClient:{url}"`
- `RxWSClientGroup` → uses child client source names

## Technical Design

### 1. Updated WebSocket Components Pattern

```python
# Before (with LogComp)
class RxWSServer(Subject):
    def __init__(self, conn_cfg, logcomp=None, ...):
        self.logcomp = logcomp or EmptyLogComp()
        self.logcomp.set_super(super())
    
    def some_method(self):
        self.logcomp.log("message", "INFO")
        rx_exception = self.logcomp.get_rx_exception(error, note="...")

# After (direct logging)
class RxWSServer(Subject):
    def __init__(self, conn_cfg, ...):  # No logcomp parameter
        self._source = f"RxWSServer:{conn_cfg['host']}:{conn_cfg['port']}"
    
    def _log(self, body: str, level: LOG_LEVEL = "INFO", **attrs) -> None:
        """Emit a log record to subscribers."""
        record = create_log_record(body, level, source=self._source, attributes=attrs)
        super().on_next(record)
    
    def some_method(self):
        self._log("message", "INFO")
        rx_exception = RxException(error, source=self._source, note="...")
```

### 2. Helper Method for Components

Each WS component will have a private `_log()` helper method:

```python
def _log(self, body: Any, level: LOG_LEVEL = "INFO", attributes: dict | None = None) -> None:
    """Emit a log record to subscribers with trace context."""
    record = create_log_record(
        body=body,
        level=level,
        source=self._source,
        attributes=attributes,
        include_trace=True,
    )
    super().on_next(record)
```

### 3. Files Changed

| File | Changes | Lines |
|------|---------|-------|
| `rxplus/logging.py` | Remove `LogComp`, `EmptyLogComp`, `NamedLogComp` classes | Lines 248-372 (remove entire section) |
| `rxplus/ws.py` | Remove `logcomp` parameter, add `_source` and `_log()` helper, update all `self.logcomp.log()` calls to `self._log()`, update `self.logcomp.get_rx_exception()` to direct `RxException()` | Lines 24 (imports), 172-189 (RxWSServer init), 284-462 (RxWSServer methods), 524-543 (RxWSClient init), 600-726 (RxWSClient methods), 848-860 (RxWSClientGroup) |
| `rxplus/__init__.py` | Remove `LogComp`, `EmptyLogComp`, `NamedLogComp` exports | Lines 19-22 (imports), Lines 109-111 (`__all__`) |
| `tests/test_logging.py` | Remove `LogComp`/`NamedLogComp`/`EmptyLogComp` tests | Lines 17-18 (imports), Lines 377-420 (NamedLogComp tests), Lines 556-604 (trace context with NamedLogComp tests) |
| `docs/logging.md` | Remove LogComp documentation section, update examples | Lines 36-38 (API exports), Lines 153-175 (LogComp Interface section), Lines 234-275 (Using with NamedLogComp section) |
| `docs/websocket.md` | Remove `logcomp` parameter from documentation | Line 49 |
| `docs/utilities.md` | Update RxException documentation | Line 95 |

## Implementation Plan

### Phase 1: Update WebSocket Components
1. Add `_source` attribute to `RxWSServer.__init__`
2. Add `_log()` helper method to `RxWSServer`
3. Replace all `self.logcomp.log()` calls with `self._log()` in `RxWSServer`
4. Replace all `self.logcomp.get_rx_exception()` with direct `RxException()` in `RxWSServer`
5. Remove `logcomp` parameter from `RxWSServer.__init__`
6. Repeat steps 1-5 for `RxWSClient`
7. Update `RxWSClientGroup` to remove `logcomp` parameter (clients created without it)

### Phase 2: Remove LogComp from Logging Module
1. Remove `LogComp` abstract base class
2. Remove `EmptyLogComp` class
3. Remove `NamedLogComp` class
4. Update imports in `logging.py` (remove unused `rx`, `Optional`, `TraceContext`)

### Phase 3: Update Exports
1. Remove `LogComp`, `EmptyLogComp`, `NamedLogComp` from `rxplus/__init__.py` imports
2. Remove from `__all__` list

### Phase 4: Update Tests
1. Remove imports of `NamedLogComp`, `EmptyLogComp` from `test_logging.py`
2. Remove `test_named_log_comp_*` tests
3. Remove `test_empty_log_comp` test
4. Remove `test_named_log_comp_with_trace_context*` tests

### Phase 5: Update Documentation
1. Remove LogComp section from `docs/logging.md`
2. Remove "Using with NamedLogComp" section from trace context documentation
3. Remove `logcomp` parameter from `docs/websocket.md`
4. Update `docs/utilities.md` RxException reference

## Testing Strategy

### Unit Tests
Existing tests to **remove**:
- `test_named_log_comp_creates_record`
- `test_named_log_comp_attributes`
- `test_named_log_comp_raises_without_super`
- `test_empty_log_comp`
- `test_named_log_comp_with_trace_context`
- `test_named_log_comp_with_trace_context_object`
- `test_named_log_comp_set_trace_context`

Existing tests that **remain unchanged**:
- All `create_log_record` tests (core functionality preserved)
- All `format_log_record` tests
- All `Logger` tests
- All operator tests (`log_filter`, `drop_log`, `log_redirect_to`)
- Trace context integration tests for `create_log_record`

### Integration Tests
No new integration tests needed - WebSocket component behavior is unchanged, only internal logging mechanism is simplified.

## Observability

### Logging
- Log output format unchanged
- Trace context injection unchanged
- Log levels and messages preserved
- Source names now include component type and connection info (e.g., `"RxWSServer:localhost:8765"`)

## Future Considerations

### Potential Enhancements
- Add optional `logger` parameter to WS components for custom log handling (post-MVP)
- Consider adding structured attributes for connection metadata

### Known Limitations
- Users who subclassed `LogComp` will need to migrate (breaking change)
- Components no longer accept external logger injection via `logcomp` parameter

## Dependencies
- No new dependencies
- Removes unused imports: `TraceContext` from logging module imports (kept in mechanism)

## Security Considerations
Not applicable - this is a refactoring change with no security impact.

## Rollout Strategy
- Release as part of next minor/major version
- Document breaking change in CHANGELOG
- Provide migration guide for users of `LogComp`

## References
- `rxplus/logging.py:248-372` - LogComp class definitions to remove
- `rxplus/ws.py:172-189` - RxWSServer init with logcomp
- `rxplus/ws.py:284-462` - RxWSServer methods using logcomp
- `rxplus/ws.py:524-543` - RxWSClient init with logcomp  
- `rxplus/ws.py:600-726` - RxWSClient methods using logcomp
- `docs/logging.md:153-175` - LogComp documentation section
