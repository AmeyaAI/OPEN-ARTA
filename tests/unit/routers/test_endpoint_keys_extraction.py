"""R75.1 — unit tests for k6 / ZAP / Axe endpoint_keys helpers.

Pre-R75.1 only Newman emitted `metadata.endpoint_keys` (via R55.7).
R72.4's per-endpoint SUT health rollup + R55.13's coverage metric
were therefore Newman-only — a partial picture of which SUT
endpoints have tests covering them.

R75.1 adds:
  - `_r75_1_normalise_url_to_endpoint_key(url, method)` — generic URL
    → canonical `METHOD:path` key. Used by ZAP + Axe (single URL per
    scan).
  - `_r75_1_extract_k6_endpoints(script_content)` — parse k6 script
    source for `http.get/post/put/...` and `http.request('METHOD',
    url)` calls. Returns deduped list of `METHOD:path` keys.

These tests lock the helper contracts so future contributors can't
break the cross-tool aggregation in R72.4 / R55.13 silently.
"""
from __future__ import annotations

import pytest

from src.api.routers.execution import (
    _r75_1_normalise_url_to_endpoint_key,
    _r75_1_extract_k6_endpoints,
)


@pytest.mark.parametrize("url,method,expected", [
    # Absolute URLs
    ("https://api.x/api/users/123", "GET", "GET:/api/users/123"),
    ("http://localhost:8080/health", "POST", "POST:/health"),
    # Relative paths
    ("/api/orders", "POST", "POST:/api/orders"),
    ("/v1/users/{id}", "PUT", "PUT:/v1/users/{id}"),
    # Trailing slash trimmed (except root)
    ("/api/users/", "GET", "GET:/api/users"),
    ("/", "GET", "GET:/"),
    # Query string stripped
    ("/api/users?filter=active", "GET", "GET:/api/users"),
    # Method case-normalised
    ("/api/x", "post", "POST:/api/x"),
    # Default method when not provided
    ("/api/y", None, "GET:/api/y"),
])
def test_normalise_url_to_endpoint_key(url, method, expected):
    if method is None:
        result = _r75_1_normalise_url_to_endpoint_key(url)
    else:
        result = _r75_1_normalise_url_to_endpoint_key(url, method=method)
    assert result == expected


def test_normalise_handles_empty_and_invalid():
    assert _r75_1_normalise_url_to_endpoint_key("") is None
    assert _r75_1_normalise_url_to_endpoint_key(None) is None  # type: ignore[arg-type]
    assert _r75_1_normalise_url_to_endpoint_key(123) is None  # type: ignore[arg-type]


def test_extract_k6_basic_method_calls():
    """k6 `http.get/post/put/delete/...` extraction."""
    script = """
    import http from 'k6/http';
    export default function() {
        http.get('https://api.x/users/1');
        http.post('https://api.x/orders', { foo: 'bar' });
        http.delete('https://api.x/users/2');
    }
    """
    keys = _r75_1_extract_k6_endpoints(script)
    assert "GET:/users/1" in keys
    assert "POST:/orders" in keys
    assert "DELETE:/users/2" in keys


def test_extract_k6_template_literals_with_env_vars():
    """k6 uses template literals heavily: http.get(`${url}/path`)."""
    script = """
    const url = `${__ENV.BASE_URL}/api/users/${userId}`;
    http.get(url);  // not extracted (not literal in the call)
    http.get(`${__ENV.BASE_URL}/api/orders/${orderId}`);
    http.put(`${__ENV.BASE_URL}/api/orders/${orderId}/status`, payload);
    """
    keys = _r75_1_extract_k6_endpoints(script)
    # Template-string content is captured as-is (R72.4 normalises ${...} to {id} via UUID/numeric rules later)
    assert any("/api/orders/${orderId}" in k for k in keys)
    assert any("PUT:" in k for k in keys)


def test_extract_k6_request_with_method_arg():
    """k6 `http.request('METHOD', url, body)` form."""
    script = """
    http.request('PATCH', 'https://api.x/users/1', JSON.stringify({active: true}));
    http.request('OPTIONS', 'https://api.x/preflight');
    """
    keys = _r75_1_extract_k6_endpoints(script)
    assert "PATCH:/users/1" in keys
    assert "OPTIONS:/preflight" in keys


def test_extract_k6_dedupes_repeated_calls():
    """The same endpoint called multiple times only appears once."""
    script = """
    http.get('https://api.x/users/1');
    http.get('https://api.x/users/1');
    http.get('https://api.x/users/1');
    """
    keys = _r75_1_extract_k6_endpoints(script)
    assert keys == ["GET:/users/1"]


def test_extract_k6_empty_when_no_http_calls():
    """k6 script with no http.X calls (e.g., a setup-only script)."""
    script = """
    export const options = { vus: 10, duration: '30s' };
    export default function() {
        console.log('no http calls here');
    }
    """
    keys = _r75_1_extract_k6_endpoints(script)
    assert keys == []


def test_extract_k6_handles_empty_input():
    """Defensive: empty / None / non-string input returns []."""
    assert _r75_1_extract_k6_endpoints("") == []
    assert _r75_1_extract_k6_endpoints(None) == []  # type: ignore[arg-type]
    assert _r75_1_extract_k6_endpoints(123) == []  # type: ignore[arg-type]
