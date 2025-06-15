
from typing import Any, Callable, Literal, Optional
import time
import os

import reactivex as rx
from reactivex import Observable, Observer, Subject, create, operators as ops


def redirect_to(cond: Callable[[Any], bool], redirect_target: Observer):
    '''
    The operator redirect the items to the specified observer, and forward other items.
    '''
    def _redirect_to(source):
        def subscribe(observer, scheduler=None):
            
            def on_next(value: Any) -> None:
                if cond(value):
                    redirect_target.on_next(value)
                else:
                    observer.on_next(value)
                    
            return source.subscribe(
                on_next=on_next,
                on_error=observer.on_error,
                on_completed=observer.on_completed,
                scheduler=scheduler
            )
        
        return Observable(subscribe)
    
    return _redirect_to
