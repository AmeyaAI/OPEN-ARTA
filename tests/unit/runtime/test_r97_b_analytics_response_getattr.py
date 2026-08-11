"""R97.B regression tests for `AnalyticsResponse.__getattr__`.

Pre-R97.B run-a1f111 had 8 × `AttributeError: 'AnalyticsResponse'
object has no attribute X` for X ∈ {narrative, generated_sql,
query_refinement, insight_metric, to_dict, get}. R95.5 patched the
sibling `Insight` class only — tests accessing AnalyticsResponse
attrs directly bypassed R95.5 entirely.

R97.B adds the SAME `__getattr__` to AnalyticsResponse so any future
LLM-invented attribute returns None instead of raising.

These tests mirror test_r95_5_insight_getattr.py case-by-case for
contract consistency.
"""
from __future__ import annotations

import pytest


def _get_resp():
    from src.automation.python_tests.arta_runtime import AnalyticsResponse
    return AnalyticsResponse()


# ── Known fields preserved ───────────────────────────────────────────


def test_known_field_refused_returns_dataclass_default():
    resp = _get_resp()
    assert resp.refused is True


def test_known_field_confidence_returns_dataclass_default():
    resp = _get_resp()
    assert resp.confidence == 0.0


def test_known_field_insight_returns_nested_insight():
    """__post_init__ replaces None insight with Insight() instance."""
    resp = _get_resp()
    # Should be an Insight, not None (per __post_init__)
    from src.automation.python_tests.arta_runtime import Insight
    assert isinstance(resp.insight, Insight)


# ── Unknown attributes return None (the exact run-a1f111 failures) ───


def test_unknown_attribute_narrative_returns_none():
    """The exact failure: `AttributeError: 'AnalyticsResponse' object
    has no attribute 'narrative'`. Post-R97.B: None."""
    resp = _get_resp()
    assert resp.narrative is None


def test_unknown_attribute_generated_sql_returns_none():
    resp = _get_resp()
    assert resp.generated_sql is None


def test_unknown_attribute_query_refinement_returns_none():
    resp = _get_resp()
    assert resp.query_refinement is None


def test_unknown_attribute_insight_metric_returns_none():
    """LLM emits insight_metric on the response (not response.insight.metric).
    Forward-compat: returns None."""
    resp = _get_resp()
    assert resp.insight_metric is None


def test_arbitrary_future_attribute_returns_none():
    """Forward-compat: any LLM-invented attribute returns None."""
    resp = _get_resp()
    assert resp.totally_new_field_2026 is None


# ── Dunder + private guard ───────────────────────────────────────────


def test_dunder_attribute_still_raises_attributeerror():
    """Pickle/copy stdlib relies on AttributeError to detect dunders'
    absence. R97.B must NOT intercept these."""
    resp = _get_resp()
    with pytest.raises(AttributeError):
        _ = resp.__deepcopy__


def test_private_underscore_attr_still_raises():
    """Underscore-prefixed names raise — guards against impl-detail
    collisions."""
    resp = _get_resp()
    with pytest.raises(AttributeError):
        _ = resp._private_thing


def test_dataclass_ness_preserved():
    """AnalyticsResponse remains a dataclass post-R97.B."""
    from dataclasses import is_dataclass
    from src.automation.python_tests.arta_runtime import AnalyticsResponse
    assert is_dataclass(AnalyticsResponse)
