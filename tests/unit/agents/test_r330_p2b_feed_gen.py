"""R330 P2b — understanding must FEED generation.

Covers the new shared seams:
- mine_path_param_values (lifted from dispatch R312.B; single source of truth)
- select_param_relevant_endpoints (truthful fallback + counters)
- param_constraint_block path-param example lines
- openapi stub-poison guards (persist_openapi_doc tags + openapi_param_details
  skips + openapi_cache treats all-stub docs as cache miss)
- persist_params_detail (annotates existing store entries only)
- _r330_auth_family_block (names/schemes only — never values)
- _r330_chain_newman_early_return threshold semantics
"""
import json

import pytest

from src.agents import api_discovery as ad
from src.agents.api_discovery import (
    mine_path_param_values,
    param_constraint_block,
    persist_params_detail,
    select_param_relevant_endpoints,
)
from src.agents.automation_engineer import AutomationEngineerAgent


# ── mine_path_param_values ───────────────────────────────────────────────────

def test_mine_matches_templated_against_concrete_sibling():
    paths = ["/v1/regions/us-texas-1/servers/{serverId}",
             "/v1/regions/us-texas-1/servers/server-1f9983ab"]
    assert mine_path_param_values(paths) == {"serverId": "server-1f9983ab"}


def test_mine_requires_identical_shape():
    paths = ["/v1/a/{x}", "/v1/b/other/deeper"]
    assert mine_path_param_values(paths) == {}


# ── select_param_relevant_endpoints ──────────────────────────────────────────

EPS = [
    {"path": "/api/orders/status", "method": "GET",
     "query_params": [{"name": "page", "required": True}]},
    {"path": "/api/unrelated", "method": "GET",
     "params_detail": [{"name": "kind", "in": "query", "enum": ["a", "b"], "required": True}]},
]


def test_select_relevance_match():
    rel, stats = select_param_relevant_endpoints(EPS, "Given the orders status page")
    assert [e["path"] for e in rel] == ["/api/orders/status"]
    assert stats == {"known": 2, "relevant": 1}


def test_select_fallback_when_filter_empties_nonempty_set():
    rel, stats = select_param_relevant_endpoints(EPS, "zzz nothing matches here")
    # falls back to constraint-carrying endpoints instead of silently emptying
    assert len(rel) == 2 and stats["relevant"] == 0 and stats["fallback"] == 2


# ── param_constraint_block path-param lines ──────────────────────────────────

def test_param_block_includes_mined_path_values():
    eps = [
        {"path": "/v1/servers/{serverId}", "method": "GET"},
        {"path": "/v1/servers/server-1f9983ab", "method": "GET"},
    ]
    blk = param_constraint_block(eps)
    assert "serverId(path)=e.g. server-1f9983ab" in blk


# ── OpenAPI stub-poison guards ───────────────────────────────────────────────

def test_openapi_param_details_skips_stub_ops(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_OPENAPI_DIR", tmp_path)
    doc = {"openapi": "3.0.0", "paths": {
        "/real": {"get": {"parameters": [
            {"name": "kind", "in": "query", "required": True,
             "schema": {"enum": ["a", "b"]}}]}},
        "/stub": {"get": {"summary": "arta-openapi-ingested", "x-arta-stub": True,
                          "parameters": [
                              {"name": "poison", "in": "query", "required": True,
                               "schema": {"enum": ["x"]}}]}},
    }}
    (tmp_path / "p1.json").write_text(json.dumps(doc))
    det = ad.openapi_param_details("p1")
    assert "GET /real" in det and not any("stub" in k.lower() for k in det)


def test_persist_openapi_doc_tags_stubs_and_preserves_mtime(tmp_path, monkeypatch):
    import os as _os
    monkeypatch.setattr(ad, "_OPENAPI_DIR", tmp_path)
    p = tmp_path / "p2.json"
    p.write_text(json.dumps({"openapi": "3.0.0", "paths": {}}))
    _os.utime(p, (1000000, 1000000))
    ad.persist_openapi_doc("p2", [{"path": "/x", "method": "get"}])
    doc = json.loads(p.read_text())
    assert doc["paths"]["/x"]["get"]["x-arta-stub"] is True
    assert p.stat().st_mtime == 1000000      # stub merges must not refresh TTL


def test_openapi_cache_all_stub_doc_is_cache_miss(tmp_path, monkeypatch):
    from src.agents import openapi_cache as oc
    monkeypatch.setattr(oc, "_CACHE_DIR", tmp_path)
    (tmp_path / "p3.json").write_text(json.dumps({"paths": {
        "/a": {"get": {"summary": "arta-openapi-ingested", "x-arta-stub": True}}}}))
    assert oc._read_cache("p3") is None
    (tmp_path / "p4.json").write_text(json.dumps({"paths": {
        "/a": {"get": {"summary": "real op"}},
        "/b": {"get": {"summary": "arta-openapi-ingested", "x-arta-stub": True}}}}))
    assert oc._read_cache("p4") is not None   # mixed doc still serves


# ── persist_params_detail ────────────────────────────────────────────────────

def test_persist_params_detail_annotates_existing_only(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_CAPTURED_DIR", tmp_path)
    store = [{"method": "GET", "path": "/api/a"}, {"method": "GET", "path": "/api/b"}]
    (tmp_path / "p5.json").write_text(json.dumps(store))
    det = [{"name": "kind", "in": "query", "enum": ["a"], "required": True}]
    n = persist_params_detail("p5", [
        {"method": "GET", "path": "/api/a", "params_detail": det},
        {"method": "GET", "path": "/api/NOT-IN-STORE", "params_detail": det},
    ])
    assert n == 1
    saved = json.loads((tmp_path / "p5.json").read_text())
    assert saved[0]["params_detail"] == det
    assert len(saved) == 2                    # never adds endpoints


# ── auth family block ────────────────────────────────────────────────────────

def _proj_with_chain():
    return {"environments": {"staging": {"auth": {
        "chain": [
            {"match": "/composite", "scheme": "bearer",
             "value_template": "Bearer svc_{org}_{session_token}", "host": "api"},
            {"match": "*", "scheme": "bearer",
             "value_template": "Bearer {agent_token}", "host": "app"},
        ],
        "host_map": {"api": "https://api.example.internal",
                     "app": "https://app.example.internal"},
    }}}}


def test_auth_family_block_renders_names_never_values():
    blk = AutomationEngineerAgent._r330_auth_family_block(_proj_with_chain())
    assert "paths '/composite*'" in blk and "org, session_token" in blk
    assert "https://api.example.internal" in blk
    # fail-closed: the raw value_template must NOT be rendered
    assert "svc_{org}" not in blk and "Bearer " not in blk


def test_auth_family_block_empty_without_chain():
    assert AutomationEngineerAgent._r330_auth_family_block({"environments": {}}) == ""


# ── chained-Newman early return ──────────────────────────────────────────────

class _AgentStub:
    _r330_chain_newman_early_return = AutomationEngineerAgent._r330_chain_newman_early_return


def test_chain_newman_fires_above_threshold(monkeypatch):
    chain = {"nodes": [
        {"method": "GET", "path_template": "/v1/orders", "sequence_index": 0},
        {"method": "GET", "path_template": "/v1/orders/{id}", "sequence_index": 1},
    ]}
    monkeypatch.setattr(ad, "load_chains", lambda pid: [chain])
    risk = {"project_id": "p9",
            "endpoints": [{"path": "/v1/orders"}, {"path": "/v1/orders/{id}"}]}
    out = _AgentStub()._r330_chain_newman_early_return("g", risk, "REQ-XY-001")
    assert out is not None and out.tool == "newman"
    assert out.metadata["r330_deterministic_chain"] is True
    items = json.loads(out.content)["item"]
    assert len(items) == 2


def test_chain_newman_skips_below_threshold(monkeypatch):
    chain = {"nodes": [{"method": "GET", "path_template": "/v1/other", "sequence_index": 0}]}
    monkeypatch.setattr(ad, "load_chains", lambda pid: [chain])
    risk = {"project_id": "p9",
            "endpoints": [{"path": "/v1/orders"}, {"path": "/v1/orders/{id}"}]}
    assert _AgentStub()._r330_chain_newman_early_return("g", risk, "REQ-XY-001") is None


def test_chain_newman_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R330_CHAIN_NEWMAN_DISABLE", "1")
    assert _AgentStub()._r330_chain_newman_early_return(
        "g", {"project_id": "p9", "endpoints": [{"path": "/a"}, {"path": "/b"}]},
        "REQ-XY-001") is None
