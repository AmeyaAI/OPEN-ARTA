"""R126.T — partial-failure semantics for chunked Ollama PW gen.

When R126.C calls the LLM once per test() body, some calls may fail
(rate-limit, truncation, timeout). Without a deterministic policy, we'd
have to decide: (a) drop the whole req on any failure (wasteful — loses
the working test bodies) OR (b) ship a malformed spec (regresses Pillar 2
execute-flawlessly).

R126.T's deterministic threshold rule:
  0 failures           → 'clean'   (ship spec with all bodies filled)
  1-49% failures       → 'partial' (ship with skip placeholders + WARN)
  ≥50% failures        → 'fatal'   (R102.A-stamp the whole spec)

Combined with R126.B's spec-still-parses contract (skip placeholders are
valid TS), partial-success specs surface truthfully at dispatch:
- successful bodies → PASS/FAIL signal (real)
- failed bodies     → SKIP rows with operator-actionable reason
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent


def test_r126t_clean_when_zero_failures():
    assert AutomationEngineerAgent._r126_t_classify_failure_ratio(0, 8) == "clean"
    assert AutomationEngineerAgent._r126_t_classify_failure_ratio(0, 1) == "clean"


def test_r126t_partial_in_safe_range():
    """1-49% failures should be classified 'partial' (ship with placeholders)."""
    # 1/8 = 12.5%
    assert AutomationEngineerAgent._r126_t_classify_failure_ratio(1, 8) == "partial"
    # 3/8 = 37.5%
    assert AutomationEngineerAgent._r126_t_classify_failure_ratio(3, 8) == "partial"
    # 1/3 = 33.3%
    assert AutomationEngineerAgent._r126_t_classify_failure_ratio(1, 3) == "partial"


def test_r126t_fatal_at_or_above_threshold():
    """50%+ failures must be classified 'fatal' (whole-spec stamp)."""
    # 4/8 = 50% (boundary — strict ≥ comparison)
    assert AutomationEngineerAgent._r126_t_classify_failure_ratio(4, 8) == "fatal"
    # 5/8 = 62.5%
    assert AutomationEngineerAgent._r126_t_classify_failure_ratio(5, 8) == "fatal"
    # 8/8 = 100%
    assert AutomationEngineerAgent._r126_t_classify_failure_ratio(8, 8) == "fatal"


def test_r126t_threshold_is_50pct():
    """Threshold constant must be 0.50 — operator-documented contract."""
    assert AutomationEngineerAgent._R126_T_FAILURE_THRESHOLD == 0.50


def test_r126t_empty_total_returns_clean():
    """Edge: 0/0 LLM_FILL calls → vacuously clean (no spec to fail)."""
    assert AutomationEngineerAgent._r126_t_classify_failure_ratio(0, 0) == "clean"


def test_r126t_skip_placeholder_is_valid_ts():
    """The placeholder body emitted on failure must parse as valid TypeScript
    (test.skip is a Playwright API). The spec containing it should be
    parseable at dispatch."""
    body = AutomationEngineerAgent._r126_t_make_skip_placeholder(
        "AC-007", "rate_limit_exceeded",
    )
    assert "test.skip(true," in body
    assert "AC-007" in body
    assert "rate_limit_exceeded" in body
    # No quotes inside the embedded reason that would break the string literal
    body_quoted_test = AutomationEngineerAgent._r126_t_make_skip_placeholder(
        "AC-099", "operator's broken context",
    )
    # Single quote in reason should be backslash-escaped
    assert "\\'" in body_quoted_test or "'operator" not in body_quoted_test.split("test.skip")[1]


def test_r126t_skip_placeholder_truncates_long_reasons():
    """Placeholder must cap reason text to keep the spec readable."""
    long_reason = "x" * 500
    body = AutomationEngineerAgent._r126_t_make_skip_placeholder("AC-001", long_reason)
    # The embedded reason should be ≤120 chars (R126.T cap)
    skip_line = [ln for ln in body.splitlines() if "test.skip" in ln][0]
    # Count the x's in the skip line; should be ≤120
    x_count = skip_line.count("x")
    assert x_count <= 120, f"reason should be truncated to 120 chars; got {x_count} x's"
