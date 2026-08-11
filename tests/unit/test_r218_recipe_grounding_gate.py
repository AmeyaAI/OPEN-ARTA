"""G3 (R218) — heed the recipe verifier's grounding signal. A recipe the
closed-loop verifier flagged `verification_failed` (its `expected_outputs` aren't
in the SUT's real response shape) must mark the analytics suite gen-BLOCKED — its
generated assertions would cite INVENTED values, producing a verdict that measures
nothing real. Truthful BLOCK beats false PASS.
"""
from __future__ import annotations

from src.agents.analytics_test_agent import _recipe_is_ungrounded


def test_g3_verification_failed_is_ungrounded():
    assert _recipe_is_ungrounded({"verification_failed": True}) == "verification_failed"


def test_g3_column_not_in_sut_shape_is_ungrounded():
    r = {"grounding_warnings": [{"kind": "recipe_column_not_in_sut_shape", "symbol": "insight_metric"}]}
    assert _recipe_is_ungrounded(r) == "recipe_column_not_in_sut_shape"


def test_g3_grounded_recipe_passes():
    r = {"verification_failed": False, "expected_outputs": {"insight_metric": "x"},
         "grounding_warnings": []}
    assert _recipe_is_ungrounded(r) is None


def test_g3_none_or_malformed_is_safe():
    assert _recipe_is_ungrounded(None) is None
    assert _recipe_is_ungrounded({}) is None
    assert _recipe_is_ungrounded({"grounding_warnings": ["bad", None]}) is None
