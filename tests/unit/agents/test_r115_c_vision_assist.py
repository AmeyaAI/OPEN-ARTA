"""R115.C — vision-assist post-LLM injector + helper TypeScript file checks.

Pre-R115.C, when R114.C's `waitForSPAReady` exhausted its 15s budget but
the SPA button STILL hadn't bound `aria-label` (post-hydration re-render
OR React Suspense boundary), the next `toBeVisible({timeout: 10000+})`
timed out with a cryptic error — even though the button was RENDERED
VISUALLY. R115.C closes the gap with an opt-in DOM-fast-path + LLM-vision
fallback pattern.

Tests cover:
1. Helper TypeScript file exists with required exports
2. Injector wraps long-timeout (≥10000ms) toBeVisible() assertions
3. Injector skips short-timeout assertions (<10000ms — no vision needed)
4. Injector is idempotent (re-running on injected content = no-op)
5. Injector skips when surroundings already contain findByVision marker
"""
from __future__ import annotations

from pathlib import Path

from src.agents.automation_engineer import AutomationEngineerAgent


_VISION_TS = Path(__file__).resolve().parents[3] / "src" / "automation" / "common" / "vision_assist.ts"


def test_r115_c_vision_assist_ts_exists_with_exports():
    """R115.C.1 — vision_assist.ts has findByVision + visionClickFallback exports."""
    assert _VISION_TS.exists(), "R115.C.1: vision_assist.ts missing"
    content = _VISION_TS.read_text()
    assert "export async function findByVision" in content, (
        "R115.C.1: findByVision export missing"
    )
    assert "export async function visionClickFallback" in content, (
        "R115.C.1: visionClickFallback export missing"
    )
    # Opt-in gate must be present (no-op when env var unset)
    assert "TARGET_VISION_ASSIST" in content, (
        "R115.C.1: opt-in gate via TARGET_VISION_ASSIST missing"
    )
    # Must call back to ARTA vision-locate endpoint
    assert "/api/internal/vision-locate" in content, (
        "R115.C.1: vision-locate endpoint URL missing"
    )


def test_r115_c_injector_wraps_long_timeout_visibility():
    """R115.C.4 — long-timeout toBeVisible() gets wrapped with vision fallback."""
    src = """\
import { test, expect } from '@playwright/test';

test('extract button visible', async ({ page }) => {
  await page.goto('/dashboard');
  await expect(page.getByRole('button', { name: 'EXTRACT' })).toBeVisible({ timeout: 10000 });
});
"""
    out, count = AutomationEngineerAgent._r115_c_inject_vision_fallback(src)
    assert count == 1, f"R115.C: expected 1 injection, got {count}"
    assert "_r115_c_loc" in out, "R115.C: locator variable not introduced"
    assert "_r115_c_visible" in out, "R115.C: visibility flag not introduced"
    assert "isVisible({ timeout: 5000 })" in out, "R115.C: DOM fast-path 5s not present"
    assert "findByVision(page, 'EXTRACT')" in out, (
        "R115.C: vision-fallback call missing OR description not derived from name"
    )
    assert "import { findByVision } from '../common/vision_assist';" in out, (
        "R115.C: findByVision import not prepended"
    )
    # Original toBeVisible call must be replaced (not duplicated)
    assert ".toBeVisible({ timeout: 10000 })" not in out, (
        "R115.C: original toBeVisible not replaced"
    )


def test_r115_c_injector_skips_short_timeout():
    """R115.C.4 — assertions with timeout < 10000ms are LEFT ALONE (no vision needed)."""
    src = """\
import { test, expect } from '@playwright/test';

test('quick visibility', async ({ page }) => {
  await page.goto('/dashboard');
  await expect(page.getByRole('button', { name: 'OK' })).toBeVisible({ timeout: 5000 });
});
"""
    out, count = AutomationEngineerAgent._r115_c_inject_vision_fallback(src)
    assert count == 0, "R115.C: short-timeout assertion must NOT be wrapped"
    assert "_r115_c_loc" not in out, "R115.C: locator variable should not appear"
    assert "findByVision" not in out, "R115.C: vision import should not be added for short-timeout"


def test_r115_c_injector_idempotent():
    """R115.C.4 — re-running injector on already-wrapped content is a no-op."""
    src = """\
import { test, expect } from '@playwright/test';

test('extract button visible', async ({ page }) => {
  await page.goto('/dashboard');
  await expect(page.getByRole('button', { name: 'EXTRACT' })).toBeVisible({ timeout: 15000 });
});
"""
    once, count1 = AutomationEngineerAgent._r115_c_inject_vision_fallback(src)
    assert count1 == 1
    twice, count2 = AutomationEngineerAgent._r115_c_inject_vision_fallback(once)
    assert count2 == 0, "R115.C: second pass must be no-op (idempotent)"
    assert twice == once, "R115.C: content should not change on second pass"


def test_r115_c_injector_skips_when_findByVision_marker_present():
    """R115.C.4 — surrounding code already references findByVision → skip."""
    src = """\
import { test, expect } from '@playwright/test';
import { findByVision } from '../common/vision_assist';

test('vision pre-wrapped', async ({ page }) => {
  // R115.C: operator already wrote vision fallback manually
  const btn = page.getByRole('button', { name: 'EXTRACT' });
  const bbox = await findByVision(page, 'EXTRACT button');
  await expect(btn).toBeVisible({ timeout: 12000 });
});
"""
    out, count = AutomationEngineerAgent._r115_c_inject_vision_fallback(src)
    assert count == 0, "R115.C: must skip when findByVision already present in surroundings"
