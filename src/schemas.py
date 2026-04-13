"""Pydantic data models for the Stacktrace Time Machine pipeline.

Defines all request/response schemas and intermediate data structures
used throughout the webhook → blame → PR → Slack pipeline.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class StackFrame(BaseModel):
    """A single frame from a Sentry stack trace."""

    filename: str
    function: str
    lineno: int
    colno: Optional[int] = None
    abs_path: Optional[str] = None
    module: Optional[str] = None
    in_app: bool = True
    context_line: Optional[str] = None


class SentryWebhookPayload(BaseModel):
    """Parsed Sentry issue alert webhook.

    Attributes:
        release: The deployed commit SHA from Sentry's release
            property. Used to pin GitHub queries to the exact
            production state. None triggers fallback to DEFAULT_BRANCH.
    """

    event_id: str
    project_slug: str
    issue_title: str
    issue_url: str
    frames: List[StackFrame]
    repo_full_name: Optional[str] = None
    release: Optional[str] = None


class FunctionLocation(BaseModel):
    """Result of AST analysis: exact location of a function."""

    file_path: str
    function_name: str
    start_line: int
    end_line: int
    source_snippet: str = ""


class BlameResult(BaseModel):
    """Result of git blame for a range of lines."""

    commit_sha: str
    author_name: str
    author_email: str
    commit_date: str
    commit_message: str
    file_path: str
    line_start: int
    line_end: int


class PullRequestInfo(BaseModel):
    """PR data fetched from GitHub GraphQL."""

    pr_number: int
    title: str
    url: str
    body: str = ""
    author_login: str = ""
    author_avatar_url: Optional[str] = None
    merged_at: Optional[str] = None
    review_comments: List[str] = Field(default_factory=list)


class IncidentReport(BaseModel):
    """Complete pipeline result, ready for Slack rendering.

    Attributes:
        ref_is_sha: Whether the Git ref used was an immutable commit
            SHA (True) or a mutable branch name (False). Drives cache
            TTL decisions downstream.
    """

    event_id: str
    issue_title: str
    issue_url: str
    frame: StackFrame
    function_location: Optional[FunctionLocation] = None
    blame: Optional[BlameResult] = None
    pull_request: Optional[PullRequestInfo] = None
    error_message: Optional[str] = None
    ref_is_sha: bool = False


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str
    redis_connected: bool
    uptime_seconds: float
