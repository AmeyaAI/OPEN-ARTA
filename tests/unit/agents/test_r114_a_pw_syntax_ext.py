"""R114.A — extensions to `validate_playwright_syntax` for 3 new patterns.

Pre-R114.A: R113.I.2 caught 3 patterns (duplicate import, test-shadow,
unquoted concat). R114.A extends with:

  1a. Missing hook import (req_am_005: bare beforeEach without import)
  1b. Hook-fixture-misuse (req_am_019: beforeEach(async ({}, use) => ...))
  1c. Broken-fixture-pattern (3 specs on disk still affected — Fix A SSOT)
"""
from __future__ import annotations

from src.agents.grounding_validator import validate_playwright_syntax


# ── R114.A.1a — Missing hook import ────────────────────────────────────

def test_r114a_1a_missing_beforeEach_import():
    """run-0179d0 req_am_005 pattern — bare beforeEach not in import list."""
    content = """
import { test, expect, Page, chromium } from '@playwright/test';
test.describe('x', () => {
  beforeEach(async ({ page }) => { await page.goto('/'); });
});
"""
    v = validate_playwright_syntax(content)
    assert any("missing_import:beforeEach" in x.symbol for x in v), (
        f"R114.A.1a: expected missing_import:beforeEach violation, got: {[x.symbol for x in v]}"
    )
    matching = next(x for x in v if "missing_import:beforeEach" in x.symbol)
    assert "BEFORE" in matching.hint and "AFTER" in matching.hint
    assert "ReferenceError" in matching.hint


def test_r114a_1a_qualified_hook_not_flagged():
    """REGRESSION: `test.beforeEach(...)` is correct — no violation."""
    content = """
import { test, expect } from '@playwright/test';
test.beforeEach(async ({ page }) => { await page.goto('/'); });
"""
    v = validate_playwright_syntax(content)
    assert not any("missing_import" in x.symbol for x in v), (
        f"qualified hook should NOT flag: {[x.symbol for x in v]}"
    )


def test_r114a_1a_imported_bare_hook_not_flagged():
    """If `beforeEach` IS imported, bare form is allowed."""
    content = """
import { test, expect, beforeEach } from '@playwright/test';
beforeEach(async ({ page }) => { await page.goto('/'); });
"""
    v = validate_playwright_syntax(content)
    missing_imports = [x for x in v if "missing_import" in x.symbol]
    assert missing_imports == [], (
        f"imported bare hook should NOT flag missing_import: {[x.symbol for x in missing_imports]}"
    )


# ── R114.A.1b — Hook-fixture-misuse ────────────────────────────────────

def test_r114a_1b_hook_fixture_misuse_bare():
    """run-0179d0 req_am_019 pattern — bare beforeEach with (arg, use) signature."""
    content = """
import { test, expect, beforeEach } from '@playwright/test';
beforeEach(async ({ page }, use) => { await use(page); });
"""
    v = validate_playwright_syntax(content)
    assert any("hook_fixture_misuse:beforeEach" in x.symbol for x in v)


def test_r114a_1b_hook_fixture_misuse_qualified():
    """Same misuse with qualified `test.beforeEach`."""
    content = """
import { test } from '@playwright/test';
test.beforeEach(async ({ page }, use) => { await use(page); });
"""
    v = validate_playwright_syntax(content)
    assert any("hook_fixture_misuse:beforeEach" in x.symbol for x in v)


def test_r114a_1b_legitimate_fixture_extend_not_flagged():
    """`test.extend({ name: async ({}, use) => })` is the CORRECT fixture form."""
    content = """
import { test as baseTest } from '@playwright/test';
const test = baseTest.extend({
  seededPage: async ({ page }, use) => { await use(page); },
});
"""
    v = validate_playwright_syntax(content)
    assert not any("hook_fixture_misuse" in x.symbol for x in v), (
        f"test.extend fixture should NOT flag: {[x.symbol for x in v]}"
    )


# ── R114.A.1c — Broken-fixture-pattern ─────────────────────────────────

def test_r114a_1c_broken_fixture_pattern():
    """`let page` at describe + body uses page.X without destructure."""
    content = """
import { test } from '@playwright/test';
test.describe('x', () => {
  let page: any;
  test.beforeEach(async ({}) => {
    await page.request.post('/seed');
  });
});
"""
    v = validate_playwright_syntax(content)
    assert any("broken_fixture_pattern" in x.symbol for x in v), (
        f"R114.A.1c: expected broken_fixture_pattern violation, got: {[x.symbol for x in v]}"
    )


def test_r114a_1c_destructured_page_not_flagged():
    """If callback destructures `{ page }`, let-page-decl is benign."""
    content = """
import { test } from '@playwright/test';
test.describe('x', () => {
  let page: any;
  test.beforeEach(async ({ page }) => { await page.goto('/'); });
});
"""
    v = validate_playwright_syntax(content)
    assert not any("broken_fixture_pattern" in x.symbol for x in v)
