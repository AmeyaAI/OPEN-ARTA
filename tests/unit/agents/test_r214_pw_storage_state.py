"""R214 P1 — repoint empty storageState override to the authenticated base-config
state. The LLM's `test.use({ storageState: {cookies:[],origins:[]} })` defeats
the authenticated base config → login wall → selector timeouts (req_am_020)."""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent as A

_SPEC = """import { test, expect } from '@playwright/test';
test.use({
  storageState: {
    cookies: [],
    origins: [],
  },
});
test('x', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'ANALYST' })).toBeVisible();
});
"""


def test_empty_storagestate_repointed():
    out, n = A._r214_fix_pw_storage_state(_SPEC)
    assert n == 1
    assert "process.env.TARGET_AUTH_STATE_PATH" in out
    assert "cookies: []" not in out


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R214_PW_STORAGE_STATE_DISABLE", "1")
    out, n = A._r214_fix_pw_storage_state(_SPEC)
    assert n == 0 and "cookies: []" in out


def test_no_storagestate_unaffected():
    spec = "test('x', async ({ page }) => { await page.goto('/'); });"
    out, n = A._r214_fix_pw_storage_state(spec)
    assert n == 0 and out == spec
