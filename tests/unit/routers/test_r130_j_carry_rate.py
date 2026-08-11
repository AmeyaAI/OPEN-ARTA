"""R130.J — qwen-pro carry-rate measurement gate unit tests.

Five cases cover the operator pivot-CTA decision logic:

  1. Empty project_tests → `pivot_recommendation="no_data"` (no false alarm).
  2. 5 escalated of 10 → `carry_rate=50%`; recipe + computation correct.
  3. `pivot_recommendation="consider_upgrade"` fires when pass<50% AND
     escalation>30% (red badge surface).
  4. `pivot_recommendation="investigate"` fires when pass<70% but
     escalation<30% (amber badge).
  5. `pivot_recommendation="ok"` fires at pass≥70% (green badge,
     no operator action needed).
"""
from __future__ import annotations

from src.api.routers.projects import _r130_j_compute_carry_rate


def _make_test_row(
    *,
    gen_source: str = "llm",
    escalated: bool = False,
    failure: dict | None = None,
    grounding_blocked: bool = False,
    model: str = "arta-qwen-pro",
) -> dict:
    """Build a synthetic test row for carry-rate computation."""
    gen_metrics: dict = {"llm": {"model": model, "provider": "ollama"}}
    if escalated:
        gen_metrics["r127_c_escalated"] = True
    if grounding_blocked:
        gen_metrics["playwright_grounding_violation"] = True
    return {
        "id": f"TC-TEST-{id(gen_metrics) & 0xFFFF:04x}",
        "generation_source": gen_source,
        "generation_failure": failure,
        "_gen_metrics": gen_metrics,
    }


# ── Case 1: empty → no_data ───────────────────────────────────────────────


def test_r130j_empty_tests_returns_no_data():
    """No samples → no recommendation possible."""
    result = _r130_j_compute_carry_rate([])
    assert result["pivot_recommendation"] == "no_data"
    assert result["samples"] == 0
    assert result["carry_rate_pct"] is None


# ── Case 2: carry-rate computation accuracy ───────────────────────────────


def test_r130j_carry_rate_5_escalated_of_10_is_50pct():
    """5 escalated + 5 clean → carry_rate=50%, escalation_rate=50%."""
    rows = [_make_test_row(escalated=False) for _ in range(5)]
    rows += [_make_test_row(escalated=True) for _ in range(5)]
    result = _r130_j_compute_carry_rate(rows)
    assert result["samples"] == 10
    assert result["carry_rate_pct"] == 50.0
    assert result["escalation_rate_pct"] == 50.0


# ── Case 3: red band — consider_upgrade ───────────────────────────────────


def test_r130j_red_band_consider_upgrade_when_low_pass_high_escalation():
    """When pass<50% AND escalation>30% → red badge + 3 operator CTAs.
    Build 10 rows: 4 passing (with gen_source=llm, no failure, no
    escalation, no grounding-blocked); 4 escalated; 2 failed."""
    rows = []
    # 4 passing
    rows += [_make_test_row(gen_source="llm") for _ in range(4)]
    # 4 escalated (also counts as not-passing because escalation noise
    # is in _gen_metrics, but we keep gen_source=llm so the pass-test
    # could include them. Our test counts them as escalated AND passed.)
    # To force pass_rate<50%, mark them as failed too.
    rows += [
        _make_test_row(
            gen_source="llm",
            escalated=True,
            failure={"stage": "r127_c", "reason": "capped_batch"},
        )
        for _ in range(4)
    ]
    # 2 grounding-blocked (not-passing)
    rows += [_make_test_row(grounding_blocked=True) for _ in range(2)]
    result = _r130_j_compute_carry_rate(rows)
    assert result["pass_rate_pct"] < 50.0, f"pass_rate={result['pass_rate_pct']}"
    assert result["escalation_rate_pct"] > 30.0
    assert result["pivot_recommendation"] == "consider_upgrade"


# ── Case 4: amber band — investigate ──────────────────────────────────────


def test_r130j_amber_band_investigate_when_pass_below_70_no_escalation():
    """When pass<70% but escalation<30% → amber band (gen-quality drift
    that's not escalation-driven; operator checks R125.I gen-quality
    tile + R128.A divergence)."""
    rows = []
    # 6 passing → 60% pass rate
    rows += [_make_test_row(gen_source="llm") for _ in range(6)]
    # 4 not-passing (failed) BUT not escalated → low escalation rate
    rows += [
        _make_test_row(
            gen_source="llm",
            failure={"stage": "validation", "reason": "structural_check"},
        )
        for _ in range(4)
    ]
    result = _r130_j_compute_carry_rate(rows)
    assert result["pass_rate_pct"] < 70.0
    assert result["escalation_rate_pct"] < 30.0
    assert result["pivot_recommendation"] == "investigate"


# ── Case 5: green band — ok (qwen-pro carrying weight) ────────────────────


def test_r130j_green_band_ok_when_pass_rate_above_70():
    """When pass≥70% → ok, no operator CTA needed."""
    rows = []
    # 8 passing → 80% pass rate
    rows += [_make_test_row(gen_source="llm") for _ in range(8)]
    # 2 failed
    rows += [_make_test_row(failure={"stage": "x"}) for _ in range(2)]
    result = _r130_j_compute_carry_rate(rows)
    assert result["pass_rate_pct"] >= 70.0
    assert result["pivot_recommendation"] == "ok"
    # Ollama model surfaced from most recent row
    assert result["ollama_model"] == "arta-qwen-pro"
