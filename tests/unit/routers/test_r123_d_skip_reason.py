"""R123.D regression tests for PW + Newman `metadata.skip_reason` wiring.

Pre-R123.D evidence (run-b5e3e4):
  - 6 PW SKIP rows had EMPTY `metadata.skip_reason` — dashboard tile
    couldn't distinguish auth-stale skips from framework-limit skips
    from intentional test.skip() calls.
  - Newman SKIP rows had `error_message` text but no `metadata.skip_reason`
    field. R114.F.2's pytest pattern uses metadata; PW + Newman did not.

R123.D extends the pattern to PW (via `_parse_playwright_json`) and
Newman (via the spec-drift / unresolved-path-param branch).
"""
from __future__ import annotations

from src.api.routers.execution import _parse_playwright_json


def _pw_report(test_status: str, title: str = "test foo", error_message: str = ""):
    """Build a minimal Playwright JSON reporter shape with one test."""
    return {
        "suites": [
            {
                "specs": [
                    {
                        "title": title,
                        "file": "req_am_002.spec.ts",
                        "tests": [
                            {
                                "title": title,
                                "status": test_status,
                                "results": [
                                    {
                                        "duration": 0,
                                        "error": {"message": error_message},
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "suites": [],
            }
        ]
    }


def test_r123_d_pw_skip_with_auth_stale_error_tagged_correctly():
    """When the PW test error mentions auth stale OR redirected to /login,
    skip_reason must be `auth_stale_redirect`."""
    report = _pw_report(
        "skipped",
        title="Login flow",
        error_message="Test skipped — auth state stale; page redirected to /login",
    )
    results = _parse_playwright_json(report, run_id="run-test-1", build_id="b1")
    assert len(results) == 1
    assert results[0]["status"] == "SKIP"
    assert results[0]["metadata"]["skip_reason"] == "auth_stale_redirect"


def test_r123_d_pw_skip_with_framework_implicit_skip():
    """A test marked 'skipped' by Playwright with no specific error →
    skip_reason defaults to `framework_limit_or_implicit`."""
    report = _pw_report("skipped", title="test xyz", error_message="")
    results = _parse_playwright_json(report, run_id="run-test-2", build_id="b1")
    assert results[0]["metadata"]["skip_reason"] == "framework_limit_or_implicit"


def test_r123_d_pw_skip_with_explicit_test_skip():
    """test.skip(true, '...') marked skip → skip_reason = explicit_test_skip."""
    report = _pw_report(
        "skipped",
        title="test foo",
        error_message="test.skip called — operator marked fixme",
    )
    results = _parse_playwright_json(report, run_id="run-test-3", build_id="b1")
    assert results[0]["metadata"]["skip_reason"] == "explicit_test_skip"


def test_r123_d_pw_pass_row_has_no_skip_reason():
    """PASS rows must NOT carry skip_reason — only SKIP rows do."""
    report = _pw_report("expected", title="test foo", error_message="")
    results = _parse_playwright_json(report, run_id="run-test-4", build_id="b1")
    assert results[0]["status"] == "PASS"
    # metadata may exist as empty dict, but no skip_reason key
    md = results[0].get("metadata", {})
    assert "skip_reason" not in md


def test_r123_d_pw_fail_row_has_no_skip_reason():
    """FAIL rows must NOT carry skip_reason."""
    report = _pw_report("unexpected", title="test foo", error_message="assertion failed")
    results = _parse_playwright_json(report, run_id="run-test-5", build_id="b1")
    assert results[0]["status"] == "FAIL"
    md = results[0].get("metadata", {})
    assert "skip_reason" not in md
