"""R305 — endpoint-grounding hygiene + path-parameter-value + response-shape grounding.

Root cause fixed: ARTA's endpoint-grounding store self-poisoned (SPA/static routes
harvested from discovery + ARTA's own failed 404/HTML test traffic written back),
so grounding rubber-stamped ARTA's hallucinations (bare /clusters → HTML, region=global
→ 404, bare-array assertion on a {servers:[...]} wrapper)."""
from __future__ import annotations

from src.agents.api_discovery import _r305_drop_reason, _r305_endpoint_hygiene
from src.agents.grounding_validator import (
    _r305_param_value_violation,
    _r305_response_root,
    validate_newman_grounded,
)
from src.agents.automation_engineer import AutomationEngineerAgent as _AE


# ── S2/R1 — store hygiene (_r305_drop_reason) ──────────────────────────────
def test_html_content_type_dropped():
    assert _r305_drop_reason({"path": "/clusters", "content_type": "text/html; charset=utf-8"}) == "html_or_static"


def test_static_path_dropped_even_without_content_type():
    assert _r305_drop_reason({"path": "/_next/webpack-hmr"}) == "html_or_static"
    assert _r305_drop_reason({"path": "/_next/static/chunks/x.js"}) == "html_or_static"
    assert _r305_drop_reason({"path": "/assets/logo.svg"}) == "html_or_static"


def test_network_test_echo_without_2xx_dropped():
    # ARTA's own failed guess written back (source=network + test title, no 2xx)
    assert _r305_drop_reason({
        "path": "/v1/regions/global/infrastructure/servers",
        "source": "network", "summary": "[API] AC-001: List servers", "status": None,
    }) == "test_traffic_echo"


def test_verified_2xx_network_capture_is_kept():
    # a genuine 2xx-JSON runtime capture (S1) — NOT a self-guess
    assert _r305_drop_reason({
        "path": "/v1/regions/us-texas-1/infrastructure/servers",
        "source": "network", "summary": "[API] List Servers", "status": 200,
        "content_type": "application/json",
    }) is None


def test_real_json_api_and_requirement_template_kept():
    assert _r305_drop_reason({"path": "/v1/regions/global/organizations",
                              "content_type": "application/json", "status": 200}) is None
    assert _r305_drop_reason({"path": "/v1/regions/{region}/infrastructure/servers",
                              "source": "requirement"}) is None


def test_hygiene_cleans_mixed_list():
    raw = [
        {"path": "/clusters", "content_type": "text/html"},
        {"path": "/_next/static/x.js"},
        {"path": "/v1/regions/global/infrastructure/servers", "source": "network",
         "summary": "[API] AC-001", "status": None},
        {"path": "/v1/regions/global/organizations", "content_type": "application/json", "status": 200},
        {"path": "/v1/regions/{region}/infrastructure/servers", "source": "requirement"},
    ]
    out = _r305_endpoint_hygiene(raw)
    paths = {e["path"] for e in out}
    assert paths == {"/v1/regions/global/organizations", "/v1/regions/{region}/infrastructure/servers"}


# ── G1 — path-parameter VALUE grounding ────────────────────────────────────
_EPS = [
    {"method": "GET", "path": "/v1/regions/us-texas-1/infrastructure/servers"},
    {"method": "GET", "path": "/v1/regions/{region}/infrastructure/servers"},
    {"method": "GET", "path": "/v1/regions/us-texas-1/infrastructure/servers/server-1f9983ab"},
    {"method": "GET", "path": "/v1/regions/global/organizations/bigcustomer"},
    {"method": "GET", "path": "/v1/regions/global/organizations/newcustomer"},
    {"method": "GET", "path": "/v1/regions/global/organizations/testcustomer"},
    {"method": "GET", "path": "/v1/regions/global/organizations/vendor"},
]


def test_g1_flags_wrong_region_value():
    v = _r305_param_value_violation("GET", "/v1/regions/global/infrastructure/servers", _EPS)
    assert v is not None and v[0] == "global" and "us-texas-1" in v[2]


def test_g1_passes_correct_region():
    assert _r305_param_value_violation("GET", "/v1/regions/us-texas-1/infrastructure/servers", _EPS) is None


def test_g1_never_blocks_new_opaque_id():
    # a genuinely-new server id must NOT be value-grounded
    assert _r305_param_value_violation(
        "GET", "/v1/regions/us-texas-1/infrastructure/servers/server-9c0ffee1", _EPS) is None


def test_g1_skips_high_cardinality_id_slot():
    # org slug slot has 4 captured values (>3) → treated as id-like, not enum → no flag
    assert _r305_param_value_violation(
        "GET", "/v1/regions/global/organizations/smallcustomer", _EPS) is None


def test_g1_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R305_PARAM_VALUE_GROUNDING_DISABLE", "1")
    assert _r305_param_value_violation("GET", "/v1/regions/global/infrastructure/servers", _EPS) is None


# ── G2 — response-shape grounding ──────────────────────────────────────────
_SHAPE_EPS = [
    {"method": "GET", "path": "/v1/regions/us-texas-1/infrastructure/servers",
     "response_body_shape": {"servers": [{"id": "x"}]}},           # raw sample wrapper
    {"method": "GET", "path": "/v1/regions/global/organizations",
     "response_body_shape": {"type": "object", "properties": {"organizations": {"type": "array"}}}},
    {"method": "GET", "path": "/v1/regions/us-texas-1/things",
     "response_body_shape": {"type": "array", "items": {"type": "object"}}},   # bare array
]


def test_g2_response_root_object_vs_array():
    assert _r305_response_root("GET", "/v1/regions/us-texas-1/infrastructure/servers", _SHAPE_EPS) == "object"
    assert _r305_response_root("GET", "/v1/regions/global/organizations", _SHAPE_EPS) == "object"
    assert _r305_response_root("GET", "/v1/regions/us-texas-1/things", _SHAPE_EPS) == "array"


def test_g2_shape_summary_extracts_wrapper_key():
    summ = _AE._r305_find_response_shape("GET", "/v1/regions/us-texas-1/infrastructure/servers", _SHAPE_EPS)
    assert summ["root"] == "object" and summ["keys"] == ["servers"] and summ["list_key"] == "servers"


def _newman_item(url, script):
    return {"item": [{"name": "List Servers", "request": {
        "method": "GET", "url": {"raw": url}},
        "event": [{"listen": "test", "script": {"exec": [script]}}]}]}


def test_g2_validator_flags_bare_array_on_wrapper():
    parsed = _newman_item(
        "{{base_url}}/v1/regions/us-texas-1/infrastructure/servers",
        "pm.test('is array', () => { const body = pm.response.json(); pm.expect(body).to.be.an('array'); });")
    viols = validate_newman_grounded(parsed, project_id="p", captured_endpoints=_SHAPE_EPS)
    assert any(v.kind == "response_shape_mismatch" for v in viols)


def test_g2_validator_passes_wrapper_key_assertion():
    parsed = _newman_item(
        "{{base_url}}/v1/regions/us-texas-1/infrastructure/servers",
        "pm.test('is array', () => { const body = pm.response.json(); pm.expect(body.servers).to.be.an('array'); });")
    viols = validate_newman_grounded(parsed, project_id="p", captured_endpoints=_SHAPE_EPS)
    assert not any(v.kind == "response_shape_mismatch" for v in viols)


def test_g2_deterministic_rewriter_grounds_all_array_forms():
    """R305 G2 — the LLM plays syntax whack-a-mole (Array.isArray(body), body.forEach,
    body.length) after the validator kills to.be.an('array'). The deterministic
    rewriter grounds EVERY array-treatment form onto body.<list_key>, and never
    touches non-array accesses (body.status) or already-grounded (body.clusters)."""
    lines = [
        "const body = pm.response.json();",
        "pm.expect(Array.isArray(body)).to.be.true;",
        "pm.expect(body.length).to.be.greaterThanOrEqual(1);",
        "body.forEach(c => { pm.expect(c).to.have.property('status'); });",
        "pm.expect(body).to.be.an('array');",
    ]
    new, n = _AE._r305_rewrite_body_array_lines(lines, "clusters")
    assert n == 4
    blob = "\n".join(new)
    assert "Array.isArray(body.clusters)" in blob
    assert "body.clusters.length" in blob
    assert "body.clusters.forEach" in blob
    assert "pm.expect(body.clusters).to.be.an('array')" in blob


def test_g2_rewriter_leaves_non_array_and_grounded_untouched():
    lines = [
        "const body=pm.response.json();",
        "pm.expect(body.status).to.equal('ok');",       # not array-treatment
        "pm.expect(body.clusters).to.be.an('array');",  # already grounded
        "body.clusters.forEach(c=>{});",                # already grounded
    ]
    new, n = _AE._r305_rewrite_body_array_lines(lines, "clusters")
    assert n == 0


def test_g2_shape_summary_exposes_list_key():
    assert _AE._r305_shape_summary({"servers": [{"id": 1}]})["list_key"] == "servers"
    assert _AE._r305_shape_summary(
        {"type": "object", "properties": {"clusters": {"type": "array"}, "total": {"type": "integer"}}}
    )["list_key"] == "clusters"


def test_r305_i_grounds_items_pagination_hallucination():
    """R305.I — the LLM asserts a generic `items` wrapper but the SUT returns
    `{organizations:[…]}` ('expected {organizations:[…]}
    to have property items'). Ground .property('items') + body.items → the real key."""
    lines = [
        "const body = pm.response.json();",
        "pm.expect(body).to.have.property('items');",
        "pm.expect(body.items.length).to.be.greaterThan(0);",
        "body.items.forEach(o => { pm.expect(o).to.have.property('id'); });",
    ]
    new, n = _AE._r305_rewrite_body_array_lines(lines, "organizations")
    blob = "\n".join(new)
    assert n >= 3
    assert "to.have.property('organizations')" in blob
    assert "body.organizations.length" in blob
    assert "body.organizations.forEach" in blob
    assert "'items'" not in blob and "body.items" not in blob
    # the item-level property('id') assertion is untouched (not 'items')
    assert "to.have.property('id')" in blob


def test_r305_i_noop_when_list_key_is_items():
    # a SUT that genuinely paginates under `items` → no rewrite
    lines = ["const body=pm.response.json();",
             "pm.expect(body).to.have.property('items');",
             "body.items.forEach(x=>{});"]
    _, n = _AE._r305_rewrite_body_array_lines(lines, "items")
    assert n == 0


_DETAIL_SHAPE_EPS = [{
    "method": "GET",
    "path": "/v1/regions/us-texas-1/infrastructure/servers/{id}",
    "response_body_shape": {"type": "object", "properties": {
        k: {"type": "string"} for k in
        ["id", "uid", "selfLink", "displayName", "description", "state",
         "status", "health", "createdAt", "updatedAt"]}},
}]


def test_g2_flags_hallucinated_property_against_complete_shape():
    parsed = {"item": [{"name": "GET server", "request": {
        "method": "GET",
        "url": {"raw": "{{base_url}}/v1/regions/us-texas-1/infrastructure/servers/server-1f9983ab"}},
        "event": [{"listen": "test", "script": {"exec": [
            "const body = pm.response.json();",
            "pm.expect(body).to.have.property('name');",        # hallucinated
            "pm.expect(body).to.have.property('displayName');"]}}]}]}  # real
    viols = validate_newman_grounded(parsed, project_id="p", captured_endpoints=_DETAIL_SHAPE_EPS)
    bad = [v for v in viols if v.kind == "response_shape_mismatch"]
    assert len(bad) == 1 and "name" in bad[0].hint


def test_g2_property_grounding_skips_sparse_shape():
    # only 3 captured keys (< 8) → do NOT flag (sample may be partial)
    eps = [{"method": "GET", "path": "/v1/x/{id}",
            "response_body_shape": {"type": "object",
                                    "properties": {"id": {}, "uid": {}, "state": {}}}}]
    parsed = {"item": [{"name": "GET x", "request": {
        "method": "GET", "url": {"raw": "{{base_url}}/v1/x/abc123def456"}},
        "event": [{"listen": "test", "script": {"exec": [
            "pm.expect(pm.response.json()).to.have.property('name');"]}}]}]}
    viols = validate_newman_grounded(parsed, project_id="p", captured_endpoints=eps)
    assert not any(v.kind == "response_shape_mismatch" for v in viols)


def test_validator_recurses_into_folders():
    """R305 — real Postman collections nest requests in folders (item[].item[]).
    Pre-R305 validate_newman_grounded only walked top-level items → EVERY check was
    silently inert on a folder-structured collection. Now it recurses to leaves."""
    parsed = {"item": [{"name": "Wave-1: Read Operations", "item": [
        {"name": "List Servers", "request": {
            "method": "GET",
            "url": {"raw": "{{base_url}}/v1/regions/us-texas-1/infrastructure/servers"}},
            "event": [{"listen": "test", "script": {"exec": [
                "const body = pm.response.json(); pm.expect(body).to.be.an('array');"]}}]},
    ]}]}
    viols = validate_newman_grounded(parsed, project_id="p", captured_endpoints=_SHAPE_EPS)
    # the folder-nested bare-array assertion IS reached and flagged
    assert any(v.kind == "response_shape_mismatch" for v in viols)
