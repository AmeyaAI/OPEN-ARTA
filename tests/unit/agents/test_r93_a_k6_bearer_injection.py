"""R93.A regression tests for `_r93_a_inject_k6_bearer_params()`.

Pre-R93.A k6 scripts hit authenticated SUT endpoints with naked
`http.get(URL)` calls → 401-flood → `checks: 0%` → no perf signal.
Mission: *"generate high quality test scripts"* → k6 specs need
Authorization headers when the project uses Bearer auth.

R93.A's prompt-level HARD CONSTRAINT instructs the LLM; this static
backstop catches outputs that still emit naked http.X() calls. It
walks every `http.METHOD(URL)` site, rewrites to
`http.METHOD(URL, _auth_r93a)`, and prepends the `_auth_r93a` const
to the default function body.

These tests lock the contract:
  - naked → injected (Bearer auth applied)
  - idempotent (already has Authorization → no-op)
  - mixed (some calls authed, some naked → only naked get rewritten)
  - method variants (GET / POST / PUT / PATCH / DELETE)
  - 3-arg POST (preserves body)
  - default function const prepend
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent


# ── Mission core: naked → injected ────────────────────────────────────


def test_naked_http_get_gets_auth_params_injected():
    """Single naked http.get(URL) → http.get(URL, _auth_r93a) + const added."""
    content = """\
import http from 'k6/http';
export default function () {
  http.get(`${__ENV.BASE_URL}/api/users`);
}
"""
    out, count = AutomationEngineerAgent._r93_a_inject_k6_bearer_params(content)
    assert count == 1
    assert "http.get(`${__ENV.BASE_URL}/api/users`, _auth_r93a)" in out
    assert "_auth_r93a = { headers: { Authorization: `Bearer ${__ENV.AUTH_TOKEN}` } }" in out


def test_method_variants_post_put_patch_delete():
    """All http.METHOD variants (POST, PUT, PATCH, DELETE) get rewritten."""
    content = """\
export default function () {
  http.get(`${__ENV.BASE_URL}/a`);
  http.post(`${__ENV.BASE_URL}/b`);
  http.put(`${__ENV.BASE_URL}/c`);
  http.patch(`${__ENV.BASE_URL}/d`);
  http.del(`${__ENV.BASE_URL}/e`);
}
"""
    out, count = AutomationEngineerAgent._r93_a_inject_k6_bearer_params(content)
    assert count == 5


# ── Idempotency ───────────────────────────────────────────────────────


def test_already_has_authorization_header_is_no_op():
    """If Authorization is already present, content unchanged + count=0."""
    content = """\
export default function () {
  http.get(`${__ENV.BASE_URL}/api/users`, {
    headers: { Authorization: 'Bearer xyz' }
  });
}
"""
    out, count = AutomationEngineerAgent._r93_a_inject_k6_bearer_params(content)
    assert count == 0
    assert out == content


def test_already_uses_auth_var_is_no_op():
    """If operator already set a `_auth` var, preserve it."""
    content = """\
export default function () {
  const _auth = { headers: { Authorization: 'Bearer xyz' } };
  http.get(`${__ENV.BASE_URL}/api/users`, _auth);
}
"""
    out, count = AutomationEngineerAgent._r93_a_inject_k6_bearer_params(content)
    assert count == 0
    assert out == content


# ── Edge cases ────────────────────────────────────────────────────────


def test_no_http_calls_returns_zero():
    """Content with no http.X() calls → no-op."""
    content = "export default function () { sleep(1); }"
    out, count = AutomationEngineerAgent._r93_a_inject_k6_bearer_params(content)
    assert count == 0


def test_const_prepended_to_default_function_body():
    """The _auth_r93a const lands inside `export default function () { ... }`."""
    content = """\
import http from 'k6/http';

export default function () {
  http.get(`${__ENV.BASE_URL}/api/users`);
}
"""
    out, _ = AutomationEngineerAgent._r93_a_inject_k6_bearer_params(content)
    # Auth const must appear AFTER the opening brace of default function
    fn_open = out.find("export default function () {")
    auth_const = out.find("_auth_r93a")
    assert fn_open < auth_const, "_auth_r93a must appear inside default function body"
