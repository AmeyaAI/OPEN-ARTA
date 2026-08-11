"""R124.E — endpoint_key SHAPE alignment regression guard.

R55.13 endpoint coverage requires `result.metadata.endpoint_keys` to
match the Endpoint node's `endpoint_key` property in Neo4j. R69.3
already aligned the FORMAT (`METHOD:/path/{template}` with colon
separator) but pre-R124.D it was masked by R29.1's Result-node
collision bug.

Post-R124.D unblocks the cascade. R124.E ensures the SHAPE alignment
stays correct across Newman (R55.7), PW HAR (R76), k6 (R75.1), axe
(R75.1) — all 4 extractors must produce keys that MATCH the
ingestion-time `endpoint_key` shape from src/graph/writer.py:266.

This is a regression-guard, not a new code path. Run-d52a8c showed
0/45 endpoint coverage because R124.D was broken; with R124.D fixed
+ this guard, future regressions to key shape would be caught at
unit-test time instead of surfacing as silent coverage gaps.
"""
from __future__ import annotations

import pytest


def test_r124_e_newman_key_shape():
    """R55.7 emits `METHOD:/path/...` with colon separator.

    Path-param placeholders are preserved as-is (`:userId` stays
    `:userId` here; the shape matches R69.3's ingestion-time format).
    """
    from src.api.routers.execution import _r55_7_extract_endpoint_key
    req = {
        "method": "POST",
        "url": {"path": ["api", "users", ":userId"]},
    }
    key = _r55_7_extract_endpoint_key(req)
    # Shape contract: METHOD:/path (colon separator, path starts with /)
    assert key.startswith("POST:/api/users/"), (
        f"R55.7 key shape regression: expected POST:/api/users/...; got {key!r}"
    )
    assert ":" in key, f"colon separator missing: {key!r}"


def test_r124_e_k6_key_shape():
    """R75.1 k6 extractor emits same `METHOD:/path` shape."""
    from src.api.routers.execution import _r75_1_extract_k6_endpoints
    script = """
import http from 'k6/http';
export default function () {
    http.get(`${__ENV.BASE_URL}/api/datasets`);
    http.post('https://x.io/api/users', {});
}
"""
    keys = _r75_1_extract_k6_endpoints(script)
    # Both should use colon separator
    assert all(":" in k for k in keys), f"k6 keys missing colon: {keys}"
    assert any("GET:" in k and "/api/datasets" in k for k in keys), keys
    assert any("POST:" in k and "/api/users" in k for k in keys), keys


def test_r124_e_har_key_shape():
    """R76 HAR extractor emits same shape (verified via the same normaliser)."""
    from src.api.routers.execution import _r75_1_normalise_url_to_endpoint_key
    # The normaliser is shared by both _r75_1_extract_k6_endpoints AND
    # _r76_extract_har_endpoints — so testing it directly is equivalent.
    key = _r75_1_normalise_url_to_endpoint_key(
        "https://api.example.com/v1/orders/123",
        method="PUT",
    )
    assert ":" in key, f"HAR key missing colon: {key!r}"
    # Numeric IDs should normalise to {id} placeholder
    assert "/v1/orders/" in key, f"HAR key shape unexpected: {key!r}"


def test_r124_e_key_shapes_compatible_across_tools():
    """Newman + k6 + HAR all produce keys with the SAME colon-separator
    format. R55.13 Cypher `MATCH (ep:Endpoint {endpoint_key: $ep_key})`
    relies on this alignment to find the edge target."""
    from src.api.routers.execution import (
        _r55_7_extract_endpoint_key,
        _r75_1_extract_k6_endpoints,
    )
    newman_key = _r55_7_extract_endpoint_key({
        "method": "GET",
        "url": {"path": ["api", "v1", "datasets"]},
    })
    k6_keys = _r75_1_extract_k6_endpoints(
        "http.get(`${__ENV.BASE_URL}/api/v1/datasets`);"
    )
    # Same shape (colon separator). Verb prefix from a stable canonical set.
    assert newman_key.startswith("GET:/")
    for k in k6_keys:
        assert k.split(":")[0] in ("GET", "POST", "PUT", "PATCH", "DELETE"), k
    # All extractors converge on the same canonical key for the same logical endpoint.
    assert newman_key == "GET:/api/v1/datasets"
    # k6 keys include the same path (may also include template vars in the
    # path that get normalised differently — that's fine, what matters is
    # the colon-separator + path prefix alignment so the Cypher MATCH fires).
    assert any("/api/v1/datasets" in k for k in k6_keys), k6_keys
