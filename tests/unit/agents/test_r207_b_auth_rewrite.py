"""R207.B — post-LLM rewrite of hard-coded auth headers → authHeaderFor(url).

The LLM ignores the prompt instruction to import `authHeaderFor` and emits its
own `getAuthHeader()` returning `Bearer ${AUTH_TOKEN}` (the agent token), which
500s on collection-manager endpoints (run-cf956e: 102 FAILs). R207.B
deterministically rewrites the valid-auth call sites to the per-path resolver.
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent as A

_SPEC = """import { test, expect } from '@playwright/test';

test.describe('Feature', () => {
  const apiBase = process.env.API_BASE_URL;
  const getAuthHeader = () => {
    const token = process.env.AUTH_TOKEN;
    return { 'Authorization': `Bearer ${token}` };
  };

  test('happy path', async ({ request }) => {
    const resp = await request.get(`${apiBase}/api/collection/x/y`, { headers: getAuthHeader() });
    expect(resp.status()).toBe(200);
  });

  test('negative — bad token', async ({ request }) => {
    const resp = await request.get(`${apiBase}/api/collection/x/y`, { headers: { 'Authorization': 'Bearer invalid' } });
    expect(resp.status()).toBe(401);
  });
});
"""


def test_r207b_rewrites_happy_path_auth():
    out, count = A._r207_b_rewrite_auth_header(_SPEC)
    assert count == 1
    assert "import { authHeaderFor } from '../common/arta_auth';" in out
    # the happy-path header now resolves per-path
    assert "authHeaderFor(`${apiBase}/api/collection/x/y`)" in out
    assert "headers: getAuthHeader()" not in out


def test_r207b_leaves_negative_header_intact():
    out, _ = A._r207_b_rewrite_auth_header(_SPEC)
    # the explicit bad-token negative case must NOT be rewritten
    assert "'Authorization': 'Bearer invalid'" in out


def test_r207b_idempotent():
    out1, _ = A._r207_b_rewrite_auth_header(_SPEC)
    out2, c2 = A._r207_b_rewrite_auth_header(out1)
    assert c2 == 0 and out2 == out1


def test_r207b_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R207_B_DISABLE", "1")
    out, count = A._r207_b_rewrite_auth_header(_SPEC)
    assert count == 0 and out == _SPEC


def test_r207b_noop_when_no_authheader():
    spec = "const x = await request.get(`${apiBase}/y`);"
    out, count = A._r207_b_rewrite_auth_header(spec)
    assert count == 0
