"""R113.K — extend R111.G assertion-field grounding to error response schemas.

Pre-R113.K: R111.G validated against the OpenAPI 2xx response schema only.
Live evidence (run-78bb3d): 40 × items marked HTTP 200-FAIL because the SUT
actually returned 401 with body `{"error": "API key missing"}` and the
LLM-generated `pm.response.json().metric` assertion crashed with
`Cannot read properties of undefined (reading 'metric')`. The LLM's
assertion targeted the 2xx shape; R111.G correctly checked 2xx but didn't
union 4xx/5xx schemas → couldn't surface that the SUT might also return
error responses needing different field access.

R113.K unions success + error response schemas. Field grounded in EITHER
is accepted. Field grounded in NEITHER is flagged with a BEFORE/AFTER hint
that includes both success + error path examples.
"""
from __future__ import annotations

from src.agents.grounding_validator import _r111_g_validate_assertion_fields


def _build_openapi(success_schema: dict | None, error_schema: dict | None) -> dict:
    """Build a minimal OpenAPI spec for `GET /api/v1/insight`."""
    paths_dict = {
        "/api/v1/insight": {
            "get": {
                "responses": {},
            }
        }
    }
    responses = paths_dict["/api/v1/insight"]["get"]["responses"]
    if success_schema is not None:
        responses["200"] = {
            "content": {"application/json": {"schema": success_schema}},
        }
    if error_schema is not None:
        responses["401"] = {
            "content": {"application/json": {"schema": error_schema}},
        }
    return {"paths": paths_dict, "components": {"schemas": {}}}


def test_r113_k_field_in_2xx_only_passes():
    """Assertion against a 2xx-declared field → no violation (existing R111.G)."""
    openapi = _build_openapi(
        success_schema={"properties": {"metric": {"type": "string"},
                                       "value": {"type": "number"}}},
        error_schema=None,
    )
    script = [
        "const json = pm.response.json();",
        "pm.expect(json.metric).to.equal('sales');",
    ]
    violations = _r111_g_validate_assertion_fields(
        name="GetInsight", method="GET", path="/api/v1/insight",
        script_lines=script, openapi_spec=openapi,
    )
    assert violations == [], (
        f"clean 2xx assertion should produce 0 violations, got: {[v.symbol for v in violations]}"
    )


def test_r113_k_field_in_4xx_schema_now_passes():
    """R113.K KEYSTONE: assertion against a 401-declared field → no violation."""
    openapi = _build_openapi(
        success_schema={"properties": {"metric": {"type": "string"}}},
        error_schema={"properties": {"error": {"type": "string"},
                                     "message": {"type": "string"}}},
    )
    script = [
        "const json = pm.response.json();",
        "pm.expect(json.error).to.equal('API key missing');",  # 4xx-schema field
    ]
    violations = _r111_g_validate_assertion_fields(
        name="GetInsight", method="GET", path="/api/v1/insight",
        script_lines=script, openapi_spec=openapi,
    )
    assert violations == [], (
        f"R113.K: field 'error' is in 4xx schema, should NOT flag. got: "
        f"{[v.symbol for v in violations]}"
    )


def test_r113_k_field_in_neither_schema_flags():
    """Assertion against a field in NEITHER 2xx nor 4xx schema → flag."""
    openapi = _build_openapi(
        success_schema={"properties": {"metric": {"type": "string"}}},
        error_schema={"properties": {"error": {"type": "string"}}},
    )
    script = [
        "const json = pm.response.json();",
        "pm.expect(json.phantom_field).to.exist;",  # not in any schema
    ]
    violations = _r111_g_validate_assertion_fields(
        name="GetInsight", method="GET", path="/api/v1/insight",
        script_lines=script, openapi_spec=openapi,
    )
    assert len(violations) == 1, f"expected 1 violation, got: {violations}"
    assert "phantom_field" in violations[0].symbol
    # R113.K hint should mention both success + error fields
    assert "2xx" in violations[0].hint.lower() or "2xx fields" in violations[0].hint
    assert "4xx" in violations[0].hint.lower() or "error" in violations[0].hint.lower()


def test_r113_k_no_error_schema_conservative_skip_hint():
    """When only 2xx declared, R113.K conservatively notes the gap in hint."""
    openapi = _build_openapi(
        success_schema={"properties": {"metric": {"type": "string"}}},
        error_schema=None,
    )
    script = [
        "const json = pm.response.json();",
        "pm.expect(json.unknown_field).to.exist;",
    ]
    violations = _r111_g_validate_assertion_fields(
        name="GetInsight", method="GET", path="/api/v1/insight",
        script_lines=script, openapi_spec=openapi,
    )
    assert len(violations) == 1
    # Hint should note error schemas not declared
    assert "error" in violations[0].hint.lower(), (
        f"hint should reference error-schema gap: {violations[0].hint[:300]}"
    )
