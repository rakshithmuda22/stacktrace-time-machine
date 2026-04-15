"""FastAPI webhook server for the Stacktrace Time Machine.

Receives Sentry webhooks, orchestrates the full blame → PR → Slack
pipeline, and handles partial failures gracefully.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from src.analyzer import (
    get_blame_range,
    is_python_file,
    locate_function,
    parse_sentry_payload,
)
from src.cache import CacheClient
from src.config import Settings, load_settings
from src.exceptions import (
    GitHubAPIError,
    PayloadParseError,
    SlackNotifyError,
)
from src.github_client import GitHubGraphQLClient
from src.schemas import FunctionLocation, IncidentReport
from src.slack_notifier import send_incident_report
from src.webhook_auth import verify_sentry_signature

logger = logging.getLogger("stm.server")

SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


def _looks_like_sha(ref: str) -> bool:
    """Check if a ref looks like a commit SHA (7-40 hex characters)."""
    return bool(SHA_PATTERN.match(ref))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared clients on startup, close on shutdown."""
    settings = load_settings()
    app.state.settings = settings
    app.state.cache = CacheClient(settings.redis_url)
    app.state.github = GitHubGraphQLClient(settings.github_token)
    app.state.start_time = time.monotonic()
    logger.info("Stacktrace Time Machine started on port %d", settings.port)
    yield
    await app.state.github.close()
    await app.state.cache.close()
    logger.info("Stacktrace Time Machine shut down")


app = FastAPI(
    title="Stacktrace Time Machine",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/api/v1/ingest")
async def ingest_sentry_webhook(
    request: Request,
    body: bytes = Depends(verify_sentry_signature),
) -> JSONResponse:
    """Receive a Sentry webhook, run the full pipeline, post to Slack.

    Pipeline steps:
    1. Parse and validate the Sentry payload
    2. Extract the top in_app stack frame + release SHA
    3. Normalize file path (strip container prefix)
    4. Resolve repo via REPO_MAP
    5. Language gate: Python → AST function-boundary blame, else exact-line
    6. GitHub GraphQL blame → commit SHA
    7. GitHub GraphQL PR lookup (cached)
    8. Build and send Slack Block Kit message

    Returns 422 for malformed payloads, 401 for bad signatures,
    200 for all other cases (even partial failures).
    """
    settings: Settings = request.app.state.settings
    github: GitHubGraphQLClient = request.app.state.github
    cache: CacheClient = request.app.state.cache

    # 1. Parse payload
    try:
        raw = json.loads(body)
        payload = parse_sentry_payload(raw)
    except (json.JSONDecodeError, PayloadParseError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # 2. Get the top in-app frame (last frame = most specific)
    frame = payload.frames[-1]

    # 3. Determine Git ref: release SHA or default branch
    ref = payload.release or settings.default_branch
    ref_is_sha = _looks_like_sha(ref)

    # 4. Normalize file path
    file_path = frame.filename
    if frame.abs_path:
        file_path = frame.abs_path
    if settings.path_strip_prefix and file_path.startswith(
        settings.path_strip_prefix
    ):
        file_path = file_path[len(settings.path_strip_prefix) :]

    # 5. Resolve repo
    repo = payload.repo_full_name or settings.repo_map.get(
        payload.project_slug
    )
    if not repo:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No repo mapping for project '{payload.project_slug}'. "
                "Set REPO_MAP in environment."
            ),
        )

    # Pipeline execution with partial failure handling
    function_location: Optional[FunctionLocation] = None
    error_message: Optional[str] = None

    # 6. Language gate + source fetch + AST + blame
    try:
        source = None
        if is_python_file(file_path):
            source = await github.get_file_content(repo, file_path, ref)
            if source:
                function_location = locate_function(
                    source, frame.function, frame.lineno
                )
                if function_location:
                    function_location.file_path = file_path

        start_line, end_line = get_blame_range(file_path, frame, source)

        # Check blame cache
        blame = await cache.get_blame_result(
            repo, file_path, ref, start_line, end_line
        )
        if not blame:
            blame = await github.get_blame_for_lines(
                repo, file_path, ref, start_line, end_line
            )
            if blame:
                await cache.set_blame_result(
                    repo,
                    file_path,
                    ref,
                    start_line,
                    end_line,
                    blame,
                    ref_is_sha=ref_is_sha,
                )
    except GitHubAPIError as exc:
        logger.warning("Blame failed: %s", exc)
        blame = None
        error_message = f"Blame lookup failed: {exc}"

    # 7. PR lookup (cached)
    pull_request = None
    if blame:
        try:
            pull_request = await cache.get_pr_info(
                repo, blame.commit_sha
            )
            if not pull_request:
                pull_request = await github.get_pr_for_commit(
                    repo, blame.commit_sha
                )
                if pull_request:
                    await cache.set_pr_info(
                        repo, blame.commit_sha, pull_request
                    )
        except GitHubAPIError as exc:
            logger.warning("PR lookup failed: %s", exc)
            error_message = f"PR lookup failed: {exc}"

    # 8. Build and send Slack message
    report = IncidentReport(
        event_id=payload.event_id,
        issue_title=payload.issue_title,
        issue_url=payload.issue_url,
        frame=frame,
        function_location=function_location,
        blame=blame,
        pull_request=pull_request,
        error_message=error_message,
        ref_is_sha=ref_is_sha,
    )

    slack_notified = False
    if settings.slack_webhook_url:
        try:
            await send_incident_report(report, settings.slack_webhook_url)
            slack_notified = True
        except SlackNotifyError as exc:
            logger.error("Slack notification failed: %s", exc)

    return JSONResponse(
        status_code=200,
        content={
            "status": "processed",
            "event_id": payload.event_id,
            "pr_found": pull_request is not None,
            "pr_number": pull_request.pr_number if pull_request else None,
            "slack_notified": slack_notified,
        },
    )


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """Health check with Redis connectivity and uptime."""
    cache: CacheClient = request.app.state.cache
    start_time: float = request.app.state.start_time

    redis_ok = await cache.health_check()

    return JSONResponse(
        content={
            "status": "healthy" if redis_ok else "degraded",
            "version": "1.0.0",
            "redis_connected": redis_ok,
            "uptime_seconds": round(time.monotonic() - start_time, 1),
        }
    )
