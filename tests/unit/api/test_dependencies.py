"""F12-1: regression test for the SSE-friendly query-param auth path.

Browser EventSource cannot send custom headers, so `require_api_key` must
also accept `?api_key=...` and `?token=<jwt>` query params. Without this
the live-status SSE returns 401 and the user sees a stale "Generating
tests..." UI even when the job is running fine.
"""
from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

from src.api.dependencies import require_api_key


@pytest.fixture
def with_api_key(monkeypatch):
    monkeypatch.setenv("ARTA_API_KEY", "f12-test-key")
    yield "f12-test-key"


@pytest.fixture
def without_api_key(monkeypatch):
    monkeypatch.delenv("ARTA_API_KEY", raising=False)
    yield


class TestRequireApiKeyHeaderPaths:
    """Pre-existing header-based paths must keep working.

    Each call passes ALL kwargs explicitly because FastAPI's `Query(None)`
    sentinel is the parameter DEFAULT — when called outside FastAPI we
    must pass `None` ourselves.
    """

    async def test_x_api_key_header_match_passes(self, with_api_key):
        # No exception → success
        await require_api_key(x_api_key=with_api_key, authorization=None,
                              api_key=None, token=None)

    async def test_authorization_bearer_api_key_passes(self, with_api_key):
        await require_api_key(x_api_key=None, authorization=f"Bearer {with_api_key}",
                              api_key=None, token=None)

    async def test_no_credentials_raises_401(self, with_api_key):
        with pytest.raises(HTTPException) as exc:
            await require_api_key(x_api_key=None, authorization=None,
                                  api_key=None, token=None)
        assert exc.value.status_code == 401

    async def test_dev_mode_no_api_key_set_passes(self, without_api_key):
        # When ARTA_API_KEY is empty, dep is a no-op
        await require_api_key(x_api_key=None, authorization=None,
                              api_key=None, token=None)


class TestRequireApiKeyQueryParam:
    """F12-1: SSE clients send credential as query param."""

    async def test_query_api_key_match_passes(self, with_api_key):
        # No exception → success
        await require_api_key(
            x_api_key=None, authorization=None,
            api_key=with_api_key, token=None,
        )

    async def test_query_api_key_mismatch_raises_401(self, with_api_key):
        with pytest.raises(HTTPException) as exc:
            await require_api_key(
                x_api_key=None, authorization=None,
                api_key="wrong-value", token=None,
            )
        assert exc.value.status_code == 401

    async def test_query_token_valid_jwt_passes(self, with_api_key, monkeypatch):
        # Build a valid JWT signed with the same secret the dep uses
        from src.api import dependencies as deps_mod
        if deps_mod.jwt is None:
            pytest.skip("python-jose not installed in this venv")
        good_jwt = deps_mod.jwt.encode(
            {"sub": "test@arta.dev"},
            deps_mod._JWT_SECRET,
            algorithm=deps_mod._JWT_ALGORITHM,
        )
        await require_api_key(
            x_api_key=None, authorization=None,
            api_key=None, token=good_jwt,
        )

    async def test_query_token_invalid_jwt_raises_401(self, with_api_key):
        with pytest.raises(HTTPException) as exc:
            await require_api_key(
                x_api_key=None, authorization=None,
                api_key=None, token="not-a-valid-jwt",
            )
        assert exc.value.status_code == 401

    async def test_query_param_takes_precedence_over_no_header(self, with_api_key):
        # The actual SSE call shape: no headers, only query param
        await require_api_key(
            x_api_key=None, authorization=None,
            api_key=with_api_key, token=None,
        )
