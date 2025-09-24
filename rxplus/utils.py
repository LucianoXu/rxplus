"""Utility helpers used across ``rxplus`` modules."""

import traceback
from typing import Callable, TypeVar, Generic

import time

from reactivex import operators as ops, Observable

TagT = TypeVar("TagT")
InnerDataT = TypeVar("InnerDataT")
class TaggedData(Generic[TagT, InnerDataT]):
    """
    A class to hold data with a tag.

    Attributes:
        tag (TagT): The tag of the data.
        data (InnerDataT): The data itself.
    """

    def __init__(self, tag: TagT, data: InnerDataT):
        self.tag = tag
        self.data = data

    def __repr__(self) -> str:
        return f"(tag={self.tag}, {repr(self.data)})"

    def __str__(self) -> str:
        return f"(tag={self.tag}, {str(self.data)})"


def untag() -> Callable[[Observable[TaggedData[object, InnerDataT]]], Observable[InnerDataT]]:
    """Return an operator that extracts the ``data`` attribute."""

    return ops.map(lambda x: x.data)  # type: ignore


def tag(tag: TagT) -> Callable[[Observable[InnerDataT]], Observable[TaggedData[TagT, InnerDataT]]]:
    """Return an operator that wraps items in :class:`TaggedData`."""

    return ops.map(lambda x: TaggedData(tag, x))


def tag_filter(tag: TagT) -> Callable[[Observable[InnerDataT]], Observable[TaggedData[TagT, InnerDataT]]]:
    """Return an operator that filters ``TaggedData`` by ``tag``. It will drop non-``TaggedData`` items."""

    return ops.filter(lambda x: isinstance(x, TaggedData) and x.tag == tag)


def get_short_error_info(e: Exception) -> str:
    """
    Get a short error information from an exception.

    Args:
        e (Exception): The exception to get the error information from.

    Returns:
        str: A short error information.
    """
    return f"{type(e).__name__}: {str(e)}"


# the function to get the full error information from an exception.
def get_full_error_info(e: Exception) -> str:
    """
    Get the full error information from an exception.

    Args:
        e (Exception): The exception to get the error information from.

    Returns:
        str: The full error information.
    """
    return "".join(traceback.format_exception(type(e), e, e.__traceback__))


class FPSMonitor:
    """Print average FPS every *interval* seconds.

    The object is *callable* so it can be used inside `ops.map` / `ops.do_action`.
    """

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._count: int = 0
        self._t0: float = time.perf_counter()

    def __call__(self, item):
        """Side effect: update counters, maybe log FPS, then pass item through."""
        self._count += 1
        now = time.perf_counter()
        elapsed = now - self._t0

        if elapsed >= self.interval:
            fps = self._count / elapsed
            print(f"[FPS] {fps:.1f}")
            self._count = 0
            self._t0 = now

        return item  # important: forward original item unchanged


# -------------------------------
class BandwidthMonitor:
    """Print average bandwidth every *interval* seconds.

    Expects items that support ``len()`` (e.g. ``bytes``). The bandwidth is
    computed over the last *interval* seconds.

    The object is *callable* so it can be used inside ``ops.map`` /
    ``ops.do_action``.
    """

    def __init__(self, interval: float = 1.0, scale: float = 1.0, unit: str = "B/s") -> None:
        """
        Args:
            interval: Time window in seconds for computing the moving average.
            scale:   Factor applied to convert raw bytes per second to another
                     unit (e.g. 1/1024 for KiB/s, 1/1_048_576 for MiB/s).
            unit:    String printed after the numeric bandwidth value.
        """
        self.interval = interval
        self.scale = scale
        self.unit = unit
        self._bytes: int = 0
        self._t0: float = time.perf_counter()

    def __call__(self, item):
        """Side effect: update counters, maybe log bandwidth, then pass item through."""
        try:
            size = len(item)
        except TypeError:
            # Item does not support len(); treat as zero-length for bandwidth.
            size = 0

        self._bytes += size
        now = time.perf_counter()
        elapsed = now - self._t0

        if elapsed >= self.interval:
            bandwidth = (self._bytes / elapsed) * self.scale
            print(f"[Bandwidth] {bandwidth:.1f} {self.unit}")
            self._bytes = 0
            self._t0 = now

        # Forward the original item unchanged
        return item
