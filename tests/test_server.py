"""Integration tests for the FastAPI server pipeline."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from src.server import app


@pytest.fixture
def settings():
    """Minimal Settings-like object for testing."""
    return SimpleNamespace(
        github_token="ghp_test",
        sentry_client_secret="test-secret",
        slack_webhook_url="https://hooks.slack.com/test",
        redis_url="redis://localhost:6379",
        repo_map={"my-project": "acme/backend"},
        path_strip_prefix="/usr/src/app/",
        default_branch="main",
        port=8000,
        log_level="INFO",
    )


@pytest.fixture
def mock_cache():
    """Mock CacheClient."""
    cache = AsyncMock()
    cache.get_blame_result = AsyncMock(return_value=None)
    cache.set_blame_result = AsyncMock()
    cache.get_pr_info = AsyncMock(return_value=None)
    cache.set_pr_info = AsyncMock()
    cache.health_check = AsyncMock(return_value=True)
    cache.close = AsyncMock()
    return cache


@pytest.fixture
def mock_github():
    """Mock GitHubGraphQLClient."""
    github = AsyncMock()
    github.close = AsyncMock()
    return github


@pytest.fixture
def client(settings, mock_cache, mock_github):
    """TestClient with mocked dependencies."""
    app.state.settings = settings
    app.state.cache = mock_cache
    app.state.github = mock_github
    app.state.start_time = 0.0
    return TestClient(app, raise_server_exceptions=False)


def _sign(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature for webhook verification."""
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()


def _make_payload(
    project: str = "my-project",
    release: str = "abc123def456abc123def456abc123def456abc123",
) -> dict:
    """Build a minimal but valid Sentry webhook payload."""
    return {
        "data": {
            "event": {
                "event_id": "evt_test_123",
                "title": "TestError: something broke",
                "release": release,
                "project": project,
                "web_url": "https://sentry.io/issues/1/",
                "exception": {
                    "values": [
                        {
                            "type": "TestError",
                            "value": "something broke",
                            "stacktrace": {
                                "frames": [
                                    {
                                        "filename": "/usr/src/app/src/handler.py",
                                        "function": "process",
                                        "lineno": 42,
                                        "abs_path": "/usr/src/app/src/handler.py",
                                        "in_app": True,
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        }
    }


class TestIngestEndpoint:
    """Tests for POST /api/v1/ingest."""

    def test_full_happy_path(
        self, client, mock_github, mock_cache, settings
    ) -> None:
        """Full pipeline: parse → blame → PR → Slack → 200."""
        from src.schemas import BlameResult, PullRequestInfo

        mock_github.get_file_content = AsyncMock(
            return_value="def process():\n    pass\n"
        )
        mock_github.get_blame_for_lines = AsyncMock(
            return_value=BlameResult(
                commit_sha="deadbeef" * 5,
                author_name="Dev",
                author_email="dev@co.com",
                commit_date="2024-01-01T00:00:00Z",
                commit_message="fix: thing",
                file_path="src/handler.py",
                line_start=42,
                line_end=42,
            )
        )
        mock_github.get_pr_for_commit = AsyncMock(
            return_value=PullRequestInfo(
                pr_number=99,
                title="fix: thing",
                url="https://github.com/acme/backend/pull/99",
                author_login="dev",
            )
        )

        payload = _make_payload()
        body = json.dumps(payload).encode()
        sig = _sign(body, settings.sentry_client_secret)

        with patch(
            "src.slack_notifier.send_incident_report",
            new_callable=AsyncMock,
            return_value=True,
        ):
            response = client.post(
                "/api/v1/ingest",
                content=body,
                headers={
                    "sentry-hook-signature": sig,
                    "Content-Type": "application/json",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "processed"
        assert data["pr_found"] is True
        assert data["pr_number"] == 99

    def test_malformed_payload_returns_422(self, client, settings) -> None:
        """Malformed JSON should return 422."""
        body = json.dumps({"bad": "data"}).encode()
        sig = _sign(body, settings.sentry_client_secret)

        response = client.post(
            "/api/v1/ingest",
            content=body,
            headers={
                "sentry-hook-signature": sig,
                "Content-Type": "application/json",
            },
        )

        assert response.status_code == 422

    def test_bad_signature_returns_401(self, client) -> None:
        """Invalid HMAC signature should return 401."""
        body = json.dumps(_make_payload()).encode()

        response = client.post(
            "/api/v1/ingest",
            content=body,
            headers={
                "sentry-hook-signature": "bad_sig",
                "Content-Type": "application/json",
            },
        )

        assert response.status_code == 401

    def test_missing_repo_mapping_returns_422(
        self, client, settings
    ) -> None:
        """Unknown project slug with no REPO_MAP entry should return 422."""
        payload = _make_payload(project="unknown-project")
        body = json.dumps(payload).encode()
        sig = _sign(body, settings.sentry_client_secret)

        response = client.post(
            "/api/v1/ingest",
            content=body,
            headers={
                "sentry-hook-signature": sig,
                "Content-Type": "application/json",
            },
        )

        assert response.status_code == 422
        assert "repo mapping" in response.json()["detail"].lower()

    def test_github_failure_returns_partial(
        self, client, mock_github, settings
    ) -> None:
        """GitHub API failure should still return 200 with partial data."""
        from src.exceptions import GitHubAPIError

        mock_github.get_file_content = AsyncMock(
            side_effect=GitHubAPIError("rate limit")
        )

        payload = _make_payload()
        body = json.dumps(payload).encode()
        sig = _sign(body, settings.sentry_client_secret)

        with patch(
            "src.slack_notifier.send_incident_report",
            new_callable=AsyncMock,
            return_value=True,
        ):
            response = client.post(
                "/api/v1/ingest",
                content=body,
                headers={
                    "sentry-hook-signature": sig,
                    "Content-Type": "application/json",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["pr_found"] is False

    def test_missing_release_uses_default_branch(
        self, client, mock_github, mock_cache, settings
    ) -> None:
        """When release is None, should fall back to DEFAULT_BRANCH."""
        mock_github.get_file_content = AsyncMock(return_value=None)
        mock_github.get_blame_for_lines = AsyncMock(return_value=None)

        payload = _make_payload()
        payload["data"]["event"]["release"] = None
        body = json.dumps(payload).encode()
        sig = _sign(body, settings.sentry_client_secret)

        with patch(
            "src.slack_notifier.send_incident_report",
            new_callable=AsyncMock,
            return_value=True,
        ):
            response = client.post(
                "/api/v1/ingest",
                content=body,
                headers={
                    "sentry-hook-signature": sig,
                    "Content-Type": "application/json",
                },
            )

        assert response.status_code == 200
        # Verify blame was called with default branch
        if mock_github.get_blame_for_lines.called:
            call_args = mock_github.get_blame_for_lines.call_args
            assert call_args[0][2] == "main" or call_args[1].get("ref") == "main"

    def test_non_python_file_skips_ast(
        self, client, mock_github, mock_cache, settings
    ) -> None:
        """Stack trace from a .js file should skip AST and blame exact line."""
        from src.schemas import BlameResult

        mock_github.get_blame_for_lines = AsyncMock(
            return_value=BlameResult(
                commit_sha="abc12345" * 5,
                author_name="Dev",
                author_email="dev@co.com",
                commit_date="2024-01-01T00:00:00Z",
                commit_message="fix: js thing",
                file_path="src/handler.js",
                line_start=10,
                line_end=10,
            )
        )
        mock_github.get_pr_for_commit = AsyncMock(return_value=None)

        payload = _make_payload()
        payload["data"]["event"]["exception"]["values"][0]["stacktrace"][
            "frames"
        ] = [
            {
                "filename": "/usr/src/app/src/handler.js",
                "function": "handleRequest",
                "lineno": 10,
                "abs_path": "/usr/src/app/src/handler.js",
                "in_app": True,
            }
        ]
        body = json.dumps(payload).encode()
        sig = _sign(body, settings.sentry_client_secret)

        with patch(
            "src.slack_notifier.send_incident_report",
            new_callable=AsyncMock,
            return_value=True,
        ):
            response = client.post(
                "/api/v1/ingest",
                content=body,
                headers={
                    "sentry-hook-signature": sig,
                    "Content-Type": "application/json",
                },
            )

        assert response.status_code == 200
        # Should NOT have called get_file_content for non-Python
        mock_github.get_file_content.assert_not_called()


class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_healthy_when_redis_connected(self, client, mock_cache) -> None:
        """Should return healthy when Redis is connected."""
        mock_cache.health_check = AsyncMock(return_value=True)

        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["redis_connected"] is True
        assert "uptime_seconds" in data
        assert data["version"] == "1.0.0"

    def test_degraded_when_redis_down(self, client, mock_cache) -> None:
        """Should return degraded when Redis is unreachable."""
        mock_cache.health_check = AsyncMock(return_value=False)

        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["redis_connected"] is False
