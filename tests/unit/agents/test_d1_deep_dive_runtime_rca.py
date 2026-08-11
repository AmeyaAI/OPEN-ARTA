"""Phase 4 (charter conformance) — D1: the 5-level deep-dive is wired into runtime
SUT-failure triage.

The `DeepDive` model existed but `defect_intel._analyze_single` never populated it (the
runtime RCA was flat). D1 adds the 5 levels to the LLM triage schema and normalises the
result to the canonical keys so every runtime defect carries a genuine per-failure
descent (symptom→immediate→upstream→architectural→process).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.agents.defect_intel import DefectIntelAgent
from src.models.root_cause_report import DEEP_DIVE_LEVELS


class _Block:
    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, text):
        self.content = [_Block(text)]


def _agent_with_llm(payload: dict) -> DefectIntelAgent:
    agent = DefectIntelAgent.__new__(DefectIntelAgent)
    agent._model = "test-model"
    agent._call_llm = AsyncMock(return_value=_Msg(json.dumps(payload)))
    return agent


_BASE = {
    "root_cause": "x", "failure_type": "BUG", "confidence": 0.8,
    "impacted_files": [], "impacted_features": [], "suggested_fix": "y",
    "fix_effort": "hours", "regression_tests_needed": [], "title": "t", "severity": "P1",
}


@pytest.mark.asyncio
async def test_d1_deep_dive_normalized_to_canonical_keys():
    payload = {**_BASE, "deep_dive": {
        "symptom": "s", "immediate_cause": "i", "upstream_cause": "u",
        "architectural_cause": "a", "process_cause": "p",
        "EXTRA_KEY": "dropped",
    }}
    agent = _agent_with_llm(payload)
    out = await agent._analyze_single({"test_id": "TC-1", "error_message": "boom"})
    dd = out["deep_dive"]
    assert set(dd.keys()) == set(DEEP_DIVE_LEVELS)   # exactly the 5, extras dropped
    assert dd["symptom"] == "s" and dd["process_cause"] == "p"
    assert "EXTRA_KEY" not in dd


@pytest.mark.asyncio
async def test_d1_missing_levels_filled_empty():
    payload = {**_BASE, "deep_dive": {"symptom": "only symptom"}}
    agent = _agent_with_llm(payload)
    out = await agent._analyze_single({"test_id": "TC-2", "error_message": "boom"})
    dd = out["deep_dive"]
    assert set(dd.keys()) == set(DEEP_DIVE_LEVELS)
    assert dd["symptom"] == "only symptom"
    assert dd["upstream_cause"] == "" and dd["architectural_cause"] == ""


@pytest.mark.asyncio
async def test_d1_no_deep_dive_is_tolerated():
    """A model that omits deep_dive entirely must not crash the triage."""
    agent = _agent_with_llm(dict(_BASE))   # no deep_dive key
    out = await agent._analyze_single({"test_id": "TC-3", "error_message": "boom"})
    assert out["root_cause"] == "x"          # rest of the RCA still returned
    assert "deep_dive" not in out or isinstance(out.get("deep_dive"), dict)
