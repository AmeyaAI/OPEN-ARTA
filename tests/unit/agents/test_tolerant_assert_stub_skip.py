"""R77.7.D — regression tests for tolerant_assert stub-default short-circuit.

When the analytics_client returns a stub-default AnalyticsResponse
(no real backend wired), generated pytest specs assert against fields
that are None. Pre-R77.7.D those assertions FAILED honestly — but
operators saw N "real failures" that were actually just "no real
backend configured". R77.7.D converts these to SKIPs:
- Operators see N skipped (stub_default) instead of N failures
- Pass-rate denominator excludes them (R42.5 zero_skips gate has its
  own counter for this category)
- Configuring ARTA_ANALYTICS_BACKEND or set_analytics_client() in
  conftest converts SKIPs back to real PASS/FAIL signal.

The short-circuit operates via TWO mechanisms:
1. Explicit `_response=...` kwarg (preferred for new tests)
2. Caller-frame introspection (transparent for existing 224 specs)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Same sys.path setup as the existing R75.3 test.
_RUNTIME_DIR = Path(__file__).resolve().parents[3] / "src" / "automation" / "python_tests"
if str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

from arta_runtime import AnalyticsResponse, Insight, tolerant_assert  # noqa: E402


# ── Explicit _response parameter (R77.7.D opt-in path) ───────────────

def test_skips_when_explicit_stub_response_passed():
    """Explicit `_response=stub` → pytest.skip."""
    stub = AnalyticsResponse(_is_stub_default=True, insight=Insight())
    with pytest.raises(pytest.skip.Exception) as exc_info:
        tolerant_assert(stub.insight.value, 125000, _response=stub)
    assert "stub-default" in str(exc_info.value).lower()


def test_does_not_skip_when_explicit_real_response_passed():
    """Real (non-stub) response → assertion runs normally."""
    real = AnalyticsResponse(_is_stub_default=False, insight=Insight(value=125000.0))
    # Real backend produced 125000; assertion passes.
    tolerant_assert(real.insight.value, 125000, _response=real)


def test_real_response_assertion_failure_surfaces():
    """When the real response has the WRONG value, the assertion still
    fails (R77.7.D doesn't mask real test failures)."""
    real = AnalyticsResponse(_is_stub_default=False, insight=Insight(value=999.0))
    with pytest.raises(AssertionError):
        tolerant_assert(real.insight.value, 125000, _response=real)


# ── Caller-frame introspection (R77.7.D legacy-spec path) ────────────

def test_skips_when_stub_response_in_caller_locals():
    """A generated pytest spec calls tolerant_assert WITHOUT _response=
    but the test function has a local AnalyticsResponse var. The helper
    walks the frame + detects + skips."""
    response = AnalyticsResponse(_is_stub_default=True, insight=Insight())
    with pytest.raises(pytest.skip.Exception) as exc_info:
        # No _response= kwarg — relies on frame introspection.
        tolerant_assert(response.insight.value, 125000)
    assert "stub-default" in str(exc_info.value).lower()


def test_does_not_skip_when_caller_has_only_real_response():
    """Caller has a non-stub response in scope → assertion runs."""
    response = AnalyticsResponse(_is_stub_default=False, insight=Insight(value=42.0))
    tolerant_assert(response.insight.value, 42)


def test_no_response_in_scope_runs_normally():
    """When no AnalyticsResponse exists in the caller's frame, the
    helper falls through to the normal assertion path. Verifies the
    frame walk doesn't false-positive on unrelated tests."""
    # No `response` variable in scope. Plain numeric comparison runs.
    tolerant_assert(42, 42)


def test_unrelated_object_with_is_stub_default_not_picked_up():
    """An arbitrary object that happens to have ``_is_stub_default``
    should NOT trigger skip — only AnalyticsResponse instances do.
    Type-name filter prevents false positives."""

    class Decoy:
        _is_stub_default = True

    decoy = Decoy()   # noqa: F841 — intentionally in scope
    # Should run normally — Decoy is not AnalyticsResponse.
    tolerant_assert(42, 42)


# ── Both paths agree ───────────────────────────────────────────────

def test_explicit_param_takes_precedence_over_frame_walk():
    """When _response= explicitly passes a REAL response but the frame
    ALSO contains a stub response, the explicit param wins."""
    stub_in_frame = AnalyticsResponse(_is_stub_default=True, insight=Insight())   # noqa: F841
    real = AnalyticsResponse(_is_stub_default=False, insight=Insight(value=99.0))
    # Explicit real param → assertion runs (no skip).
    tolerant_assert(real.insight.value, 99, _response=real)
