import os
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Literal, Optional

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
            if isinstance(log_observer, Observer):
                redirect_fun = log_observer.on_next
            else:
                redirect_fun = log_observer

            def on_next(value: Any) -> None:
                if isinstance(value, LogItem):
                    if value.level in levels:
                        redirect_fun(value)

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
    def set_super(self, obs: rx.abc.ObserverBase): ...

    @abstractmethod
    def log(self, msg: Any, level: LOG_LEVEL = "INFO"): ...

    @abstractmethod
    def get_rx_exception(self, error: Exception, note: str = "") -> RxException: ...


class EmptyLogComp(LogComp):
    def set_super(self, obs: rx.abc.ObserverBase):
        pass

    def log(self, msg: Any, level: LOG_LEVEL = "INFO"):
        pass

    def get_rx_exception(self, error: Exception, note: str = "") -> RxException:
        return RxException(error, note=note)


class NamedLogComp(LogComp):
    def __init__(self, name: str = "LogSource"):
        self.name = name
        self.super_obs: Optional[rx.abc.ObserverBase] = None

    def set_super(self, obs: rx.abc.ObserverBase):
        """
        Set the super observer to redirect the log messages.
        """
        self.super_obs = obs

    def log(self, msg: Any, level: LOG_LEVEL = "INFO"):
        """
        Log a message with the specified level.
        """
        log_item = LogItem(self.name + ": " + msg, level, self.name)
        if self.super_obs is None:
            raise Exception("Super observer is not set. Please call set_super() first.")
        self.super_obs.on_next(log_item)

    def get_rx_exception(self, error: Exception, note: str = "") -> RxException:
        """
        Get a RxException with the specified source and note.
        """
        return RxException(error, source=self.name, note=note)


class Logger(Subject):
    """
    Logger is a subject, filter and redirect logging items forward. The typical usage is to be subscribed by `print` method.

    The logger is not an observer, because it cannot be the terminal of handling errors. It will record the errors and forward them to the subscribers.

    The log file will be created when the first log item is received.
    """

    def __init__(self, logcomp: Optional[LogComp] = None, logfile_prefix: str = "log"):
        super().__init__()
        self.logfile_prefix = logfile_prefix + time.strftime(
            "_%Y%m%d_%H%M%S", time.gmtime()
        )

        if logcomp is None:
            self.logcomp = EmptyLogComp()
        else:
            self.logcomp = logcomp

        self.logcomp.set_super(super())
        self.pfile = None

    def on_next(self, value: Any) -> None:
        if isinstance(value, LogItem):
            try:
                if self.pfile is None or self.pfile.closed:
                    # ensure the folder exists
                    dir = os.path.dirname(self.logfile_prefix)
                    if dir:
                        os.makedirs(os.path.dirname(self.logfile_prefix), exist_ok=True)

                    self.pfile = open(f"{self.logfile_prefix}.log", "a")
                    self.pfile.write("\n")

                self.pfile.write(str(value))
                self.pfile.flush()
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
        super().on_error(error)
