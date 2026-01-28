# LogSink Source-to-Scope Mapping Design Document

## Current Context

- `LogSink` is a Subject that receives API `LogRecord` objects flowing through Rx streams
- LogRecords carry a `log.source` attribute identifying the originating component (e.g., "HTTPServer", "WebSocket")
- Currently, `LogSink` uses a **single fixed logger**: `provider.get_logger("rxplus.logsink")` to emit all records
- This means all logs appear under one instrumentation scope regardless of their source
- OTel best practice: each logical component should have its own instrumentation scope for proper filtering/routing in backends

## Requirements

### Functional Requirements
- Map `log.source` attribute to OTel instrumentation scopes at emission time
- Cache SDK loggers to avoid repeated `get_logger()` calls
- Preserve the "log as data flow" design philosophy

### Non-Functional Requirements
- Minimal memory overhead for logger cache
- Thread-safe logger cache access (loggers may be created from multiple threads)
- No performance regression for single-source applications

## Design Decisions

### 1. Source-to-Scope Mapping Strategy
Will implement **lazy caching with dict lookup** because:
- Simple and efficient - O(1) lookup after first occurrence
- Memory-bounded - number of unique sources is typically small
- No configuration needed - automatically derives scope from `log.source`
- Fallback available - use default scope if `log.source` is missing

### 2. Logger Cache Location
Will implement cache **inside LogSink instance** because:
- Each LogSink has its own provider, so caches should be provider-specific
- Allows different LogSink instances to have independent caches
- Garbage collection cleans up cache when LogSink is disposed

### 3. Scope Name Format
Will use `rxplus.{source}` format because:
- Maintains `rxplus` namespace prefix for identification
- Directly uses source name for clarity (e.g., `rxplus.HTTPServer`)
- Consistent with existing `rxplus.logsink` naming

## Technical Design

### 1. Core Components

```python
class LogSink(Subject):
    """LogSink with source-to-scope mapping."""
    
    def __init__(
        self,
        otel_provider: LoggerProvider | None = None,
        otel_exporter: LogRecordExporter | None = None,
        resource: Resource | None = None,
    ):
        super().__init__()
        # ... existing setup ...
        
        # New: Cache for source -> SDK logger mapping
        self._logger_cache: dict[str, Any] = {}
        self._default_scope = "rxplus.default"
    
    def _get_logger_for_source(self, source: str) -> Any:
        """Get or create SDK logger for the given source."""
        if source not in self._logger_cache:
            scope_name = f"rxplus.{source}" if source else self._default_scope
            self._logger_cache[source] = self._otel_provider.get_logger(scope_name)
        return self._logger_cache[source]
    
    def on_next(self, value: Any) -> None:
        if isinstance(value, LogRecord):
            try:
                if self._otel_provider is not None:
                    # Extract source from attributes
                    source = (
                        value.attributes.get("log.source", "")
                        if value.attributes
                        else ""
                    )
                    # Get scoped logger and emit
                    logger = self._get_logger_for_source(source)
                    logger.emit(value)
                
                super().on_next(value)
            except Exception as e:
                # ... existing error handling ...
```

### 2. Data Models
No new data models required. Uses existing:
- `LogRecord` from `opentelemetry._logs`
- `log.source` attribute convention (already established)

### 3. Integration Points
- **Input**: LogRecords with `log.source` attribute (no change)
- **Output**: Emissions to SDK loggers with proper instrumentation scope
- **OTel Backend**: Will see logs organized by scope (e.g., Jaeger, Grafana)

### 4. Files Changed
- `rxplus/logging.py` (lines 192-242, LogSink class)
- `tests/test_logging.py` (add new test cases)

## Implementation Plan

1. **Phase 1: Add Logger Cache**
   - Add `_logger_cache: dict[str, Any]` to `LogSink.__init__`
   - Add `_default_scope` constant
   - Remove single `_otel_logger` attribute

2. **Phase 2: Implement Scope Resolution**
   - Add `_get_logger_for_source(source: str)` method
   - Handle empty/missing source with fallback to default scope

3. **Phase 3: Update Emission Logic**
   - Modify `on_next` to extract `log.source` from record
   - Use `_get_logger_for_source` to get appropriate logger
   - Emit through scoped logger

4. **Phase 4: Update Documentation**
   - Update LogSink docstring
   - Add usage example showing scope behavior

## Testing Strategy

### Unit Tests
- `test_logsink_creates_scoped_logger_for_source`: Verify different sources get different loggers
- `test_logsink_caches_loggers`: Verify same source reuses cached logger
- `test_logsink_handles_missing_source`: Verify fallback to default scope
- `test_logsink_handles_empty_source`: Verify empty string source gets default scope

### Integration Tests
- Not required for this change (internal refactor with same external behavior)

## Observability

### Logging
- No additional logging needed - this is the logging infrastructure itself

### Metrics
- Not applicable for this change

## Future Considerations

### Potential Enhancements
- Add configurable scope name format (e.g., `{prefix}.{source}` template)
- Add logger cache size limit with LRU eviction for extreme cases
- Add scope aliasing (map multiple sources to same scope)

### Known Limitations
- Logger cache grows unbounded (acceptable for typical source count < 100)
- Source name must be a valid scope identifier (no validation added)
- No thread-safety added (Python GIL provides sufficient safety for dict operations)

## Dependencies
- `opentelemetry-sdk` (existing dependency)
- No new dependencies required

## Security Considerations
Not applicable - no security impact from this internal refactoring.

## Rollout Strategy
Not applicable - internal refactoring with no staged rollout needed.

## References
- [rxplus/logging.py:192-242](rxplus/logging.py:192-242) - Current LogSink implementation
- [OTel Logging Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/) - Instrumentation scope semantics
- [docs/logging.md](docs/logging.md) - Current logging documentation
