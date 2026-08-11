"""Smoke tests for /api/settings/* — LLM provider switching + reads."""
from __future__ import annotations

import os

import pytest


class TestSettingsRouter:

    async def test_get_llm_settings_returns_resolved_provider(self, test_app):
        resp = await test_app.get("/api/settings/llm")
        # 200 (resolved) or 503 (no client) — pin no-500 contract
        assert resp.status_code in (200, 503), \
            f"settings/llm must return 200/503, got {resp.status_code}"
        if resp.status_code == 200:
            body = resp.json()
            assert "provider" in body or "current" in body, \
                f"settings/llm shape changed: {body}"

    async def test_unauth_put_settings_returns_401(self, test_app):
        os.environ["ARTA_API_KEY"] = "f7-test-key"
        try:
            resp = await test_app.put("/api/settings/llm", json={
                "provider": "ollama", "model": "qwen3:8b",
            })
            # PUT /api/settings/llm should be authed (mutating endpoint)
            assert resp.status_code in (401, 403, 422), \
                f"expected 401/403/422, got {resp.status_code}"
        finally:
            os.environ.pop("ARTA_API_KEY", None)
