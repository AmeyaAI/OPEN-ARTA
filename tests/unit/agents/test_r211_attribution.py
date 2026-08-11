"""R211 Phase G — defect attribution from the traceability spine.

The unified strategy's payoff: every pass/fail becomes a trustworthy, attributable
statement about the SUT, so the Pillar-4 verdict is credible.
"""
from __future__ import annotations

from src.agents.defect_intel import DefectIntelAgent

_triage = DefectIntelAgent._triage_failure


def test_traceability_blocked_is_arta_side_not_sut():
    res = _triage({"status_code": 0, "error_message": "blocked at gen",
                   "metadata": {"blocked_reason": "traceability_blocked"}})
    assert res["triage_category"] == "test_gen_bug"
    assert "traceability_blocked" in res["triage_signals"]
    assert res["recommended_action"] == "self_heal"


def test_grounded_traceable_5xx_boosts_sut_confidence():
    md = {"status_code": 500,
          "traceability": {"grounded": True, "traceable": True,
                           "matched_endpoint_keys": ["GET:/api/x"]},
          "code_api_links": [{"fe_route": "/x"}]}
    res = _triage({"status_code": 500, "error_message": "Internal Server Error",
                   "auth_was_valid": True, "metadata": md})
    assert res["triage_category"] == "sut_regression"
    assert "grounded_traceable" in res["triage_signals"]
    assert res["triage_confidence"] > 0.90   # boosted above the base 0.90


def test_ungrounded_5xx_stays_base_confidence():
    res = _triage({"status_code": 500, "error_message": "Internal Server Error",
                   "auth_was_valid": True, "metadata": {"status_code": 500}})
    assert res["triage_category"] == "sut_regression"
    assert "grounded_traceable" not in res["triage_signals"]
    assert res["triage_confidence"] == 0.90   # unchanged — not attributable


def test_untraceable_api_test_fail_is_arta_side_not_operator_review():
    # R213 V4.1 — a FAIL on a test that references real endpoints but traces to
    # NONE of the req's mapped surface → test_gen_bug (self-heal), not the
    # ambiguous operator_review fallback. (No status code → previously fell
    # through every classifier to operator_review.)
    md = {"traceability": {"grounded": True, "traceable": False,
                           "test_endpoint_count": 2, "matched_endpoint_keys": []}}
    res = _triage({"status_code": None,
                   "error_message": "expect(received).toBe(expected)",
                   "metadata": md})
    assert res["triage_category"] == "test_gen_bug"
    assert "untraceable_test_endpoints" in res["triage_signals"]
    assert res["recommended_action"] == "self_heal"


def test_ui_only_test_zero_endpoints_not_misattributed():
    # UI-only test (0 endpoints) is traceable → the V4.1 branch must NOT fire;
    # it should fall through to the normal classifiers (operator_review here).
    md = {"traceability": {"grounded": True, "traceable": True,
                           "test_endpoint_count": 0, "matched_endpoint_keys": []}}
    res = _triage({"status_code": None, "error_message": "some ambiguous failure",
                   "metadata": md})
    assert res["triage_category"] != "test_gen_bug" or \
        "untraceable_test_endpoints" not in res.get("triage_signals", [])


def test_untraceable_branch_respects_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R211_ATTRIBUTION_DISABLE", "1")
    md = {"traceability": {"grounded": True, "traceable": False,
                           "test_endpoint_count": 2}}
    res = _triage({"status_code": None, "error_message": "expect(x).toBe(y)",
                   "metadata": md})
    assert "untraceable_test_endpoints" not in res.get("triage_signals", [])


def test_attribution_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R211_ATTRIBUTION_DISABLE", "1")
    md = {"status_code": 500,
          "traceability": {"grounded": True, "traceable": True,
                           "matched_endpoint_keys": ["GET:/api/x"]}}
    res = _triage({"status_code": 500, "error_message": "Internal Server Error",
                   "auth_was_valid": True, "metadata": md})
    assert "grounded_traceable" not in res["triage_signals"]
    # traceability_blocked also ignored under the killswitch
    res2 = _triage({"status_code": 0, "error_message": "x",
                    "metadata": {"blocked_reason": "traceability_blocked"}})
    assert res2["triage_category"] != "test_gen_bug" or "traceability_blocked" not in res2.get("triage_signals", [])
