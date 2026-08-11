"""R114.B — R102.E rewriter regex extended to catch bare lifecycle hooks.

Pre-R114.B: R102.E only matched `test.beforeEach(async ({...}, use) => ...)`.
Bare hook form `beforeEach(async ({...}, use) => ...)` slipped through.
Live evidence (run-0179d0):
  - req_am_005: bare `beforeEach()` + missing import → ReferenceError (0 tests)
  - req_am_019: bare `beforeEach()` + correct import + `(arg, use) => await use(page)` → TypeError (6 of 12 tests FAIL)

R114.B makes `test.` optional via group `((?:test\\.)?(?:before|after)(?:Each|All))`.
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent


def test_r114b_bare_beforeEach_rewritten():
    """req_am_019 pattern — bare beforeEach with (arg, use) gets split into paired hooks."""
    src = """
import { test, expect, beforeEach } from '@playwright/test';
beforeEach(async ({ page }, use) => {
  await seedData(page);
  await use(page);
  await cleanupData(page);
});
"""
    out, count = AutomationEngineerAgent._r102_e_rewrite_hook_use(src)
    assert count == 1, f"R114.B: bare beforeEach must be rewritten, got count={count}"
    # Bare hook form should produce bare afterEach (no test. prefix)
    assert "beforeEach(async ({ page }) =>" in out, f"R114.B: rewritten beforeEach not found: {out}"
    assert "afterEach(async ({ page }) =>" in out, f"R114.B: paired afterEach missing: {out}"
    assert ", use)" not in out, f"R114.B: , use) parameter must be stripped: {out}"
    assert "await use(" not in out, f"R114.B: await use() must be removed: {out}"


def test_r114b_qualified_test_beforeEach_still_works():
    """REGRESSION GUARD: R102.E original `test.beforeEach` behavior preserved."""
    src = """
test.beforeEach(async ({ page }, use) => {
  await seedData(page);
  await use(page);
  await cleanupData(page);
});
"""
    out, count = AutomationEngineerAgent._r102_e_rewrite_hook_use(src)
    assert count == 1
    # Qualified hook form should produce qualified afterEach (with test. prefix)
    assert "test.beforeEach(async ({ page }) =>" in out
    assert "test.afterEach(async ({ page }) =>" in out
    assert ", use)" not in out


def test_r114b_idempotent_on_clean():
    """Already-clean content (no `, use)` parameter) is unchanged."""
    src = "test.beforeEach(async ({ page }) => { await page.goto('/'); });\n"
    out, count = AutomationEngineerAgent._r102_e_rewrite_hook_use(src)
    assert count == 0
    assert out == src


def test_r114b_bare_afterEach_rewritten():
    """Bare afterEach with fixture signature also handled."""
    src = """
afterEach(async ({ page }, use) => {
  await cleanupAll(page);
  await use();
});
"""
    out, count = AutomationEngineerAgent._r102_e_rewrite_hook_use(src)
    assert count == 1
    # afterEach with no body-after-use should become bare afterEach with setup body
    assert "afterEach(async ({ page }) =>" in out
    assert ", use)" not in out
