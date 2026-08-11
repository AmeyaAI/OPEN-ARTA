"""R330 (SUT-Understanding P1) — grounding coverage aggregation.

Makes ARTA's per-endpoint provenance honest + visible: how much of the SUT API
surface ARTA actually KNOWS (from the SUT's OpenAPI/source) vs merely observed vs
requirement-declared vs human-corrected.
"""
from __future__ import annotations

import json

import src.agents.api_discovery as ad


def test_endpoint_provenance_buckets():
    assert ad.endpoint_provenance({"source": "openapi"}) == "source_grounded"
    assert ad.endpoint_provenance({"source": "github"}) == "source_grounded"
    assert ad.endpoint_provenance({"source": "human_correction"}) == "human_corrected"
    assert ad.endpoint_provenance({"source": "manual"}) == "human_corrected"
    assert ad.endpoint_provenance({"source": "requirement"}) == "requirement_declared"
    assert ad.endpoint_provenance({"source": "network"}) == "observed"
    # HAR-captured (real runtime evidence) but no explicit source → observed, not unlabeled
    assert ad.endpoint_provenance({"source_har": "run.har"}) == "observed"
    assert ad.endpoint_provenance({"discovered_at": "2026-01-01"}) == "observed"
    assert ad.endpoint_provenance({}) == "unlabeled"


def test_grounding_coverage_buckets_by_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_CAPTURED_DIR", tmp_path)
    (tmp_path / "pid.json").write_text(json.dumps([
        {"method": "GET", "path": "/v1/a", "source": "openapi", "content_type": "application/json"},
        {"method": "GET", "path": "/v1/b", "source": "github", "content_type": "application/json"},
        {"method": "GET", "path": "/v1/c", "source": "network", "content_type": "application/json"},
        {"method": "GET", "path": "/v1/d", "source": "requirement", "content_type": "application/json"},
        {"method": "GET", "path": "/v1/e", "source": "human_correction", "content_type": "application/json"},
    ]))
    cov = ad.grounding_coverage("pid")
    assert cov["total_endpoints"] == 5
    assert cov["by_provenance"]["source_grounded"] == 2   # openapi + github
    assert cov["by_provenance"]["human_corrected"] == 1
    assert cov["by_provenance"]["requirement_declared"] == 1
    assert cov["by_provenance"]["observed"] == 1
    # grounded = source_grounded(2) + human_corrected(1) + observed(1) = 4 → 80%
    assert cov["grounded_endpoints"] == 4
    assert cov["grounded_pct"] == 80.0
    # source_grounded (aspirational) = openapi + github = 2 → 40%
    assert cov["source_grounded_endpoints"] == 2
    assert cov["source_grounded_pct"] == 40.0


def test_grounding_coverage_empty_store(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_CAPTURED_DIR", tmp_path)
    cov = ad.grounding_coverage("nope")
    assert cov["total_endpoints"] == 0 and cov["grounded_pct"] == 0.0


def test_param_constraint_block(monkeypatch):
    # R330 P2 — feed KNOWN param values/constraints UPSTREAM into gen.
    eps = [
        {"method": "GET", "path": "/v1/regions", "params_detail": [
            {"name": "provider", "enum": ["aws", "azure", "gcp"], "required": True},
            {"name": "limit", "minimum": 1, "maximum": 100},
            {"name": "slug", "pattern": "^[a-z-]+$"},
        ]},
        {"method": "GET", "path": "/v1/clusters", "response_value_samples": {
            "status": ["Ready", "Provisioning", "Failed"]}},
        {"method": "GET", "path": "/v1/orgs/{org}", "params_detail": [
            {"name": "org", "in": "path", "required": True},       # redundant → dropped
            {"name": "expand", "in": "query", "required": True},   # gen omits → kept
        ]},
        {"method": "GET", "path": "/v1/nothing"},  # no constraints → omitted
    ]
    block = ad.param_constraint_block(eps)
    # request-param constraints under their OWN header (not conflated with response)
    assert "REQUEST PARAM VALUES" in block
    assert "provider: ∈ [aws, azure, gcp]; required" in block
    assert "limit: range 1..100" in block
    assert "slug: pattern /^[a-z-]+$/" in block
    assert "expand: required" in block          # query required kept
    assert "org: required" not in block         # path-only-required dropped as redundant
    # response-field values under a DISTINCT header (assertion grounding, not params)
    assert "OBSERVED RESPONSE FIELD VALUES" in block
    assert "status ∈ [Ready, Provisioning, Failed]" in block
    # the response section explicitly disclaims being request params
    assert "NOT request params" in block
    assert "/v1/nothing" not in block  # endpoints with nothing known are omitted
    # empty when nothing known — never fabricates
    assert ad.param_constraint_block([{"method": "GET", "path": "/x"}]) == ""
    assert ad.param_constraint_block([]) == ""
    # killswitch
    monkeypatch.setenv("ARTA_R330_PARAM_CONSTRAINTS_DISABLE", "1")
    assert ad.param_constraint_block(eps) == ""


def test_openapi_param_details_extracts_from_contract(tmp_path, monkeypatch):
    # R330 P2 — mine enum/min/max/pattern/required from the SUT's OWN contract,
    # including $ref resolution + path-level params + templated-path matching.
    monkeypatch.setattr(ad, "_OPENAPI_DIR", tmp_path)
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/v1/regions": {
                "get": {"parameters": [
                    {"name": "provider", "in": "query", "required": True,
                     "schema": {"type": "string", "enum": ["aws", "azure", "gcp"]}},
                    {"name": "limit", "in": "query",
                     "schema": {"type": "integer", "minimum": 1, "maximum": 100}},
                ]},
            },
            "/v1/clusters/{id}": {
                "parameters": [  # path-level param applies to all methods
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"$ref": "#/components/schemas/IdParam"}}],
                "get": {"parameters": [
                    {"$ref": "#/components/parameters/RegionQuery"}]},
            },
        },
        "components": {
            "schemas": {"IdParam": {"type": "string", "pattern": "^c-[0-9]+$"}},
            "parameters": {"RegionQuery": {
                "name": "region", "in": "query",
                "schema": {"type": "string", "enum": ["us", "eu"]}}},
        },
    }
    (tmp_path / "pid.json").write_text(json.dumps(spec))
    m = ad.openapi_param_details("pid")
    assert m["GET /v1/regions"][0]["enum"] == ["aws", "azure", "gcp"]
    assert m["GET /v1/regions"][0]["required"] is True
    assert m["GET /v1/regions"][1]["minimum"] == 1 and m["GET /v1/regions"][1]["maximum"] == 100
    # path-level param ($ref resolved) + operation param ($ref parameter) both present
    got = {p["name"]: p for p in m["GET /v1/clusters/{id}"]}
    assert got["id"]["pattern"] == "^c-[0-9]+$"
    assert got["region"]["enum"] == ["us", "eu"]

    # enrich matches a CONCRETE captured path against the templated contract path
    eps = [
        {"method": "GET", "path": "/v1/regions"},
        {"method": "GET", "path": "/v1/clusters/c-42"},   # concrete → matches {id}
        {"method": "GET", "path": "/v1/unknown"},
    ]
    ad.enrich_endpoints_with_openapi_params("pid", eps)
    assert eps[0]["params_detail"][0]["enum"] == ["aws", "azure", "gcp"]
    assert any(p["name"] == "id" for p in eps[1]["params_detail"])
    assert "params_detail" not in eps[2]
    # the enriched endpoints now yield a real param-constraint block
    assert "provider: ∈ [aws, azure, gcp]" in ad.param_constraint_block(eps)


def test_param_constraint_block_reads_query_params():
    # R330 P2 — the primary live lever: sut_topology `query_params` (captured values
    # + declared required/type) fed into the REQUEST section, deduped vs params_detail,
    # redaction-skipped, length-capped.
    eps = [
        {"method": "GET", "path": "/a", "query_params": [
            {"name": "page_size", "value": "1000"},                 # captured value
            {"name": "include_detail", "value": "false"},
            {"name": "cursor", "value": "<<REDACTED_HEADER_VALUE>>"},  # skipped
        ]},
        {"method": "POST", "path": "/b", "query_params": [
            {"name": "trRequest", "required": True, "type": "string"}]},  # declared
        {"method": "GET", "path": "/c", "params_detail": [
            {"name": "region", "in": "query", "enum": ["us", "eu"]}],
            "query_params": [{"name": "region", "value": "us"}]},   # dedup: params_detail wins
    ]
    block = ad.param_constraint_block(eps)
    assert "REQUEST PARAM VALUES" in block
    assert "page_size=e.g. 1000" in block
    assert "include_detail=e.g. false" in block
    assert "cursor" not in block                       # redacted skipped
    assert "trRequest: required string" in block       # declared required+type
    assert "region: ∈ [us, eu]" in block               # params_detail kept
    assert "region=e.g. us" not in block               # not duplicated from query_params
    # long value length-capped
    long_eps = [{"method": "GET", "path": "/d", "query_params": [
        {"name": "filters", "value": "x" * 200}]}]
    assert "…" in ad.param_constraint_block(long_eps)


def test_param_constraint_block_respects_char_budget():
    # R330 P2 — guard the documented prompt-bloat → truncation regression: a large
    # constrained set must not blow past the budget (request params prioritized).
    big = [
        {"method": "GET", "path": f"/v1/resource{i}", "params_detail": [
            {"name": f"filter{i}", "in": "query", "enum": [f"opt-{j}" for j in range(8)]}]}
        for i in range(40)
    ]
    block = ad.param_constraint_block(big, max_endpoints=40, max_chars=300)
    # header + a few lines, but bounded well under an unbudgeted dump
    assert len(block) < 300 + 260   # section header + <=budget of lines
    assert "REQUEST PARAM VALUES" in block
    assert block.count("\n/v1/resource") <= 40  # capped by budget, not all 40
