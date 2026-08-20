"""Deterministic charter RCA — the LLM-free defect majority (test_gen_bug /
grounding_blocked / sut_contract_change / operator_review) must carry the
charter's impact + preventive_action + 5-level deep_dive WITHOUT an LLM call
(efficiency mandate). The root_cause + fix + severity are set elsewhere; this
fills the three that were missing."""
from __future__ import annotations

from src.agents.defect_intel import _DETERMINISTIC_RCA, deterministic_rca_fields

_LEVELS = {"symptom", "immediate_cause", "upstream_cause",
           "architectural_cause", "process_cause"}


def test_every_known_class_has_the_full_charter_structure():
    for cls in ("grounding_blocked", "test_gen_bug", "sut_contract_change",
                "operator_review"):
        out = deterministic_rca_fields(cls, ["some_signal"])
        assert out["impact"] and out["preventive_action"], cls
        # all 5 deep-dive levels present and DISTINCT (no restatement)
        dd = out["deep_dive"]
        assert set(dd) == _LEVELS, cls
        assert len(set(dd.values())) == 5, f"{cls}: deep-dive levels not distinct"


def test_signals_are_interpolated():
    out = deterministic_rca_fields("test_gen_bug", ["hallucinated_selector", "K2_trycatch"])
    joined = " ".join(out["deep_dive"].values())
    assert "hallucinated_selector" in joined and "K2_trycatch" in joined
    assert "{signals}" not in joined                       # placeholder consumed
    # empty signals → a graceful default, still no leftover placeholder
    out2 = deterministic_rca_fields("test_gen_bug", [])
    assert "{signals}" not in " ".join(out2["deep_dive"].values())


def test_unknown_class_and_killswitch_are_fail_open():
    assert deterministic_rca_fields("sut_regression", []) == {}   # LLM path owns this
    assert deterministic_rca_fields(None, []) == {}
    assert deterministic_rca_fields("nonsense", []) == {}


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_DETERMINISTIC_RCA_DISABLE", "1")
    assert deterministic_rca_fields("grounding_blocked", ["x"]) == {}


def test_taxonomy_covers_the_deterministic_classes():
    # the 4 classes the LLM-free branch in analyze_failures produces
    assert set(_DETERMINISTIC_RCA) == {
        "grounding_blocked", "test_gen_bug", "sut_contract_change", "operator_review"}
