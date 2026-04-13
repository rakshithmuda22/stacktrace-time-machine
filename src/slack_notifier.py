"""Slack Block Kit message builder and webhook sender.

Builds rich incident context messages with PR attribution and posts
them to Slack. Handles partial data gracefully — if PR or blame info
is unavailable, the message degrades to show whatever is available.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.exceptions import SlackNotifyError
from src.schemas import BlameResult, IncidentReport, PullRequestInfo

logger = logging.getLogger("stm.slack")


async def send_incident_report(
    report: IncidentReport,
    webhook_url: str,
) -> bool:
    """Send a rich Slack Block Kit message for an incident.

    Args:
        report: Complete or partial pipeline result.
        webhook_url: Slack incoming webhook URL.

    Returns:
        True if the message was sent successfully.

    Raises:
        SlackNotifyError: If the Slack API returns an error.
    """
    blocks = build_slack_blocks(report)
    payload = {"blocks": blocks}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(webhook_url, json=payload)
            if response.status_code >= 400:
                raise SlackNotifyError(
                    f"Slack returned status {response.status_code}"
                )
            logger.info(
                "Slack notification sent event_id=%s",
                report.event_id,
            )
            return True
    except httpx.RequestError as exc:
        raise SlackNotifyError(
            f"Failed to send Slack notification: {exc}"
        ) from exc


def build_slack_blocks(report: IncidentReport) -> list[dict[str, Any]]:
    """Build Block Kit blocks from an IncidentReport.

    Handles partial data gracefully: if PR is None, shows only
    commit info. If blame is None, shows only stack trace info.

    Args:
        report: The incident report to render.

    Returns:
        List of Slack Block Kit block dicts.
    """
    blocks: list[dict[str, Any]] = []

    blocks.append(_build_header_block(report))
    blocks.append(_build_stacktrace_block(report))
    blocks.append({"type": "divider"})

    if report.blame and report.pull_request:
        blocks.append(
            _build_pr_block(report.pull_request, report.blame)
        )
        if report.pull_request.body:
            blocks.append(_build_pr_description_block(report.pull_request))
        if report.pull_request.review_comments:
            blocks.append(
                _build_review_comments_block(report.pull_request)
            )
    elif report.blame:
        blocks.append(_build_commit_only_block(report.blame))
    else:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":warning: Could not trace this error to a specific "
                    "commit. The file may be new or the blame data is "
                    "unavailable."
                ),
            },
        })

    if report.error_message:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":warning: Pipeline note: {report.error_message}",
                }
            ],
        })

    blocks.append(_build_context_block(report))

    return blocks


def _build_header_block(report: IncidentReport) -> dict[str, Any]:
    """Error title and Sentry link header."""
    title = report.issue_title
    if len(title) > 100:
        title = title[:97] + "..."

    text = f":rotating_light: *<{report.issue_url}|{title}>*"

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }


def _build_stacktrace_block(report: IncidentReport) -> dict[str, Any]:
    """File, function, and line number section."""
    frame = report.frame
    blame_type = "function-level" if report.function_location else "line-level"

    text = (
        f"*File:* `{frame.filename}`\n"
        f"*Function:* `{frame.function}`\n"
        f"*Line:* {frame.lineno}\n"
        f"*Blame type:* {blame_type}"
    )

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }


def _build_pr_block(
    pr: PullRequestInfo, blame: BlameResult
) -> dict[str, Any]:
    """PR title, author, link, and commit info section."""
    text = (
        f"*Introduced in PR:* <{pr.url}|#{pr.pr_number} {pr.title}>\n"
        f"*Author:* @{pr.author_login}\n"
        f"*Merged:* {pr.merged_at or 'N/A'}\n"
        f"*Commit:* `{blame.commit_sha[:8]}` — {blame.commit_message}"
    )

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }


def _build_pr_description_block(pr: PullRequestInfo) -> dict[str, Any]:
    """Truncated PR description as a quote block."""
    body = pr.body
    if len(body) > 300:
        body = body[:297] + "..."

    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f">{body.replace(chr(10), chr(10) + '>')}",
        },
    }


def _build_review_comments_block(pr: PullRequestInfo) -> dict[str, Any]:
    """Top review comments section."""
    comments_text = "\n".join(
        f"• {comment}" for comment in pr.review_comments[:3]
    )

    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Key Review Comments:*\n{comments_text}",
        },
    }


def _build_commit_only_block(blame: BlameResult) -> dict[str, Any]:
    """Fallback block when no PR is found for the commit."""
    text = (
        f":mag: *Commit found but no associated PR*\n"
        f"*Commit:* `{blame.commit_sha[:8]}`\n"
        f"*Author:* {blame.author_name} ({blame.author_email})\n"
        f"*Date:* {blame.commit_date}\n"
        f"*Message:* {blame.commit_message}"
    )

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }


def _build_context_block(report: IncidentReport) -> dict[str, Any]:
    """Footer context with event ID and metadata."""
    ref_type = "SHA-pinned" if report.ref_is_sha else "branch-based"

    elements = [
        {
            "type": "mrkdwn",
            "text": (
                f":mag: Sentry Event `{report.event_id}` | "
                f"Ref: {ref_type}"
            ),
        }
    ]

    return {"type": "context", "elements": elements}
