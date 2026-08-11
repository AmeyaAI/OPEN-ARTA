"""R125.A — fixture-parameter validator catches non-PW fixtures in test()/hook signatures.

Pre-R125.A: LLM emitted `test('foo', async ({page, ordersResp, usersResp}) => ...)`.
Playwright runtime raised `Test has unknown parameter "ordersResp"` at dispatch.
Live evidence: pw-run-12764a-req_am_002.spec.json — 45 such errors in one spec.

R125.A adds `_r125_a_validate_fixture_params` invoked from
`validate_playwright_api_usage`. Closes the gap by flagging unknown fixture
names at gen time so R57.1 retry-with-hint surfaces the BEFORE/AFTER snippet
and the LLM corrects on attempt 2.
"""
from __future__ import annotations

from src.agents.grounding_validator import (
    _r125_a_validate_fixture_params,
    validate_playwright_api_usage,
)


def test_r125a_unknown_fixture_flagged():
    """req_am_002 reproduction — unknown fixtures in test() destructure."""
    content = """
import { test, expect } from '@playwright/test';

test('user creates org', async ({ page, ordersResp, usersResp, projectsResp }) => {
  await page.goto('/');
});
"""
    violations = _r125_a_validate_fixture_params(content)
    symbols = {v.symbol for v in violations}
    assert "ordersResp" in symbols, f"expected ordersResp flagged, got {symbols}"
    assert "usersResp" in symbols
    assert "projectsResp" in symbols
    # `page` is built-in — must NOT be flagged
    assert "page" not in symbols
    # Hint must include BEFORE/AFTER snippets
    v = next(iter(violations))
    assert "BEFORE" in v.hint and "AFTER" in v.hint
    assert v.kind == "unknown_fixture_parameter"
    assert v.tool == "playwright"


def test_r125a_builtin_fixtures_not_flagged():
    """page/context/request/browser are PW built-ins — no violation."""
    content = """
import { test, expect } from '@playwright/test';

test('foo', async ({ page, context, request, browser, browserName }) => {
  await page.goto('/');
});
"""
    violations = _r125_a_validate_fixture_params(content)
    assert violations == [], f"expected no violations for built-ins, got {[v.symbol for v in violations]}"


def test_r125a_extend_declared_fixture_not_flagged():
    """`test.extend({myDb: ...})` declares myDb — must be allowed in test()."""
    content = """
import { test as baseTest, expect } from '@playwright/test';

const test = baseTest.extend({
  myDb: async ({}, use) => { await use({ connect: () => null }); },
  authToken: async ({}, use) => { await use('TOKEN'); },
});

test('uses extended fixtures', async ({ page, myDb, authToken }) => {
  await page.goto('/');
});
"""
    violations = _r125_a_validate_fixture_params(content)
    symbols = {v.symbol for v in violations}
    assert "myDb" not in symbols, f"myDb was declared via .extend; got {symbols}"
    assert "authToken" not in symbols
    assert violations == [], f"expected zero violations, got {[v.symbol for v in violations]}"


def test_r125a_multiple_unknowns_deduplicated():
    """Same unknown fixture in multiple tests → one violation only (deduped)."""
    content = """
import { test } from '@playwright/test';

test('one', async ({ page, fakeFix }) => { await page.goto('/'); });
test('two', async ({ page, fakeFix }) => { await page.goto('/x'); });
test('three', async ({ page, fakeFix }) => { await page.goto('/y'); });
"""
    violations = _r125_a_validate_fixture_params(content)
    fake_viols = [v for v in violations if v.symbol == "fakeFix"]
    assert len(fake_viols) == 1, (
        f"expected dedup to 1 violation, got {len(fake_viols)}"
    )


def test_r125a_hook_signatures_also_checked():
    """beforeEach/afterEach with unknown fixtures → flagged."""
    content = """
import { test } from '@playwright/test';

test.beforeEach(async ({ page, secretConfig }) => {
  await page.goto('/');
});
"""
    violations = _r125_a_validate_fixture_params(content)
    symbols = {v.symbol for v in violations}
    assert "secretConfig" in symbols, f"hook signature not scanned; got {symbols}"


def test_r125a_wired_through_validate_playwright_api_usage():
    """Integration: R125.A violations appear in the public entry point."""
    content = """
import { test } from '@playwright/test';

test('foo', async ({ page, ordersResp }) => {
  await page.goto('/');
});
"""
    violations = validate_playwright_api_usage(content)
    r125_a_viols = [v for v in violations if v.kind == "unknown_fixture_parameter"]
    assert len(r125_a_viols) >= 1, (
        f"R125.A must be wired into validate_playwright_api_usage; got {violations}"
    )
