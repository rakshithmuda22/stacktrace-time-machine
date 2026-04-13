"""Shared test fixtures for the Stacktrace Time Machine test suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sentry_webhook_payload() -> dict:
    """Raw Sentry webhook payload as a dict."""
    return json.loads((FIXTURES_DIR / "sentry_webhook.json").read_text())


@pytest.fixture
def sentry_webhook_body() -> bytes:
    """Raw Sentry webhook payload as bytes (for HMAC tests)."""
    return (FIXTURES_DIR / "sentry_webhook.json").read_bytes()


@pytest.fixture
def sample_python_source() -> str:
    """Sample Python source code for AST parsing tests."""
    return (FIXTURES_DIR / "sample_module.py").read_text()


@pytest.fixture
def graphql_blame_response() -> dict:
    """Sample GitHub GraphQL blame response."""
    return json.loads(
        (FIXTURES_DIR / "graphql_blame_response.json").read_text()
    )


@pytest.fixture
def graphql_pr_response() -> dict:
    """Sample GitHub GraphQL PR response."""
    return json.loads(
        (FIXTURES_DIR / "graphql_pr_response.json").read_text()
    )
