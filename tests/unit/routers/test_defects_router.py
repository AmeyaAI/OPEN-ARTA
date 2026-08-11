"""F7-5: Tests for the defects router.

Covers F6-7 (POST /api/defects/{id}/analyze requires auth) and F6-17
(MOCK_DEFECTS / MOCK_FLAKY are emptied in production mode).
"""
from __future__ import annotations

import importlib
import os

import pytest

# F9-6: Requires live Postgres — auto-skipped when DB unreachable (see tests/conftest.py).
pytestmark = pytest.mark.integration


class TestDefectsRouter:

    async def test_list_defects_returns_200(self, test_app):
        """GET /api/defects responds 200 with the expected response shape.

        The pre-existing recursion bug surfaced by F7-5 (jsonable_encoder
        infinite-looping on `metadata` because `_to_dict` returned SQLAlchemy's
        class-level `MetaData` instead of the JSONB column) was fixed in
        `db/repository.py::_to_dict` — column-attr lookup now uses the mapper's
        Python attribute name, not the SQL column name.
        """
        resp = await test_app.get("/api/defects")
        assert resp.status_code == 200, f"got {resp.status_code}: {resp.text[:200]}"
        body = resp.json()
        assert "defects" in body
        assert "total" in body
        assert isinstance(body["defects"], list)

    async def test_unauth_analyze_returns_401_when_api_key_set(self, test_app):
        os.environ["ARTA_API_KEY"] = "f7-test-key"
        try:
            resp = await test_app.post("/api/defects/DEF-001/analyze")
            assert resp.status_code == 401, "F6-7: /defects/{id}/analyze must reject unauth"
        finally:
            os.environ.pop("ARTA_API_KEY", None)

    async def test_mock_defects_empty_in_production_env(self, monkeypatch):
        """F6-17: ENVIRONMENT=production zeroes out MOCK_DEFECTS at module import."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        # Force re-import so the module-level guard re-evaluates _IS_PROD.
        from src.api.routers import defects as _defects_mod
        importlib.reload(_defects_mod)
        try:
            assert _defects_mod.MOCK_DEFECTS == [], \
                "F6-17: production must empty MOCK_DEFECTS"
            assert _defects_mod.MOCK_FLAKY == [], \
                "F6-17: production must empty MOCK_FLAKY"
        finally:
            # Restore dev mode so other tests don't see the empty seeds.
            monkeypatch.setenv("ENVIRONMENT", "development")
            importlib.reload(_defects_mod)
