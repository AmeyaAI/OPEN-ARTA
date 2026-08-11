"""R303.C — contract-grounded 4xx attribution. Generalizes the 404 endpoint-existence
grounding to 400/409/422 and adds 400 body-contract grounding, so these move out of the
"unattributable" bucket (the 82%) into a confident, evidence-backed verdict — while
preserving honest abstention (None) when there is no proof either way."""
from __future__ import annotations

from src.agents.defect_intel import _r258_skel, _r303_c_decompose_4xx


def _caps(*paths):
    return {_r258_skel(p, aggressive=False) for p in paths}


# ── endpoint-existence (branch 1) — applies to ANY 4xx ─────────────────────────
def test_400_on_invented_endpoint_is_test_gen_bug():
    v = _r303_c_decompose_4xx(
        status_code=400, path="/v1/regions/global/nonexistent",
        captured_keys=_caps("/v1/regions/global/organizations"))
    assert v and v["triage_category"] == "test_gen_bug"
    assert v["test_gen_bug_subtype"] == "unknown_endpoint" and v["triage_confidence"] >= 0.85


def test_409_and_422_on_invented_endpoint_also_test_gen_bug():
    for sc in (409, 422):
        v = _r303_c_decompose_4xx(status_code=sc, path="/v1/made/up",
                                  captured_keys=_caps("/v1/regions"))
        assert v and v["triage_category"] == "test_gen_bug"


def test_source_verified_endpoint_is_not_called_invented():
    # not in captured, but source-verified → must NOT be branch-1 test_gen_bug
    v = _r303_c_decompose_4xx(
        status_code=400, path="/v1/regions/global/servers",
        captured_keys=_caps("/v1/regions/global/organizations"),
        source_verified=True)
    assert v is None  # no body → honest abstention, not a false ARTA-bug


# ── 400 body-contract grounding (branch 2) ─────────────────────────────────────
# realistic captured shape: _r95_4's captured-shape fallback only enforces when the
# shape is well-populated (>=5 distinct fields) — a conservatism against sparse shapes.
_EP = [{
    "method": "POST", "path": "/v1/regions/global/organizations",
    "request_body_shape": {
        "name": "string", "displayName": "string", "description": "string",
        "status": "string", "version": "string", "memberCount": "number",
    },
}]


def test_400_body_with_undeclared_field_is_test_gen_bug():
    v = _r303_c_decompose_4xx(
        status_code=400, path="/v1/regions/global/organizations", method="POST",
        request_body_raw='{"name": "x", "bogusField": 1}',
        captured_keys=_caps("/v1/regions/global/organizations"),
        captured_endpoints=_EP)
    assert v and v["triage_category"] == "test_gen_bug"
    assert v["test_gen_bug_subtype"] == "request_schema_violation"


def test_400_conforming_body_on_real_endpoint_is_sut_contract_change():
    v = _r303_c_decompose_4xx(
        status_code=400, path="/v1/regions/global/organizations", method="POST",
        request_body_raw='{"name": "x", "displayName": "y"}',
        captured_keys=_caps("/v1/regions/global/organizations"),
        captured_endpoints=_EP)
    assert v and v["triage_category"] == "sut_contract_change"
    assert v["triage_confidence"] >= 0.75   # crosses the R259 0.7 gate as SUT


# ── honest abstention + guards ─────────────────────────────────────────────────
def test_400_on_captured_endpoint_without_body_abstains():
    # captured endpoint, no body to ground → None (falls through, no over-attribution)
    v = _r303_c_decompose_4xx(
        status_code=400, path="/v1/regions/global/organizations",
        captured_keys=_caps("/v1/regions/global/organizations"))
    assert v is None


def test_non_4xx_returns_none():
    assert _r303_c_decompose_4xx(status_code=500, path="/x", captured_keys=_caps("/x")) is None
    assert _r303_c_decompose_4xx(status_code=200, path="/x", captured_keys=_caps("/x")) is None


def test_no_captured_keys_and_no_body_abstains():
    assert _r303_c_decompose_4xx(status_code=400, path="/x") is None


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R303_C_DISABLE", "1")
    v = _r303_c_decompose_4xx(status_code=400, path="/invented",
                              captured_keys=_caps("/v1/regions"))
    assert v is None
