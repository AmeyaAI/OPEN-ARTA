"""R190 — a RESOLVED path-param value (UUID/hex/numeric) must NOT be flagged
as an orphan/hallucinated path segment by the R118.E.1 segment-completeness check.

R180/R186 substitute templated path params (`:org_id`) with the literal session
value, e.g. `/organization/424e744f-94a5-4aae-b1ae-f24719f1a426/workspaces`.
Captured endpoints store the path TEMPLATED (`/organization/{organization_id}/
workspaces`), whose `{...}` tokens are dropped from the captured-token set. Pre-R190
the resolved literal UUID looked like an orphan segment → FALSE `unknown_endpoint`
that wrongly BLOCKED specs whose endpoint IS real (the workspaces GET in run-c88b1f's
48 captured endpoints). R190 treats id-shaped segments as `{var}` wildcards.
"""
from __future__ import annotations

from src.agents.grounding_validator import validate_playwright_grounded

_CAPTURED = [
    {"method": "GET", "path": "/organization/{organization_id}/workspaces"},
]
# Provide a UI catalog so the validator doesn't short-circuit (the endpoint
# check runs whenever captured_endpoints is present, but pass a catalog anyway).
_CATALOG = {"testids": ["nav-workspaces"]}
_STABLE = {"role_names": {("button", "Add")}}

_RESOLVED_UUID = "424e744f-94a5-4aae-b1ae-f24719f1a426"


def _endpoint_violations(content: str):
    vs = validate_playwright_grounded(
        content, project_id="test", dom_catalog=_CATALOG,
        stable_selectors=_STABLE, captured_endpoints=_CAPTURED,
    )
    return [v for v in vs if v.kind == "unknown_endpoint"]


def test_r190_resolved_uuid_segment_not_flagged():
    """KEYSTONE — a real endpoint with a RESOLVED UUID path-param is NOT an
    unknown_endpoint (the UUID matches the captured `{organization_id}`)."""
    content = (
        "import { test, expect } from '@playwright/test';\n"
        "test('ws', async ({ page }) => {\n"
        f"  const r = await page.request.get(`${{apiBase}}/organization/{_RESOLVED_UUID}/workspaces`);\n"
        "  expect(r.ok()).toBeTruthy();\n"
        "});\n"
    )
    assert _endpoint_violations(content) == []


def test_r190_genuinely_hallucinated_segment_still_flagged():
    """A real id but a HALLUCINATED tail segment ('frobnicate') is still an
    orphan → unknown_endpoint. R190 must not suppress real hallucinations."""
    content = (
        "import { test, expect } from '@playwright/test';\n"
        "test('bad', async ({ page }) => {\n"
        f"  const r = await page.request.get(`${{apiBase}}/organization/{_RESOLVED_UUID}/frobnicate`);\n"
        "});\n"
    )
    viols = _endpoint_violations(content)
    assert len(viols) == 1
    assert "frobnicate" in (viols[0].hint or "") or "frobnicate" in viols[0].symbol


def test_r190_numeric_id_segment_not_flagged():
    """Numeric ids (e.g. /workspaces/12345) are also `{var}` wildcards."""
    captured = [{"method": "GET", "path": "/organization/{org}/workspaces/{ws_id}"}]
    content = (
        "import { test, expect } from '@playwright/test';\n"
        "test('n', async ({ page }) => {\n"
        f"  await page.request.get(`${{apiBase}}/organization/{_RESOLVED_UUID}/workspaces/12345`);\n"
        "});\n"
    )
    vs = validate_playwright_grounded(
        content, project_id="test", dom_catalog=_CATALOG,
        stable_selectors=_STABLE, captured_endpoints=captured,
    )
    assert [v for v in vs if v.kind == "unknown_endpoint"] == []
