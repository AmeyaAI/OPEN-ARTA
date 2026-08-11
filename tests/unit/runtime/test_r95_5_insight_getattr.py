"""R95.5 regression tests for `Insight.__getattr__` forward-compatibility.

Pre-R95.5 the Insight dataclass had ~12 well-known fields. LLM-generated
analytics tests reference 69+ distinct attribute names (join_type,
retry_count, plotly_axes, aggregation_pipeline_generated, etc.).
Result: 27 × AttributeError in run-2f077d, all of the shape
`'Insight' object has no attribute 'X'`.

R95.5 adds `__getattr__` to return None for any unknown attribute,
so generated tests run to their `tolerant_assert` call where R77.7.D's
stub-default short-circuit fires → 27 FAILs convert to 27 SKIPs
(category `stub_default`).

These tests lock the contract.
"""
from __future__ import annotations

import pytest


def _get_insight():
    """Import-time fresh Insight instance."""
    from src.automation.python_tests.arta_runtime import Insight
    return Insight()


# ── Known fields preserved ───────────────────────────────────────────


def test_known_field_value_returns_typed_default():
    """`value` is a known dataclass field with default None."""
    insight = _get_insight()
    assert insight.value is None


def test_known_field_metric_returns_typed_default():
    insight = _get_insight()
    assert insight.metric is None


def test_known_field_parsed_claims_returns_default_list():
    """parsed_claims default = []; __post_init__ replaces None with []."""
    insight = _get_insight()
    assert insight.parsed_claims == []


# ── Unknown attributes return None ───────────────────────────────────


def test_unknown_attribute_join_type_returns_none():
    """The exact failure pattern from run-2f077d:
    `AttributeError: 'Insight' object has no attribute 'join_type'`
    Post-R95.5: returns None instead of raising."""
    insight = _get_insight()
    assert insight.join_type is None


def test_unknown_attribute_retry_count_returns_none():
    insight = _get_insight()
    assert insight.retry_count is None


def test_unknown_attribute_aggregation_pipeline_generated_returns_none():
    """One of the longer attribute names the LLM emits."""
    insight = _get_insight()
    assert insight.aggregation_pipeline_generated is None


def test_arbitrary_future_attribute_returns_none():
    """Forward-compatibility: any future LLM-invented attribute
    returns None without code changes."""
    insight = _get_insight()
    assert insight.totally_new_attribute_2026 is None
    assert insight.another_one is None


# ── Dunder + private guard ───────────────────────────────────────────


def test_dunder_method_still_raises_attributeerror():
    """Python's pickle/copy machinery uses AttributeError to detect
    `__deepcopy__` / `__reduce__` etc. R95.5 must NOT intercept these,
    or it'll break stdlib introspection."""
    insight = _get_insight()
    with pytest.raises(AttributeError):
        _ = insight.__deepcopy__


def test_private_underscore_attr_still_raises():
    """Underscore-prefixed names (private convention) raise — guards
    against implementation-detail collisions."""
    insight = _get_insight()
    with pytest.raises(AttributeError):
        _ = insight._private_thing


def test_dataclass_is_still_dataclass():
    """Verify Insight remains a dataclass — instances support
    __post_init__ + field-typed defaults."""
    from dataclasses import is_dataclass
    from src.automation.python_tests.arta_runtime import Insight
    assert is_dataclass(Insight)
