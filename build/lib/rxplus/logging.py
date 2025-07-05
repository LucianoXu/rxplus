
from abc import ABC, abstractmethod
from typing import Any, Callable, Literal, Optional
import time
import os

import reactivex as rx
from reactivex import Observable, Observer, Subject, create, operators as ops

from .utils import get_full_error_info



def stream_print_out(name: str = "Stream-Print-Out"):
    '''
    Print out and forward the data stream. For debug or info output purpose.
    '''
    def _stream_print_out(source):

        def subscribe(observer, scheduler=None):
            def on_next(value) -> None:
                print(f"{name}:")
                print(value)
                observer.on_next(value)

            def on_error(error):
                print(f"{name}: {type(error)} Error: {error}")
                observer.on_error(error)

            def on_completed():
                print(f"{name}: Completed.")
                observer.on_completed()

            return source.subscribe(
                on_next=on_next,
                on_error=on_error,
                on_completed=on_completed,
                scheduler=scheduler
            )
        
        return Observable(subscribe)
    
    return _stream_print_out

'''
The objects to deal with loggings.
'''

LOG_LEVEL = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

class LogItem:
    '''
    Use this term to represent the emitted log information.
    '''
    def __init__(self, msg: Any, level: LOG_LEVEL = "INFO", source: str = 'UNKNOWN'):
        self.level = level
        self.timestamp_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.source = source
        self.msg = msg

    def __str__(self) -> str:
        return f"[{self.level}] {self.timestamp_str} {self.source}\t: {self.msg}\n"
    
def log_filter(levels: set[LOG_LEVEL] = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}):
    '''
    The operator to filter the log items by the level.
    '''
    return ops.filter(lambda log: isinstance(log, LogItem) and log.level in levels)


def drop_log():
    return ops.filter(lambda log: not isinstance(log, LogItem))



def log_redirect_to(log_observer: Observer, levels: set[LOG_LEVEL] = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}):
    '''
    The operator redirect the log items to the specified observer, and forward other items.
    The log items outside the specifed levels are ignored.
    '''

    def _log_redirect_to(source):
        def subscribe(observer, scheduler=None):
            
            def on_next(value: Any) -> None:
                if isinstance(value, LogItem):
                    if value.level in levels:
                        log_observer.on_next(value)
                    
                else:
                    observer.on_next(value)
                    
            return source.subscribe(
                on_next=on_next,
                on_error=observer.on_error,
                on_completed=observer.on_completed,
                scheduler=scheduler
            )
        
        return Observable(subscribe)
    
    return _log_redirect_to


class LogComp(ABC):
    '''
    The abstract class for the log source, a component that can log messages.
    '''
    @abstractmethod
    def set_super(self, obs: Observer):
        ...

    @abstractmethod
    def log(self, msg: Any, level: LOG_LEVEL = "INFO"):
        ...

class EmptyLogComp(LogComp):
    def set_super(self, obs: Observer):
        pass

    def log(self, msg: Any, level: LOG_LEVEL = "INFO"):
        pass

class NamedLogComp(LogComp):
    def __init__(self, name: str = "LogSource"):
        self.name = name
        self.super_obs: Optional[Observer] = None

    def set_super(self, obs: Observer):
        '''
        Set the super observer to redirect the log messages.
        '''
        self.super_obs = obs

    def log(self, msg: Any, level: LOG_LEVEL = "INFO"):
        '''
        Log a message with the specified level.
        '''
        log_item = LogItem(self.name + ": " + msg, level, self.name)
        if self.super_obs is None:
            raise Exception("Super observer is not set. Please call set_super() first.")
        self.super_obs.on_next(log_item)
        

class Logger(Subject):
    '''
    Logger is a subject, filter and redirect logging items forward. The typical usage is to be subscribed by `print` method.
    '''
    def __init__(self, 
            logcomp: Optional[LogComp] = None,
            logfile_prefix: str = "log"):
        super().__init__()
        self.logfile_prefix = logfile_prefix
        if logcomp is None:
            logcomp = EmptyLogComp()
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
                self.logcomp.log(f"Error in Logger:\n{get_full_error_info(e)}", "ERROR")
                super().on_error(e)

    def on_completed(self) -> None:
        '''
        The logger will never be completed.
        '''
        pass

    def on_error(self, error: Exception) -> None:
        self.logcomp.log(f"Error Observed:\n{get_full_error_info(error)}", "ERROR")
        super().on_error(error)
