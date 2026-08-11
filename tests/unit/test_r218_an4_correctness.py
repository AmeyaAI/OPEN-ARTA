"""AN4 (R218) — analytics CORRECTNESS on controlled data. The SUT analyzed data we
GENERATED, so we know the true insight properties (the recipe's computed
expected_outputs). Verify the SUT's answer PROPERTY-by-PROPERTY in the insight-engine
idiom: direction exact, magnitude within tolerance, metric/category tolerant.
"""
from __future__ import annotations

import pytest

from src.automation.python_tests.arta_runtime import (
    AnalyticsResponse, Insight,
    verify_insight_properties, assert_analytics_correct,
)

# Computed ground-truth (what AN1/recipe-verifier derived from the generated data).
_EXPECTED = {
    "insight.direction": "up",
    "insight.magnitude_pct": 12.5,
    "insight.metric": "revenue",
    "insight.region.most_common": "north",
}


def _resp(direction="up", magnitude_pct=12.5, metric="revenue", region_mode="north", **kw):
    ins = Insight(direction=direction, magnitude_pct=magnitude_pct, metric=metric)
    setattr(ins, "most_common", region_mode)
    return AnalyticsResponse(refused=False, answer="Revenue rose ~12% in the north.",
                             insight=ins, **kw)


def test_an4_all_properties_correct():
    res = verify_insight_properties(_resp(), _EXPECTED)
    assert res["total"] == 4 and res["correct"] == 4 and res["accuracy"] == 1.0


def test_an4_magnitude_within_tolerance():
    # SUT says 13.0 vs computed 12.5 → within 15% tolerance → correct.
    res = verify_insight_properties(_resp(magnitude_pct=13.0), _EXPECTED)
    assert res["accuracy"] == 1.0


def test_an4_wrong_direction_counted():
    res = verify_insight_properties(_resp(direction="down", magnitude_pct=12.5), _EXPECTED)
    # direction wrong; but narrative "rose" mentions up... use a non-leaking answer:
    r = _resp(direction="down")
    r.answer = "metric changed."
    res = verify_insight_properties(r, _EXPECTED)
    assert res["correct"] < res["total"]
    assert any(m["property"] == "insight.direction" for m in res["mismatches"])


def test_an4_assert_passes_above_floor(monkeypatch):
    monkeypatch.setenv("ARTA_AN_ACCURACY_FLOOR", "0.7")
    out = assert_analytics_correct(_resp(), _EXPECTED, "how did revenue trend?")
    assert out["accuracy"] >= 0.7


def test_an4_assert_fails_below_floor(monkeypatch):
    monkeypatch.setenv("ARTA_AN_ACCURACY_FLOOR", "0.9")
    bad = _resp(direction="down", metric="cost")
    bad.answer = "unrelated."
    with pytest.raises(AssertionError, match="CORRECTNESS"):
        assert_analytics_correct(bad, _EXPECTED, "q")


def test_an4_skips_on_stub():
    with pytest.raises(pytest.skip.Exception):
        assert_analytics_correct(AnalyticsResponse(_is_stub_default=True), _EXPECTED, "q")


def test_an4_fails_on_backend_error():
    err = AnalyticsResponse(refused=False, answer="ARTA error", _is_error=True)
    with pytest.raises(AssertionError, match="ERRORED"):
        assert_analytics_correct(err, _EXPECTED, "q")


def test_an4_multivalue_answer_text_all_properties_match():
    """R218.AM.7 — a COMBINED question ('how many rows AND total amount?') yields ONE
    verbose answer containing several ground-truth numbers. Each numeric property (no
    structured insight.<prop>) must match against ANY number in the answer, not just the
    first. The old first-number-only fallback failed every property but the first — a false
    correctness FAIL on a CORRECT SUT answer. LIVE-GROUNDED: excel count==42 + sum==6321."""
    ans = ("### Executive Summary\n\nAfter reviewing the top 5 rows, the dataset contains a "
           "total of 42 records, and the sum of the amount column is 6,321.")
    r = AnalyticsResponse(refused=False, answer=ans, insight=Insight())
    res = verify_insight_properties(r, {"row_count": 42, "amount_sum": 6321})
    assert res["total"] == 2 and res["correct"] == 2 and res["accuracy"] == 1.0, res
    # first number is 5 ("top 5 rows") — must NOT be mistaken for row_count
    r2 = AnalyticsResponse(refused=False, answer="Top 5 rows. 42 rows, total 6321.",
                           insight=Insight())
    assert verify_insight_properties(r2, {"row_count": 42, "amount_sum": 6321})["accuracy"] == 1.0
    # a genuinely wrong answer still fails (no false PASS)
    r3 = AnalyticsResponse(refused=False, answer="The dataset has 999 rows, sum 12345.",
                           insight=Insight())
    assert verify_insight_properties(r3, {"row_count": 42, "amount_sum": 6321})["accuracy"] == 0.0
