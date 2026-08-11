"""R259.F — the improvement loop's exit criterion is noise-aware.

Pre-R259.F the loop exited on TARGET_RAW_PCT alone, so a tool could sit at 95%
raw with every residual failure caused by ARTA's own fabricated ids and be
declared done — loop satisfied, mission (report SUT quality) unmet. Triage
existed in the module but only ROUTED; it never SCORED.
"""
from src.api.services.improvement_loop import (
    TARGET_NOISE_RATIO,
    _per_tool_fidelity,
)


def _fail(tool, tid):
    return {"automation_tool": tool, "status": "FAIL", "test_id": tid}


def test_r259_f_arta_noise_dominates():
    results = [_fail("newman", f"t{i}") for i in range(10)]
    index = {f"t{i}": {"triage_category": "test_gen_bug"} for i in range(10)}
    out = _per_tool_fidelity(results, index)
    assert out["newman"]["arta_attributed"] == 10
    assert out["newman"]["sut_signal"] == 0
    assert out["newman"]["noise_signal_ratio"] == 10.0
    assert out["newman"]["noise_signal_ratio"] > TARGET_NOISE_RATIO


def test_r259_f_healthy_tool_is_mostly_sut_signal():
    results = [_fail("newman", f"t{i}") for i in range(11)]
    index = {f"t{i}": {"triage_category": "sut_regression"} for i in range(10)}
    index["t10"] = {"triage_category": "test_gen_bug"}
    out = _per_tool_fidelity(results, index)
    assert out["newman"]["sut_signal"] == 10
    assert out["newman"]["arta_attributed"] == 1
    assert out["newman"]["noise_signal_ratio"] == 0.1
    assert out["newman"]["noise_signal_ratio"] <= TARGET_NOISE_RATIO


def test_r259_f_untriaged_failures_are_unknown_not_signal():
    """An unclassified failure must never be credited as SUT signal — that is
    exactly how 542 PW failures would have inflated the SUT's bug count."""
    results = [_fail("playwright", f"t{i}") for i in range(5)]
    out = _per_tool_fidelity(results, {})
    assert out["playwright"]["unknown"] == 5
    assert out["playwright"]["sut_signal"] == 0
    assert out["playwright"]["arta_attributed"] == 0


def test_r259_f_ignores_pass_blocked_and_skip():
    """Fidelity is about FAILURES; BLOCKED is a deliberate non-run (R173)."""
    results = [
        {"automation_tool": "newman", "status": "PASS", "test_id": "p"},
        {"automation_tool": "newman", "status": "BLOCKED", "test_id": "b"},
        {"automation_tool": "newman", "status": "SKIP", "test_id": "s"},
        _fail("newman", "f"),
    ]
    index = {"f": {"triage_category": "test_gen_bug"}}
    out = _per_tool_fidelity(results, index)
    assert out["newman"]["arta_attributed"] == 1
    assert out["newman"]["unknown"] == 0


def test_r259_f_grounding_blocked_is_arta_noise():
    results = [_fail("playwright", "t0")]
    index = {"t0": {"triage_category": "grounding_blocked"}}
    out = _per_tool_fidelity(results, index)
    assert out["playwright"]["arta_attributed"] == 1


def test_r259_f_per_tool_isolation():
    """Newman's noise must not mask Playwright's, or vice versa."""
    results = [_fail("newman", "n0"), _fail("playwright", "p0")]
    index = {
        "n0": {"triage_category": "sut_regression"},
        "p0": {"triage_category": "test_gen_bug"},
    }
    out = _per_tool_fidelity(results, index)
    assert out["newman"]["sut_signal"] == 1
    assert out["newman"]["noise_signal_ratio"] == 0.0
    assert out["playwright"]["arta_attributed"] == 1
    assert out["playwright"]["noise_signal_ratio"] == 1.0


def test_r259_f_empty_results():
    assert _per_tool_fidelity([], {}) == {}
