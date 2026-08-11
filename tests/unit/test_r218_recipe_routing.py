"""C3 (R218) — a PERFORMANCE/latency-only quantitative AC must NOT route a
requirement to the analytics-recipe path (which would emit a RECIPE_INVALID
"# No scenarios" stub). Latency is k6's domain, not a dataset. Genuine analytics
requirements (with analytics-domain signal) still route.
"""
from __future__ import annotations

from src.agents.atdd_designer import _requires_data_fixtures


def _req(*ac_texts):
    return {"acceptance_criteria": [{"statement": t} for t in ac_texts]}


def test_c3_perf_only_latency_not_recipe():
    # "p95 response time < 800ms" — perf, no analytics signal → NOT recipe.
    r = _req("The API p95 response time must be under 800ms for the login flow.")
    assert _requires_data_fixtures(r) is False


def test_c3_throughput_only_not_recipe():
    r = _req("The endpoint must sustain 500 requests per second (rps) under load.")
    assert _requires_data_fixtures(r) is False


def test_c3_genuine_analytics_still_routes():
    # Has an analytics-domain signal (metric/magnitude) → still needs a recipe.
    r = _req("The revenue metric must show a magnitude increase of 12% vs last quarter.")
    assert _requires_data_fixtures(r) is True


def test_c3_data_count_threshold_still_routes():
    # A data-value threshold (percentage of records), no perf token → recipe.
    r = _req("At least 95% of dataset records must have a non-null insight value.")
    assert _requires_data_fixtures(r) is True


def test_c3_perf_killswitch_restores_recipe_routing(monkeypatch):
    monkeypatch.setenv("ARTA_C3_PERF_NOT_RECIPE_DISABLE", "1")
    r = _req("The API p95 response time must be under 800ms.")
    assert _requires_data_fixtures(r) is True  # legacy behavior


def test_c3_pure_flow_ac_not_recipe():
    r = _req("User clicks login then sees the dashboard.")
    assert _requires_data_fixtures(r) is False
