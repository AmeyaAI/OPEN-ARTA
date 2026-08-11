"""R218 G1 — discover_analytics_workflow_from_source: ARTA reads any SUT's analytics
source and emits the WORKFLOW MANIFEST (dataset modes+engines, endpoints, routing,
hosts), which the runtime deep-merges over the default. The autonomy layer the operator
asked for ("ARTA should go through the SUT code and understand — generic"). LLM+fetch
are mocked (no live GitHub/LLM), like the auth-chain discovery tests."""
from __future__ import annotations

import asyncio
import json

import src.agents.github_context as ghc
from src.automation.python_tests.arta_runtime import analytics_manifest as am


def test_validate_keeps_wellformed_drops_junk():
    extracted = {
        "dataset_modes": {
            "excel": {"id_prefix": "excel_", "dataset_type": "excel", "engine": "tabular",
                      "is_excel": True, "verifies": ["count", "content"]},
            "bad": {"dataset_type": "x"},                       # no id_prefix → dropped
        },
        "endpoints": {
            "trigger_job": {"host": "monitor", "method": "post", "path": "/x/monitoring/event/job-create"},
            "junk": {"host": "analytics"},                     # no path → dropped
        },
        "routing_rules": {"count": "excel", "content": "ghost"},  # ghost mode not present → dropped
        "hosts": {"analytics": "https://api.example.com", "backend": "REACT_APP_BACKEND_URL"},
    }
    man = ghc._validate_workflow_manifest(extracted)
    assert set(man["dataset_modes"]) == {"excel"}
    assert man["dataset_modes"]["excel"]["is_excel"] is True
    assert set(man["endpoints"]) == {"trigger_job"}
    assert man["endpoints"]["trigger_job"]["method"] == "POST"      # normalized upper
    assert man["routing_rules"] == {"count": "excel"}              # ghost dropped
    assert man["hosts"] == {"analytics": {"default": "https://api.example.com"}}  # env-name dropped


def test_discover_returns_partial_manifest(monkeypatch):
    async def _fetch(project, **k):
        return [{"repo": "r", "file": "add.js", "content": "x"}]

    async def _extract(snips, **k):
        return {"dataset_modes": {"excel": {"id_prefix": "excel_", "engine": "tabular", "is_excel": True}},
                "endpoints": {"trigger_job": {"host": "monitor", "method": "POST", "path": "/a/job-create"}},
                "routing_rules": {"count": "excel"}}

    monkeypatch.setattr(ghc, "_a2_fetch_auth_client_source", _fetch)
    monkeypatch.setattr(ghc, "_an_workflow_llm_extract", _extract)
    man = asyncio.run(ghc.discover_analytics_workflow_from_source({"id": "p"}))
    assert man["dataset_modes"]["excel"]["is_excel"] is True
    assert "trigger_job" in man["endpoints"] and man["routing_rules"]["count"] == "excel"


def test_discover_none_when_no_valid_modes(monkeypatch):
    async def _fetch(project, **k):
        return [{"repo": "r", "file": "f", "content": "x"}]

    async def _extract(snips, **k):
        return {"endpoints": {"query": {"host": "analytics", "method": "POST", "path": "/q"}}}  # no modes

    monkeypatch.setattr(ghc, "_a2_fetch_auth_client_source", _fetch)
    monkeypatch.setattr(ghc, "_an_workflow_llm_extract", _extract)
    assert asyncio.run(ghc.discover_analytics_workflow_from_source({"id": "p"})) is None


def test_discover_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_AN_WORKFLOW_DISCOVER_DISABLE", "1")
    assert asyncio.run(ghc.discover_analytics_workflow_from_source({"id": "p"})) is None


def test_workflow_scorer_accepts_py_and_scores_jobcreate():
    # .py accepted (backend routers carry engine routing + job-create body)
    assert ghc._an_workflow_score_path("src/routers/query_router.py") > 0
    assert ghc._an_workflow_score_path("src/api/analytics_router.py") >= 0
    assert ghc._an_workflow_score_path("web/src/AnalyticsToolApi.js") > 0
    assert ghc._an_workflow_score_path("node_modules/x/dataset.js") == -1
    assert ghc._an_workflow_score_path("readme.md") == -1


def test_discovered_manifest_merges_over_runtime_default(monkeypatch):
    """END-TO-END seam: the discovered partial manifest, installed via
    ARTA_AN_WORKFLOW_MANIFEST, deep-merges over the runtime DEFAULT — a new SUT's
    modes/hosts refine it while the defaults (engines, routing) survive."""
    monkeypatch.delenv("TARGET_ANALYTICS_BASE_URL", raising=False)
    partial = {"hosts": {"analytics": {"default": "https://api.newsut.example.com/an/v1"}},
               "dataset_modes": {"excel": {"id_prefix": "xls_"}}}
    monkeypatch.setenv("ARTA_AN_WORKFLOW_MANIFEST", json.dumps(partial))
    man = am.load_manifest()
    assert am.host_base(man, "analytics") == "https://api.newsut.example.com/an/v1"
    assert man["dataset_modes"]["excel"]["id_prefix"] == "xls_"          # SUT-specific override
    assert man["dataset_modes"]["excel"]["is_excel"] is True             # default field survives
    assert man["dataset_modes"]["files"]["engine"] == "document_rag"     # untouched default
    assert am.mode_for_verification(man, "count") == "excel"             # routing default survives
