"""R113.I.2 — post-LLM TypeScript syntax validator for Playwright specs.

Pre-R113.I.2: the LLM reproduced three concrete bad patterns in run-78bb3d:
  1. Duplicate import name from same module (req_am_014:2 — `chromium` twice)
  2. `export const test = test.extend({...})` shadow (req_am_016:28)
  3. Unquoted string concatenation (req_am_013:490 — `"Why?" when they rose`)

All 3 specs failed to compile → Playwright found 0 tests → 0 PASS rows.
R113.I.2 catches each at gen time via `validate_playwright_syntax`, raising
GroundingViolations with BEFORE/AFTER hints so R57.1 retry-with-hint can heal.
"""
from __future__ import annotations

from src.agents.grounding_validator import validate_playwright_syntax


def test_r113_i_duplicate_import_chromium():
    """req_am_014 pattern: chromium imported twice from @playwright/test."""
    content = """
import { test, expect, chromium } from '@playwright/test';
import { chromium, test as baseTest } from '@playwright/test';

test('foo', async ({ page }) => {
  await page.goto('/');
});
"""
    violations = validate_playwright_syntax(content)
    assert any(v.kind == "pw_syntax_error" and "duplicate_import:chromium" in v.symbol
               for v in violations), (
        f"expected duplicate_import:chromium violation, got: {[v.symbol for v in violations]}"
    )
    # Verify hint includes BEFORE/AFTER
    matching = next(v for v in violations if "duplicate_import:chromium" in v.symbol)
    assert "BEFORE" in matching.hint and "AFTER" in matching.hint, (
        f"hint missing BEFORE/AFTER: {matching.hint[:200]}"
    )


def test_r113_i_export_test_shadow():
    """req_am_016 pattern: `export const test = test.extend(...)`."""
    content = """
import { test, expect } from '@playwright/test';

export const test = test.extend({
  page: async ({}, use) => {
    await use(null);
  },
});

test('foo', async ({ page }) => {});
"""
    violations = validate_playwright_syntax(content)
    assert any(v.kind == "pw_syntax_error" and "duplicate_declaration:test" in v.symbol
               for v in violations), (
        f"expected duplicate_declaration:test violation, got: {[v.symbol for v in violations]}"
    )
    matching = next(v for v in violations if "duplicate_declaration:test" in v.symbol)
    assert "as baseTest" in matching.hint, (
        f"hint should suggest rename via 'as baseTest': {matching.hint[:300]}"
    )


def test_r113_i_unquoted_string_concat():
    """req_am_013 pattern: `query: "Why did sales drop?" when they rose`."""
    content = """
import { test, expect } from '@playwright/test';

test('foo', async ({ page }) => {
  const data = {
    query: "Why did sales drop?" when they rose
  };
});
"""
    violations = validate_playwright_syntax(content)
    assert any(v.kind == "pw_syntax_error" and "unquoted_concat" in v.symbol
               for v in violations), (
        f"expected unquoted_concat violation, got: {[v.symbol for v in violations]}"
    )


def test_r113_i_clean_spec_no_violations():
    """Clean PW spec (no syntax issues) produces zero R113.I.2 violations."""
    content = """
import { test, expect, chromium } from '@playwright/test';

test('clean test', async ({ page }) => {
  await page.goto('/dashboard');
  await expect(page.getByRole('heading')).toBeVisible();
  const data = {
    query: `Why did sales drop? when they rose`,
  };
});
"""
    violations = validate_playwright_syntax(content)
    assert violations == [], (
        f"clean spec should produce 0 violations, got: {[v.symbol for v in violations]}"
    )


def test_r113_i_legitimate_value_position_string_not_flagged():
    """False-positive guard: legitimate `key: "value"` patterns don't flag."""
    content = """
import { test } from '@playwright/test';

test('foo', async ({ page }) => {
  // Standard object literal — string then comma/closing
  const config = {
    name: "Welcome",
    label: "Submit",
  };
  // String followed by closing bracket — not concat
  expect(page.locator("button")).toBeVisible();
  // String + closing paren
  await page.click("[data-testid='save']");
});
"""
    violations = validate_playwright_syntax(content)
    # Filter just unquoted_concat violations (we want zero)
    concat_violations = [v for v in violations if "unquoted_concat" in v.symbol]
    assert concat_violations == [], (
        f"false-positive on legitimate code: {[v.symbol for v in concat_violations]}"
    )
