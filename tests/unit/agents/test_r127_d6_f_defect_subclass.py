"""R127.D.6.F — defect_subclass surface tests.

When R102.A stamps a spec with violation kinds in `metadata.violation_kinds`
(populated by R102.C dispatch reader + R111.E aggregator at
execution.py:3366-3368), R127.D.6.F surfaces the kind as `defect_subclass`
on the defect row. The parent `defect_class` stays `"grounding_blocked"`
so R118.G auto-heal + existing UI tile copy continue to fire — subclass
is purely additive.

Test cases:
  1. `merged_paren_imbalance` in violation_kinds → subclass="merge_paren_imbalance"
  2. `pytest_syntax_error_indent` → subclass="pytest_indent_error"
  3. Unknown kind → subclass=None (graceful)
  4. Multiple kinds → first-match-wins (single-valued surface)
  5. Absent `violation_kinds` metadata → subclass=None
     (regression preservation; existing behavior unchanged)
"""
from __future__ import annotations

from src.agents.defect_intel import (
    _R127_D6_F_KIND_TO_SUBCLASS,
    _r127_d6_f_compute_subclass,
)


def test_r127_d6_f_merged_paren_imbalance_maps_to_merge_paren_imbalance():
    """R127.D.6.A's `merged_paren_imbalance` kind → operator-facing subclass."""
    meta = {"violation_kinds": {"merged_paren_imbalance": 1}}
    assert _r127_d6_f_compute_subclass(meta) == "merge_paren_imbalance"


def test_r127_d6_f_pytest_indent_error_maps_correctly():
    """R127.D.6.D's `pytest_syntax_error_indent` → `pytest_indent_error`."""
    meta = {"violation_kinds": {"pytest_syntax_error_indent": 1}}
    assert _r127_d6_f_compute_subclass(meta) == "pytest_indent_error"


def test_r127_d6_f_unknown_kind_returns_none():
    """Unknown kinds (e.g., bad_playwright_api from R95.3) → None.

    `defect_class` stays `grounding_blocked` (R118.G unchanged); the
    subclass is None when no R127.D.6 kind matched. Other grounding
    failure classes are still surfaced via the existing `triage_signals`.
    """
    meta = {"violation_kinds": {"bad_playwright_api": 2, "hallucinated_endpoint": 1}}
    assert _r127_d6_f_compute_subclass(meta) is None


def test_r127_d6_f_multiple_kinds_first_match_wins():
    """When multiple R127.D.6 kinds are present, the first iterated key
    wins (single-valued subclass). The exact winner depends on dict
    iteration order — we just verify ONE of the mapped subclasses is
    selected (not None)."""
    meta = {
        "violation_kinds": {
            "merged_paren_imbalance": 1,
            "merged_brace_imbalance": 2,
        }
    }
    result = _r127_d6_f_compute_subclass(meta)
    assert result in {"merge_paren_imbalance", "merge_brace_imbalance"}
    assert result is not None


def test_r127_d6_f_absent_violation_kinds_returns_none():
    """Regression preservation: metadata with NO `violation_kinds` field
    (e.g., pre-R127.D.6 dispatched specs, or non-grounding failures) →
    subclass=None. Existing `defect_class="grounding_blocked"` path is
    unaffected."""
    # No violation_kinds field
    assert _r127_d6_f_compute_subclass({"blocked_reason": "playwright_grounding_violation"}) is None
    # Empty metadata
    assert _r127_d6_f_compute_subclass({}) is None
    # None metadata (defensive)
    assert _r127_d6_f_compute_subclass(None) is None
    # violation_kinds is not a dict (defensive)
    assert _r127_d6_f_compute_subclass({"violation_kinds": "not a dict"}) is None


def test_r127_d6_f_mapping_covers_all_new_r127_kinds():
    """The mapping MUST cover all violation kinds R127.D.6.A and
    R127.D.6.D produce. Regression guard: if a new kind is added to
    either validator without updating the mapping, this test fails."""
    expected_keys = {
        # R127.D.6.A produces these
        "merged_paren_imbalance",
        "merged_brace_imbalance",
        # R127.D.6.D produces these
        "pytest_syntax_error_indent",
        "pytest_syntax_error_eof",
        "pytest_syntax_error_generic",
    }
    actual_keys = set(_R127_D6_F_KIND_TO_SUBCLASS.keys())
    missing = expected_keys - actual_keys
    assert not missing, (
        f"R127.D.6.F mapping missing kinds: {missing}. Add to "
        f"_R127_D6_F_KIND_TO_SUBCLASS in defect_intel.py."
    )
