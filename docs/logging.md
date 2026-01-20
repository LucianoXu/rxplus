# Logging

Structured logging that flows through reactive pipelines.

## Design Intention

Traditional loggers write directly to files or consoles, which doesn't compose well with reactive streams. `rxplus` logging treats log entries as first-class stream items (`LogItem`) that can be filtered, redirected, and recorded using standard Rx operators.

Components that produce logs implement `LogComp`, allowing log output to be injected into any observer.

## API

```python
from rxplus import (
    LogItem, LOG_LEVEL, Logger,
    LogComp, NamedLogComp, EmptyLogComp,
    log_filter, log_redirect_to, drop_log, keep_log,
)
```

### `LogItem`

Represents a single log entry:

| Field | Type | Description |
|-------|------|-------------|
| `level` | `LOG_LEVEL` | `"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"`, `"CRITICAL"` |
| `timestamp_str` | `str` | ISO 8601 UTC timestamp |
| `source` | `str` | Component name |
| `msg` | `Any` | Log message |

### `Logger`

A `Subject` that filters and persists `LogItem` entries to a file while forwarding them to subscribers.

```python
from datetime import timedelta

# Basic usage - creates timestamped log file (e.g., app_20250120T103045.log)
logger = Logger(logfile="app.log")
logger.subscribe(lambda item: print(item))
logger.on_next(LogItem("Started", "INFO", "Main"))

# With rotation every 1000 records
logger = Logger(logfile="app.log", rotate_interval=1000)

# With rotation every hour
logger = Logger(logfile="app.log", rotate_interval=timedelta(hours=1))

# With automatic cleanup of logs older than 7 days
logger = Logger(
    logfile="app.log",
    rotate_interval=timedelta(hours=1),
    max_log_age=timedelta(days=7),
)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `logfile` | `str \| None` | `None` | Base path for log files. Files are created with timestamp postfix (e.g., `app_20250120T103045.log`). |
| `rotate_interval` | `int \| timedelta \| None` | `None` | When to rotate to a new file. `int` = after N records, `timedelta` = after time elapsed. `None` = no rotation. |
| `max_log_age` | `timedelta \| None` | `None` | Delete log files older than this age when rotating. `None` = no cleanup. |
| `lock_timeout` | `float` | `10.0` | Timeout for acquiring file lock. |
| `lock_poll_interval` | `float` | `0.05` | Poll interval when waiting for lock. |

**Features:**

- Lazy file creation on first log
- Each log file includes a timestamp in the filename for uniqueness
- Automatic log rotation based on record count or time interval
- Optional cleanup of old log files based on age
- Cross-process file locking (uses `fcntl` on POSIX, lockfile fallback on Windows)
- Errors converted to `LogItem` and forwarded (never terminates the stream)
- `on_completed()` is a no-op to keep the logger alive

### `LogComp` Interface

Components that log implement this abstract class:

```python
class LogComp(ABC):
    def set_super(self, obs): ...       # Connect to parent observer
    def log(self, msg, level="INFO"): ...
    def get_rx_exception(self, error, note=""): ...
```

**Implementations:**

- `NamedLogComp(name)` — Logs with a source name
- `EmptyLogComp()` — Silent; useful for testing

### Operators

| Operator | Purpose |
|----------|---------|
| `log_filter(levels)` | Keep only `LogItem`s matching specified levels |
| `log_redirect_to(observer, levels)` | Route matching logs to another observer, forward other items |
| `drop_log()` | Remove all `LogItem`s from the stream |
| `keep_log(func)` | Decorator: applies `func` only to non-log items |

**Example: Separate logs from data**

```python
stream.pipe(
    log_redirect_to(logger, {"WARNING", "ERROR"}),
    drop_log(),  # only data flows downstream
).subscribe(process_data)
```
