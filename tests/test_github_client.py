"""Tests for GitHub GraphQL client: file content, blame, and PR lookup."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.exceptions import GitHubAPIError, GitHubRateLimitError
from src.github_client import GitHubGraphQLClient

_REQ = httpx.Request("POST", "https://api.github.com/graphql")


def _resp(status: int, **kwargs) -> httpx.Response:
    """Build an httpx.Response with a request attached."""
    return httpx.Response(status, request=_REQ, **kwargs)


@pytest.fixture
def client() -> GitHubGraphQLClient:
    return GitHubGraphQLClient(token="ghp_test_token")


@pytest.fixture
def mock_post():
    """Patch httpx.AsyncClient.post for all tests."""
    with patch.object(
        httpx.AsyncClient, "post", new_callable=AsyncMock
    ) as mock:
        yield mock


class TestGetFileContent:
    """Tests for get_file_content()."""

    @pytest.mark.asyncio
    async def test_returns_file_text(self, client, mock_post) -> None:
        mock_post.return_value = _resp(
            200,
            json={
                "data": {
                    "repository": {
                        "object": {"text": "def hello():\n    pass\n"}
                    }
                }
            },
        )

        result = await client.get_file_content(
            "acme/backend", "src/app.py", "abc123"
        )
        assert result == "def hello():\n    pass\n"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, client, mock_post) -> None:
        mock_post.return_value = _resp(
            200,
            json={"data": {"repository": {"object": None}}},
        )

        result = await client.get_file_content(
            "acme/backend", "nonexistent.py", "abc123"
        )
        assert result is None


class TestGetBlameForLines:
    """Tests for get_blame_for_lines()."""

    @pytest.mark.asyncio
    async def test_returns_most_recent_commit(
        self, client, mock_post, graphql_blame_response
    ) -> None:
        mock_post.return_value = _resp(200, json=graphql_blame_response)

        result = await client.get_blame_for_lines(
            "acme/backend", "src/calculator.py", "abc123", 25, 30
        )

        assert result is not None
        assert result.commit_sha == "f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3"
        assert result.author_name == "Jane Doe"
        assert "zero division" in result.commit_message.lower()

    @pytest.mark.asyncio
    async def test_returns_none_when_no_ref(self, client, mock_post) -> None:
        mock_post.return_value = _resp(
            200,
            json={"data": {"repository": {"ref": None}}},
        )

        result = await client.get_blame_for_lines(
            "acme/backend", "src/app.py", "badref", 1, 10
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_overlapping_ranges(
        self, client, mock_post, graphql_blame_response
    ) -> None:
        mock_post.return_value = _resp(200, json=graphql_blame_response)

        result = await client.get_blame_for_lines(
            "acme/backend", "src/calculator.py", "abc123", 100, 110
        )
        assert result is None


class TestGetPrForCommit:
    """Tests for get_pr_for_commit()."""

    @pytest.mark.asyncio
    async def test_returns_pr_info(
        self, client, mock_post, graphql_pr_response
    ) -> None:
        mock_post.return_value = _resp(200, json=graphql_pr_response)

        result = await client.get_pr_for_commit(
            "acme/backend", "f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3"
        )

        assert result is not None
        assert result.pr_number == 142
        assert result.author_login == "janedoe"
        assert result.merged_at is not None
        assert len(result.review_comments) == 2

    @pytest.mark.asyncio
    async def test_returns_none_when_no_pr(self, client, mock_post) -> None:
        mock_post.return_value = _resp(
            200,
            json={
                "data": {
                    "repository": {
                        "object": {
                            "associatedPullRequests": {"nodes": []}
                        }
                    }
                }
            },
        )

        result = await client.get_pr_for_commit("acme/backend", "abc123")
        assert result is None

    @pytest.mark.asyncio
    async def test_truncates_long_pr_body(self, client, mock_post) -> None:
        long_body = "A" * 600
        mock_post.return_value = _resp(
            200,
            json={
                "data": {
                    "repository": {
                        "object": {
                            "associatedPullRequests": {
                                "nodes": [
                                    {
                                        "number": 1,
                                        "title": "Test",
                                        "url": "https://github.com/a/b/pull/1",
                                        "body": long_body,
                                        "author": {"login": "user"},
                                        "mergedAt": None,
                                        "reviews": {"nodes": []},
                                    }
                                ]
                            }
                        }
                    }
                }
            },
        )

        result = await client.get_pr_for_commit("acme/backend", "abc123")

        assert result is not None
        assert len(result.body) == 500
        assert result.body.endswith("...")


class TestRateLimitHandling:
    """Tests for rate limit retry logic."""

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self, client, mock_post) -> None:
        mock_post.side_effect = [
            _resp(429, headers={"Retry-After": "0"}),
            _resp(
                200,
                json={
                    "data": {
                        "repository": {
                            "object": {"text": "content"}
                        }
                    }
                },
            ),
        ]

        result = await client.get_file_content(
            "acme/backend", "src/app.py", "abc123"
        )

        assert result == "content"
        assert mock_post.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self, client, mock_post) -> None:
        mock_post.return_value = _resp(
            429, headers={"Retry-After": "0"}
        )

        with pytest.raises(GitHubRateLimitError):
            await client.get_file_content(
                "acme/backend", "src/app.py", "abc123"
            )


class TestGraphQLErrors:
    """Tests for GraphQL error handling."""

    @pytest.mark.asyncio
    async def test_graphql_errors_raise(self, client, mock_post) -> None:
        mock_post.return_value = _resp(
            200,
            json={
                "errors": [{"message": "Field 'bad' not found"}],
                "data": None,
            },
        )

        with pytest.raises(GitHubAPIError, match="GraphQL error"):
            await client.get_file_content(
                "acme/backend", "src/app.py", "abc123"
            )
