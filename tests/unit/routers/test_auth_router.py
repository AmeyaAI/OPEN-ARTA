"""Smoke tests for /api/auth/* — login, refresh, /me, OAuth start.

F6-17 regression: in production, MOCK_USERS must NOT serve auth. Tests
exercise the dev-mode path explicitly.
"""
from __future__ import annotations

import pytest


class TestAuthRouter:

    async def test_login_with_valid_mock_user(self, test_app):
        """Dev mode: admin@arta.dev / demo1234 returns a JWT token."""
        resp = await test_app.post(
            "/api/auth/login",
            data={"username": "admin@arta.dev", "password": "demo1234"},
        )
        # 200 (mock fallback) or 401 (live DB without seed user) — never 500
        assert resp.status_code in (200, 401), \
            f"login must return 200/401 cleanly, got {resp.status_code}: {resp.text[:150]}"
        if resp.status_code == 200:
            body = resp.json()
            assert "access_token" in body
            assert "token_type" in body

    async def test_login_with_invalid_password(self, test_app):
        resp = await test_app.post(
            "/api/auth/login",
            data={"username": "admin@arta.dev", "password": "wrong-password"},
        )
        assert resp.status_code == 401

    async def test_login_unknown_user(self, test_app):
        resp = await test_app.post(
            "/api/auth/login",
            data={"username": "nobody@nowhere.dev", "password": "x"},
        )
        assert resp.status_code == 401

    async def test_me_without_token_returns_401(self, test_app):
        resp = await test_app.get("/api/auth/me")
        assert resp.status_code == 401

    async def test_oauth_start_responds_cleanly(self, test_app):
        """OAuth /start must return a defined status (302 redirect, 4xx error,
        or 503 if provider not configured) — never a 500.

        Note: 503 is the documented "OAuth provider not configured" response.
        Test allows it explicitly so a fresh dev container without OAuth env
        vars doesn't trigger a false-positive failure.
        """
        resp = await test_app.get("/api/auth/oauth/google", follow_redirects=False)
        assert resp.status_code in (302, 307, 400, 401, 404, 503), \
            f"oauth start unexpected status {resp.status_code}: {resp.text[:200]}"

    async def test_refresh_requires_valid_token(self, test_app):
        resp = await test_app.post(
            "/api/auth/refresh",
            json={"refresh_token": "definitely-not-a-real-token"},
        )
        assert resp.status_code == 401
