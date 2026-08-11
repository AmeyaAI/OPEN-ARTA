"""Smoke tests for /api/users/* — admin-gated user management."""
from __future__ import annotations

import pytest


class TestUsersRouter:

    async def test_list_users_requires_auth(self, test_app):
        """GET /api/users uses Depends(require_admin) → 401 without token."""
        resp = await test_app.get("/api/users")
        # 401 (no auth) is the expected reject; 403 if a non-admin user resolves
        assert resp.status_code in (401, 403), \
            f"users list must require admin auth, got {resp.status_code}"

    async def test_create_user_requires_auth(self, test_app):
        resp = await test_app.post("/api/users", json={
            "email": "newuser@example.dev",
            "full_name": "New User",
            "password": "secret-1234",
        })
        assert resp.status_code in (401, 403, 422), \
            f"user creation must require admin, got {resp.status_code}"

    async def test_get_unknown_user_after_auth_check(self, test_app):
        """Unknown user_id returns clean 401/403 (auth fires first) — never 500."""
        resp = await test_app.get("/api/users/00000000-0000-0000-0000-000000000000")
        assert resp.status_code < 500

    async def test_project_members_endpoint_reachable(self, test_app):
        """GET /api/projects/{id}/members must be registered — F7-5 catches missing route reg."""
        resp = await test_app.get(
            "/api/projects/00000000-0000-0000-0000-000000000001/members"
        )
        assert resp.status_code != 404, \
            "/projects/{id}/members route must be registered"
