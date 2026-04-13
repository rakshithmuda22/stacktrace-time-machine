"""Sentry webhook HMAC signature verification.

Provides a FastAPI dependency that validates the sentry-hook-signature
header against the request body using HMAC-SHA256. Rejects requests
with invalid or missing signatures to prevent fake payload spam.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request

from src.exceptions import WebhookAuthError


async def verify_sentry_signature(
    request: Request,
) -> bytes:
    """FastAPI dependency that verifies the Sentry webhook HMAC signature.

    Reads the raw request body, computes HMAC-SHA256 using the
    configured SENTRY_CLIENT_SECRET, and compares it to the value
    in the sentry-hook-signature header.

    Args:
        request: The incoming FastAPI request.

    Returns:
        The raw request body bytes (for downstream parsing).

    Raises:
        HTTPException: 401 if the signature is missing or invalid.
    """
    body = await request.body()
    secret: str = request.app.state.settings.sentry_client_secret

    if not secret:
        return body

    signature = request.headers.get("sentry-hook-signature")
    if not signature:
        raise HTTPException(
            status_code=401,
            detail="Missing sentry-hook-signature header",
        )

    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )

    return body
