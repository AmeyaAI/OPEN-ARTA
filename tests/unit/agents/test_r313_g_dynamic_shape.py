"""R313.G — general deterministic rewriter: exact-value assertions on dynamic
JSON-response-body fields → shape assertions by literal type. Generalises R313.E.2
to uncaptured fields + numeric counts. STRICTLY gated to parsed-response variables —
status codes, DOM, non-body vars, and proven constants must be untouched (the
negative cases are the regression guard for a broad assertion-strategy change)."""
from __future__ import annotations

from src.agents.grounding_validator import _r313_g_rewrite_dynamic_value_asserts

# each spec declares a json body var so the gate is armed
HEAD = "const body = await resp.json();\n"


def _rw(body_line, domains=None):
    return _r313_g_rewrite_dynamic_value_asserts(HEAD + body_line, domains)


# ── positive: dynamic response fields become shape assertions ──────────────────
def test_string_state_becomes_typeof_string():
    new, n = _rw("expect(body.currentState).toBe('ready');")
    assert n == 1 and "expect(typeof body.currentState).toBe('string')" in new
    assert "toBe('ready')" not in new


def test_numeric_count_becomes_typeof_number():
    new, n = _rw("expect(body.items.length).toBe(3);")
    assert n == 1 and "expect(typeof body.items.length).toBe('number')" in new


def test_boolean_becomes_typeof_boolean():
    new, n = _rw("expect(body.enabled).toBe(true);")
    assert n == 1 and "expect(typeof body.enabled).toBe('boolean')" in new


def test_indexed_and_chained_path():
    new, n = _rw("expect(body.servers[0].status).toBe('active');")
    assert n == 1 and "expect(typeof body.servers[0].status).toBe('string')" in new


def test_r313h_isarray_on_optional_field_gets_presence_guard():
    """R313.H — expect(Array.isArray(optionalField)).toBe(true) false-FAILs when the
    field is absent; guard it so it asserts array-ness only when present."""
    new, n = _rw("expect(Array.isArray(body.stateTransitionLog)).toBe(true);")
    assert n == 1
    assert "body.stateTransitionLog === undefined" in new
    assert "Array.isArray(body.stateTransitionLog)" in new
    assert new.count("(") == new.count(")")


def test_r313h_isarray_on_local_alias_of_body_field():
    """R313.G.3 — a local aliased from a body field is a response ref: the LLM does
    `const log = hostData.stateTransitionLog; expect(Array.isArray(log)).toBe(true)`."""
    spec = ("const hostData = await resp.json();\n"
            "const log = hostData?.stateTransitionLog;\n"
            "expect(Array.isArray(log)).toBe(true);")
    new, n = _r313_g_rewrite_dynamic_value_asserts(spec, {})
    assert n == 1 and "log === undefined || log === null || Array.isArray(log)" in new


def test_r313h_leaves_isarray_on_non_body_var():
    # a local NOT derived from a response body must not be touched
    spec = "const cfg = loadConfig();\nexpect(Array.isArray(cfg.items)).toBe(true);"
    new, n = _r313_g_rewrite_dynamic_value_asserts(spec, {})
    assert n == 0


def test_optional_chaining_subject_is_rewritten():
    """R313.G.2 — the LLM writes `hostData?.currentState` heavily; the path must
    accept `?.` (a `\\.\\w+`-only path silently skipped every optional-chained
    fabrication)."""
    new, n = _rw("expect(body?.currentState).toBe('ready');")
    assert n == 1 and "expect(typeof body?.currentState).toBe('string')" in new


def test_named_const_tocontain_on_body_field_is_rewritten():
    """R313.G.4 — the LLM prefers `const validStates=[...]; expect(validStates)
    .toContain(body.currentState)` (named const). The inline-array pass missed it;
    this must reground it to shape (the form that blocked the last kui_539 regen)."""
    spec = ("const validStates = ['active', 'error', 'stopped'];\n"
            "expect(validStates).toContain(body.currentState);")
    new, n = _rw(spec)
    assert n == 1 and "expect(typeof body.currentState).toBe('string')" in new
    assert "toContain" not in new.split("\n")[-1]


def test_named_const_tocontain_on_non_body_var_untouched():
    spec = ("const localList = ['a', 'b'];\n"
            "expect(localList).toContain(cfg.region);")  # cfg not a .json() body var
    new, n = _r313_g_rewrite_dynamic_value_asserts(spec, {})
    assert n == 0


def test_inline_array_tocontain_is_rewritten():
    """R313.G.2 — an INLINE guessed enum array `expect(['a','b']).toContain(field)`
    (R313 only handled a NAMED const) → shape assertion on the field."""
    new, n = _rw("expect(['running', 'stopped']).toContain(body.currentState);")
    assert n == 1 and "expect(typeof body.currentState).toBe('string')" in new


def test_inline_array_tocontain_with_optional_chain_arg():
    new, n = _rw("expect(['healthy', 'unhealthy']).toContain(body.healthStatus?.status);")
    assert n == 1 and "expect(typeof body.healthStatus?.status).toBe('string')" in new


# ── negative: things that must NEVER be rewritten ──────────────────────────────
def test_status_code_untouched():
    new, n = _rw("expect(resp.status()).toBe(200);")
    assert n == 0 and "expect(resp.status()).toBe(200)" in new


def test_dom_url_untouched():
    # page.url() is not a json-body var member-access
    new, n = _r313_g_rewrite_dynamic_value_asserts(
        "const x = 1;\nexpect(page.url()).toBe('https://sut/home');")
    assert n == 0


def test_non_body_var_untouched():
    # `cfg` is not assigned from .json() → not a response body
    new, n = _r313_g_rewrite_dynamic_value_asserts(
        "const cfg = loadConfig();\nexpect(cfg.region).toBe('us-texas-1');")
    assert n == 0


def test_no_body_var_declared_is_noop():
    new, n = _r313_g_rewrite_dynamic_value_asserts(
        "expect(body.currentState).toBe('ready');")  # no `= .json()` anywhere
    assert n == 0 and new.endswith("toBe('ready');")


def test_proven_singleton_constant_kept():
    # a field observed to have exactly one value == the asserted literal is a real
    # invariant — keep the exact assertion
    new, n = _rw("expect(body.kind).toBe('server');", {"kind": {"server"}})
    assert n == 0 and "toBe('server')" in new


def test_multivalue_domain_still_rewritten():
    new, n = _rw("expect(body.currentState).toBe('ready');",
                 {"currentState": {"registered", "queued"}})
    assert n == 1 and "typeof body.currentState" in new


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R313_G_REWRITE_DISABLE", "1")
    new, n = _rw("expect(body.currentState).toBe('ready');")
    assert n == 0


def test_syntax_stays_balanced():
    spec = HEAD + (
        "expect(body.currentState).toBe('ready');\n"
        "expect(body.count).toBe(5);\n"
        "expect(resp.status()).toBe(200);\n")
    new, n = _r313_g_rewrite_dynamic_value_asserts(spec)
    assert n == 2
    assert new.count("(") == new.count(")")
    assert new.count("{") == new.count("}")
