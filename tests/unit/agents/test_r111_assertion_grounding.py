"""R111.G — Newman pm.test assertion-field grounding against OpenAPI response schema.

Live evidence (run-99dbcf): 363 Newman items returned HTTP 200 but their
`pm.test` assertions failed because the LLM cited response fields that
don't exist in the SUT's actual response schema. Pre-R111.G these flowed
to R34.1 → `sut_regression` (poisoning ARTA's Jira queue with its own
gen bugs). R111.G surfaces them at gen-time as `unknown_response_field`.
"""
from __future__ import annotations

from src.agents.grounding_validator import validate_newman_grounded


_OPENAPI = {
    "openapi": "3.0.0",
    "paths": {
        "/api/v1/datasets": {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "name": {"type": "string"},
                                        "owner": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


def _newman_item(name, method, path, exec_lines):
    return {
        "item": [{
            "name": name,
            "request": {
                "method": method,
                "url": {"raw": "{{base_url}}" + path, "host": ["{{base_url}}"], "path": path.strip("/").split("/")},
                "header": [],
            },
            "event": [{
                "listen": "test",
                "script": {"exec": exec_lines, "type": "text/javascript"},
            }],
        }],
    }


def test_r111_g_hallucinated_response_field_flagged():
    """`json.metric` not in 2xx response schema → unknown_response_field."""
    collection = _newman_item(
        "list datasets", "GET", "/api/v1/datasets",
        [
            "pm.test('has metric', () => {",
            "  const json = pm.response.json();",
            "  pm.expect(json.metric).to.equal('sales');",
            "});",
        ],
    )
    vs = validate_newman_grounded(
        collection, project_id="x", env_vars={"base_url": ""},
        captured_endpoints=[{"method": "GET", "path": "/api/v1/datasets"}],
        openapi_spec=_OPENAPI,
    )
    rv = [v for v in vs if v.kind == "unknown_response_field"]
    assert rv, f"expected unknown_response_field: {[v.kind for v in vs]}"
    assert "metric" in rv[0].symbol
    assert "BEFORE" in rv[0].hint and "AFTER" in rv[0].hint
    # Should surface VALID fields from the schema
    assert "id" in rv[0].hint or "name" in rv[0].hint or "owner" in rv[0].hint


def test_r111_g_real_response_field_not_flagged():
    """`json.name` IS in schema → no violation."""
    collection = _newman_item(
        "list datasets", "GET", "/api/v1/datasets",
        [
            "pm.test('has name', () => {",
            "  const json = pm.response.json();",
            "  pm.expect(json.name).to.exist;",
            "});",
        ],
    )
    vs = validate_newman_grounded(
        collection, project_id="x", env_vars={"base_url": ""},
        captured_endpoints=[{"method": "GET", "path": "/api/v1/datasets"}],
        openapi_spec=_OPENAPI,
    )
    rv = [v for v in vs if v.kind == "unknown_response_field"]
    assert not rv, f"valid field flagged spuriously: {[v.symbol for v in rv]}"


def test_r111_g_var_bound_pm_response_json_tracked():
    """`let X = pm.response.json(); X.<field>` — track var-bound access."""
    collection = _newman_item(
        "list datasets", "GET", "/api/v1/datasets",
        [
            "let data = pm.response.json();",
            "pm.test('check', () => pm.expect(data.phantom_field).to.exist);",
        ],
    )
    vs = validate_newman_grounded(
        collection, project_id="x", env_vars={"base_url": ""},
        captured_endpoints=[{"method": "GET", "path": "/api/v1/datasets"}],
        openapi_spec=_OPENAPI,
    )
    rv = [v for v in vs if v.kind == "unknown_response_field"]
    assert rv, "var-bound json access should flag hallucinated field"
    assert "phantom_field" in rv[0].symbol


def test_r111_g_no_openapi_graceful():
    """Skip silently when openapi_spec is absent (cold-start)."""
    collection = _newman_item(
        "list datasets", "GET", "/api/v1/datasets",
        ["pm.test('x', () => pm.expect(json.anything).to.exist);"],
    )
    vs = validate_newman_grounded(
        collection, project_id="x", env_vars={"base_url": ""},
        captured_endpoints=[{"method": "GET", "path": "/api/v1/datasets"}],
        openapi_spec=None,
    )
    rv = [v for v in vs if v.kind == "unknown_response_field"]
    assert not rv, "cold-start should not flag"
