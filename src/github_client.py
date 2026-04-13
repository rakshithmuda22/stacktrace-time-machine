"""GitHub GraphQL client for file content, blame, and PR lookup.

All Git operations go through the GitHub GraphQL API, keeping the
service fully stateless with no local disk I/O. Includes exponential
backoff for rate limit handling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from src.exceptions import GitHubAPIError, GitHubRateLimitError
from src.schemas import BlameResult, PullRequestInfo

logger = logging.getLogger("stm.github")

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0

FILE_CONTENT_QUERY = """
query($owner: String!, $repo: String!, $expression: String!) {
  repository(owner: $owner, name: $repo) {
    object(expression: $expression) {
      ... on Blob {
        text
      }
    }
  }
}
"""

BLAME_QUERY = """
query($owner: String!, $repo: String!, $ref: String!, $path: String!) {
  repository(owner: $owner, name: $repo) {
    ref(qualifiedName: $ref) {
      target {
        ... on Commit {
          blame(path: $path) {
            ranges {
              startingLine
              endingLine
              commit {
                oid
                message
                author {
                  name
                  email
                  date
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

PR_LOOKUP_QUERY = """
query($owner: String!, $repo: String!, $sha: GitObjectID!) {
  repository(owner: $owner, name: $repo) {
    object(oid: $sha) {
      ... on Commit {
        associatedPullRequests(
          first: 1
          orderBy: {field: CREATED_AT, direction: DESC}
        ) {
          nodes {
            number
            title
            url
            body
            author {
              login
              avatarUrl
            }
            mergedAt
            reviews(last: 50) {
              nodes {
                body
                author {
                  login
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


class GitHubGraphQLClient:
    """Async GitHub GraphQL client for blame, file content, and PR lookup.

    Attributes:
        _token: GitHub PAT for authentication.
        _http: httpx.AsyncClient for API calls.
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._http = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"bearer {token}",
                "Content-Type": "application/json",
            },
        )

    async def get_file_content(
        self,
        repo_full_name: str,
        path: str,
        ref: str,
    ) -> str | None:
        """Fetch file content from a repository at a specific ref.

        Args:
            repo_full_name: Repository in owner/repo format.
            path: File path relative to repo root.
            ref: Git ref (branch name or commit SHA).

        Returns:
            File content as a string, or None if not found.
        """
        owner, repo = repo_full_name.split("/", 1)
        expression = f"{ref}:{path}"

        data = await self._execute_graphql(
            FILE_CONTENT_QUERY,
            {"owner": owner, "repo": repo, "expression": expression},
        )

        obj = (data.get("repository") or {}).get("object")
        if obj and "text" in obj:
            return obj["text"]
        return None

    async def get_blame_for_lines(
        self,
        repo_full_name: str,
        path: str,
        ref: str,
        start_line: int,
        end_line: int,
    ) -> BlameResult | None:
        """Get blame data for a line range using GitHub GraphQL.

        Queries the full file blame and filters to the requested
        range. Returns the most recent commit touching those lines.

        Args:
            repo_full_name: Repository in owner/repo format.
            path: File path relative to repo root.
            ref: Git ref (branch name or commit SHA).
            start_line: First line to blame (1-indexed).
            end_line: Last line to blame (1-indexed).

        Returns:
            BlameResult for the most recent commit in the range,
            or None if blame data is unavailable.
        """
        owner, repo = repo_full_name.split("/", 1)

        data = await self._execute_graphql(
            BLAME_QUERY,
            {"owner": owner, "repo": repo, "ref": ref, "path": path},
        )

        ref_data = (data.get("repository") or {}).get("ref")
        if not ref_data:
            return None

        target = (ref_data.get("target") or {})
        blame = (target.get("blame") or {})
        ranges = blame.get("ranges", [])

        overlapping = []
        for r in ranges:
            r_start = r["startingLine"]
            r_end = r["endingLine"]
            if r_start <= end_line and r_end >= start_line:
                overlapping.append(r)

        if not overlapping:
            return None

        most_recent = max(
            overlapping,
            key=lambda r: r["commit"]["author"].get("date", ""),
        )

        commit = most_recent["commit"]
        author = commit.get("author", {})

        return BlameResult(
            commit_sha=commit["oid"],
            author_name=author.get("name", "Unknown"),
            author_email=author.get("email", ""),
            commit_date=author.get("date", ""),
            commit_message=commit.get("message", ""),
            file_path=path,
            line_start=start_line,
            line_end=end_line,
        )

    async def get_pr_for_commit(
        self,
        repo_full_name: str,
        commit_sha: str,
    ) -> PullRequestInfo | None:
        """Map a commit SHA to its associated Pull Request.

        Args:
            repo_full_name: Repository in owner/repo format.
            commit_sha: Full 40-character commit SHA.

        Returns:
            PullRequestInfo if a PR is found, None otherwise.
        """
        owner, repo = repo_full_name.split("/", 1)

        data = await self._execute_graphql(
            PR_LOOKUP_QUERY,
            {"owner": owner, "repo": repo, "sha": commit_sha},
        )

        obj = (data.get("repository") or {}).get("object")
        if not obj:
            return None

        prs = (obj.get("associatedPullRequests") or {}).get("nodes", [])
        if not prs:
            return None

        pr = prs[0]
        author = pr.get("author") or {}

        review_nodes = (pr.get("reviews") or {}).get("nodes", [])
        review_comments = [
            f"@{r['author']['login']}: {r['body']}"
            for r in review_nodes
            if r.get("body") and r.get("author")
        ][:3]

        body = pr.get("body", "") or ""
        if len(body) > 500:
            body = body[:497] + "..."

        return PullRequestInfo(
            pr_number=pr["number"],
            title=pr["title"],
            url=pr["url"],
            body=body,
            author_login=author.get("login", "unknown"),
            author_avatar_url=author.get("avatarUrl"),
            merged_at=pr.get("mergedAt"),
            review_comments=review_comments,
        )

    async def _execute_graphql(
        self,
        query: str,
        variables: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a GraphQL query with retry on rate limits.

        Args:
            query: GraphQL query string.
            variables: Query variables dict.

        Returns:
            The 'data' field from the GraphQL response.

        Raises:
            GitHubRateLimitError: If rate limit exhausted after retries.
            GitHubAPIError: For other API errors.
        """
        backoff = INITIAL_BACKOFF

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self._http.post(
                    GITHUB_GRAPHQL_URL,
                    json={"query": query, "variables": variables},
                )

                if response.status_code == 429:
                    retry_after = int(
                        response.headers.get("Retry-After", backoff)
                    )
                    if attempt < MAX_RETRIES:
                        logger.warning(
                            "GitHub rate limit hit, retrying "
                            "attempt=%d retry_after=%d",
                            attempt + 1,
                            retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        backoff *= 2
                        continue
                    raise GitHubRateLimitError(
                        "GitHub API rate limit exhausted after retries"
                    )

                response.raise_for_status()
                body = response.json()

                if "errors" in body:
                    error_msg = body["errors"][0].get("message", "Unknown")
                    raise GitHubAPIError(f"GraphQL error: {error_msg}")

                return body.get("data", {})

            except httpx.HTTPStatusError as exc:
                raise GitHubAPIError(
                    f"GitHub API error: {exc.response.status_code}"
                ) from exc
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "GitHub request failed, retrying "
                        "attempt=%d error=%s",
                        attempt + 1,
                        exc,
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise GitHubAPIError(
                    f"GitHub API request failed: {exc}"
                ) from exc

        raise GitHubAPIError("Unexpected: exhausted retries without result")

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()
