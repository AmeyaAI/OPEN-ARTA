"""R213 V2.3 — TDZ (temporal dead zone) use-before-init detector in
validate_playwright_syntax. A const/let used before its declaration in the
same scope throws `Cannot access 'X' before initialization` at runtime
(run-3ad7dc: 24 PW FAILs). Conservative: same-depth, same-scope only."""
from __future__ import annotations

from src.agents.grounding_validator import validate_playwright_syntax


def _tdz(viols):
    return [v for v in viols if str(getattr(v, "symbol", "")).startswith("tdz_use_before_init")]


def test_same_scope_use_before_const_is_flagged():
    spec = """import { test, expect } from '@playwright/test';
test('x', async ({ page }) => {
  const total = subscriberId + 1;
  const subscriberId = await page.evaluate(() => 42);
  expect(total).toBe(43);
});
"""
    v = _tdz(validate_playwright_syntax(spec))
    assert len(v) == 1
    assert "subscriberId" in v[0].symbol


def test_declaration_before_use_is_clean():
    spec = """import { test, expect } from '@playwright/test';
test('x', async ({ page }) => {
  const subscriberId = await page.evaluate(() => 42);
  const total = subscriberId + 1;
  expect(total).toBe(43);
});
"""
    assert _tdz(validate_playwright_syntax(spec)) == []


def test_closure_deferred_use_not_flagged():
    # `config` used inside an arrow body (deeper depth) that runs AFTER init —
    # NOT a TDZ bug. Must not false-positive.
    spec = """import { test, expect } from '@playwright/test';
test('x', async ({ page }) => {
  const handler = () => { return config.value; };
  const config = { value: 7 };
  expect(handler()).toBe(7);
});
"""
    assert _tdz(validate_playwright_syntax(spec)) == []


def test_sibling_scope_reuse_not_flagged():
    # `id` declared in two separate test bodies — the use in the first is its
    # own `id`, not the second decl. Depth drops below between them → not TDZ.
    spec = """import { test, expect } from '@playwright/test';
test('a', async ({ page }) => {
  const id = 1;
  expect(id).toBe(1);
});
test('b', async ({ page }) => {
  const id = 2;
  expect(id).toBe(2);
});
"""
    assert _tdz(validate_playwright_syntax(spec)) == []


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R213_TDZ_DISABLE", "1")
    spec = """import { test, expect } from '@playwright/test';
test('x', async ({ page }) => {
  const total = subscriberId + 1;
  const subscriberId = 42;
  expect(total).toBe(43);
});
"""
    assert _tdz(validate_playwright_syntax(spec)) == []
