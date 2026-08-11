"""Smoke tests for /api/reports/* — run/coverage/compliance export."""
from __future__ import annotations

import pytest


class TestReportsRouter:

    async def test_run_export_unknown_run_404_or_405(self, test_app):
        """Export of an unknown run must respond cleanly, not 500."""
        resp = await test_app.get("/api/reports/runs/nonexistent-run-id/export")
        assert resp.status_code < 500, \
            f"reports must handle unknown run cleanly, got {resp.status_code}"

    async def test_coverage_export_returns_file(self, test_app):
        """Coverage export should return either a file or a clean error code."""
        resp = await test_app.get("/api/reports/coverage/export")
        assert resp.status_code < 500
        # If 200, expect a content-type that's either a JSON envelope or a file.
        if resp.status_code == 200:
            ct = resp.headers.get("content-type", "")
            assert ct, "200 response missing content-type"

    async def test_compliance_export_returns_file_or_json(self, test_app):
        resp = await test_app.get("/api/reports/compliance/export")
        assert resp.status_code < 500
