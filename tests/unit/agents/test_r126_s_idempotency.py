"""R126.S — Rewriter idempotency contract with R126.B scaffolder.

R126.B emits deterministic boilerplate including:
- imports (test, expect, Page, waitForSPAReady, skipIfAuthStale, smartVisible)
- test.beforeEach with goto + waitForSPAReady + skipIfAuthStale already injected
- per-test LLM_FILL placeholders

Post-LLM rewriters (R102.E, R112.E, R114.C, R115.C, R118.A) used to
re-inject these patterns on EVERY spec. Without R126.S, R126.B-scaffolded
specs would get DOUBLE injection (e.g., 2 waitForSPAReady() calls in a
row in beforeEach), causing parse errors or wasted operations.

R126.S contract: each rewriter checks for the
`// R126.B SKELETON SCAFFOLDED` marker as the first line. If present, the
rewriter short-circuits the responsibilities R126.B owns (beforeEach
injections) while still applying its other responsibilities (e.g., R115.C
vision-fallback wrapping happens inside test() bodies, NOT beforeEach —
that part stays active).
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent


R126_B_MARKER_LINE = "// R126.B SKELETON SCAFFOLDED — req_id=REQ-TEST-001 at 2026-05-21T00:00:00+00:00\n"

PLAIN_LLM_SPEC = """\
import { test, expect } from '@playwright/test';

test('foo', async ({ page }) => {
  await page.goto('/dashboard');
  await expect(page.getByRole('main')).toBeVisible();
});
"""

SCAFFOLDED_SPEC = R126_B_MARKER_LINE + """\
import { test, expect, Page } from '@playwright/test';
import { waitForSPAReady, skipIfAuthStale } from '../common/sub_flows';

const API_BASE_URL = process.env.API_BASE_URL || process.env.BASE_URL;
if (!API_BASE_URL) { throw new Error('[ARTA] API_BASE_URL not set'); }

test.describe('REQ-TEST-001', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(process.env.BASE_URL ?? '/');
    await waitForSPAReady(page);
    await skipIfAuthStale(page);
  });
  test('AC-001', async ({ page }) => {
    await page.goto('/dashboard');
    await expect(page.getByRole('main')).toBeVisible();
  });
});
"""


def test_r126s_r112e_skips_when_marker_present():
    """R112.E auth-verify must skip injection when R126.B marker is present."""
    out, injected = AutomationEngineerAgent._r112_e_inject_auth_verify(SCAFFOLDED_SPEC)
    assert injected == 0, "R112.E must not inject on R126.B-scaffolded spec"
    assert out == SCAFFOLDED_SPEC, "content unchanged (R126.S idempotency)"


def test_r126s_r112e_still_runs_on_plain_llm_spec():
    """R112.E preserves its existing behavior on plain (non-scaffolded) specs."""
    out, injected = AutomationEngineerAgent._r112_e_inject_auth_verify(PLAIN_LLM_SPEC)
    # Inject at the /dashboard goto site (NOT a login/signup route)
    assert injected >= 1, "R112.E must still inject on plain LLM specs"
    assert "isLoggedIn" in out or "skipIfAuthStale" in out


def test_r126s_r114c_skips_when_marker_present():
    """R114.C SPA hydration must skip injection when R126.B marker is present."""
    out, injected = AutomationEngineerAgent._r114_c_inject_spa_hydration(SCAFFOLDED_SPEC)
    assert injected == 0, "R114.C must not inject on R126.B-scaffolded spec"
    assert out == SCAFFOLDED_SPEC


def test_r126s_r114c_still_runs_on_plain_llm_spec():
    """R114.C preserves its existing behavior on plain (non-scaffolded) specs."""
    out, injected = AutomationEngineerAgent._r114_c_inject_spa_hydration(PLAIN_LLM_SPEC)
    # Should inject waitForSPAReady after /dashboard goto
    assert injected >= 1 or "waitForSPAReady" in out, (
        "R114.C must still inject on plain LLM specs"
    )


def test_r126s_marker_prefix_is_constant():
    """The R126.B marker line MUST be the canonical prefix the rewriters check.
    Single source of truth — both R126.B and R126.S must use this constant."""
    assert AutomationEngineerAgent._R126_B_MARKER_PREFIX == "// R126.B SKELETON SCAFFOLDED"


def test_r126s_marker_at_start_of_file_recognized():
    """Marker MUST be the very first line (after optional leading whitespace)."""
    # With leading blank line (should still be recognized via .lstrip())
    spec_with_blank = "\n" + R126_B_MARKER_LINE + "import { test } from '@playwright/test';\n"
    out, injected = AutomationEngineerAgent._r112_e_inject_auth_verify(spec_with_blank)
    assert injected == 0, "marker after leading whitespace must still be detected"


def test_r126s_marker_not_at_start_is_not_recognized():
    """A marker buried mid-file (NOT first line) does NOT trigger idempotency.
    Defensive: only the first-line marker is trusted (prevents adversarial
    LLM-generated content from disabling the rewriters by including the
    marker text somewhere in the middle)."""
    spec = (
        "import { test, expect } from '@playwright/test';\n"
        "// Note: R126.B SKELETON SCAFFOLDED (commented mid-file)\n"
        "test('foo', async ({ page }) => {\n"
        "  await page.goto('/dashboard');\n"
        "});\n"
    )
    out, injected = AutomationEngineerAgent._r112_e_inject_auth_verify(spec)
    # Mid-file marker is NOT recognized; R112.E should still run
    assert injected >= 1, (
        "marker buried mid-file must not disable R112.E (defensive against LLM-emitted text)"
    )
