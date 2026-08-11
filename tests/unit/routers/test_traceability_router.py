"""Smoke tests for /api/traceability — graph + by-trace cohort endpoints."""
from __future__ import annotations

import pytest

# F9-6: Requires live Postgres — auto-skipped when DB unreachable (see tests/conftest.py).
pytestmark = pytest.mark.integration


class TestTraceabilityRouter:

    async def test_graph_endpoint_returns_nodes_and_edges(self, test_app):
        resp = await test_app.get("/api/traceability/graph")
        assert resp.status_code == 200
        body = resp.json()
        assert "nodes" in body
        assert "edges" in body
        assert isinstance(body["nodes"], list)
        assert isinstance(body["edges"], list)

    async def test_trace_cohort_unknown_trace_returns_empty_or_404(self, test_app):
        """Unknown trace_id should return 404 or an empty cohort, never 500."""
        resp = await test_app.get(
            "/api/traceability/trace/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code in (200, 404), \
            f"unknown trace should be handled cleanly, got {resp.status_code}"
