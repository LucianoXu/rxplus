"""Clocks of a reacting/1 source (standard library only)."""

import time
from typing import Protocol


class Clock(Protocol):
    """Time source of a reporter: Unix wall time and a monotonic clock."""

    def wall(self) -> float: ...
    def monotonic(self) -> float: ...


class SystemClock:
    """The process clocks: :func:`time.time` and :func:`time.monotonic`."""

    def wall(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """A clock that moves only when told to; for deterministic tests."""

    def __init__(self, start: float = 1_762_430_400.0) -> None:
        self._start = start
        self._offset = 0.0

    def wall(self) -> float:
        return self._start + self._offset

    def monotonic(self) -> float:
        return self._offset

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a clock does not go back")
        self._offset += seconds
