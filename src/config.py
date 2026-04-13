"""Application configuration loaded from environment variables.

Uses python-dotenv to load a .env file if present, then reads
settings from the environment with sensible defaults.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Application settings populated from environment variables.

    Attributes:
        github_token: GitHub PAT with repo scope for GraphQL API.
        sentry_client_secret: Secret for verifying Sentry webhook HMAC.
        slack_webhook_url: Slack incoming webhook URL.
        redis_url: Redis connection string.
        repo_map: Mapping of Sentry project slugs to GitHub owner/repo.
        path_strip_prefix: Prefix stripped from Sentry file paths to
            produce Git-relative paths (e.g. "/usr/src/app/").
        default_branch: Fallback Git ref when Sentry release SHA is
            missing.
        port: Server listen port.
        log_level: Logging verbosity.
    """

    def __init__(self) -> None:
        self.github_token: str = os.environ.get("GITHUB_TOKEN", "")
        self.sentry_client_secret: str = os.environ.get(
            "SENTRY_CLIENT_SECRET", ""
        )
        self.slack_webhook_url: str = os.environ.get("SLACK_WEBHOOK_URL", "")
        self.redis_url: str = os.environ.get(
            "REDIS_URL", "redis://localhost:6379"
        )
        self.path_strip_prefix: str = os.environ.get(
            "PATH_STRIP_PREFIX", ""
        )
        self.default_branch: str = os.environ.get("DEFAULT_BRANCH", "main")
        self.port: int = int(os.environ.get("PORT", "8000"))
        self.log_level: str = os.environ.get("LOG_LEVEL", "INFO")

        raw_map = os.environ.get("REPO_MAP", "")
        self.repo_map: dict[str, str] = {}
        if raw_map:
            for entry in raw_map.split(","):
                entry = entry.strip()
                if ":" in entry:
                    slug, repo = entry.split(":", 1)
                    self.repo_map[slug.strip()] = repo.strip()


def load_settings() -> Settings:
    """Create and return a Settings instance."""
    return Settings()
