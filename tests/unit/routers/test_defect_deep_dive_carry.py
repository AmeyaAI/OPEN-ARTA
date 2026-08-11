"""FE-sync P1 — the defects router must CARRY the 5-level deep_dive + preventive_action
that DefectIntelAgent produces (it used to merge only root_cause/suggested_fix/confidence/
category and return a 3-field dict, silently discarding the deep-dive the dashboard needs).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import src.api.routers.defects as defects_mod


class _State:
    anthropic = object()  # truthy → passes the "LLM client configured" guard


class _App:
    state = _State()


class _Req:
    app = _App()


_ANALYSIS = {
    "root_cause": "rc", "suggested_fix": "sf", "confidence": 0.8, "category": "test_gen_bug",
    "deep_dive": {
        "symptom": "s", "immediate_cause": "i", "upstream_cause": "u",
        "architectural_cause": "a", "process_cause": "p",
    },
    "preventive_action": "add a pre-dispatch validator",
}


@pytest.mark.asyncio
async def test_analyze_defect_carries_deep_dive_and_preventive_action(monkeypatch):
    defect = {"id": "DEF-TEST", "test_id": "TC-1", "title": "boom",
              "severity": "P1", "status": "open", "description": "err"}
    monkeypatch.setattr(defects_mod, "MOCK_DEFECTS", [defect])

    with patch("src.agents.defect_intel.DefectIntelAgent") as MockAgent:
        # _analyze_single is the primary (explicit-deep-dive) path; analyze_failures
        # remains wired as the fallback safety net.
        MockAgent.return_value._analyze_single = AsyncMock(return_value=dict(_ANALYSIS))
        MockAgent.return_value.analyze_failures = AsyncMock(return_value=[_ANALYSIS])
        result = await defects_mod.analyze_defect("DEF-TEST", _Req())

    # the response now carries the deep-dive + preventive action (were discarded)
    assert result["deep_dive"] == _ANALYSIS["deep_dive"]
    assert result["preventive_action"] == "add a pre-dispatch validator"
    # and the in-memory defect record is updated so subsequent GETs see them
    assert defect["deep_dive"]["process_cause"] == "p"
    assert defect["preventive_action"] == "add a pre-dispatch validator"
    # existing fields still returned
    assert result["root_cause"] == "rc"


@pytest.mark.asyncio
async def test_analyze_defect_falls_back_to_db_for_real_defects(monkeypatch):
    """FE-sync P1 follow-through — analyze must work on REAL DB-backed defects, not
    just MOCK_DEFECTS. Without the DB fallback the /defects deep-dive button 404s on
    every operator-visible defect. Also asserts the analysis is persisted back."""
    from contextlib import asynccontextmanager

    monkeypatch.setattr(defects_mod, "MOCK_DEFECTS", [])  # force the DB path

    class _Row:
        metadata_ = None
        root_cause = None
        suggested_fix = None
        root_cause_category = None

    row = _Row()
    row.metadata_ = {}

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class _FakeSession:
        async def execute(self, q):  # persist path: select(Defect).where(...)
            return _FakeResult([row])

        async def flush(self):  # noqa: D401 — persist no-op
            pass

    @asynccontextmanager
    async def _fake_try_db():
        yield _FakeSession()

    class _FakeRepo:
        def __init__(self, db):
            pass

        async def get(self, defect_id):  # DB-fallback lookup path
            return row

    def _fake_to_dict(obj, exclude=None):
        return {"id": "DEF-DB-1", "defect_id": "DEF-DB-1", "title": "db boom",
                "description": "db err", "severity": "P2", "status": "open",
                "metadata_": {}}

    monkeypatch.setattr("src.api.db_adapter.try_db", _fake_try_db)
    monkeypatch.setattr("src.db.repository.DefectRepo", _FakeRepo)
    monkeypatch.setattr("src.db.repository._to_dict", _fake_to_dict)

    with patch("src.agents.defect_intel.DefectIntelAgent") as MockAgent:
        MockAgent.return_value._analyze_single = AsyncMock(return_value=dict(_ANALYSIS))
        MockAgent.return_value.analyze_failures = AsyncMock(return_value=[_ANALYSIS])
        result = await defects_mod.analyze_defect("DEF-DB-1", _Req())

    # the DB-backed defect analyzes (was a 404) and carries the 5-level deep-dive
    assert result["deep_dive"] == _ANALYSIS["deep_dive"]
    assert result["preventive_action"] == "add a pre-dispatch validator"
    # and the analysis was persisted back onto the DB row (real cols + metadata JSONB)
    assert row.root_cause == "rc"
    assert row.suggested_fix == "sf"
    assert row.metadata_["deep_dive"]["process_cause"] == "p"
    assert row.metadata_["preventive_action"] == "add a pre-dispatch validator"
