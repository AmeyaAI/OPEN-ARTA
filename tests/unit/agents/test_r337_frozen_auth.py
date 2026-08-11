"""R337 — the frozen-AUTH_TOKEN rewriter (within-file expiry fix).

The in-spec refresh keeps process.env.AUTH_TOKEN fresh; a describe/module-scope
capture freezes it at spec-load and every call after the ~15-min TTL 401s. R337
rewrites both frozen forms the LLM emits into fresh per-request reads."""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent as AE


def _fix(src: str):
    return AE._r337_inline_frozen_auth_token(src)


class TestR337BareConst:
    def test_bare_const_inlined_fresh(self):
        src = (
            "test.describe('x', () => {\n"
            "  const authToken = process.env.AUTH_TOKEN || '';\n"
            "  test('t', async ({ request }) => {\n"
            "    await request.get(url, { headers: { Authorization: `Bearer ${authToken}` } });\n"
            "  });\n"
            "});\n"
        )
        out, n = _fix(src)
        assert n >= 1
        assert "const authToken = process.env.AUTH_TOKEN" not in out
        assert "Bearer ${(process.env.AUTH_TOKEN || '')}" in out


class TestR337BHeaderObject:
    def test_frozen_header_object_becomes_getter(self):
        # the kui_605 pattern (run-32294b: 40 auth-cascade 401s)
        src = (
            "test.describe('x', () => {\n"
            "  const authHeaders = {\n"
            "    'Authorization': `Bearer ${process.env.AUTH_TOKEN || ''}`,\n"
            "    'Content-Type': 'application/json',\n"
            "  };\n"
            "  test('t', async ({ request }) => {\n"
            "    const resp = await request.get(url, { headers: authHeaders });\n"
            "  });\n"
            "});\n"
        )
        out, n = _fix(src)
        assert n >= 1
        # frozen object literal is gone; a fresh-reading getter replaces it
        assert "const authHeaders = () => (" in out
        assert "const authHeaders = {" not in out
        # usage now calls the getter (re-reads the token per request)
        assert "headers: authHeaders()" in out
        # the token read is still present inside the getter body
        assert "process.env.AUTH_TOKEN" in out

    def test_non_auth_object_untouched(self):
        # an object const WITHOUT AUTH_TOKEN must not be converted
        src = (
            "  const cfg = { 'Content-Type': 'application/json', retries: 2 };\n"
            "  await request.get(url, { headers: cfg });\n"
        )
        out, n = _fix(src)
        assert n == 0
        assert "const cfg = { 'Content-Type'" in out
        assert "cfg()" not in out

    def test_idempotent(self):
        src = (
            "  const authHeaders = { 'Authorization': `Bearer ${process.env.AUTH_TOKEN || ''}` };\n"
            "  await request.get(url, { headers: authHeaders });\n"
        )
        once, _ = _fix(src)
        twice, n2 = _fix(once)
        assert once == twice and n2 == 0  # second pass is a no-op
