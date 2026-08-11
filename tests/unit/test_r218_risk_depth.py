"""C1 (R218) — risk-driven test depth. The BMAD-TEA risk score must DRIVE how
many adversarial probes a requirement gets (high-risk → more, low-risk → fewer).
Pre-C1 it was computed + persisted but used only as a prompt comment, so every
requirement got the same 7 probes — risk-based testing was dead code.
"""
from __future__ import annotations

from src.agents.analytics_test_agent import _risk_to_adversarial_count


def test_c1_priority_scales_depth():
    assert _risk_to_adversarial_count({"priority": "P0"}) == 12
    assert _risk_to_adversarial_count({"priority": "P1"}) == 9
    assert _risk_to_adversarial_count({"priority": "P2"}) == 6
    assert _risk_to_adversarial_count({"priority": "P3"}) == 4


def test_c1_high_risk_gets_more_than_low_risk():
    assert _risk_to_adversarial_count({"priority": "P0"}) > _risk_to_adversarial_count({"priority": "P3"})


def test_c1_risk_score_fallback_when_no_priority():
    assert _risk_to_adversarial_count({"risk_score": 9}) == 12   # P0 band
    assert _risk_to_adversarial_count({"risk_score": 4}) == 9    # P1 band
    assert _risk_to_adversarial_count({"risk_score": 1}) == 4    # P3 band


def test_c1_missing_or_malformed_defaults_to_7():
    assert _risk_to_adversarial_count(None) == 7
    assert _risk_to_adversarial_count({}) == 7
    assert _risk_to_adversarial_count({"risk_score": "bad"}) == 7


def test_c1_killswitch_constant_7(monkeypatch):
    monkeypatch.setenv("ARTA_C1_RISK_DEPTH_DISABLE", "1")
    assert _risk_to_adversarial_count({"priority": "P0"}) == 7
