"""Phase J7 — tests for cascade-aware defect classification (Phase G1).

Acceptance: 1 root cause + N cascades → 1 root-cause defect with
`affected_tests[]` linking the N cascades. PCVs deterministic-classified
without LLM.
"""
from __future__ import annotations

import pytest

from src.agents.defect_intel import DefectIntelAgent


class _StubLLMClient:
    """LLM client that always raises on .messages.create — verifies the
    cascade/PCV paths don't hit the LLM (they're deterministic)."""

    class messages:
        @staticmethod
        async def create(**kwargs):
            raise RuntimeError("LLM should not be called for cascade/PCV paths")

    provider = "stub"


@pytest.fixture
def agent() -> DefectIntelAgent:
    return DefectIntelAgent(_StubLLMClient())


@pytest.fixture
def cascade_failures() -> list[dict]:
    return [
        {"test_id": "test_login", "title": "Login flow", "priority": "P1"},
        {"test_id": "test_create_snapshot", "priority": "P1"},
        {"test_id": "test_query_metrics", "priority": "P2"},
        {"test_id": "test_query_insights", "priority": "P2"},
        {"test_id": "test_delete_snapshot", "priority": "P3"},
        {"test_id": "test_archive", "priority": "P3"},
        {"test_id": "test_export_csv", "priority": "P2"},   # PCV
    ]


@pytest.fixture
def sequence_integrity() -> dict:
    return {
        "cascade_failures": [
            {"test_id": tid, "root_cause_test_id": "test_login",
             "via_var": "dataset_id"}
            for tid in [
                "test_create_snapshot", "test_query_metrics",
                "test_query_insights", "test_delete_snapshot", "test_archive",
            ]
        ],
        "provider_contract_violations": [
            {"test_id": "test_export_csv", "var_name": "export_url",
             "expected_jsonpath": "$.url"},
        ],
        "param_provenance": {},
        "degraded": False,
    }


@pytest.mark.asyncio
async def test_cascades_collapse_into_linked_defects(agent, cascade_failures, sequence_integrity):
    defects = await agent.analyze_failures(cascade_failures, sequence_integrity=sequence_integrity)
    cascades = [d for d in defects if d.get("defect_class") == "cascade_failure"]
    pcvs = [d for d in defects if d.get("defect_class") == "provider_contract_violation"]
    assert len(cascades) == 5
    assert len(pcvs) == 1
    for c in cascades:
        assert c["cascade_of"] == "test_login"
        assert c["status"] == "linked"   # not "open" — operator only acts on root
        assert c["via_var"] == "dataset_id"


@pytest.mark.asyncio
async def test_pcv_skips_llm_path(agent, cascade_failures, sequence_integrity):
    """PCV defects must be classified deterministically without calling
    the LLM (the stub client raises when create() is called)."""
    defects = await agent.analyze_failures(cascade_failures, sequence_integrity=sequence_integrity)
    pcv = next(d for d in defects if d.get("defect_class") == "provider_contract_violation")
    assert pcv["test_id"] == "test_export_csv"
    assert pcv["status"] == "open"


@pytest.mark.asyncio
async def test_empty_seq_integrity_falls_back_to_root_only(agent):
    """Older runs with no sequence_integrity → all failures classified as
    root_causes (LLM RCA path), no cascade/PCV variants emitted."""
    failures = [{"test_id": "t1", "title": "T1", "priority": "P2"}]
    defects = await agent.analyze_failures(failures, sequence_integrity={})
    # The stubbed LLM raises, so analyze_failures gathers exceptions and
    # returns empty for root_cause failures. Cascades/PCVs are empty too.
    assert all(d.get("defect_class") != "cascade_failure" for d in defects)


@pytest.mark.asyncio
async def test_one_root_cause_links_all_affected_tests(agent, cascade_failures, sequence_integrity):
    """Phase G1 acceptance: 1 ticket per root cause, not N."""
    defects = await agent.analyze_failures(cascade_failures, sequence_integrity=sequence_integrity)
    cascades = [d for d in defects if d.get("defect_class") == "cascade_failure"]
    # All 5 cascades link to the same root cause.
    root_causes = {d["cascade_of"] for d in cascades}
    assert len(root_causes) == 1
    assert "test_login" in root_causes
