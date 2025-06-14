
from typing import Any, Callable, Literal, Optional
import time
import os

import reactivex as rx
from reactivex import Observable, Observer, Subject, create, operators as ops



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

class Logger(Subject):
    '''
    Logger is a subject, filter and redirect logging items forward. The typical usage is to be subscribed by `print` method.
    '''
    def __init__(self, logfile_prefix: str = "log", name: str = "logger"):
        super().__init__()
        self.logfile_prefix = logfile_prefix
        self.name = name
        self.pfile = None

        self.on_next(LogItem("Logging started.", "INFO", self.name))
    
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
                print("-----------------------------------------------")
                print(LogItem(f"Logger Error: {e}", "ERROR", self.name))
                print("-----------------------------------------------")
                super().on_error(e)

    def on_completed(self) -> None:
        '''
        The logger will never be completed.
        '''
        pass

    def on_error(self, error: Exception) -> None:
        self.on_next(LogItem(f"Error Observed: {error}", "ERROR", self.name))
        super().on_error(error)
