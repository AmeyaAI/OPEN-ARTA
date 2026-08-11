"""R115.B — validate_playwright_api_usage catches `request(url)` direct invocation.

Pre-R115.B: R101.C Pattern 4 caught `request.<verb>(` (namespace method call)
but NOT bare `request(url)` (factory-as-function invocation). The latter
compiles via Babel to `(0, _test.request)(url)` which fails at runtime with
`TypeError: (0, _test.request) is not a function`.

Live evidence (run-8da91d req_am_015 TC-AM-015-AUTO001/002): 2 PW FAILs from
this exact pattern. R115.B adds Pattern 4b to close the gap.
"""
from __future__ import annotations

from src.agents.grounding_validator import validate_playwright_api_usage


def test_r115b_bare_request_invocation_caught():
    """`request(url)` with `request` imported from '@playwright/test' → flagged."""
    content = """
import { test, expect, request } from '@playwright/test';

test('foo', async ({ page }) => {
  const resp = await request('/api/x');
  expect(resp.ok()).toBeTruthy();
});
"""
    violations = validate_playwright_api_usage(content)
    matching = [v for v in violations if "bare_request_invocation" in v.symbol]
    assert len(matching) >= 1, (
        f"R115.B: expected bare_request_invocation violation, got: {[v.symbol for v in violations]}"
    )
    assert "BEFORE" in matching[0].hint and "AFTER" in matching[0].hint
    assert "_test.request" in matching[0].hint or "TypeError" in matching[0].hint


def test_r115b_request_namespace_call_not_flagged_twice():
    """`request.get(url)` is Pattern 4 (already caught); shouldn't double-flag."""
    content = """
import { test, request } from '@playwright/test';

test('foo', async ({ page }) => {
  const resp = await request.get('/api/x');
});
"""
    violations = validate_playwright_api_usage(content)
    # Pattern 4 should fire (request.get without fixture destructure)
    pattern_4 = [v for v in violations if v.symbol.startswith("request.get")]
    # Pattern 4b (R115.B) should NOT fire — there's no bare `request(`
    pattern_4b = [v for v in violations if "bare_request_invocation" in v.symbol]
    assert len(pattern_4b) == 0, (
        f"R115.B false positive on request.get(): {[v.symbol for v in pattern_4b]}"
    )


def test_r115b_request_fixture_destructure_suppresses_flag():
    """If `{ request }` is destructured in test arg, Pattern 4b should NOT fire."""
    content = """
import { test } from '@playwright/test';

test('foo', async ({ page, request }) => {
  const resp = await request('/api/x');
});
"""
    violations = validate_playwright_api_usage(content)
    # When `{ request }` is destructured as fixture, request IS callable
    # → suppression matches existing Pattern 4 suppression logic
    pattern_4b = [v for v in violations if "bare_request_invocation" in v.symbol]
    assert len(pattern_4b) == 0, (
        f"R115.B: should suppress when `{{ request }}` fixture destructured, got: {[v.symbol for v in pattern_4b]}"
    )
