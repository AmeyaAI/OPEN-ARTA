"""R95.4 regression tests for Newman body-field schema grounding.

Pre-R95.4 the Newman gen pipeline checked endpoint URL existence
(unknown_endpoint) but NOT body-field correctness. The LLM invented
body field names not in the SUT's OpenAPI request schema → SUT
returned 400 with "field X is unexpected" or "field Y is missing".

Live evidence (run-2f077d): 135 × HTTP 400 from Newman items.

R95.4 extends validate_newman_grounded() with an `unknown_request_field`
kind that flags JSON body fields absent from OpenAPI's
`requestBody.content.application/json.schema.properties`. The R57.1
retry-with-hint loop then surfaces "field X not in schema; valid:
[list]" → LLM corrects on retry.

These tests lock the contract.
"""
from __future__ import annotations

from src.agents.grounding_validator import validate_newman_grounded


# ── Helpers: build canonical OpenAPI + Newman fixtures ────────────────


def _spec(properties: dict, *, method: str = "POST", path: str = "/api/users") -> dict:
    """Construct a minimal OpenAPI 3.0 spec with a single endpoint."""
    return {
        "openapi": "3.0.0",
        "paths": {
            path: {
                method.lower(): {
                    "operationId": f"{method.lower()}{path.replace('/', '_')}",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": properties,
                                }
                            }
                        }
                    },
                }
            }
        },
    }


def _item(*, method: str = "POST", path: str = "/api/users", body_raw: str = "{}") -> dict:
    """Construct a Newman item with a JSON body for grounding test."""
    return {
        "item": [
            {
                "name": "test-item",
                "request": {
                    "method": method,
                    "url": {"raw": "{{base_url}}" + path, "path": path.lstrip("/").split("/")},
                    "header": [
                        {"key": "Content-Type", "value": "application/json"},
                    ],
                    "body": {"mode": "raw", "raw": body_raw},
                },
                "event": [
                    {
                        "listen": "test",
                        "script": {"exec": ['pm.test("ok", () => pm.response.to.have.status(200))']},
                    }
                ],
            }
        ]
    }


# ── R95.4 tests ───────────────────────────────────────────────────────


def test_unknown_field_flagged():
    """Body has 'frobnicate' which isn't in OpenAPI schema → violation."""
    spec = _spec({"name": {"type": "string"}, "email": {"type": "string"}})
    collection = _item(body_raw='{"name": "x", "frobnicate": true}')
    violations = validate_newman_grounded(
        collection, project_id="test", env_vars={}, openapi_spec=spec,
    )
    unknown = [v for v in violations if v.kind == "unknown_request_field"]
    assert len(unknown) == 1
    assert "frobnicate" in unknown[0].symbol


def test_all_valid_fields_no_violation():
    """All body fields exist in schema → no unknown_request_field violation."""
    spec = _spec({"name": {"type": "string"}, "email": {"type": "string"}})
    collection = _item(body_raw='{"name": "x", "email": "y@z.com"}')
    violations = validate_newman_grounded(
        collection, project_id="test", env_vars={}, openapi_spec=spec,
    )
    unknown = [v for v in violations if v.kind == "unknown_request_field"]
    assert unknown == []


def test_get_method_skipped():
    """GET method doesn't have request body — body-field check skips."""
    spec = _spec({"name": {"type": "string"}}, method="GET")
    collection = _item(method="GET", body_raw='{"frobnicate": true}')
    violations = validate_newman_grounded(
        collection, project_id="test", env_vars={}, openapi_spec=spec,
    )
    unknown = [v for v in violations if v.kind == "unknown_request_field"]
    assert unknown == []


def test_openapi_spec_absent_skipped():
    """No OpenAPI spec → R95.4 check skipped entirely (cold-start)."""
    collection = _item(body_raw='{"anything_goes": true}')
    violations = validate_newman_grounded(
        collection, project_id="test", env_vars={}, openapi_spec=None,
    )
    unknown = [v for v in violations if v.kind == "unknown_request_field"]
    assert unknown == []


def test_ref_resolved_one_level_deep():
    """When schema is `$ref`, resolve to components.schemas and validate."""
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/api/users": {
                "post": {
                    "operationId": "createUser",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/User"}
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
                }
            }
        },
    }
    collection = _item(body_raw='{"name": "x", "phantom": "field"}')
    violations = validate_newman_grounded(
        collection, project_id="test", env_vars={}, openapi_spec=spec,
    )
    unknown = [v for v in violations if v.kind == "unknown_request_field"]
    assert len(unknown) == 1
    assert "phantom" in unknown[0].symbol


def test_malformed_body_skipped_gracefully():
    """Non-JSON body (form-data) → R95.4 silently skips."""
    spec = _spec({"name": {"type": "string"}})
    collection = _item(body_raw='not-json-at-all')
    # Should not crash; no unknown_request_field violation
    violations = validate_newman_grounded(
        collection, project_id="test", env_vars={}, openapi_spec=spec,
    )
    unknown = [v for v in violations if v.kind == "unknown_request_field"]
    assert unknown == []


# ── R143.C — captured response_body_shape fallback ─────────────


def _captured_endpoint(method, path, shape_keys):
    """Build a captured_endpoint dict with the given response_body_shape."""
    return {
        "method": method,
        "path": path,
        "response_body_shape": {k: None for k in shape_keys},
    }


def test_r143_c_captured_shape_flags_hallucinated_fields():
    """R143.C — when OpenAPI is absent, captured response_body_shape
    grounds body fields. Body with mostly-unknown fields → violation."""
    from src.agents.grounding_validator import _r95_4_validate_body_fields
    import json
    body = json.dumps({
        "totallyMadeUp1": "v",
        "totallyMadeUp2": "v",
        "totallyMadeUp3": "v",
        "actualKnownField": "v",
    })
    captured = [_captured_endpoint("POST", "/api/collection/items",
        ["actualKnownField", "id", "createdAt", "updatedAt", "status", "name"])]
    violations = _r95_4_validate_body_fields(
        name="POST /api/collection/items",
        method="POST",
        path="/api/collection/items",
        body_raw=body,
        openapi_spec=None,
        captured_endpoints=captured,
    )
    assert len(violations) == 1
    assert violations[0].kind == "unknown_request_field"
    assert "R143.C" in violations[0].hint
    assert "captured response shape" in violations[0].hint


def test_r143_c_skips_when_shape_under_5_keys():
    """Conservative threshold: captured shape with <5 keys is too thin
    to flag confidently (could be a partial response capture). Skip."""
    from src.agents.grounding_validator import _r95_4_validate_body_fields
    import json
    body = json.dumps({"a": 1, "b": 2, "c": 3, "d": 4})
    captured = [_captured_endpoint("POST", "/api/collection/items", ["x", "y"])]
    violations = _r95_4_validate_body_fields(
        name="POST /api/collection/items",
        method="POST",
        path="/api/collection/items",
        body_raw=body,
        openapi_spec=None,
        captured_endpoints=captured,
    )
    assert violations == []


def test_r143_c_skips_when_unknown_ratio_below_50pct():
    """Threshold: only flag when ≥50% of body fields are unknown. With
    a single hallucinated field out of 5, ratio = 20% → too noisy."""
    from src.agents.grounding_validator import _r95_4_validate_body_fields
    import json
    body = json.dumps({"a": 1, "b": 2, "c": 3, "d": 4, "hallucinated": 5})
    # captured shape covers 4 of 5 body fields
    captured = [_captured_endpoint("POST", "/api/x",
        ["a", "b", "c", "d", "extra1", "extra2"])]
    violations = _r95_4_validate_body_fields(
        name="POST /api/x",
        method="POST",
        path="/api/x",
        body_raw=body,
        openapi_spec=None,
        captured_endpoints=captured,
    )
    assert violations == []


def test_r143_c_openapi_wins_over_captured_when_both_present():
    """When OpenAPI declares schema AND captured shape exists, OpenAPI
    takes precedence (Tier 1 returns; Tier 2 R143.C never runs)."""
    from src.agents.grounding_validator import _r95_4_validate_body_fields
    import json
    body = json.dumps({"name": "x", "extra_invented": "y"})
    openapi = {
        "paths": {
            "/api/users": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"properties": {"name": {"type": "string"}}}
                            }
                        }
                    }
                }
            }
        }
    }
    # Captured shape is wider, but OpenAPI rules
    captured = [_captured_endpoint("POST", "/api/users",
        ["name", "extra_invented", "x", "y", "z", "w"])]
    violations = _r95_4_validate_body_fields(
        name="POST /api/users",
        method="POST",
        path="/api/users",
        body_raw=body,
        openapi_spec=openapi,
        captured_endpoints=captured,
    )
    assert len(violations) == 1
    # OpenAPI hint (NOT R143.C hint) — proves Tier 1 fired
    assert "R143.C" not in violations[0].hint
    assert "OpenAPI" in violations[0].hint
