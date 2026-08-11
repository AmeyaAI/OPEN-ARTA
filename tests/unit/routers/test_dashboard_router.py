"""Smoke tests for /api/dashboard.

Pin the contract that frontend's `DashboardData` (F5-9) depends on so a
backend response-shape regression breaks the test rather than the UI.
"""
from __future__ import annotations

import pytest

# F9-6: Requires live Postgres — auto-skipped when DB unreachable (see tests/conftest.py).
pytestmark = pytest.mark.integration


class TestDashboardEndpoint:

    async def test_dashboard_returns_required_fields(self, test_app):
        resp = await test_app.get("/api/dashboard")
        assert resp.status_code == 200
        body = resp.json()
        # Fields the frontend dashboard.tsx + DashboardData type require:
        for field in ("quality_score", "coverage_pct", "pass_rate",
                      "open_defects", "p0_defects", "total_scenarios",
                      "active_agents", "pipeline"):
            assert field in body, f"dashboard response missing {field!r}"
        # F5-9 added these — they may be None when no requirements exist
        for field in ("p0_count", "p1_count", "p0_coverage_pct", "p1_coverage_pct"):
            assert field in body, f"F5-9: dashboard missing {field!r}"
        assert isinstance(body["pipeline"], dict)
        assert "stages" in body["pipeline"]
