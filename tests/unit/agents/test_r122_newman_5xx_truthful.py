"""R122 regression test for Newman-error false-positive trap.

Pre-R122: R111.H's malformed_patterns list included `"expected "`
(with trailing space). Newman's universal error wrapper always begins
with `"expected response to have status code 200 but got 500"`, so
EVERY Newman 5xx matched as malformed_body_cascade → routed to
test_gen_bug instead of sut_regression.

Live evidence (run-af070d): 2188 × HTTP 500 Newman FAILs → 0 sut_regression
defects produced. Pillar 4 truthfulness gap.

R122 narrows the malformed-body patterns to specific schema-validation
phrases that DON'T appear in Newman's generic error format.
"""
from __future__ import annotations

from src.agents.defect_intel import DefectIntelAgent


def test_r122_newman_500_with_internal_error_routes_to_sut_regression():
    """Newman's canonical 500-FAIL error message:
        `expected response to have status code 200 but got 500 |
         body: {"message": "Internal Server Error"}`
    must route to sut_regression (real SUT 5xx). Pre-R122 it routed to
    test_gen_bug because `"expected "` matched the wrapper."""
    failure = {
        "test_id": "API-req_am_001-AUTO001",
        "automation_tool": "newman",
        "status": "FAIL",
        "status_code": 500,
        "error_message": (
            'expected response to have status code 200 but got 500 | '
            'body: {"message": "Internal Server Error"}'
        ),
    }
    triage = DefectIntelAgent._triage_failure(failure)
    assert triage["triage_category"] == "sut_regression", (
        f"Expected sut_regression for real SUT 500; got "
        f"{triage['triage_category']}\nsignals: {triage.get('triage_signals')}"
    )
    # Confidence should be high (R34.1 default 0.90)
    assert triage["triage_confidence"] >= 0.80


def test_r122_genuine_malformed_body_still_routes_to_test_gen_bug():
    """A SUT response that genuinely indicates malformed body content
    (e.g., 'missing required field X') must still route to test_gen_bug
    via R111.H Layer 1A.2 — the narrowing didn't break the legitimate
    cascade-detection case."""
    failure = {
        "test_id": "API-req_am_001-AUTO001",
        "automation_tool": "newman",
        "status": "FAIL",
        "status_code": 500,
        "error_message": (
            'expected response to have status code 200 but got 500 | '
            'body: {"detail": "missing required field: email"}'
        ),
    }
    triage = DefectIntelAgent._triage_failure(failure)
    assert triage["triage_category"] == "test_gen_bug", (
        "Genuine malformed_body cascade must still route to test_gen_bug"
    )


def test_r122_auth_cascade_5xx_unchanged():
    """R113.B auth_cascade_5xx pattern detection must still fire for
    Internal authorization error bodies — narrowing the malformed
    patterns didn't affect the auth path."""
    failure = {
        "test_id": "API-req_am_001-AUTO001",
        "automation_tool": "newman",
        "status": "FAIL",
        "status_code": 500,
        "error_message": (
            'expected response to have status code 200 but got 500 | '
            'body: {"message": "Internal authorization error"}'
        ),
    }
    triage = DefectIntelAgent._triage_failure(failure)
    assert triage["triage_category"] == "operator_review", (
        "auth_cascade_5xx must still classify as operator_review"
    )
    assert any("auth_cascade_5xx" in s for s in triage.get("triage_signals", []))


def test_r122_html_response_body_500_routes_to_sut_regression():
    """A SUT that responds with HTML on a JSON-expected endpoint
    (e.g., login page redirect that the test interpreted as 500) —
    no malformed-body phrase, no auth phrase → default to sut_regression."""
    failure = {
        "test_id": "API-req_am_002-AUTO001",
        "automation_tool": "newman",
        "status": "FAIL",
        "status_code": 500,
        "error_message": (
            'expected response to have status code 200 but got 500 | '
            'body: <!doctype html><html lang=en><title>Error</title>'
        ),
    }
    triage = DefectIntelAgent._triage_failure(failure)
    assert triage["triage_category"] == "sut_regression", (
        "HTML response on 500 must default to sut_regression"
    )
