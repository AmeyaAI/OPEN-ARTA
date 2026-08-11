"""R154.C — dispatch-time non-mutation gate (last safety net).

R154.A protects probe; R154.B protects gen; R154.C is the final
guarantee at dispatch. Even if R154.B's gen-time validator missed a
destructive pattern (e.g., due to bug-bash + opt-in marker present),
R154.C verifies BOTH:
  1. `ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1` env var set
  2. `SUT_TEST_DATA_NAMESPACE=<sandbox>` env var non-empty

before dispatching a spec carrying the `@intentional-destructive` marker.
Without all three (marker + ALLOW + NAMESPACE), R154.C BLOCKs the spec.

This file verifies the R154.C wire-up logic via source-level inspection
of execution.py (the helpers + integration are independently
test-covered in test_r154_b_destructive_patterns.py; R154.C's job is
the dispatch-time orchestration of those helpers).
"""
from __future__ import annotations

import re
from pathlib import Path

EXECUTION_PY = (
    Path(__file__).resolve().parents[3]
    / "src" / "api" / "routers" / "execution.py"
)


def _exec_source() -> str:
    assert EXECUTION_PY.is_file(), f"execution.py missing at {EXECUTION_PY}"
    return EXECUTION_PY.read_text(encoding="utf-8")


def test_r154_c_marker_present_in_dispatch_path():
    """R154.C dispatch-gate code block is present + correctly labeled.
    Regression guard against accidental removal.
    """
    src = _exec_source()
    assert "R154.C" in src
    assert "dispatch-time non-mutation gate" in src
    assert "destructive_test_blocked_default_deny" in src


def test_r154_c_imports_helpers_from_grounding_validator():
    """R154.C imports `_r154_b_extract_destructive_patterns` +
    `_r154_b_has_opt_in_marker` from grounding_validator. Single source of
    truth for destructive pattern logic.
    """
    src = _exec_source()
    # Helper imports surface in execution.py
    assert "_r154_b_extract_destructive_patterns" in src
    assert "_r154_b_has_opt_in_marker" in src


def test_r154_c_killswitch_recognized():
    """ARTA_R154_C_DISPATCH_GATE_DISABLE=1 disables the gate (reverts to
    pre-R154 behavior).
    """
    src = _exec_source()
    assert "ARTA_R154_C_DISPATCH_GATE_DISABLE" in src
    # Killswitch checked via env.get(...) == "1"
    assert re.search(
        r"ARTA_R154_C_DISPATCH_GATE_DISABLE[^\n]*==\s*['\"]1['\"]",
        src,
    ), "Killswitch MUST check for explicit '1' (not truthy)"


def test_r154_c_opt_in_requires_both_env_vars():
    """Opt-in marker alone is NOT sufficient — operator must ALSO set
    BOTH ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1 AND SUT_TEST_DATA_NAMESPACE.
    Triple-conjunction defense.
    """
    src = _exec_source()
    # Both env vars referenced
    assert "ARTA_R154_ALLOW_DESTRUCTIVE_TESTS" in src
    assert "SUT_TEST_DATA_NAMESPACE" in src
    # Predicate combines both — find the dispatch-gate decision branch
    gate_idx = src.find("R154.C: spec=%s INTENTIONAL-DESTRUCTIVE")
    assert gate_idx > 0, "Could not find R154.C INTENTIONAL-DESTRUCTIVE log line"
    # Within ~600 chars before the log, both env-var checks should appear
    gate_pre = src[max(0, gate_idx - 800):gate_idx]
    assert "_r154_c_allow" in gate_pre
    assert "_r154_c_namespace" in gate_pre


def test_r154_c_blocked_row_shape():
    """R154.C emits BLOCKED rows with the canonical metadata shape:
    `blocked_reason=destructive_test_blocked_default_deny` + per-spec
    diagnostic fields.
    """
    src = _exec_source()
    # BLOCKED row test_id pattern
    assert re.search(
        r'test_id.*PW-R154-C-',
        src,
    ), "BLOCKED row test_id MUST follow PW-R154-C-<stem> pattern"
    # Metadata diagnostic fields
    assert "r154_c_has_opt_in_marker" in src
    assert "r154_c_destructive_kinds" in src
    assert "r154_c_allow_env_set" in src
    assert "r154_c_namespace_env_set" in src


def test_r154_c_blocks_added_to_blocked_paths():
    """R154.C-blocked specs are added to `blocked_paths` so the
    downstream non_a11y_specs list excludes them — prevents accidental
    dispatch despite BLOCKED row being emitted.
    """
    src = _exec_source()
    # Within the R154.C block, blocked_paths.add(...) is called
    gate_idx = src.find("R154.C: BLOCKED spec=")
    assert gate_idx > 0
    gate_pre = src[max(0, gate_idx - 1500):gate_idx]
    assert "blocked_paths.add(str(_spec_path))" in gate_pre, (
        "R154.C-BLOCKED specs MUST be added to blocked_paths set "
        "to prevent dispatch despite BLOCKED row emission"
    )
