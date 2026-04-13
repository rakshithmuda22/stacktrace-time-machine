"""Custom exception hierarchy for the Stacktrace Time Machine.

Each pipeline stage raises a specific exception type, allowing the
server to handle failures gracefully and send partial results to Slack.
"""

from __future__ import annotations


class TimeMachineError(Exception):
    """Base exception for all pipeline errors."""


class WebhookAuthError(TimeMachineError):
    """HMAC signature verification failed."""


class PayloadParseError(TimeMachineError):
    """Sentry webhook payload is malformed or missing required fields."""


class GitHubAPIError(TimeMachineError):
    """Generic GitHub API error."""


class GitHubRateLimitError(GitHubAPIError):
    """GitHub API rate limit exhausted after retries."""


class SlackNotifyError(TimeMachineError):
    """Failed to send Slack notification."""
