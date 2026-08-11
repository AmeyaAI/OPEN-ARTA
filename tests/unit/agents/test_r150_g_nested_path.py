"""R150.G — extend R111.G assertion-field grounding with NESTED-PATH traversal.

Pre-R150.G: R111.G regex captured only `pm.response.json().<field>` single
segments. Dotted paths like `json.insight.metric` were checked at the top-
level segment (`insight`) but the LLM could still hallucinate `.metric` on
that valid `insight` parent. Live evidence (Iter 9 run-8b552c): 38 × Newman
HTTP-200-marked-FAIL crashed with `Cannot read properties of undefined
(reading 'metric')` on assertions like `json.insight.metric` because the
top-level `insight` was valid but the nested `.metric` access wasn't
grounded against `insight`'s sub-schema.

R150.G adds `_r150_g_extract_nested_paths` which recursively walks the
OpenAPI response schema collecting dotted paths (e.g. `insight`,
`insight.metric`, `insight.org_id`). The validator regex now captures
full dotted-path strings and validates the END-TO-END path against the
union of nested-path sets across all declared response codes.

Killswitch `ARTA_R150_G_NESTED_PATH_DISABLE=1` reverts to top-level-only
(legacy R111.G behavior).
"""
from __future__ import annotations

import os

from src.agents.grounding_validator import (
    _r111_g_validate_assertion_fields,
    _r150_g_extract_nested_paths,
)


def _build_openapi_nested(success_schema: dict | None = None,
                          error_schema: dict | None = None) -> dict:
    """Build minimal OpenAPI for `GET /api/v1/insight` with nested response."""
    paths_dict = {
        "/api/v1/insight": {
            "get": {"responses": {}}
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


# ─── _r150_g_extract_nested_paths helper tests ──────────────────────────────


def test_r150_g_flat_schema_extracts_top_level_paths():
    """Flat schema → set of top-level keys (no dots)."""
    resp_obj = {
        "content": {"application/json": {"schema": {
            "properties": {"metric": {"type": "string"},
                           "value": {"type": "number"}},
        }}}
    }
    paths = _r150_g_extract_nested_paths(resp_obj, {})
    assert paths == {"metric", "value"}, f"got: {paths}"


def test_r150_g_nested_object_yields_dotted_paths():
    """Nested object yields parent + parent.child dotted entries."""
    resp_obj = {
        "content": {"application/json": {"schema": {
            "properties": {
                "insight": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string"},
                        "org_id": {"type": "string"},
                    },
                },
                "meta": {
                    "type": "object",
                    "properties": {"total_count": {"type": "integer"}},
                },
            },
        }}}
    }
    paths = _r150_g_extract_nested_paths(resp_obj, {})
    assert "insight" in paths
    assert "insight.metric" in paths
    assert "insight.org_id" in paths
    assert "meta" in paths
    assert "meta.total_count" in paths


def test_r150_g_array_items_descended_in_place():
    """Array `items` schema descends under same prefix (PW/Newman access
    `array[0].field` flattens to `array.field` in our access regex)."""
    resp_obj = {
        "content": {"application/json": {"schema": {
            "properties": {
                "records": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "metric_name": {"type": "string"},
                            "value": {"type": "number"},
                        },
                    },
                },
            },
        }}}
    }
    paths = _r150_g_extract_nested_paths(resp_obj, {})
    assert "records" in paths
    assert "records.metric_name" in paths
    assert "records.value" in paths


def test_r150_g_empty_or_missing_schema_returns_empty_set():
    """Missing schema → empty set (graceful skip; matches R113.K convention)."""
    assert _r150_g_extract_nested_paths({}, {}) == set()
    assert _r150_g_extract_nested_paths(None, {}) == set()  # type: ignore[arg-type]
    no_props = {"content": {"application/json": {"schema": {"type": "object"}}}}
    # No `properties` → no paths
    assert _r150_g_extract_nested_paths(no_props, {}) == set()


# ─── _r111_g_validate_assertion_fields end-to-end tests with R150.G ──────────


def test_r150_g_top_level_assertion_regression_preserved():
    """Top-level field assertion (no dots) still uses R111.G/R113.K legacy
    check. Regression guard: R150.G must not break single-segment access."""
    openapi = _build_openapi_nested(
        success_schema={"properties": {"metric": {"type": "string"}}},
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
        f"top-level assertion on declared field should pass; got: "
        f"{[v.symbol for v in violations]}"
    )


def test_r150_g_two_level_nested_path_passes_when_grounded():
    """2-level nested path `json.insight.metric` validates against the
    insight sub-schema. KEYSTONE — closes Iter 9's 38 × 200-FAIL cluster."""
    openapi = _build_openapi_nested(
        success_schema={"properties": {
            "insight": {
                "type": "object",
                "properties": {"metric": {"type": "string"}},
            },
        }},
    )
    script = [
        "const json = pm.response.json();",
        "pm.expect(json.insight.metric).to.equal('sales');",
    ]
    violations = _r111_g_validate_assertion_fields(
        name="GetInsight", method="GET", path="/api/v1/insight",
        script_lines=script, openapi_spec=openapi,
    )
    assert violations == [], (
        f"R150.G: 2-level nested grounded path should pass; got: "
        f"{[v.symbol for v in violations]}"
    )


def test_r150_g_three_level_nested_path_passes():
    """3-level nested path validates against deeper sub-schemas."""
    openapi = _build_openapi_nested(
        success_schema={"properties": {
            "insight": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "object",
                        "properties": {"value": {"type": "number"}},
                    },
                },
            },
        }},
    )
    script = [
        "const json = pm.response.json();",
        "pm.expect(json.insight.metric.value).to.equal(125);",
    ]
    violations = _r111_g_validate_assertion_fields(
        name="GetInsight", method="GET", path="/api/v1/insight",
        script_lines=script, openapi_spec=openapi,
    )
    assert violations == [], (
        f"R150.G: 3-level nested path should pass; got: "
        f"{[v.symbol for v in violations]}"
    )


def test_r150_g_missing_nested_segment_flagged_with_full_path_in_hint():
    """Nested access where intermediate segment is missing → violation;
    hint surfaces the FULL dotted path so the LLM sees which segment is wrong."""
    openapi = _build_openapi_nested(
        success_schema={"properties": {
            "insight": {
                "type": "object",
                "properties": {"metric": {"type": "string"}},
            },
        }},
    )
    script = [
        "const json = pm.response.json();",
        # `insight.phantom_field` — `insight` exists but `.phantom_field` doesn't
        "pm.expect(json.insight.phantom_field).to.equal('x');",
    ]
    violations = _r111_g_validate_assertion_fields(
        name="GetInsight", method="GET", path="/api/v1/insight",
        script_lines=script, openapi_spec=openapi,
    )
    assert len(violations) == 1, f"expected 1 violation, got: {violations}"
    # Symbol surfaces the full dotted path
    assert "insight.phantom_field" in violations[0].symbol
    assert violations[0].kind == "unknown_response_field"


def test_r150_g_killswitch_reverts_to_top_level_check():
    """Setting ARTA_R150_G_NESTED_PATH_DISABLE=1 falls back to legacy
    top-level-only behavior. With killswitch on, `json.insight.bogus` is
    accepted because the FIRST segment `insight` is in valid_fields."""
    openapi = _build_openapi_nested(
        success_schema={"properties": {
            "insight": {
                "type": "object",
                "properties": {"metric": {"type": "string"}},
            },
        }},
    )
    script = [
        "const json = pm.response.json();",
        "pm.expect(json.insight.bogus_nested).to.equal('x');",
    ]
    os.environ["ARTA_R150_G_NESTED_PATH_DISABLE"] = "1"
    try:
        violations = _r111_g_validate_assertion_fields(
            name="GetInsight", method="GET", path="/api/v1/insight",
            script_lines=script, openapi_spec=openapi,
        )
    finally:
        del os.environ["ARTA_R150_G_NESTED_PATH_DISABLE"]
    # With killswitch: first-segment 'insight' is valid (top-level only) →
    # legacy behavior accepts the access without traversing
    assert violations == [], (
        f"R150.G killswitch: should fall back to top-level check; got: "
        f"{[v.symbol for v in violations]}"
    )
