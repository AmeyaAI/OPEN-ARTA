"""R316 — unified structural expect() parser + value-fabrication classifier. One parse
handles every syntactic form the 7 regex passes (E.2/G/G.2/G.3/G.4/H) each hand-matched,
PLUS toEqual/toStrictEqual for free. This is the durable consolidation; these tests are
the regression net for migrating the wiring off the regex passes."""
from __future__ import annotations

from src.agents.grounding_validator import (
    _iter_expect_calls,
    _r316_unified_value_rewrite,
)

HEAD = "const body = await resp.json();\n"


def _rw(line):
    return _r316_unified_value_rewrite(HEAD + line, {})


# ── parser: structural extraction across all forms ─────────────────────────────
def test_parser_extracts_subject_matcher_arg():
    def one(s):
        c = list(_iter_expect_calls(s))
        return (c[0]["subject"], c[0]["matcher"], c[0]["arg"]) if c else None
    assert one("expect(body.x).toBe('a');") == ("body.x", "toBe", "'a'")
    assert one("expect(a?.b).toBe('c');") == ("a?.b", "toBe", "'c'")
    assert one("expect(['a','b']).toContain(body.x);") == ("['a','b']", "toContain", "body.x")
    assert one("expect(Array.isArray(l)).toBe(true);") == ("Array.isArray(l)", "toBe", "true")
    assert one("expect(resp.status()).toBe(200);") == ("resp.status()", "toBe", "200")
    # parens inside a string literal must not break balancing
    assert one("expect(body.m).toBe('a (b) c');") == ("body.m", "toBe", "'a (b) c'")


# ── classifier: value fabrications on response fields → shape ───────────────────
def test_tobe_toequal_tostrictequal_all_rewritten():
    for mt, lit, jst in [("toBe", "'ready'", "string"),
                         ("toEqual", "'ready'", "string"),
                         ("toStrictEqual", "3", "number"),
                         ("toBe", "true", "boolean")]:
        new, n = _rw(f"expect(body.f).{mt}({lit});")
        assert n == 1 and f"expect(typeof body.f).toBe('{jst}')" in new, (mt, new)


def test_optional_chaining_and_index_paths():
    new, n = _rw("expect(body?.a.b[0].c).toBe('x');")
    assert n == 1 and "typeof body?.a.b[0].c" in new


def test_inline_and_named_const_membership():
    new, n = _rw("expect(['a','b']).toContain(body.state);")
    assert n == 1 and "expect(typeof body.state).toBe('string')" in new
    spec = "const vs = ['a','b'];\nexpect(vs).toContain(body.state);"
    new, n = _r316_unified_value_rewrite(HEAD + spec, {})
    assert n == 1 and "expect(typeof body.state).toBe('string')" in new


def test_isarray_presence_guard_incl_local_alias():
    spec = "const log = body.stateTransitionLog;\nexpect(Array.isArray(log)).toBe(true);"
    new, n = _r316_unified_value_rewrite(HEAD + spec, {})
    assert n == 1 and "log === undefined || log === null || Array.isArray(log)" in new


# ── negatives: must never touch these ──────────────────────────────────────────
def test_status_code_untouched():
    new, n = _rw("expect(resp.status()).toBe(200);")
    assert n == 0


def test_non_body_var_untouched():
    new, n = _r316_unified_value_rewrite(
        "const cfg = load();\nexpect(cfg.region).toBe('us-1');", {})
    assert n == 0


def test_typeof_output_is_idempotent():
    # R316 must not re-rewrite its own `expect(typeof x).toBe('string')` output
    new, n = _rw("expect(typeof body.f).toBe('string');")
    assert n == 0


def test_proven_singleton_constant_kept():
    new, n = _r316_unified_value_rewrite(
        HEAD + "expect(body.kind).toBe('server');", {"kind": {"server"}})
    assert n == 0


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R316_UNIFIED_DISABLE", "1")
    new, n = _rw("expect(body.f).toBe('x');")
    assert n == 0
