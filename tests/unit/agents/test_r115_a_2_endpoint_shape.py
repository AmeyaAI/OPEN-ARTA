"""R115.A.2 — Newman endpoint-shape validator.

Catches 3 LLM-hallucinated path patterns that `validate_newman_grounded`
doesn't surface (they're shape-correct hallucinations, not unknown_endpoint):
  1. Static-asset paths (/static/, *.js, *.css, etc.)
  2. SPA frontend routes (/login, /settings, /dashboard, etc.)
  3. Likely typos (edit-distance 1-2 from captured endpoint segments)

Live evidence (run-8da91d): 238 × 404 cluster, 85% (~200) from these.
"""
from __future__ import annotations

from src.agents.grounding_validator import validate_newman_endpoint_shape


def _collection_with_url(url: str, item_name: str = "test") -> dict:
    """Build a minimal Postman v2.1 collection with one item."""
    return {
        "item": [{
            "name": item_name,
            "request": {
                "method": "GET",
                "url": {"raw": url},
                "header": [],
            },
        }],
    }


def test_r115_a_2_static_asset_flagged():
    """`/static/js/bundle.js` → endpoint_shape_mismatch."""
    coll = _collection_with_url("{{base_url}}/static/js/bundle.js")
    violations = validate_newman_endpoint_shape(coll, captured_endpoints=[])
    assert any("static_asset" in v.symbol for v in violations), (
        f"static asset should flag: {[v.symbol for v in violations]}"
    )


def test_r115_a_2_frontend_route_flagged():
    """`/settings` → endpoint_shape_mismatch (frontend route, not API)."""
    coll = _collection_with_url("{{base_url}}/settings")
    violations = validate_newman_endpoint_shape(coll, captured_endpoints=[])
    assert any("frontend_route" in v.symbol for v in violations), (
        f"frontend route should flag: {[v.symbol for v in violations]}"
    )


def test_r115_a_2_typo_flagged_against_captured():
    """`/api/v1/organizationss` (typo) → typo:... violation (matches captured '/api/v1/organization')."""
    captured = [{"method": "GET", "path": "/api/v1/organization"}]
    coll = _collection_with_url("{{base_url}}/api/v1/organizationss")
    violations = validate_newman_endpoint_shape(coll, captured_endpoints=captured)
    assert any("typo" in v.symbol for v in violations), (
        f"typo should flag: {[v.symbol for v in violations]}"
    )


def test_r115_a_2_legitimate_api_path_not_flagged():
    """`/api/v1/datasets` is a legitimate API path — no violation."""
    captured = [{"method": "GET", "path": "/api/v1/datasets"}]
    coll = _collection_with_url("{{base_url}}/api/v1/datasets")
    violations = validate_newman_endpoint_shape(coll, captured_endpoints=captured)
    assert violations == [], (
        f"legitimate path should NOT flag: {[v.symbol for v in violations]}"
    )


def test_r115_a_2_api_prefix_login_not_flagged():
    """`/api/auth/login` is API endpoint despite `login` substring — not flagged."""
    coll = _collection_with_url("{{base_url}}/api/auth/login")
    violations = validate_newman_endpoint_shape(coll, captured_endpoints=[])
    # `/api/auth/login` STARTS with `/api/`, so the frontend-route guard doesn't fire
    assert not any("frontend_route" in v.symbol for v in violations), (
        f"`/api/auth/login` should NOT flag (it's an API endpoint): {[v.symbol for v in violations]}"
    )
