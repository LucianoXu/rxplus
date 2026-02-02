"""Tests for CLI utilities."""

import pytest
from reactivex import operators as ops
from reactivex.subject import Subject

from rxplus import to_cli


class TestToCli:
    """Tests for to_cli operator."""

    def test_to_cli_passes_through_values(self):
        """Values flow downstream unchanged."""
        subject = Subject()
        results = []

        subject.pipe(to_cli()).subscribe(on_next=results.append)

        subject.on_next("hello")
        subject.on_next("world")

        assert results == ["hello", "world"]

    def test_to_cli_with_prefix_passthrough(self):
        """Values pass through unchanged even with prefix."""
        subject = Subject()
        results = []

        subject.pipe(to_cli(prefix="[test] ")).subscribe(on_next=results.append)

        subject.on_next("message")

        # Values still pass through unchanged (prefix only affects display)
        assert results == ["message"]

    def test_to_cli_completes_on_source_complete(self):
        """Completion propagates downstream."""
        subject = Subject()
        completed = []

        subject.pipe(to_cli()).subscribe(on_completed=lambda: completed.append(True))

        subject.on_completed()

        assert completed == [True]

    def test_to_cli_propagates_errors(self):
        """Errors propagate downstream."""
        subject = Subject()
        errors = []

        subject.pipe(to_cli()).subscribe(on_error=errors.append)

        test_error = Exception("test error")
        subject.on_error(test_error)

        assert len(errors) == 1
        assert errors[0] is test_error

    def test_to_cli_empty_prefix_passthrough(self):
        """Empty prefix (default) passes values through unchanged."""
        subject = Subject()
        results = []

        subject.pipe(to_cli()).subscribe(on_next=results.append)

        subject.on_next("bare message")

        assert results == ["bare message"]

    def test_to_cli_with_non_string_values(self):
        """Non-string values pass through unchanged."""
        subject = Subject()
        results = []

        subject.pipe(to_cli(prefix="num: ")).subscribe(on_next=results.append)

        subject.on_next(42)
        subject.on_next({"key": "value"})

        # Original values pass through unchanged
        assert results == [42, {"key": "value"}]

