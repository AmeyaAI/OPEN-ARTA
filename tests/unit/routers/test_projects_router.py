"""Tests for projects router — mock fallback mode (no DB)."""
import pytest

# F9-6: Requires live Postgres — auto-skipped when DB unreachable (see tests/conftest.py).
pytestmark = pytest.mark.integration


class TestProjectsRouter:

    async def test_list_projects(self, test_app):
        resp = await test_app.get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert "projects" in data
        assert data["total"] >= 3  # 3 demo projects

    async def test_create_project(self, test_app):
        resp = await test_app.post("/api/projects", json={
            "name": "Test Project",
            "description": "Created by pytest",
            "llm_config": {"provider": "ollama", "model": "qwen2.5:32b"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Project"
        assert data["llm_config"]["api_key"] == ""  # no key set, no mask

    async def test_create_project_invalid_provider(self, test_app):
        resp = await test_app.post("/api/projects", json={
            "name": "Bad Provider",
            "llm_config": {"provider": "nonexistent_provider", "model": "x"},
        })
        assert resp.status_code == 400

    async def test_get_project_not_found(self, test_app):
        resp = await test_app.get("/api/projects/nonexistent-id")
        assert resp.status_code == 404

    async def test_list_tests_unknown_project_empty(self, test_app):
        resp = await test_app.get("/api/tests?project_id=unknown")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    async def test_list_defects_unknown_project_empty(self, test_app):
        resp = await test_app.get("/api/defects?project_id=unknown")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
