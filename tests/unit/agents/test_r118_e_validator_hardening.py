"""R118.E.1 + R118.E.2 regression tests for grounding validator hardening.

Pre-R118.E run-de1031 + R117 bulk regen evidence: PW specs still landed
with `unknown_endpoint` (R101.E) and `hallucinated_role` (R78.6)
violations across 3 attempts because:

- R101.E's prefix-only match let `/v1/pipeline/run` pass when `/api/`
  existed somewhere — even though `/pipeline/` segment didn't appear
  as a complete token in ANY captured path
- R78.6's hint said "use a role from the catalog" without telling the
  LLM what to do when the desired role (e.g., 'textbox' for `<input>`
  fields) was fully absent → LLM defaulted to re-emitting

R118.E.1 adds segment-completeness as a secondary check after prefix
match. R118.E.2 maps common semantic roles → concrete fallback selector
recipes (textbox → page.getByLabel, cell → page.getByText, etc.).
"""
from __future__ import annotations

from src.agents.grounding_validator import validate_playwright_grounded


# ── R118.E.1 — segment-completeness ────────────────────────────────────


def test_r118_e_1_orphan_segment_flagged():
    """Pre-R118.E.1 the prefix-only check matched on first-3 segments,
    so spec `/api/v1/datasets/pipeline/status/{id}` PASSED because
    captured `/api/v1/datasets/{id}/profile` startswith /api/v1/datasets.
    The orphan segments 'pipeline' + 'status' (positions 4-5) were
    never in ANY captured token → silent gen-quality leak.

    R118.E.1 scans ALL spec segments (not just first 3) against the
    union of captured tokens → flags orphan list."""
    captured = [
        {"method": "GET", "path": "/api/v1/datasets/{id}/profile"},
        {"method": "GET", "path": "/api/v1/datasets"},
        {"method": "POST", "path": "/api/v1/datasets/{id}/clone"},
    ]
    spec = """
test('foo', async ({ page }) => {
  const r = await page.request.get(`${apiBase}/api/v1/datasets/pipeline/status/123`);
});
"""
    vs = validate_playwright_grounded(
        spec,
        project_id="test-pid",
        stable_selectors={"role_names": {("button", "Submit")}},
        captured_endpoints=captured,
    )
    unknown = [v for v in vs if v.kind == "unknown_endpoint" and "pipeline" in v.symbol]
    assert len(unknown) == 1, f"Expected pipeline endpoint to flag; got: {[v.symbol for v in vs]}"
    # Hint surfaces R118.E.1 orphan diagnostic
    assert "R118.E.1" in unknown[0].hint, f"R118.E.1 marker missing from hint:\n{unknown[0].hint}"
    assert "pipeline" in unknown[0].hint
    assert "status" in unknown[0].hint


def test_r118_e_1_both_prefix_and_segments_match_no_flag():
    """`/api/users` exists in captured AND every spec segment ('api',
    'users') is a captured token → no flag (legitimate endpoint use)."""
    captured = [
        {"method": "GET", "path": "/api/users"},
        {"method": "POST", "path": "/api/users"},
    ]
    spec = """
test('foo', async ({ page }) => {
  const r = await page.request.get(`${apiBase}/api/users`);
});
"""
    vs = validate_playwright_grounded(
        spec,
        project_id="test-pid",
        stable_selectors={"role_names": {("button", "X")}},
        captured_endpoints=captured,
    )
    unknown = [v for v in vs if v.kind == "unknown_endpoint"]
    assert unknown == [], f"Legitimate path should not flag; got: {[v.symbol for v in unknown]}"


def test_r118_e_1_structural_keywords_excluded():
    """'api'/'v1'/'v2' are structural keywords — even if they ONLY
    appear as path prefixes (not as a segment token in any user-facing
    endpoint name), R118.E.1's orphan check must NOT flag them.

    Here `/v2/datasets` matches captured prefix `/v2/...` AND every
    segment ('v2', 'datasets') is in the captured token set → no flag.
    """
    captured = [
        {"method": "GET", "path": "/v2/datasets"},
        {"method": "POST", "path": "/v2/datasets"},
    ]
    spec = "await page.request.get(`${apiBase}/v2/datasets`);"
    vs = validate_playwright_grounded(
        spec,
        project_id="test-pid",
        stable_selectors={"role_names": {("button", "X")}},
        captured_endpoints=captured,
    )
    unknown = [v for v in vs if v.kind == "unknown_endpoint"]
    assert unknown == [], (
        f"Structural prefix v2 with matching segment must not flag; "
        f"got: {[v.symbol for v in unknown]}"
    )


# ── R118.E.2 — concrete-selector fallback ──────────────────────────────


def test_r118_e_2_textbox_role_absent_concrete_fallback():
    """spec uses `getByRole('textbox', { name: 'Email' })` but the SUT's
    catalog has only `button` roles. R118.E.2 surfaces the textbox →
    page.getByLabel fallback recipe in the hint."""
    spec = """
test('foo', async ({ page }) => {
  await page.getByRole('textbox', { name: 'Email' }).fill('a@b.com');
});
"""
    vs = validate_playwright_grounded(
        spec,
        project_id="test-pid",
        stable_selectors={"role_names": {("button", "Login"), ("button", "Cancel")}},
        captured_endpoints=None,
    )
    hroles = [v for v in vs if v.kind == "hallucinated_role" and "textbox" in v.symbol]
    assert len(hroles) == 1, f"textbox role must flag; got: {[v.symbol for v in vs]}"
    # R118.E.2 fallback block present with concrete getByLabel/locator advice
    assert "R118.E.2" in hroles[0].hint, f"Hint missing R118.E.2 marker: {hroles[0].hint}"
    assert "getByLabel" in hroles[0].hint, "Hint missing concrete getByLabel fallback"


def test_r118_e_2_unknown_role_not_in_map_no_fallback():
    """spec uses a custom role 'banner-region' (not in
    ROLE_TO_FALLBACK map) — R78.6 still flags hallucinated_role, but
    R118.E.2 fallback block is OMITTED (no canonical recipe exists)."""
    spec = """
test('foo', async ({ page }) => {
  await page.getByRole('banner-region', { name: 'Top' }).click();
});
"""
    vs = validate_playwright_grounded(
        spec,
        project_id="test-pid",
        stable_selectors={"role_names": {("button", "Login")}},
        captured_endpoints=None,
    )
    hroles = [v for v in vs if v.kind == "hallucinated_role" and "banner-region" in v.symbol]
    assert len(hroles) == 1
    # Custom role NOT in map → no R118.E.2 block
    assert "R118.E.2" not in hroles[0].hint, (
        f"Custom-role hint should NOT carry R118.E.2 marker; got: {hroles[0].hint}"
    )
    # But original hint (BEFORE/AFTER) still present
    assert "BEFORE" in hroles[0].hint and "AFTER" in hroles[0].hint


def test_r118_e_2_role_present_just_name_absent_no_e2_block():
    """The role IS present in the catalog ('button'), but the specific
    name 'Phantom' is absent. R78.6 emits `hallucinated_role_name`
    (different kind from `hallucinated_role`). R118.E.2 block must NOT
    appear (that path doesn't go through the role-absent branch)."""
    spec = """
test('foo', async ({ page }) => {
  await page.getByRole('button', { name: 'Phantom' }).click();
});
"""
    vs = validate_playwright_grounded(
        spec,
        project_id="test-pid",
        stable_selectors={"role_names": {("button", "Login"), ("button", "Cancel")}},
        captured_endpoints=None,
    )
    # The button role IS in the catalog; the name is not → hallucinated_role_name
    hnames = [v for v in vs if v.kind == "hallucinated_role_name"]
    assert len(hnames) == 1, f"Expected hallucinated_role_name; got: {[v.kind for v in vs]}"
    # That hint uses R117.B (smushed-name split) machinery — NOT R118.E.2
    assert "R118.E.2" not in hnames[0].hint, (
        "R118.E.2 fallback must only appear in the role-fully-absent branch"
    )
