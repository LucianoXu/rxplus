"""Miscellaneous Rx operators used by ``rxplus``."""

import os
import time
from typing import Any, Callable, Literal, Optional

import reactivex as rx
from reactivex import Observable, Observer, Subject, create
from reactivex import operators as ops

from .mechanism import RxException


def stream_print_out(prompt: str = "Stream-Print-Out"):
    """
    Print out and forward the data stream. For debug or info output purpose.
    """

    def _stream_print_out(source):

        def subscribe(observer, scheduler=None):
            def on_next(value) -> None:
                print(f"{prompt}{value}")
                observer.on_next(value)

            def on_error(error):
                print(f"Error observed: {error}")
                observer.on_error(error)

            def on_completed():
                print(f"{prompt}: Completed.")
                observer.on_completed()

            return source.subscribe(
                on_next=on_next,
                on_error=on_error,
                on_completed=on_completed,
                scheduler=scheduler,
            )

        return Observable(subscribe)

    return _stream_print_out


def redirect_to(cond: Callable[[Any], bool], redirect_target: Observer):
    """
    The operator redirect the items to the specified observer, and forward other items.
    """

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
                scheduler=scheduler,
            )

        return Observable(subscribe)

    return _redirect_to
