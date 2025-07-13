"""Core error types for :mod:`rxplus`."""


class RxException(Exception):
    """Base class for all RxPlus exceptions."""

    def __init__(self, exception: Exception, source: str = "Unknown", note: str = ""):
        super().__init__(f"<{source}> {note}: {exception}")
        self.exception = exception
        self.source = source
        self.note = note

    def __str__(self):
        return f"<{self.source}> {self.note}: {self.exception}"
