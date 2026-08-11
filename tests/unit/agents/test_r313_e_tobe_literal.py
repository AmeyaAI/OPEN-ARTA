"""R313.E — flag fabricated direct-equality assertions on mutable runtime fields:
expect(body.currentState).toBe('ready') when the SUT's observed domain is a
multi-valued enum or omits the literal. This is the toBe(literal) sibling of the
toContain([...]) enum check and was the dominant residual FAIL class (run-9b3dc7:
32/44 FAILs). It must NOT touch typeof-shape asserts, numeric status codes, or a
proven singleton constant."""
from __future__ import annotations

from src.agents.grounding_validator import (
    _r313_e_validate_tobe_literal,
    _r313_e2_rewrite_fabricated_tobe,
)


def test_r313_e2_rewrites_fabricated_tobe_to_shape():
    """R313.E.2 deterministic rewriter: fabricated toBe(literal) on a mutable field
    → typeof-shape, reliably (no LLM round-trip), leaving valid syntax."""
    spec = ("expect(body.currentState).toBe('ready');\n"
            "expect(resp.status()).toBe(200);\n"          # numeric — untouched
            "expect(typeof body.currentState).toBe('string');")  # already shape — untouched
    new, n = _r313_e2_rewrite_fabricated_tobe(spec, DOMAINS)
    assert n == 1
    assert "expect(typeof body.currentState).toBe('string');" in new
    assert "toBe('ready')" not in new
    assert "toBe(200)" in new                              # numeric preserved
    # the detector now finds nothing → no false BLOCK, no retry needed
    assert _r313_e_validate_tobe_literal(new, DOMAINS) == []
    # syntax preserved
    assert new.count("(") == new.count(")")


def test_r313_e2_leaves_singleton_constant_and_unknown_field():
    spec = ("expect(body.kind).toBe('server');\n"          # singleton constant
            "expect(body.unknownField).toBe('x');")        # no domain
    new, n = _r313_e2_rewrite_fabricated_tobe(spec, {"kind": {"server"}})
    assert n == 0 and new == spec


def test_r313_e2_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R313_E2_REWRITE_DISABLE", "1")
    spec = "expect(body.currentState).toBe('ready');"
    new, n = _r313_e2_rewrite_fabricated_tobe(spec, DOMAINS)
    assert n == 0 and new == spec


DOMAINS = {"currentState": {"registered", "queued"}, "status": {"active", "failed"}}


def test_flags_fabricated_state_literal_not_observed():
    spec = "expect(body.currentState).toBe('ready');"
    v = _r313_e_validate_tobe_literal(spec, DOMAINS)
    assert len(v) == 1 and v[0].kind == "fabricated_value_domain"
    assert "typeof" in v[0].hint  # steers to shape assertion


def test_flags_observed_but_mutable_value():
    # 'registered' IS observed, but the field is a multi-valued mutable enum → still
    # fragile to assert one exact value.
    spec = "expect(body.currentState).toBe('registered');"
    assert len(_r313_e_validate_tobe_literal(spec, DOMAINS)) == 1


def test_ignores_typeof_shape_assertion():
    spec = "expect(typeof body.currentState).toBe('string');"
    assert _r313_e_validate_tobe_literal(spec, DOMAINS) == []


def test_ignores_numeric_status_code():
    spec = "expect(resp.status()).toBe(200);"
    assert _r313_e_validate_tobe_literal(spec, DOMAINS) == []


def test_ignores_field_without_observed_domain():
    spec = "expect(body.unknownField).toBe('whatever');"
    assert _r313_e_validate_tobe_literal(spec, DOMAINS) == []


def test_singleton_constant_asserted_with_real_value_is_ok():
    spec = "expect(body.kind).toBe('server');"
    assert _r313_e_validate_tobe_literal(spec, {"kind": {"server"}}) == []


def test_dedup_by_field_and_literal():
    spec = ("expect(body.currentState).toBe('ready');\n"
            "expect(body.currentState).toBe('ready');")
    assert len(_r313_e_validate_tobe_literal(spec, DOMAINS)) == 1


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R313_E_TOBE_VALIDATOR_DISABLE", "1")
    spec = "expect(body.currentState).toBe('ready');"
    assert _r313_e_validate_tobe_literal(spec, DOMAINS) == []


def test_cold_start_no_domain_is_noop():
    assert _r313_e_validate_tobe_literal("expect(body.x).toBe('y');", {}) == []
