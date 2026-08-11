"""R154.B — gen-time destructive-pattern validator (Pillar 1+1b non-mutation
guarantee).

Operator directive (verbatim): *"The testcases and test scripts should not
test destructive test cases"*. R154.B implements the gen-phase guarantee
by rejecting LLM-generated PW specs that contain destructive operations
against the SUT, unless the operator has opted in via:
  1. ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1 env var (R154.C dispatch gate)
  2. SUT_TEST_DATA_NAMESPACE=<sandbox> env var (R154.C dispatch gate)
  3. Comment marker `// @intentional-destructive: <reason>` in spec header

When any destructive pattern is detected AND the opt-in marker is absent,
R154.B emits one GroundingViolation per kind (capped at one per kind to
avoid retry whack-a-mole). R57.1 retry-with-hint surfaces the BEFORE/AFTER
snippet to the LLM; after 3 attempts exhaust, R102.A stamps + R102.C
dispatch BLOCKs.

Patterns covered:
  - request.{post,put,patch,delete}() and page.request.{...}() direct calls
  - page.fill() (form input mutation)
  - getByRole('checkbox'|'radio') (state toggle)
  - type="submit" buttons (form submit triggers)
  - Pytest destructive helpers (create_/delete_/update_/insert_)
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

from src.agents.grounding_validator import (
    _r154_b_extract_destructive_patterns,
    _r154_b_has_opt_in_marker,
    validate_playwright_destructive_patterns,
)


@contextmanager
def _env(**kwargs):
    """Temporarily set env vars + restore."""
    prior = {k: os.environ.get(k) for k in kwargs}
    for k, v in kwargs.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_r154_b_clean_spec_passes_through():
    """Read-only spec produces zero destructive violations."""
    content = """
import { test, expect } from '@playwright/test';
test('AC-001', async ({ page }) => {
  await page.goto('/dashboard');
  await expect(page.getByText('Welcome')).toBeVisible();
  const resp = await page.request.get(`${apiBase}/api/v1/datasets`);
  expect(resp.status()).toBe(200);
});
"""
    violations = validate_playwright_destructive_patterns(content)
    assert violations == [], f"Clean spec produced violations: {violations}"


def test_r154_b_request_post_destructive_rejected():
    """`request.post()` direct call is rejected as destructive_http_method."""
    content = """
test('AC-001', async ({ request }) => {
  const resp = await request.post(`${apiBase}/api/v1/datasets`, { data: {...} });
});
"""
    violations = validate_playwright_destructive_patterns(content)
    assert len(violations) == 1
    assert violations[0].kind == "destructive_test_pattern"
    assert "destructive_http_method" in violations[0].symbol


def test_r154_b_page_request_delete_destructive_rejected():
    """`page.request.delete()` is rejected."""
    content = """
test('AC-001', async ({ page }) => {
  await page.request.delete(`${apiBase}/api/v1/datasets/123`);
});
"""
    violations = validate_playwright_destructive_patterns(content)
    assert len(violations) == 1
    assert "destructive_http_method" in violations[0].symbol


def test_r154_b_page_fill_rejected():
    """`page.fill()` is rejected as destructive_form_fill."""
    content = """
test('AC-001', async ({ page }) => {
  await page.fill('input[name="email"]', 'test@example.com');
});
"""
    violations = validate_playwright_destructive_patterns(content)
    assert len(violations) == 1
    assert "destructive_form_fill" in violations[0].symbol


def test_r154_b_checkbox_radio_rejected():
    """getByRole checkbox/radio rejected as destructive_state_toggle."""
    content = """
test('AC-001', async ({ page }) => {
  await page.getByRole('checkbox', { name: 'Accept Terms' }).click();
});
"""
    violations = validate_playwright_destructive_patterns(content)
    assert any("destructive_state_toggle" in v.symbol for v in violations)


def test_r154_b_multiple_kinds_single_pass():
    """When multiple destructive kinds are present, each kind emits AT MOST
    one violation. Avoids whack-a-mole retry where LLM fixes one pattern
    + re-emits another. Operator sees ALL kinds in one R57.1 retry hint.
    """
    content = """
test('AC-001', async ({ page, request }) => {
  await page.fill('input', 'x');
  await request.post(`${api}/x`);
  await request.delete(`${api}/y`);  // duplicate http_method — still 1 violation
  await page.fill('input2', 'y');     // duplicate form_fill — still 1 violation
});
"""
    violations = validate_playwright_destructive_patterns(content)
    kinds_seen = {v.symbol.split(":")[0] for v in violations}
    assert "destructive_http_method" in kinds_seen
    assert "destructive_form_fill" in kinds_seen
    # Each KIND emits AT MOST one violation (regardless of recurrence)
    assert len(violations) == 2, (
        f"Expected 2 violations (one per kind), got {len(violations)}: "
        f"{[v.symbol for v in violations]}"
    )


def test_r154_b_opt_in_marker_exempts_spec():
    """Spec with `// @intentional-destructive` in first 5 lines is exempt.
    R154.C dispatch gate still requires SUT_TEST_DATA_NAMESPACE env var.
    """
    content = """// @intentional-destructive: verifying DELETE flow on test data
import { test, expect } from '@playwright/test';
test('AC-001 destructive bug-bash', async ({ request }) => {
  await request.delete(`${apiBase}/api/v1/datasets/test-fixture-001`);
});
"""
    violations = validate_playwright_destructive_patterns(content)
    assert violations == [], (
        "Opt-in marker MUST exempt the spec from R154.B at gen time. "
        f"Got violations: {[v.symbol for v in violations]}"
    )


def test_r154_b_marker_outside_head_5_lines_not_exempt():
    """Marker must be in first 5 lines to count — operator can't sneak it
    in mid-spec to bypass the validator.
    """
    content = """
import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import * as util from 'util';
// @intentional-destructive: this comment is on line 7, too late
test('AC-001', async ({ request }) => {
  await request.delete(`${apiBase}/api/v1/x`);
});
"""
    violations = validate_playwright_destructive_patterns(content)
    assert len(violations) >= 1, (
        "Marker on line 7+ MUST NOT exempt the spec; got 0 violations"
    )


def test_r154_b_killswitch_disables_validator():
    """ARTA_R154_B_DESTRUCTIVE_VALIDATOR_DISABLE=1 short-circuits to []."""
    content = """
test('AC-001', async ({ request }) => {
  await request.post(`${api}/x`);
});
"""
    # Without killswitch: violation fires
    assert len(validate_playwright_destructive_patterns(content)) == 1
    # With killswitch: empty list
    with _env(ARTA_R154_B_DESTRUCTIVE_VALIDATOR_DISABLE="1"):
        assert validate_playwright_destructive_patterns(content) == []


def test_r154_b_has_opt_in_marker_helper():
    """`_r154_b_has_opt_in_marker` recognizes the canonical marker."""
    assert _r154_b_has_opt_in_marker("// @intentional-destructive: foo")
    assert _r154_b_has_opt_in_marker(
        "import { test } from 'pw';\n// @intentional-destructive: bar\ntest('x', ...);"
    )
    # Negative: no marker
    assert not _r154_b_has_opt_in_marker("// regular comment\ntest('x', ...);")
    # Negative: empty content
    assert not _r154_b_has_opt_in_marker("")


def test_r154_b_extract_destructive_patterns_helper():
    """`_r154_b_extract_destructive_patterns` returns list of kinds for
    R154.C dispatch-time inspection.
    """
    content_destructive = """
test('x', async ({ request, page }) => {
  await request.post(`${api}/x`);
  await page.fill('input', 'y');
});
"""
    kinds = _r154_b_extract_destructive_patterns(content_destructive)
    assert "destructive_http_method" in kinds
    assert "destructive_form_fill" in kinds

    # Opt-in marker exempts
    content_opt_in = "// @intentional-destructive: bug-bash\n" + content_destructive
    assert _r154_b_extract_destructive_patterns(content_opt_in) == []

    # Clean content returns []
    assert _r154_b_extract_destructive_patterns("test('x', () => expect(1).toBe(1));") == []
