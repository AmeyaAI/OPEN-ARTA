"""Tests for requirements router — mock fallback mode (no DB)."""
import pytest

# F9-6: Requires live Postgres — auto-skipped when DB unreachable (see tests/conftest.py).
pytestmark = pytest.mark.integration


class TestRequirementsRouter:

    async def test_list_requirements_returns_mock(self, test_app):
        resp = await test_app.get("/api/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert "requirements" in data
        assert data["total"] >= 1

    async def test_list_requirements_priority_filter(self, test_app):
        resp = await test_app.get("/api/requirements?priority=P0")
        assert resp.status_code == 200
        data = resp.json()
        for req in data["requirements"]:
            assert req["priority"] == "P0"

    async def test_list_requirements_unknown_project_returns_empty(self, test_app):
        resp = await test_app.get("/api/requirements?project_id=nonexistent-id")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["requirements"] == []

    async def test_list_requirements_ecommerce_project_returns_data(self, test_app):
        resp = await test_app.get(
            "/api/requirements?project_id=00000000-0000-0000-0000-000000000001"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
