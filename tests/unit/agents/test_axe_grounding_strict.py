"""B2 — strict axe grounding: the validator requires waitForSPAReady +
skipIfAuthStale (so axe scans a real, hydrated, authenticated page) and flags
root-only specs. The legacy injectAxe/checkA11y/route checks stay always-on.
"""
from __future__ import annotations

from src.agents.grounding_validator import validate_axe_spec_grounded as _v

_GROUNDED = (
    "import { test, expect } from '@playwright/test';\n"
    "import { injectAxe, checkA11y, getViolations } from 'axe-playwright';\n"
    "import { waitForSPAReady, skipIfAuthStale } from '../common/sub_flows';\n"
    "test('a11y', async ({ page }) => {\n"
    "  await page.goto(`${process.env.BASE_URL}/organizations`);\n"
    "  await waitForSPAReady(page); await skipIfAuthStale(page); await injectAxe(page);\n"
    "  await checkA11y(page);\n});\n"
)


def _kinds(viols):
    return {v.kind for v in viols}


def test_grounded_spec_passes_strict():
    assert _v(_GROUNDED, dom_catalog={"routes": ["/organizations"]}) == []


def test_missing_spa_ready_and_auth_verify_flagged():
    spec = (
        "import { injectAxe, checkA11y } from 'axe-playwright';\n"
        "test('a', async ({ page }) => {\n"
        "  await page.goto('/organizations'); await injectAxe(page); await checkA11y(page);\n});\n"
    )
    k = _kinds(_v(spec, dom_catalog={"routes": ["/organizations"]}))
    assert "axe_missing_spa_ready" in k
    assert "axe_missing_auth_verify" in k


def test_root_only_goto_flagged_when_routes_exist():
    spec = _GROUNDED.replace("`${process.env.BASE_URL}/organizations`", "process.env.BASE_URL || '/'") \
                    .replace("await page.goto(process.env.BASE_URL || '/')",
                             "await page.goto('/')")
    # ensure the only literal goto is '/'
    spec = (
        "import { injectAxe, checkA11y } from 'axe-playwright';\n"
        "import { waitForSPAReady, skipIfAuthStale } from '../common/sub_flows';\n"
        "test('a', async ({ page }) => {\n"
        "  await page.goto('/'); await waitForSPAReady(page); await skipIfAuthStale(page);\n"
        "  await injectAxe(page); await checkA11y(page);\n});\n"
    )
    k = _kinds(_v(spec, dom_catalog={"routes": ["/organizations", "/datasets"]}))
    assert "axe_root_only_goto" in k


def test_killswitch_disables_strict_but_legacy_fires(monkeypatch):
    monkeypatch.setenv("ARTA_AXE_GROUND_STRICT_DISABLE", "1")
    # no waitForSPAReady/skipIfAuthStale + missing injectAxe
    spec = "test('a', async ({ page }) => { await checkA11y(page); });\n"
    k = _kinds(_v(spec, dom_catalog={"routes": ["/x"]}))
    assert "axe_missing_spa_ready" not in k and "axe_missing_auth_verify" not in k  # strict off
    assert "axe_missing_inject" in k  # legacy always-on
