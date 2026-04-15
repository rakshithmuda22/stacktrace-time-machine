"""Redis caching layer with SHA-aware TTLs.

When the Git ref is an immutable commit SHA, cached data gets a long
TTL (30 days) since the underlying data will never change. When the
ref is a mutable branch name, short TTLs are used instead.
"""

from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis

from src.schemas import BlameResult, PullRequestInfo

logger = logging.getLogger("stm.cache")

TTL_SHA_SECONDS = 30 * 24 * 3600  # 30 days for immutable SHA-based data
TTL_BRANCH_BLAME = 3600           # 1 hour for mutable branch-based blame
TTL_BRANCH_PR = 24 * 3600         # 24 hours for branch-based PR data


class CacheClient:
    """Redis caching layer with typed get/set for blame and PR data.

    Attributes:
        _redis: Async Redis client instance.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis: aioredis.Redis = aioredis.from_url(
            redis_url, decode_responses=True
        )

    async def get_blame_result(
        self,
        repo: str,
        path: str,
        ref: str,
        start: int,
        end: int,
    ) -> Optional[BlameResult]:
        """Get a cached blame result.

        Args:
            repo: Repository in owner/repo format.
            path: File path relative to repo root.
            ref: Git ref (SHA or branch name).
            start: Start line of the blame range.
            end: End line of the blame range.

        Returns:
            Cached BlameResult or None on miss.
        """
        key = f"stm:blame:{repo}:{path}:{ref}:{start}-{end}"
        raw = await self._redis.get(key)
        if raw:
            return BlameResult.model_validate_json(raw)
        return None

    async def set_blame_result(
        self,
        repo: str,
        path: str,
        ref: str,
        start: int,
        end: int,
        result: BlameResult,
        ref_is_sha: bool = False,
    ) -> None:
        """Cache a blame result with a SHA-aware TTL.

        Args:
            repo: Repository in owner/repo format.
            path: File path relative to repo root.
            ref: Git ref (SHA or branch name).
            start: Start line of the blame range.
            end: End line of the blame range.
            result: The BlameResult to cache.
            ref_is_sha: True if ref is an immutable commit SHA.
        """
        key = f"stm:blame:{repo}:{path}:{ref}:{start}-{end}"
        ttl = TTL_SHA_SECONDS if ref_is_sha else TTL_BRANCH_BLAME
        await self._redis.set(key, result.model_dump_json(), ex=ttl)

    async def get_pr_info(
        self,
        repo: str,
        commit_sha: str,
    ) -> Optional[PullRequestInfo]:
        """Get cached PR info for a commit SHA.

        Args:
            repo: Repository in owner/repo format.
            commit_sha: The commit SHA to look up.

        Returns:
            Cached PullRequestInfo or None on miss.
        """
        key = f"stm:pr:{repo}:{commit_sha}"
        raw = await self._redis.get(key)
        if raw:
            return PullRequestInfo.model_validate_json(raw)
        return None

    async def set_pr_info(
        self,
        repo: str,
        commit_sha: str,
        pr: PullRequestInfo,
    ) -> None:
        """Cache PR info with a 30-day TTL.

        Commit-to-PR mappings are immutable, so a long TTL is safe.

        Args:
            repo: Repository in owner/repo format.
            commit_sha: The commit SHA this PR is associated with.
            pr: The PullRequestInfo to cache.
        """
        key = f"stm:pr:{repo}:{commit_sha}"
        await self._redis.set(key, pr.model_dump_json(), ex=TTL_SHA_SECONDS)

    async def health_check(self) -> bool:
        """Ping Redis and return True if connected."""
        try:
            return await self._redis.ping()
        except Exception:
            return False

    async def close(self) -> None:
        """Close the Redis connection."""
        await self._redis.close()
