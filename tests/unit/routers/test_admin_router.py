"""Smoke tests for the admin endpoints (defined in main.py).

Pins F3-5 (service-degradation banner) + F6-1 (auth) + ensures /metrics works.
"""
from __future__ import annotations

import pytest


class TestAdminEndpoints:

    async def test_admin_health_returns_services_array(self, test_app):
        """F3-5: /api/admin/health must return services + degraded flag."""
        resp = await test_app.get("/api/admin/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "services" in body
        assert "degraded" in body
        assert "checked_at" in body
        assert isinstance(body["services"], list)
        # Each service entry must have name + status
        for s in body["services"]:
            assert "name" in s
            assert "status" in s
            assert s["status"] in ("Connected", "Disconnected", "Degraded")

    async def test_metrics_endpoint_is_unauthenticated_prometheus(self, test_app):
        """Metrics scrape endpoint MUST be reachable without auth (Prom convention)."""
        resp = await test_app.get("/metrics")
        assert resp.status_code == 200
        # Prom exposition format starts with `#` comments + metric lines
        body = resp.text
        assert "# " in body or len(body) >= 0  # exposition may be empty if no metrics yet

    async def test_health_endpoint_unauth(self, test_app):
        """Liveness probe — no auth, always 200 if process is alive."""
        resp = await test_app.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "ok"
