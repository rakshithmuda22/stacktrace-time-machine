"""Tests for Sentry webhook HMAC signature verification."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.webhook_auth import verify_sentry_signature


def _make_request(body: bytes, signature: str | None, secret: str) -> AsyncMock:
    """Build a mock FastAPI Request with the given body and signature."""
    request = AsyncMock()
    request.body = AsyncMock(return_value=body)
    request.headers = {}
    if signature is not None:
        request.headers["sentry-hook-signature"] = signature
    request.app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(sentry_client_secret=secret)
        )
    )
    return request


def _compute_signature(body: bytes, secret: str) -> str:
    """Compute the expected HMAC-SHA256 hex digest."""
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()


@pytest.mark.asyncio
async def test_valid_signature_passes(sentry_webhook_body: bytes) -> None:
    """A correctly signed request should return the raw body."""
    secret = "test-secret-key"
    sig = _compute_signature(sentry_webhook_body, secret)
    request = _make_request(sentry_webhook_body, sig, secret)

    result = await verify_sentry_signature(request)

    assert result == sentry_webhook_body


@pytest.mark.asyncio
async def test_tampered_body_fails() -> None:
    """Modifying the body after signing should raise 401."""
    secret = "test-secret-key"
    original_body = b'{"event": "original"}'
    sig = _compute_signature(original_body, secret)
    tampered_body = b'{"event": "tampered"}'
    request = _make_request(tampered_body, sig, secret)

    with pytest.raises(Exception) as exc_info:
        await verify_sentry_signature(request)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_signature_header_fails() -> None:
    """A request without the signature header should raise 401."""
    secret = "test-secret-key"
    body = b'{"event": "test"}'
    request = _make_request(body, signature=None, secret=secret)

    with pytest.raises(Exception) as exc_info:
        await verify_sentry_signature(request)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_signature_fails() -> None:
    """A request with the wrong signature value should raise 401."""
    secret = "test-secret-key"
    body = b'{"event": "test"}'
    request = _make_request(body, signature="badhex", secret=secret)

    with pytest.raises(Exception) as exc_info:
        await verify_sentry_signature(request)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_empty_secret_skips_verification() -> None:
    """When SENTRY_CLIENT_SECRET is empty, skip verification."""
    body = b'{"event": "test"}'
    request = _make_request(body, signature=None, secret="")

    result = await verify_sentry_signature(request)

    assert result == body
