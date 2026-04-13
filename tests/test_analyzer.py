"""Tests for Sentry payload parsing, AST function location, and language gate."""

from __future__ import annotations

import pytest

from src.analyzer import (
    get_blame_range,
    is_python_file,
    locate_function,
    parse_sentry_payload,
)
from src.exceptions import PayloadParseError
from src.schemas import StackFrame


class TestParseSentryPayload:
    """Tests for parse_sentry_payload()."""

    def test_extracts_in_app_frames(self, sentry_webhook_payload: dict) -> None:
        """Should extract only in_app=True frames."""
        result = parse_sentry_payload(sentry_webhook_payload)

        assert len(result.frames) == 2
        assert all(f.in_app for f in result.frames)

    def test_extracts_release_sha(self, sentry_webhook_payload: dict) -> None:
        """Should extract the release SHA from the event."""
        result = parse_sentry_payload(sentry_webhook_payload)

        assert result.release == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"

    def test_extracts_event_metadata(self, sentry_webhook_payload: dict) -> None:
        """Should extract event_id, issue_title, and issue_url."""
        result = parse_sentry_payload(sentry_webhook_payload)

        assert result.event_id == "evt_abc123def456"
        assert "ZeroDivisionError" in result.issue_title

    def test_missing_release_returns_none(
        self, sentry_webhook_payload: dict
    ) -> None:
        """When release is absent, it should be None."""
        del sentry_webhook_payload["data"]["event"]["release"]
        result = parse_sentry_payload(sentry_webhook_payload)

        assert result.release is None

    def test_no_exception_values_raises(self) -> None:
        """Payload with no exception values should raise PayloadParseError."""
        payload = {
            "data": {
                "event": {
                    "event_id": "test",
                    "title": "Error",
                    "exception": {"values": []},
                }
            }
        }
        with pytest.raises(PayloadParseError, match="No exception values"):
            parse_sentry_payload(payload)

    def test_no_in_app_frames_raises(self) -> None:
        """Payload with only non-in_app frames should raise."""
        payload = {
            "data": {
                "event": {
                    "event_id": "test",
                    "title": "Error",
                    "exception": {
                        "values": [
                            {
                                "stacktrace": {
                                    "frames": [
                                        {
                                            "filename": "lib.py",
                                            "function": "f",
                                            "lineno": 1,
                                            "in_app": False,
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                }
            }
        }
        with pytest.raises(PayloadParseError, match="No in_app frames"):
            parse_sentry_payload(payload)

    def test_malformed_payload_raises(self) -> None:
        """Completely malformed payload should raise PayloadParseError."""
        with pytest.raises(PayloadParseError):
            parse_sentry_payload({"bad": "data"})


class TestLocateFunction:
    """Tests for locate_function() AST parsing."""

    def test_finds_top_level_function(self, sample_python_source: str) -> None:
        """Should find a top-level function by name."""
        result = locate_function(sample_python_source, "top_level_function", 9)

        assert result is not None
        assert result.function_name == "top_level_function"
        assert result.start_line == 8
        assert result.end_line == 10

    def test_finds_class_method(self, sample_python_source: str) -> None:
        """Should find a method inside a class."""
        result = locate_function(sample_python_source, "divide", 28)

        assert result is not None
        assert result.function_name == "divide"
        assert result.start_line <= 28 <= result.end_line

    def test_disambiguates_nested_vs_top_level(
        self, sample_python_source: str
    ) -> None:
        """When two functions share a name, pick the one closest to target_line."""
        nested_result = locate_function(
            sample_python_source, "inner_function", 42
        )
        assert nested_result is not None
        assert nested_result.start_line < 50

        top_level_result = locate_function(
            sample_python_source, "inner_function", 48
        )
        assert top_level_result is not None

    def test_finds_async_function(self, sample_python_source: str) -> None:
        """Should find async function definitions."""
        result = locate_function(sample_python_source, "async_handler", 58)

        assert result is not None
        assert result.function_name == "async_handler"

    def test_missing_function_returns_none(
        self, sample_python_source: str
    ) -> None:
        """A function name not in the source should return None."""
        result = locate_function(
            sample_python_source, "nonexistent_function", 1
        )

        assert result is None

    def test_syntax_error_returns_none(self) -> None:
        """Source with syntax errors should return None, not raise."""
        bad_source = "def broken(\n    pass pass pass"
        result = locate_function(bad_source, "broken", 1)

        assert result is None

    def test_includes_source_snippet(self, sample_python_source: str) -> None:
        """Returned FunctionLocation should include a source snippet."""
        result = locate_function(sample_python_source, "divide", 28)

        assert result is not None
        assert len(result.source_snippet) > 0
        assert "divide" in result.source_snippet


class TestIsPythonFile:
    """Tests for is_python_file()."""

    @pytest.mark.parametrize(
        "path",
        ["app.py", "src/server.py", "/usr/src/app/main.py", "script.pyw"],
    )
    def test_python_files(self, path: str) -> None:
        assert is_python_file(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "app.js",
            "main.ts",
            "server.go",
            "Main.java",
            "lib.rs",
            "style.css",
        ],
    )
    def test_non_python_files(self, path: str) -> None:
        assert is_python_file(path) is False


class TestGetBlameRange:
    """Tests for get_blame_range() language gate."""

    def test_python_with_source_returns_function_range(
        self, sample_python_source: str
    ) -> None:
        """Python files with source should return function boundaries."""
        frame = StackFrame(
            filename="calculator.py",
            function="divide",
            lineno=28,
            in_app=True,
        )
        start, end = get_blame_range(
            "src/calculator.py", frame, sample_python_source
        )

        assert start < end
        assert start <= 28 <= end

    def test_python_without_source_returns_exact_line(self) -> None:
        """Python files without source should fall back to exact line."""
        frame = StackFrame(
            filename="calculator.py",
            function="divide",
            lineno=28,
            in_app=True,
        )
        start, end = get_blame_range("src/calculator.py", frame, source=None)

        assert start == 28
        assert end == 28

    def test_non_python_returns_exact_line(self) -> None:
        """Non-Python files should always return exact line blame."""
        frame = StackFrame(
            filename="handler.ts",
            function="handleRequest",
            lineno=42,
            in_app=True,
        )
        start, end = get_blame_range(
            "src/handler.ts", frame, source="function handleRequest() {}"
        )

        assert start == 42
        assert end == 42

    def test_python_with_missing_function_falls_back(
        self, sample_python_source: str
    ) -> None:
        """If AST can't find the function, fall back to exact line."""
        frame = StackFrame(
            filename="calculator.py",
            function="nonexistent",
            lineno=15,
            in_app=True,
        )
        start, end = get_blame_range(
            "src/calculator.py", frame, sample_python_source
        )

        assert start == 15
        assert end == 15
