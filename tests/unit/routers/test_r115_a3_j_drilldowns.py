"""R115.A.3 + R115.J — operator-actionable drill-down endpoints.

R115.A.3: GET /api/runs/{run_id}/auth-scope-summary — top-10 prefixes
by 401 count + sample paths. Pre-R115.A.3: dashboard showed aggregate
"53 operator_review" — operator manually filtered Defects page.

R115.J: GET /api/runs/{run_id}/top-endpoints-5xx — top-10 endpoint
templates by 5xx count + pct + total requests.
"""
from __future__ import annotations

import re
from pathlib import Path


_EXECUTION_PY = Path(__file__).resolve().parents[3] / "src" / "api" / "routers" / "execution.py"


def test_r115_a3_endpoint_registered():
    """Source check: /runs/{run_id}/auth-scope-summary endpoint registered."""
    content = _EXECUTION_PY.read_text()
    pattern = re.compile(
        r"@router\.get\(\"/runs/\{run_id\}/auth-scope-summary\".*?async def get_auth_scope_summary",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R115.A.3: /runs/{run_id}/auth-scope-summary endpoint missing"
    )


def test_r115_a3_normalizes_uuid_paths():
    """Source check: path normalization with /{id} for UUIDs + digits."""
    content = _EXECUTION_PY.read_text()
    # Look for the path normalization regex within the auth-scope summary
    auth_scope_start = content.find("get_auth_scope_summary")
    assert auth_scope_start > 0
    window = content[auth_scope_start:auth_scope_start + 4000]
    pattern = re.compile(
        r"r\"/\[0-9a-f-\]\{36\}\|/\\d\+\"",
    )
    assert pattern.search(window), (
        "R115.A.3: UUID/digit path normalization regex missing"
    )


def test_r115_a3_endpoint_keys_fallback():
    """R115.A.3-v2 — when `request_path` is missing, fall back to
    `endpoint_keys` array (canonical Newman dispatch shape).

    Pre-R115.A.3-v2: live run-f45f85 had `metadata.endpoint_keys` but no
    `request_path` → endpoint returned `total_401s: 0` despite 306 × 401
    in DB. Fallback closes the gap.
    """
    content = _EXECUTION_PY.read_text()
    auth_scope_start = content.find("get_auth_scope_summary")
    window = content[auth_scope_start:auth_scope_start + 4000]
    assert "endpoint_keys" in window, (
        "R115.A.3-v2: endpoint_keys fallback missing"
    )
    # The fallback splits on first colon to extract path from METHOD:PATH
    assert 'split(":", 1)[1]' in window, (
        "R115.A.3-v2: METHOD:PATH split not present"
    )


def test_r115_j_endpoint_keys_fallback():
    """R115.J-v2 — same fallback as R115.A.3-v2 for the 5xx drill-down."""
    content = _EXECUTION_PY.read_text()
    j_start = content.find("get_top_endpoints_5xx")
    window = content[j_start:j_start + 4000]
    assert "endpoint_keys" in window, (
        "R115.J-v2: endpoint_keys fallback missing in 5xx drill-down"
    )


def test_r115_a3_returns_top_10_prefixes():
    """Source check: limit to top-10 prefixes via most_common(10)."""
    content = _EXECUTION_PY.read_text()
    auth_scope_start = content.find("get_auth_scope_summary")
    window = content[auth_scope_start:auth_scope_start + 4000]
    assert "most_common(10)" in window, (
        "R115.A.3: top-10 prefix limit missing"
    )


def test_r115_j_endpoint_registered():
    """Source check: /runs/{run_id}/top-endpoints-5xx endpoint registered."""
    content = _EXECUTION_PY.read_text()
    pattern = re.compile(
        r"@router\.get\(\"/runs/\{run_id\}/top-endpoints-5xx\".*?async def get_top_endpoints_5xx",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R115.J: /runs/{run_id}/top-endpoints-5xx endpoint missing"
    )


def test_r115_j_computes_pct_5xx():
    """Source check: pct_5xx calculation present (count_5xx / total)."""
    content = _EXECUTION_PY.read_text()
    j_start = content.find("get_top_endpoints_5xx")
    assert j_start > 0
    window = content[j_start:j_start + 6000]
    assert "pct_5xx" in window
    assert "count_5xx" in window
    assert "total_requests" in window


def test_r115_j_only_counts_5xx():
    """Source check: only status codes starting with '5' counted."""
    content = _EXECUTION_PY.read_text()
    j_start = content.find("get_top_endpoints_5xx")
    window = content[j_start:j_start + 6000]
    assert 'sc.startswith("5")' in window, (
        "R115.J: 5xx filter missing"
    )
