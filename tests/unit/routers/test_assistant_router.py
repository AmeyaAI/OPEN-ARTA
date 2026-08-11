"""Smoke tests for /api/assistant/* — chat + slash commands."""
from __future__ import annotations

import os

import pytest


class TestAssistantRouter:

    async def test_unauth_chat_returns_401(self, test_app):
        os.environ["ARTA_API_KEY"] = "f7-test-key"
        try:
            resp = await test_app.post(
                "/api/assistant/chat",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            assert resp.status_code == 401, \
                f"assistant/chat must require auth, got {resp.status_code}"
        finally:
            os.environ.pop("ARTA_API_KEY", None)

    async def test_unauth_command_returns_401(self, test_app):
        os.environ["ARTA_API_KEY"] = "f7-test-key"
        try:
            resp = await test_app.post(
                "/api/assistant/command",
                json={"command": "/help", "args": []},
            )
            assert resp.status_code == 401
        finally:
            os.environ.pop("ARTA_API_KEY", None)

    async def test_command_help_in_dev_mode(self, test_app):
        """Dev mode (no API key) — slash commands should respond cleanly.

        503 = "LLM client not initialised" (the documented dev-mode response
        added by the fix to the bug this test surfaced — previously a raw
        AttributeError produced a 500). 200 = command dispatched. 422 = bad
        shape. All acceptable; only an unhandled 500 would indicate a regression.
        """
        resp = await test_app.post(
            "/api/assistant/command",
            json={"command": "/help"},
        )
        assert resp.status_code in (200, 422, 503), \
            f"/command unexpected status {resp.status_code}: {resp.text[:200]}"

    # ── R320 refinement copilot ────────────────────────────────────────────
    async def test_chat_provider_robust_no_500(self, test_app):
        """/chat must resolve the client via getattr + 503 (mirrors /command),
        never a raw 500 AttributeError when app.state.anthropic is unset."""
        resp = await test_app.post(
            "/api/assistant/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code in (200, 503), \
            f"/chat unexpected status {resp.status_code}: {resp.text[:200]}"

    async def test_refine_draft_classifies_without_writing(self, test_app):
        """Draft mode returns a provenance verdict + proposed fact and writes
        nothing (the trust gate)."""
        resp = await test_app.post("/api/assistant/refine", json={
            "project_id": "00000000-0000-0000-0000-000000000000",
            "test_id": "TC-X", "requirement_id": "REQ-X", "tool": "newman",
            "kind": "endpoint", "method": "GET",
            "to_value": "/v1/regions/zz-none/never/seen/here-xyz",
            "correction_text": "real route", "confirm": False,
        })
        assert resp.status_code == 200, resp.text[:200]
        d = resp.json()
        assert d["mode"] == "draft"
        assert d["verdict"] in ("human_knowledge", "arta_knew")
        assert d["proposed_fact"]["kind"] == "endpoint"

    async def test_corrections_list_ok(self, test_app):
        resp = await test_app.get(
            "/api/assistant/corrections"
            "?project_id=00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 200
        assert "corrections" in resp.json()

    async def test_corrections_analytics_shape(self, test_app):
        resp = await test_app.get(
            "/api/assistant/corrections/analytics"
            "?project_id=00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 200
        d = resp.json()
        for k in ("total", "arta_knew", "human_knowledge", "arta_defect_rate",
                  "top_corrected_endpoints"):
            assert k in d

    async def test_verify_missing_correction_no_500(self, test_app):
        # No DB in unit env → 503; DB present + not found → 404. Never a 500.
        resp = await test_app.post("/api/assistant/corrections/corr-nope/verify")
        assert resp.status_code in (404, 503), resp.text[:200]


def test_correction_repo_sanitize_coerces_uuid_and_drops_iso_created_at():
    """R320 — the repo must coerce a str project_id → UUID and DROP an ISO-string
    created_at (let the model default fire) — the exact type mismatch that 500'd
    the Exploratory router."""
    import uuid as _u
    from src.db.repository import TestCorrectionRepo
    bad = TestCorrectionRepo._sanitize({
        "project_id": "not-a-uuid", "created_at": "2026-08-06T00:00:00+00:00",
        "kind": "endpoint"})
    assert bad["project_id"] is None
    assert "created_at" not in bad
    good = _u.uuid4()
    ok = TestCorrectionRepo._sanitize({"project_id": str(good)})
    assert ok["project_id"] == good
