"""Sentry payload parsing, AST function locator, and language gate.

Handles two responsibilities:
1. Parsing Sentry webhook JSON into structured data.
2. Locating function boundaries in Python source via the ast module,
   with a language gate that falls back to exact-line blame for
   non-Python files.
"""

from __future__ import annotations

import ast
import logging
from typing import Any

from src.schemas import (
    FunctionLocation,
    SentryWebhookPayload,
    StackFrame,
)
from src.exceptions import PayloadParseError

logger = logging.getLogger("stm.analyzer")

PYTHON_EXTENSIONS = {".py", ".pyw"}


def parse_sentry_payload(raw: dict[str, Any]) -> SentryWebhookPayload:
    """Extract structured data from a raw Sentry webhook payload.

    Parses the nested event structure, filters to in_app frames only,
    and extracts the release SHA if present.

    Args:
        raw: The raw JSON-parsed webhook body.

    Returns:
        SentryWebhookPayload with extracted frames and metadata.

    Raises:
        PayloadParseError: If required fields are missing or malformed.
    """
    try:
        event = raw["data"]["event"]
        event_id = event.get("event_id", "unknown")
        issue_title = event.get("title", "Unknown Error")
        issue_url = event.get("web_url", "")
        release = event.get("release")

        exception_values = event.get("exception", {}).get("values", [])
        if not exception_values:
            raise PayloadParseError("No exception values in payload")

        all_frames: list[StackFrame] = []
        for exc_value in exception_values:
            raw_frames = (
                exc_value.get("stacktrace", {}).get("frames", [])
            )
            for f in raw_frames:
                frame = StackFrame(
                    filename=f.get("filename", ""),
                    function=f.get("function", ""),
                    lineno=f.get("lineno", 0),
                    colno=f.get("colno"),
                    abs_path=f.get("abs_path"),
                    module=f.get("module"),
                    in_app=f.get("in_app", False),
                    context_line=f.get("context_line"),
                )
                if frame.in_app:
                    all_frames.append(frame)

        if not all_frames:
            raise PayloadParseError("No in_app frames found in stack trace")

        return SentryWebhookPayload(
            event_id=event_id,
            project_slug=raw.get("data", {})
            .get("event", {})
            .get("project", "")
            or _extract_project_slug(raw),
            issue_title=issue_title,
            issue_url=issue_url,
            frames=all_frames,
            release=release,
        )

    except (KeyError, TypeError) as exc:
        raise PayloadParseError(f"Malformed Sentry payload: {exc}") from exc


def _extract_project_slug(raw: dict[str, Any]) -> str:
    """Try alternate paths to find the project slug."""
    try:
        return raw["data"]["event"]["project"]
    except (KeyError, TypeError):
        pass
    try:
        return raw["data"]["triggered_rule"].split()[0].lower()
    except (KeyError, TypeError, IndexError, AttributeError):
        return "unknown"


def locate_function(
    source: str,
    function_name: str,
    target_line: int,
) -> FunctionLocation | None:
    """Find a function definition in Python source code using AST.

    Walks the AST to find all FunctionDef and AsyncFunctionDef nodes
    (including class methods via nested ClassDef traversal). When
    multiple functions share the same name, picks the one closest to
    target_line.

    Args:
        source: Python source code string.
        function_name: Name of the function to locate.
        target_line: Line number from the stack trace for disambiguation.

    Returns:
        FunctionLocation if found, None otherwise.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        logger.warning("Failed to parse source: SyntaxError")
        return None

    candidates: list[dict[str, Any]] = []
    _collect_functions(tree, candidates)

    matches = [c for c in candidates if c["name"] == function_name]
    if not matches:
        return None

    best = _find_closest(matches, target_line)

    source_lines = source.splitlines()
    snippet_lines = source_lines[best["start"] - 1 : best["start"] + 4]
    snippet = "\n".join(snippet_lines)

    return FunctionLocation(
        file_path="",
        function_name=function_name,
        start_line=best["start"],
        end_line=best["end"],
        source_snippet=snippet,
    )


def _collect_functions(
    node: ast.AST,
    results: list[dict[str, Any]],
    class_name: str | None = None,
) -> None:
    """Recursively collect all function definitions from an AST.

    Args:
        node: Current AST node.
        results: Accumulator for found functions.
        class_name: Name of the enclosing class, if any.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            _collect_functions(child, results, class_name=child.name)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_line = _get_end_line(child)
            results.append({
                "name": child.name,
                "start": child.lineno,
                "end": end_line,
                "class": class_name,
            })
            _collect_functions(child, results, class_name=class_name)


def _get_end_line(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Get the last line number of a function definition."""
    if hasattr(node, "end_lineno") and node.end_lineno is not None:
        return node.end_lineno
    max_line = node.lineno
    for child in ast.walk(node):
        if hasattr(child, "lineno") and child.lineno is not None:
            max_line = max(max_line, child.lineno)
    return max_line


def _find_closest(
    matches: list[dict[str, Any]],
    target_line: int,
) -> dict[str, Any]:
    """Pick the function definition closest to the target line.

    Prefers the function whose body contains the target line. If
    none contain it, picks the one with the nearest start line.

    Args:
        matches: List of function dicts with start/end lines.
        target_line: Line number to match against.

    Returns:
        The best-matching function dict.
    """
    containing = [
        m for m in matches
        if m["start"] <= target_line <= m["end"]
    ]
    if containing:
        return min(containing, key=lambda m: m["end"] - m["start"])

    return min(matches, key=lambda m: abs(m["start"] - target_line))


def is_python_file(file_path: str) -> bool:
    """Check whether a file path has a Python extension.

    Args:
        file_path: The file path to check.

    Returns:
        True if the file is a Python file.
    """
    lower = file_path.lower()
    return any(lower.endswith(ext) for ext in PYTHON_EXTENSIONS)


def get_blame_range(
    file_path: str,
    frame: StackFrame,
    source: str | None = None,
) -> tuple[int, int]:
    """Determine the line range to blame for a stack frame.

    Language gate: Python files with available source get full
    function-boundary detection via AST. All other files fall
    back to exact-line blame.

    Args:
        file_path: Git-relative file path.
        frame: The stack frame to analyze.
        source: Python source code string (None for non-Python files
            or when source fetch failed).

    Returns:
        Tuple of (start_line, end_line) for the blame query.
    """
    if is_python_file(file_path) and source:
        location = locate_function(source, frame.function, frame.lineno)
        if location:
            return (location.start_line, location.end_line)

    return (frame.lineno, frame.lineno)
