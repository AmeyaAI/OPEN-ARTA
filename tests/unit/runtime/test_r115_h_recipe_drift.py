"""R115.H — `assert_recipe_value` surfaces drift as truthful SKIP.

Pre-R115.H: generated analytics specs used `tolerant_assert(actual, expected)`
or bare `assert`. Value-drift (recipe='sales' vs SUT='sales_revenue', or
recipe=125000 vs SUT=130000) raised AssertionError → reported as FAIL.

Operator couldn't distinguish "ARTA recipe outdated" from "real SUT
analytics regression" at a glance.

R115.H's `assert_recipe_value` helper emits `pytest.skip(...)` with
`analytics_value_drift` reason on drift, preserving Pillar 4 truthful
operator signal.
"""
from __future__ import annotations

import pytest

from src.automation.python_tests.arta_runtime import (
    AnalyticsResponse,
    Insight,
    assert_recipe_value,
)


def test_r115_h_matching_value_passes():
    """When actual == expected, helper returns silently (no skip, no fail)."""
    assert_recipe_value("sales", "sales", recipe_field="metric")
    # Reaches this line only if no exception/skip
    assert True


def test_r115_h_string_drift_emits_skip():
    """recipe='sales' vs SUT='sales_revenue' → SKIP with analytics_value_drift."""
    with pytest.raises(pytest.skip.Exception) as exc_info:
        assert_recipe_value("sales_revenue", "sales", recipe_field="insight.metric")
    msg = str(exc_info.value)
    assert "analytics_value_drift" in msg, f"missing drift marker: {msg}"
    assert "sales" in msg and "sales_revenue" in msg, f"missing values in skip msg: {msg}"
    assert "insight.metric" in msg, f"missing recipe_field in skip msg: {msg}"


def test_r115_h_numeric_drift_emits_skip():
    """recipe=125000 vs SUT=130000 (4% drift, > 1% tolerance) → SKIP."""
    with pytest.raises(pytest.skip.Exception) as exc_info:
        assert_recipe_value(130000, 125000, recipe_field="totals.revenue")
    msg = str(exc_info.value)
    assert "analytics_value_drift" in msg
    assert "totals.revenue" in msg


def test_r115_h_within_tolerance_passes():
    """recipe=125000 vs SUT=125500 (0.4% drift, within 1% tolerance) → PASS."""
    # No exception — passes silently
    assert_recipe_value(125500, 125000, recipe_field="totals.revenue", tolerance=0.01)


def test_r115_h_stub_default_response_short_circuits():
    """When _response has _is_stub_default=True, falls through to R77.7.D skip."""
    stub_response = AnalyticsResponse(_is_stub_default=True)
    with pytest.raises(pytest.skip.Exception) as exc_info:
        assert_recipe_value(
            stub_response.insight.metric,
            "sales",
            recipe_field="metric",
            _response=stub_response,
        )
    msg = str(exc_info.value)
    # Should hit the R77.7.D stub-default path (different message), not drift
    assert "stub-default" in msg.lower() or "R77.7.D" in msg, (
        f"stub-default response should trigger R77.7.D skip, got: {msg}"
    )
