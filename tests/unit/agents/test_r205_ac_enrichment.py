"""R205 — source-grounded AC enrichment.

Operator directive: ARTA has SUT code access — use it to make up for weak
requirements. R205 appends a concrete, source-derived measurable clause (real
captured endpoint + HTTP status contract) to each UNMEASURABLE acceptance
criterion so the AC scores measurable (raising measurable_ac_pct) and ATDD has
a real endpoint/status to assert on (reducing the upstream gherkin_block_rate).
Deterministic — no LLM call.
"""
from __future__ import annotations

import json

import src.agents.api_discovery as ad
from src.agents.upstream_quality import (
    enrich_requirement_acs_with_source,
    validate_requirement_quality,
    _MEASURABLE_RE,
)

_PID = "r205-test-pid"


def _seed_endpoints(monkeypatch, tmp_path):
    """Point the captured-endpoints loader at a temp file with real-ish paths."""
    f = tmp_path / f"{_PID}.json"
    f.write_text(json.dumps([
        {"method": "GET", "path": "/acct/api/user-access/data"},
        {"method": "GET", "path": "/acct/api/datasets/list"},
        {"method": "POST", "path": "/acct/api/datasets/create"},
    ]))

    def _fake_load(pid):
        return json.loads(f.read_text()) if pid == _PID else []
    monkeypatch.setattr(ad, "_load_captured_endpoints", _fake_load)


def test_r205_unmeasurable_ac_becomes_measurable(monkeypatch, tmp_path):
    _seed_endpoints(monkeypatch, tmp_path)
    req = {"req_id": "R1", "acceptance_criteria": [
        "The user can access their data",          # unmeasurable, matches /user-access/data
    ]}
    before = _MEASURABLE_RE.search(req["acceptance_criteria"][0])
    assert not before
    out = enrich_requirement_acs_with_source(req, _PID)
    enriched = out["acceptance_criteria"][0]
    assert _MEASURABLE_RE.search(enriched), "AC must now be measurable"
    # Grounded in the real matching endpoint.
    assert "/user-access/data" in enriched
    assert out.get("_r205_acs_enriched") == 1


def test_r205_already_measurable_ac_untouched(monkeypatch, tmp_path):
    _seed_endpoints(monkeypatch, tmp_path)
    req = {"req_id": "R2", "acceptance_criteria": [
        "The endpoint returns HTTP 200 within 500ms",   # already measurable
    ]}
    orig = req["acceptance_criteria"][0]
    out = enrich_requirement_acs_with_source(req, _PID)
    assert out["acceptance_criteria"][0] == orig
    assert not out.get("_r205_acs_enriched")


def test_r205_raises_measurable_pct(monkeypatch, tmp_path):
    _seed_endpoints(monkeypatch, tmp_path)
    req = {"req_id": "R3", "acceptance_criteria": [
        "The user can access their data",
        "Datasets are listed for the user",
        "The system handles the request gracefully",
    ]}
    # Before: 0/3 measurable.
    res_before = validate_requirement_quality({**req, "acceptance_criteria": list(req["acceptance_criteria"])})
    assert res_before.criteria_results["_metrics"]["measurable_pct"] == 0.0
    enrich_requirement_acs_with_source(req, _PID)
    res_after = validate_requirement_quality(req)
    assert res_after.criteria_results["_metrics"]["measurable_pct"] == 100.0


def test_r205_dict_acs_supported(monkeypatch, tmp_path):
    _seed_endpoints(monkeypatch, tmp_path)
    req = {"req_id": "R4", "acceptance_criteria": [
        {"id": "AC-1", "statement": "The user can view datasets"},
    ]}
    out = enrich_requirement_acs_with_source(req, _PID)
    ac = out["acceptance_criteria"][0]
    assert isinstance(ac, dict) and ac["id"] == "AC-1"
    assert _MEASURABLE_RE.search(ac["statement"])


def test_r205_no_endpoints_uses_generic_measurable_clause(monkeypatch, tmp_path):
    monkeypatch.setattr(ad, "_load_captured_endpoints", lambda pid: [])
    req = {"req_id": "R5", "acceptance_criteria": ["The user can do the thing"]}
    out = enrich_requirement_acs_with_source(req, _PID)
    # Still made measurable via the domain-neutral clause.
    assert _MEASURABLE_RE.search(out["acceptance_criteria"][0])


def test_r205_idempotent(monkeypatch, tmp_path):
    _seed_endpoints(monkeypatch, tmp_path)
    req = {"req_id": "R6", "acceptance_criteria": ["The user can access their data"]}
    enrich_requirement_acs_with_source(req, _PID)
    first = req["acceptance_criteria"][0]
    enrich_requirement_acs_with_source(req, _PID)   # re-run
    assert req["acceptance_criteria"][0] == first, "enrichment must be idempotent"


def test_r205_killswitch(monkeypatch, tmp_path):
    _seed_endpoints(monkeypatch, tmp_path)
    monkeypatch.setenv("ARTA_R205_AC_ENRICH_DISABLE", "1")
    req = {"req_id": "R7", "acceptance_criteria": ["The user can access their data"]}
    out = enrich_requirement_acs_with_source(req, _PID)
    assert out["acceptance_criteria"][0] == "The user can access their data"
