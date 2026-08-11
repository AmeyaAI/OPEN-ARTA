"""R125.B — validate_playwright_syntax catches half-translated Gherkin specs.

Pre-R125.B: when the LLM truncated mid-translation OR hit auth failure
mid-stream, the raw Gherkin scenario text (Scenario:/Given/When/Then) landed
on disk WITHOUT a `test(...)` wrapper. Playwright then found 0 tests +
returned `tests=0 returncode=1`.

Live evidence: req_am_002.spec.ts contained 5 lines of raw Gherkin only —
no test() blocks, but produced 45 dispatched-spec assertion errors because
downstream tooling didn't catch the malformed shape.

R125.B adds `_r125_b_validate_gherkin_translation` invoked from
`validate_playwright_syntax`. Flags the half-translated state so R57.1
retry-with-hint surfaces a BEFORE/AFTER snippet and the LLM either
translates correctly OR omits Gherkin entirely.
"""
from __future__ import annotations

from src.agents.grounding_validator import (
    _r125_b_validate_gherkin_translation,
    validate_playwright_syntax,
)


def test_r125b_raw_gherkin_no_test_blocks_flagged():
    """req_am_002 reproduction — raw Gherkin without test() wrappers."""
    content = """
Scenario: User creates organization
  Given the user is logged in
  When the user navigates to /organizations
  Then the page title contains 'Organizations'
"""
    violations = _r125_b_validate_gherkin_translation(content)
    assert len(violations) == 1, f"expected 1 violation, got {len(violations)}"
    v = violations[0]
    assert v.kind == "incomplete_gherkin_translation"
    assert v.tool == "playwright"
    assert "BEFORE" in v.hint and "AFTER" in v.hint
    assert "test(" in v.hint  # hint shows the canonical fix


def test_r125b_full_spec_with_test_blocks_not_flagged():
    """Properly translated spec (test() blocks present) → no violation."""
    content = """
import { test, expect } from '@playwright/test';

test('User creates organization', async ({ page }) => {
  await page.goto('/organizations');
  await expect(page).toHaveTitle(/Organizations/);
});
"""
    violations = _r125_b_validate_gherkin_translation(content)
    assert violations == [], (
        f"properly-translated spec should not flag; got {[v.symbol for v in violations]}"
    )


def test_r125b_gherkin_in_comments_not_flagged():
    """Gherkin as comments alongside real test() blocks → not flagged."""
    content = """
import { test, expect } from '@playwright/test';

// Scenario: User creates org
test('User creates organization', async ({ page }) => {
  // Given the user is logged in
  // When the user navigates to /organizations
  await page.goto('/organizations');
  // Then page title matches
  await expect(page).toHaveTitle(/Organizations/);
});
"""
    violations = _r125_b_validate_gherkin_translation(content)
    assert violations == [], (
        f"Gherkin-in-comments must not flag when test() exists; got {[v.symbol for v in violations]}"
    )


def test_r125b_gherkin_in_test_name_string_not_flagged():
    """Gherkin keyword inside test() name string (e.g., 'Given user logs in') → not flagged."""
    content = """
import { test, expect } from '@playwright/test';

test('Given user logs in, When they click Submit, Then they see the dashboard', async ({ page }) => {
  await page.goto('/');
});
"""
    violations = _r125_b_validate_gherkin_translation(content)
    assert violations == [], (
        f"Gherkin in test name string should not flag; got {[v.symbol for v in violations]}"
    )


def test_r125b_wired_through_validate_playwright_syntax():
    """Integration: R125.B violations appear in the public entry point."""
    content = """
Scenario: User creates org
  Given the user is logged in
  When the user navigates to /organizations
"""
    violations = validate_playwright_syntax(content)
    r125_b_viols = [v for v in violations if v.kind == "incomplete_gherkin_translation"]
    assert len(r125_b_viols) >= 1, (
        f"R125.B must be wired into validate_playwright_syntax; got {[v.kind for v in violations]}"
    )
