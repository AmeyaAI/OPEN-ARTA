"""R145.D — spec-level cascade-skip classification tests.

Pre-R145.D: Iter 4 (run-863889) shipped 420 PW SKIPs with empty
error_message. Investigation: spec-level cascade — when test[0]
FAILs with ERR_TIMED_OUT, Playwright's beforeEach re-runs the failing
page.goto, sibling tests cascade-skip with no error.message. R144.D's
`_r144_d_compute_skip_cascade` buckets these as `unspecified` →
dashboard reports clean state while cascade is dominant signal.

R145.D detects the pattern: SKIP + empty error_message + prior sibling
test marked unexpected (failed) in the same spec → classify as
`spec_cascade_from_prior_fail`. Priority preserved: R144.H cause-prefix,
explicit test_skip, sut_unavailable ALL win over cascade.
"""
from __future__ import annotations

from src.api.routers.execution import (
    _r144_d_compute_skip_cascade,
    _r145_d_is_spec_cascade,
)


def test_r145_d_skip_with_prior_failed_test_classifies_as_cascade():
    """R145.D KEYSTONE — empty-error SKIP after a sibling FAIL in
    same spec → classify as spec_cascade_from_prior_fail."""
    spec_tests = [
        {"results": [{"status": "unexpected"}]},   # test 0 FAILed
        {"results": [{"status": "skipped"}]},      # test 1 (current)
    ]
    assert _r145_d_is_spec_cascade(
        spec_tests, current_test_index=1,
        current_status="SKIP", current_error_msg="",
    ) is True


def test_r145_d_skip_without_prior_failure_returns_false():
    """R145.D — first test in spec is a SKIP with empty error →
    not cascade (no prior failure to cascade from)."""
    spec_tests = [{"results": [{"status": "skipped"}]}]
    assert _r145_d_is_spec_cascade(
        spec_tests, current_test_index=0,
        current_status="SKIP", current_error_msg="",
    ) is False


def test_r145_d_skip_with_explicit_error_msg_returns_false():
    """R145.D — populated error_message means R123.D / R144.H
    heuristics will classify; cascade only fires on EMPTY error_msg."""
    spec_tests = [
        {"results": [{"status": "unexpected"}]},
        {"results": [{"status": "skipped"}]},
    ]
    assert _r145_d_is_spec_cascade(
        spec_tests, current_test_index=1,
        current_status="SKIP", current_error_msg="some explicit reason",
    ) is False


def test_r145_d_fail_status_never_cascade():
    """R145.D — cascade detector ONLY fires on SKIP; FAIL goes to
    other classifier paths."""
    spec_tests = [
        {"results": [{"status": "unexpected"}]},
        {"results": [{"status": "unexpected"}]},
    ]
    assert _r145_d_is_spec_cascade(
        spec_tests, current_test_index=1,
        current_status="FAIL", current_error_msg="",
    ) is False


def test_r145_d_aggregator_buckets_cascade_separately():
    """R145.D — `_r144_d_compute_skip_cascade` now reports
    `cascade_skips` + `cascade_ratio` + `top_cascade_specs` alongside
    the original R144.D auth-stale fields."""
    results = [
        {"automation_tool": "playwright", "status": "SKIP",
         "test_id": "TC-AM-005.req_am_005.spec.ts:test_b",
         "title": "test_b",
         "metadata": {"skip_reason": "spec_cascade_from_prior_fail"}},
        {"automation_tool": "playwright", "status": "SKIP",
         "test_id": "TC-AM-005.req_am_005.spec.ts:test_c",
         "title": "test_c",
         "metadata": {"skip_reason": "spec_cascade_from_prior_fail"}},
        {"automation_tool": "playwright", "status": "SKIP",
         "test_id": "TC-AM-005.req_am_005.spec.ts:test_d",
         "title": "test_d",
         "metadata": {"skip_reason": "spec_cascade_from_prior_fail"}},
        {"automation_tool": "playwright", "status": "SKIP",
         "test_id": "TC-AM-006.req_am_006.spec.ts:test_a",
         "title": "test_a",
         "metadata": {"skip_reason": "auth_stale_url_redirect"}},
        {"automation_tool": "playwright", "status": "PASS",
         "metadata": {}},
    ]
    summary = _r144_d_compute_skip_cascade(results, pw_total=5)
    assert summary["cascade_skips"] == 3
    assert summary["cascade_ratio"] == 0.6
    assert summary["auth_stale_skips"] == 1
    assert summary["ratio"] == 0.2
    # top_cascade_specs sorted desc by count — TC-AM-005 has 3 cascade
    # rows; should rank first
    assert summary["top_cascade_specs"][0]["count"] == 3
    # skip_by_cause buckets BOTH categories
    assert summary["skip_by_cause"]["spec_cascade_from_prior_fail"] == 3
    assert summary["skip_by_cause"]["auth_stale_url_redirect"] == 1


def test_r145_d_backward_compat_with_r144_d_fields():
    """R145.D — R144.D's pre-existing field set (ratio,
    auth_stale_skips, skip_by_cause) preserved exactly so the existing
    dashboard tile + tests do not regress."""
    summary = _r144_d_compute_skip_cascade([], pw_total=0)
    # Required R144.D fields still present
    assert "ratio" in summary
    assert "auth_stale_skips" in summary
    assert "skip_by_cause" in summary
    # New R145.D fields additive
    assert "cascade_skips" in summary
    assert "cascade_ratio" in summary
    assert "top_cascade_specs" in summary
    # Empty input → zeros and empty containers, not exception
    assert summary["ratio"] == 0.0
    assert summary["cascade_skips"] == 0
    assert summary["top_cascade_specs"] == []
