"""R95.3 regression tests for `validate_playwright_api_usage()`.

Pre-R95.3 the LLM emitted Playwright code with three recurring API
misuse patterns that R42.1's selector grounding doesn't catch:

  1. `_test.test.info(...).fixture` — TestInfo has no .fixture method
     (run-2f077d: TC-AM-013-01 × 2 instances)
  2. `expect(<page-locator>).toBeOK()` — toBeOK is APIResponse-only
     (run-2f077d: TC-AM-021-AUTO001)
  3. `await page.fixture(...)` — page has no .fixture method

Together these produced 38 of 160 Playwright failures (24% of all
PW fails). R95.3's deterministic lint catches them post-LLM, raises
GroundingViolation, and triggers R42.1's retry-with-hint loop so the
LLM corrects on attempt 2.

These tests lock the lint contract.
"""
from __future__ import annotations

from src.agents.grounding_validator import (
    validate_playwright_api_usage,
)


# ── Pattern 1: testInfo.fixture / _test.test.info().fixture ───────────


def test_info_fixture_flagged():
    """The exact pattern from run-2f077d TC-AM-013-01 (2 occurrences)."""
    code = """\
import { test } from '@playwright/test';
test('user can sign up', async ({ page }) => {
  const f = _test.test.info({}).fixture;
  await f();
});
"""
    violations = validate_playwright_api_usage(code)
    assert len(violations) == 1
    assert violations[0].kind == "bad_playwright_api"
    assert "fixture" in violations[0].symbol
    assert "TestInfo has no" in violations[0].hint


def test_testInfo_fixture_short_form_flagged():
    """Some LLM outputs use `testInfo.fixture` (no _test prefix)."""
    code = """\
import { test } from '@playwright/test';
test('foo', async ({ }, testInfo) => {
  const fixt = testInfo.fixture;
});
"""
    violations = validate_playwright_api_usage(code)
    assert len(violations) == 1
    assert violations[0].kind == "bad_playwright_api"


# ── Pattern 2: toBeOK on page-side expression ─────────────────────────


def test_to_be_ok_on_page_locator_flagged():
    """toBeOK only valid for APIResponse; page.getByTestId() → flagged."""
    code = """\
import { test, expect } from '@playwright/test';
test('foo', async ({ page }) => {
  await expect(page.locator('button')).toBeOK();
});
"""
    violations = validate_playwright_api_usage(code)
    assert len(violations) == 1
    assert "toBeOK" in violations[0].symbol
    assert "APIResponse" in violations[0].hint


def test_to_be_ok_on_api_response_NOT_flagged():
    """When toBeOK is correctly used on an APIResponse, no violation."""
    code = """\
import { test, expect } from '@playwright/test';
test('foo', async ({ request }) => {
  const response = await request.get('/api/x');
  await expect(response).toBeOK();
});
"""
    violations = validate_playwright_api_usage(code)
    # response is not a page-side expression → no false positive
    assert not any("toBeOK" in v.symbol for v in violations)


# ── Pattern 3: page.fixture() ─────────────────────────────────────────


def test_page_fixture_flagged():
    """`page.fixture()` doesn't exist."""
    code = """\
import { test } from '@playwright/test';
test('foo', async ({ page }) => {
  const f = await page.fixture('myFixture');
});
"""
    violations = validate_playwright_api_usage(code)
    assert any(v.symbol.startswith("await page.fixture") for v in violations) or \
           any(v.symbol.startswith("page.fixture") for v in violations)


# ── Clean baseline (no violations) ────────────────────────────────────


def test_clean_playwright_spec_no_violations():
    """A well-formed Playwright spec produces zero violations."""
    code = """\
import { test, expect } from '@playwright/test';

test('user signup', async ({ page }) => {
  await page.goto('/signup');
  await page.getByTestId('email').fill('test@example.com');
  await page.getByTestId('submit-btn').click();
  await expect(page.getByTestId('success-banner')).toBeVisible();
});

test('api smoke', async ({ request }) => {
  const response = await request.get('/api/health');
  await expect(response).toBeOK();
});
"""
    violations = validate_playwright_api_usage(code)
    assert violations == []


def test_multiple_patterns_all_flagged():
    """Spec hitting all three misuse patterns → 3 violations."""
    code = """\
import { test, expect } from '@playwright/test';
test('mixed bugs', async ({ page }) => {
  const f1 = _test.test.info({}).fixture;
  const f2 = await page.fixture('x');
  await expect(page.locator('div')).toBeOK();
});
"""
    violations = validate_playwright_api_usage(code)
    # 3 distinct misuse patterns → at least 3 violations
    assert len(violations) >= 3
    kinds = {v.kind for v in violations}
    assert kinds == {"bad_playwright_api"}
