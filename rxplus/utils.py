"""Utility helpers used across ``rxplus`` modules."""

import time
import traceback
from collections.abc import Callable, Sized
from typing import TYPE_CHECKING, cast

from reactivex import Observable
from reactivex import operators as ops

if TYPE_CHECKING:
    from opentelemetry._logs import LoggerProvider


def _otel_report(
    name: str, logger_provider: "LoggerProvider | None"
) -> Callable[[str], None]:
    """An INFO log through OTel: ``logger_provider``, else the global
    provider (silent unless one is configured)."""
    from opentelemetry._logs import get_logger_provider

    from .telemetry import OTelLogger

    provider = logger_provider or get_logger_provider()
    logger = OTelLogger(provider.get_logger(f"rxplus.{name}"), source=name)
    return logger.info


class TaggedData[TagT, InnerDataT]:
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


def untag[InnerDataT]() -> Callable[
    [Observable[TaggedData[object, InnerDataT]]], Observable[InnerDataT]
]:
    """Return an operator that extracts the ``data`` attribute."""

    return ops.map(lambda x: x.data)  # type: ignore


def tag[TagT, InnerDataT](
    tag: TagT,
) -> Callable[[Observable[InnerDataT]], Observable[TaggedData[TagT, InnerDataT]]]:
    """Return an operator that wraps items in :class:`TaggedData`."""

    return ops.map(lambda x: TaggedData(tag, x))


def tag_filter[TagT, InnerDataT](
    tag: TagT,
) -> Callable[[Observable[InnerDataT]], Observable[TaggedData[TagT, InnerDataT]]]:
    """Return an operator that filters ``TaggedData`` by ``tag``.

    It will drop non-``TaggedData`` items."""

    return ops.filter(lambda x: isinstance(x, TaggedData) and x.tag == tag)  # type: ignore[return-value]


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
    """Report the average FPS every *interval* seconds.

    The object is *callable* so it can be used inside `ops.map` / `ops.do_action`.

    Args:
        interval: Seconds between two reports.
        report: Called with the FPS. Default: an INFO log through OTel
            (``logger_provider``, else the global provider). 0.1 printed.
        logger_provider: For the default report.
    """

    def __init__(
        self,
        interval: float = 1.0,
        report: Callable[[float], None] | None = None,
        logger_provider: "LoggerProvider | None" = None,
    ) -> None:
        self.interval = interval
        if report is None:
            log = _otel_report("FPSMonitor", logger_provider)

            def report(fps: float) -> None:
                log(f"[FPS] {fps:.1f}")

        self._report = report
        self._count: int = 0
        self._t0: float = time.perf_counter()

    def __call__[T](self, item: T) -> T:
        """Side effect: update counters, maybe log FPS, then pass item through."""
        self._count += 1
        now = time.perf_counter()
        elapsed = now - self._t0

        if elapsed >= self.interval:
            fps = self._count / elapsed
            self._report(fps)
            self._count = 0
            self._t0 = now

        return item  # important: forward original item unchanged


# -------------------------------
class BandwidthMonitor:
    """Report the average bandwidth every *interval* seconds.

    Expects items that support ``len()`` (e.g. ``bytes``). The bandwidth is
    computed over the last *interval* seconds.

    The object is *callable* so it can be used inside ``ops.map`` /
    ``ops.do_action``.
    """

    def __init__(
        self,
        interval: float = 1.0,
        scale: float = 1.0,
        unit: str = "B/s",
        report: Callable[[float], None] | None = None,
        logger_provider: "LoggerProvider | None" = None,
    ) -> None:
        """
        Args:
            interval: Time window in seconds for computing the moving average.
            scale:   Factor applied to convert raw bytes per second to another
                     unit (e.g. 1/1024 for KiB/s, 1/1_048_576 for MiB/s).
            unit:    Unit written after the value in the default report.
            report:  Called with the scaled bandwidth. Default: an INFO log
                     through OTel (``logger_provider``, else the global
                     provider). 0.1 printed.
            logger_provider: For the default report.
        """
        self.interval = interval
        self.scale = scale
        self.unit = unit
        if report is None:
            log = _otel_report("BandwidthMonitor", logger_provider)

            def report(bandwidth: float) -> None:
                log(f"[Bandwidth] {bandwidth:.1f} {self.unit}")

        self._report = report
        self._bytes: int = 0
        self._t0: float = time.perf_counter()

    def __call__[T](self, item: T) -> T:
        """Side effect: update counters, maybe log bandwidth, then pass item through."""
        # An item without a usable len() counts as zero bytes (e.g. a
        # 0-d numpy array, which has __len__ but raises TypeError).
        try:
            size = len(cast(Sized, item))
        except TypeError:
            size = 0

        self._bytes += size
        now = time.perf_counter()
        elapsed = now - self._t0

        if elapsed >= self.interval:
            bandwidth = (self._bytes / elapsed) * self.scale
            self._report(bandwidth)
            self._bytes = 0
            self._t0 = now

        # Forward the original item unchanged
        return item
