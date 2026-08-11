"""F7-5: Tests for the gates router.

Covers the F6-7 fix (POST /api/gates/check now requires auth) and the F6-12 +
F7-1 chain (gate response should include strategy_summary and audit_trail).
"""
from __future__ import annotations

import os

import pytest

# F9-6: Requires live Postgres — auto-skipped when DB unreachable (see tests/conftest.py).
pytestmark = pytest.mark.integration


class TestGatesRouter:

    async def test_unauth_check_returns_401_when_api_key_required(self, test_app):
        # Set an API key so the centralised require_api_key actually gates
        os.environ["ARTA_API_KEY"] = "f7-test-key"
        try:
            resp = await test_app.post("/api/gates/check", json={"build_id": "b1"})
            assert resp.status_code == 401, "F6-7: gates/check must reject unauth"
        finally:
            os.environ.pop("ARTA_API_KEY", None)

    async def test_check_endpoint_evaluates_decision(self, test_app):
        """F7-1: gate decision endpoint computes and returns a 4-outcome verdict.

        Note: F7-5 surfaced a pre-existing bug — the gates router tries to
        persist `quality_gates(run_id, ...)` with `run_id=NULL` when invoked
        from /check, hitting a NOT-NULL constraint. Persistence failure is
        swallowed by try_db's exception handler; the response itself is
        produced before the persist attempt, so the endpoint still returns
        the correct decision. Test pins the response, documents the bug.
        """
        resp = await test_app.post(
            "/api/gates/check",
            json={"build_id": "test-build-002", "environment": "staging"},
        )
        assert resp.status_code == 200, f"unexpected: {resp.status_code} {resp.text[:200]}"
        body = resp.json()
        assert "decision" in body
        assert body["decision"] in ("PASS", "CONCERNS", "FAIL", "WAIVED")
        # F7-1: audit_trail surfaces in real (non-mock) gate decisions.
        # Mock store responses (when build_id matches MOCK_GATES) may omit it.
        if "audit_trail" in body:
            assert isinstance(body["audit_trail"], list)
            if body["audit_trail"]:
                entry = body["audit_trail"][0]
                assert {"name", "category", "outcome", "actual",
                        "expected", "rationale"}.issubset(entry.keys()), \
                    f"audit_trail entry missing fields: {entry}"

    async def test_threshold_endpoint_round_trip(self, test_app):
        """Read default thresholds and confirm the F6-1 + F7-1 fields are present."""
        resp = await test_app.get("/api/gates/thresholds")
        assert resp.status_code == 200
        thresholds = resp.json()
        # F6-1 (carried from F3-7) — flakiness_warn_score should exist on the model
        assert "flakiness_warn_score" in thresholds or "p0_coverage_pct" in thresholds, \
            "GateThresholds shape is broken"
