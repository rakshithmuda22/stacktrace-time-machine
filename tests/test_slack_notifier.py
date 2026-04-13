"""Tests for Slack Block Kit message building and sending."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.exceptions import SlackNotifyError
from src.schemas import (
    BlameResult,
    IncidentReport,
    PullRequestInfo,
    StackFrame,
    FunctionLocation,
)
from src.slack_notifier import build_slack_blocks, send_incident_report


@pytest.fixture
def full_report() -> IncidentReport:
    """A complete incident report with all fields populated."""
    return IncidentReport(
        event_id="evt_abc123",
        issue_title="ZeroDivisionError: division by zero",
        issue_url="https://sentry.io/issues/12345/",
        frame=StackFrame(
            filename="src/calculator.py",
            function="divide",
            lineno=28,
            in_app=True,
        ),
        function_location=FunctionLocation(
            file_path="src/calculator.py",
            function_name="divide",
            start_line=26,
            end_line=30,
            source_snippet="def divide(self, a, b):\n    ...",
        ),
        blame=BlameResult(
            commit_sha="f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3",
            author_name="Jane Doe",
            author_email="jane@acme.com",
            commit_date="2024-12-15T10:30:00Z",
            commit_message="fix: handle zero division edge case",
            file_path="src/calculator.py",
            line_start=26,
            line_end=30,
        ),
        pull_request=PullRequestInfo(
            pr_number=142,
            title="fix: handle zero division edge case",
            url="https://github.com/acme/backend/pull/142",
            body="Added guard clause for division by zero.",
            author_login="janedoe",
            merged_at="2024-12-15T11:00:00Z",
            review_comments=[
                "@reviewer1: Looks good!",
                "@reviewer2: LGTM",
            ],
        ),
        ref_is_sha=True,
    )


@pytest.fixture
def blame_only_report() -> IncidentReport:
    """Report with blame but no PR."""
    return IncidentReport(
        event_id="evt_def456",
        issue_title="ValueError: invalid input",
        issue_url="https://sentry.io/issues/67890/",
        frame=StackFrame(
            filename="src/handler.py",
            function="process",
            lineno=42,
            in_app=True,
        ),
        blame=BlameResult(
            commit_sha="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            author_name="John Smith",
            author_email="john@acme.com",
            commit_date="2024-12-10T14:00:00Z",
            commit_message="feat: add input handler",
            file_path="src/handler.py",
            line_start=42,
            line_end=42,
        ),
        ref_is_sha=False,
    )


@pytest.fixture
def minimal_report() -> IncidentReport:
    """Report with only stack trace info (no blame, no PR)."""
    return IncidentReport(
        event_id="evt_ghi789",
        issue_title="RuntimeError: something went wrong",
        issue_url="https://sentry.io/issues/99999/",
        frame=StackFrame(
            filename="src/main.ts",
            function="handleRequest",
            lineno=100,
            in_app=True,
        ),
        error_message="GitHub API rate limit exceeded",
    )


class TestBuildSlackBlocks:
    """Tests for build_slack_blocks()."""

    def test_full_report_has_all_sections(
        self, full_report: IncidentReport
    ) -> None:
        """Full report should include header, trace, PR, comments, context."""
        blocks = build_slack_blocks(full_report)

        block_types = [b["type"] for b in blocks]
        assert "section" in block_types
        assert "divider" in block_types
        assert "context" in block_types
        assert len(blocks) >= 6

    def test_full_report_contains_pr_info(
        self, full_report: IncidentReport
    ) -> None:
        """Should contain PR number, author, and link."""
        blocks = build_slack_blocks(full_report)
        all_text = _extract_all_text(blocks)

        assert "#142" in all_text
        assert "janedoe" in all_text
        assert "github.com" in all_text

    def test_blame_only_shows_commit_info(
        self, blame_only_report: IncidentReport
    ) -> None:
        """Without PR, should show commit info as fallback."""
        blocks = build_slack_blocks(blame_only_report)
        all_text = _extract_all_text(blocks)

        assert "no associated pr" in all_text.lower()
        assert "a1b2c3d4" in all_text
        assert "John Smith" in all_text

    def test_minimal_report_shows_warning(
        self, minimal_report: IncidentReport
    ) -> None:
        """Without blame, should show a warning message."""
        blocks = build_slack_blocks(minimal_report)
        all_text = _extract_all_text(blocks)

        assert "could not trace" in all_text.lower()

    def test_error_message_shown_in_context(
        self, minimal_report: IncidentReport
    ) -> None:
        """Pipeline error messages should appear in the blocks."""
        blocks = build_slack_blocks(minimal_report)
        all_text = _extract_all_text(blocks)

        assert "rate limit" in all_text.lower()

    def test_function_level_blame_indicated(
        self, full_report: IncidentReport
    ) -> None:
        """Should indicate function-level blame when AST was used."""
        blocks = build_slack_blocks(full_report)
        all_text = _extract_all_text(blocks)

        assert "function-level" in all_text

    def test_line_level_blame_for_non_python(
        self, minimal_report: IncidentReport
    ) -> None:
        """Should indicate line-level blame for non-Python files."""
        blocks = build_slack_blocks(minimal_report)
        all_text = _extract_all_text(blocks)

        assert "line-level" in all_text

    def test_sha_pinned_ref_shown(
        self, full_report: IncidentReport
    ) -> None:
        """Should show SHA-pinned ref type in context."""
        blocks = build_slack_blocks(full_report)
        all_text = _extract_all_text(blocks)

        assert "SHA-pinned" in all_text

    def test_branch_based_ref_shown(
        self, blame_only_report: IncidentReport
    ) -> None:
        """Should show branch-based ref type in context."""
        blocks = build_slack_blocks(blame_only_report)
        all_text = _extract_all_text(blocks)

        assert "branch-based" in all_text


class TestSendIncidentReport:
    """Tests for send_incident_report()."""

    @pytest.mark.asyncio
    async def test_successful_send(self, full_report: IncidentReport) -> None:
        """Should return True on successful Slack webhook POST."""
        _req = httpx.Request("POST", "https://hooks.slack.com/services/test")
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.return_value = httpx.Response(
                200, text="ok", request=_req
            )

            result = await send_incident_report(
                full_report, "https://hooks.slack.com/services/test"
            )

            assert result is True
            mock_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_slack_error_raises(
        self, full_report: IncidentReport
    ) -> None:
        """Should raise SlackNotifyError on Slack API failure."""
        _req = httpx.Request("POST", "https://hooks.slack.com/services/test")
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.return_value = httpx.Response(
                500, text="error", request=_req
            )

            with pytest.raises(SlackNotifyError):
                await send_incident_report(
                    full_report, "https://hooks.slack.com/services/test"
                )


def _extract_all_text(blocks: list[dict]) -> str:
    """Extract all text content from Slack blocks for assertion."""
    texts = []
    for block in blocks:
        if "text" in block and isinstance(block["text"], dict):
            texts.append(block["text"].get("text", ""))
        if "elements" in block:
            for elem in block["elements"]:
                if isinstance(elem, dict):
                    texts.append(elem.get("text", ""))
    return " ".join(texts)
