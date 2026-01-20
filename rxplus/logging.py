import os
import time
import errno
import glob
import re
from contextlib import contextmanager
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Callable, Literal, Optional, Union

import reactivex as rx
from reactivex import Observable, Observer, Subject, create
from reactivex import operators as ops

from .mechanism import RxException
from .utils import get_full_error_info

"""
The objects to deal with loggings.
"""

LOG_LEVEL = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class LogItem:
    """
    Use this term to represent the emitted log information.
    """

    def __init__(self, msg: Any, level: LOG_LEVEL = "INFO", source: str = "Unknown"):
        self.level = level
        self.timestamp_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.source = source
        self.msg = msg

    def __str__(self) -> str:
        return f"[{self.level}] {self.timestamp_str} {self.source}\t: {self.msg}\n"


def keep_log(func: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """
    A decorator to keep the log item type. Can be used to monkey-patch the function to keep the log item type.
    """

    def wrapper(x):
        if isinstance(x, LogItem):
            return x
        else:
            return func(x)

    return wrapper


def log_filter(
    levels: set[LOG_LEVEL] = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
):
    """
    The operator to filter the log items by the level.
    """
    return ops.filter(lambda log: isinstance(log, LogItem) and log.level in levels)


def drop_log():
    return ops.filter(lambda log: not isinstance(log, LogItem))


def log_redirect_to(
    log_observer: Observer|Callable,
    levels: set[LOG_LEVEL] = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
):
    """
    The operator redirect the log items to the specified observer (or function), and forward other items.
    The log items outside the specifed levels are ignored.
    """

    def _log_redirect_to(source):
        def subscribe(observer, scheduler=None):

            # Determine the redirection function
            if hasattr(log_observer, "on_next"):
                redirect_fun = log_observer.on_next
            else:
                redirect_fun = log_observer

            def on_next(value: Any) -> None:
                if isinstance(value, LogItem):
                    if value.level in levels:
                        redirect_fun(value) # type: ignore

                else:
                    observer.on_next(value)

            return source.subscribe(
                on_next=on_next,
                on_error=observer.on_error,
                on_completed=observer.on_completed,
                scheduler=scheduler,
            )

        return Observable(subscribe)

    return _log_redirect_to


class LogComp(ABC):
    """
    The abstract class for the log source, a component that can log messages.
    """

    @abstractmethod
    def set_super(self, obs: rx.abc.ObserverBase|Callable): ...

    @abstractmethod
    def log(self, msg: Any, level: LOG_LEVEL = "INFO"): ...

    @abstractmethod
    def get_rx_exception(self, error: Exception, note: str = "") -> RxException: ...


class EmptyLogComp(LogComp):
    def set_super(self, obs: rx.abc.ObserverBase|Callable):
        pass

    def log(self, msg: Any, level: LOG_LEVEL = "INFO"):
        pass

    def get_rx_exception(self, error: Exception, note: str = "") -> RxException:
        return RxException(error, note=note)


class NamedLogComp(LogComp):
    def __init__(self, name: str = "LogSource"):
        self.name = name
        self.super_obs: Optional[rx.abc.ObserverBase|Callable] = None

    def set_super(self, obs: rx.abc.ObserverBase|Callable):
        """
        Set the super observer to redirect the log messages.
        """
        self.super_obs = obs

    def log(self, msg: Any, level: LOG_LEVEL = "INFO"):
        """
        Log a message with the specified level.
        """
        log_item = LogItem(msg, level, self.name)
        if self.super_obs is None:
            raise Exception("Super observer is not set. Please call set_super() first.")
        if hasattr(self.super_obs, "on_next"):
            self.super_obs.on_next(log_item)
        else:
            self.super_obs(log_item)    # type: ignore

    def get_rx_exception(self, error: Exception, note: str = "") -> RxException:
        """
        Get a RxException with the specified source and note.
        """
        return RxException(error, source=self.name, note=note)


class Logger(Subject):
    """
    Logger is a Subject that filters, records, and forwards LogItem entries.

    Behavior
    - Writes logs to a file when `logfile` is provided, which is lazily created on
      the first log item. Each log file is named with a timestamp postfix
      (e.g., `app_20250120T103045.log`).
    - Forwards LogItem instances to its subscribers; non‑log items pass through
      unchanged via the stream where applicable.
    - Conceptually not a terminal observer: errors are recorded as LogItem and
      forwarded rather than terminating the stream.
    - Never completes (`on_completed` is a no‑op).
    - Supports automatic log rotation based on record count or time interval.
    - Optionally cleans up old log files based on age.

    Concurrency and file locking
    - Supports multiple processes writing to the same log file by serializing
      writes. Acquires an exclusive cross‑process lock before writing:
        * Uses `fcntl.flock` on POSIX platforms when available.
        * Falls back to a lockfile (`.lock`) created with `O_EXCL` and retries
          until acquired or a timeout occurs.
    - The locking ensures atomic append segments and prevents open/write races
      across processes.

    Parameters
    - logfile: Optional[str]
        If provided, enables file logging and sets the log file path base.
        The actual file will have a timestamp postfix (e.g., `app_20250120T103045.log`).
    - rotate_interval: Optional[int | timedelta], default None
        If int, rotates to a new log file after that many records.
        If timedelta, rotates after that time interval has passed since the current
        file was created. None means no rotation (a new timestamped file is created
        only at startup).
    - max_log_age: Optional[timedelta], default None
        If provided, log files older than this age will be deleted during rotation.
        The cleanup only happens when a new log file is created.
    - lock_timeout: float, default 10.0
        Maximum time to wait when acquiring the lock in the fallback mode.
    - lock_poll_interval: float, default 0.05
        Sleep interval between retries when waiting for the lock.
    """

    # Regex pattern to match timestamped log files: base_YYYYMMDDTHHmmss.ext
    _TIMESTAMP_PATTERN = re.compile(r"^(.+)_(\d{8}T\d{6})(\.[^.]+)?$")

    def __init__(
        self,
        logfile: Optional[str] = None,
        *,
        rotate_interval: Optional[Union[int, timedelta]] = None,
        max_log_age: Optional[timedelta] = None,
        lock_timeout: float = 10.0,
        lock_poll_interval: float = 0.05,
    ):
        super().__init__()
        self._logfile_base = logfile
        self._rotate_interval = rotate_interval
        self._max_log_age = max_log_age

        # Current active log file path (with timestamp)
        self._current_logfile: Optional[str] = None
        self.pfile = None

        # Rotation tracking
        self._record_count = 0
        self._file_created_time: Optional[datetime] = None

        self._lock_timeout = lock_timeout
        self._lock_poll_interval = lock_poll_interval

    @property
    def logfile(self) -> Optional[str]:
        """Return the current active log file path (for backward compatibility)."""
        return self._current_logfile

    def _generate_logfile_path(self) -> str:
        """
        Generate a new log file path with timestamp postfix.
        
        Format: base_YYYYMMDDTHHmmss.ext
        Example: app_20250120T103045.log
        """
        if self._logfile_base is None:
            raise ValueError("logfile base is not set")

        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")

        # Split base path into name and extension
        base, ext = os.path.splitext(self._logfile_base)
        return f"{base}_{timestamp}{ext}"

    def _should_rotate(self) -> bool:
        """Check if we should rotate to a new log file."""
        if self._rotate_interval is None:
            return False

        if isinstance(self._rotate_interval, int):
            return self._record_count >= self._rotate_interval

        if isinstance(self._rotate_interval, timedelta):
            if self._file_created_time is None:
                return False
            return datetime.now() - self._file_created_time >= self._rotate_interval

        return False

    def _cleanup_old_logs(self) -> None:
        """Delete log files older than max_log_age."""
        if self._max_log_age is None or self._logfile_base is None:
            return

        base, ext = os.path.splitext(self._logfile_base)
        dir_path = os.path.dirname(self._logfile_base) or "."

        # Find all matching log files
        pattern = f"{os.path.basename(base)}_*{ext}"
        log_files = glob.glob(os.path.join(dir_path, pattern))

        cutoff_time = datetime.now() - self._max_log_age

        for log_file in log_files:
            # Don't delete the current file
            if log_file == self._current_logfile:
                continue

            # Extract timestamp from filename
            filename = os.path.basename(log_file)
            match = self._TIMESTAMP_PATTERN.match(filename)
            if match:
                timestamp_str = match.group(2)
                try:
                    file_time = datetime.strptime(timestamp_str, "%Y%m%dT%H%M%S")
                    if file_time < cutoff_time:
                        try:
                            os.remove(log_file)
                        except OSError:
                            pass  # Best effort cleanup
                except ValueError:
                    pass  # Invalid timestamp format, skip

    def _rotate_file(self) -> None:
        """Close current file and prepare for a new one."""
        if self.pfile is not None and not self.pfile.closed:
            self.pfile.close()
        self.pfile = None
        self._current_logfile = None
        self._record_count = 0
        self._file_created_time = None

    def _ensure_file_open(self) -> None:
        """Ensure the log file is open, creating a new one if necessary."""
        if self._logfile_base is None:
            return

        # Check if we need to rotate
        if self._should_rotate():
            self._rotate_file()

        # Create new file if needed
        if self._current_logfile is None:
            self._current_logfile = self._generate_logfile_path()
            self._file_created_time = datetime.now()
            self._record_count = 0

            # Cleanup old logs when creating a new file
            self._cleanup_old_logs()

        # Open file if not open
        if self.pfile is None or self.pfile.closed:
            dir_name = os.path.dirname(self._current_logfile)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            self.pfile = open(self._current_logfile, "a")
            self.pfile.write("\n")

    def _lock_path(self) -> Optional[str]:
        if self._current_logfile is None:
            return None
        return f"{self._current_logfile}.lock"

    @contextmanager
    def _acquire_lock(self):
        """
        Cross-process file lock with waiting. Uses fcntl when available and
        falls back to an exclusive create + retry strategy otherwise.

        Improvements over the previous version:
        - Never swallows pre-yield exceptions (prevents "generator didn't yield").
        - Treats Windows-specific permission errors as a contention signal.
        - Performs best-effort cleanup without masking underlying failures.
        """
        lock_path = self._lock_path()
        if lock_path is None:
            # No file logging, no locking needed
            yield
            return

        dir_name = os.path.dirname(lock_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        # Try fcntl-based advisory lock first (POSIX platforms)
        if os.name != "nt":  # pragma: no cover - platform specific guard
            try:
                import fcntl  # type: ignore

                f = open(lock_path, "a+")
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    yield
                finally:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    finally:
                        f.close()
                return
            except Exception:
                # Fall back to portable lockfile with O_EXCL
                pass

        # Fallback: spin on exclusive create of lock file, then unlink on exit
        start = time.time()
        fd = None
        contended_errnos = {errno.EEXIST, errno.EACCES, errno.EPERM, errno.EBUSY}

        try:
            while True:
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    break
                except OSError as exc:
                    # Windows reports open files as PermissionError (EACCES / EPERM)
                    if exc.errno in contended_errnos:
                        if time.time() - start > self._lock_timeout:
                            raise TimeoutError(f"Timeout acquiring log lock: {lock_path}") from exc
                        time.sleep(self._lock_poll_interval)
                        continue
                    raise

            yield
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
            except PermissionError:
                # Windows may briefly deny removal if another process raced to open
                # the file after we closed our handle. Leaving the file behind is a
                # safe fallback because contention will be handled by retries.
                pass

    def on_next(self, value: Any) -> None:
        if isinstance(value, LogItem):
            try:
                if self._logfile_base is not None:
                    # Serialize writes across processes
                    # First ensure file is open (this handles rotation and cleanup)
                    self._ensure_file_open()
                    
                    with self._acquire_lock():
                        # Re-check file state after acquiring lock
                        self._ensure_file_open()
                        
                        if self.pfile is not None:
                            self.pfile.write(str(value))
                            self.pfile.flush()
                            self._record_count += 1

                super().on_next(value)

            except Exception as e:
                rx_exception = RxException(e, note="Error in Logger")
                super().on_error(rx_exception)

    def on_completed(self) -> None:
        """
        The logger will never be completed.
        """
        pass

    def on_error(self, error: Exception) -> None:
        if isinstance(error, RxException):
            logitem = LogItem(str(error), "ERROR", source=error.source)
        else:
            logitem = LogItem(str(error), "ERROR")

        # log the error
        self.on_next(logitem)
