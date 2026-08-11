"""G2 (R218) — invariant/property assertions measure the SUT analytics' REAL
quality WITHOUT the LLM-invented recipe expected_outputs: did it ANSWER
(well-formed), is the answer GROUNDED (cites sources/insight), is it internally
CONSISTENT. Same contract as the other helpers: SKIP on stub, FAIL on backend error.
"""
from __future__ import annotations

import pytest

from src.automation.python_tests.arta_runtime import (
    AnalyticsResponse, Insight,
    assert_well_formed, assert_grounded, assert_internally_consistent,
)


def _resp(**kw):
    return AnalyticsResponse(**kw)


# ── well-formed ──────────────────────────────────────────────────────────────
def test_g2_well_formed_passes_on_real_answer():
    assert_well_formed(_resp(refused=False, answer="Revenue was $2.45M last quarter."), "q")


def test_g2_well_formed_fails_on_refused():
    with pytest.raises(AssertionError, match="G2"):
        assert_well_formed(_resp(refused=True, answer="x"), "q")


def test_g2_well_formed_fails_on_empty():
    with pytest.raises(AssertionError, match="G2"):
        assert_well_formed(_resp(refused=False, answer="  "), "q")


def test_g2_well_formed_fails_on_error():
    with pytest.raises(AssertionError, match="ERRORED"):
        assert_well_formed(_resp(refused=False, answer="ARTA error", _is_error=True), "q")


def test_g2_well_formed_skips_on_stub():
    with pytest.raises(pytest.skip.Exception):
        assert_well_formed(_resp(_is_stub_default=True, answer="stub"), "q")


# ── grounded ─────────────────────────────────────────────────────────────────
def test_g2_grounded_passes_with_sources():
    assert_grounded(_resp(answer="a", sources=["doc-1"]), "q")


def test_g2_grounded_passes_with_insight_value():
    assert_grounded(_resp(answer="a", insight=Insight(value=2450000.0)), "q")


def test_g2_grounded_passes_with_source_page():
    assert_grounded(_resp(answer="a", insight=Insight(source_page="p12")), "q")


def test_g2_grounded_fails_when_ungrounded():
    with pytest.raises(AssertionError, match="UNGROUNDED"):
        assert_grounded(_resp(answer="a confident narrative citing nothing", sources=[]), "q")


# ── grounded — R308 conversational / prose SUT path ──────────────────────────
# A conversational analytics SUT answers in PROSE and populates NEITHER `sources`
# NOR structured `insight.*` (by contract). The pre-R308 structured-only check
# false-failed EVERY such answer (the over-spec class R299/R303 fixed on the
# assertion side). R308 assesses grounding on the prose for a real conversational
# answer; `_g2_guard` still SKIPs on stub and FAILs on backend error.
def test_g2_grounded_r308_substantive_prose_passes():
    assert_grounded(
        _resp(refused=False,
              answer="Sales rose 12% in Q3, led by the EMEA region's strong uptick.",
              _is_stub_default=False), "summarize sales")


def test_g2_grounded_r308_prose_with_no_figure_but_substantive_passes():
    # ≥40 chars substantive narrative, no digit, no structured insight → grounded.
    assert_grounded(
        _resp(refused=False,
              answer="Revenue grew notably across every enterprise segment this period.",
              _is_stub_default=False), "q")


def test_g2_grounded_r308_hollow_prose_fails():
    # Real conversational answer (≥10 chars) but hollow: no rows, no figure,
    # <40 chars → still UNGROUNDED (truthful — the SUT engaged with nothing).
    with pytest.raises(AssertionError, match="UNGROUNDED"):
        assert_grounded(_resp(refused=False, answer="I have no idea.",
                              _is_stub_default=False), "q")


def test_g2_grounded_r308_error_still_fails():
    # A backend error is NOT a conversational answer — `_g2_guard` FAILs it.
    with pytest.raises(AssertionError, match="ERRORED"):
        assert_grounded(_resp(refused=False, answer="500 Internal Server Error",
                              _is_error=True), "q")


def test_g2_grounded_r308_stub_still_skips():
    with pytest.raises(pytest.skip.Exception):
        assert_grounded(_resp(_is_stub_default=True, answer="stub"), "q")


def test_g2_grounded_r308_killswitch_reverts_to_structured_only(monkeypatch):
    monkeypatch.setenv("ARTA_R308_CONVERSATIONAL_GROUNDING_DISABLE", "1")
    with pytest.raises(AssertionError, match="UNGROUNDED"):
        assert_grounded(
            _resp(refused=False,
                  answer="Sales rose 12% in Q3, led by the EMEA region's strong uptick.",
                  _is_stub_default=False), "q")


# ── internally consistent ────────────────────────────────────────────────────
def test_g2_consistent_passes_when_agree():
    assert_internally_consistent(
        _resp(answer="sales increased sharply", insight=Insight(direction="up")), "q")


def test_g2_consistent_fails_on_contradiction():
    with pytest.raises(AssertionError, match="contradiction"):
        assert_internally_consistent(
            _resp(answer="sales decreased and dropped", insight=Insight(direction="up")), "q")


def test_g2_consistent_noop_without_direction():
    # No structured direction → nothing to contradict → passes.
    assert_internally_consistent(_resp(answer="some narrative", insight=Insight()), "q")
