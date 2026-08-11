"""R213.K.2 — truthful k6 health for Pillar-2 scoring.

A single k6 PASS used to mark k6 "healthy", letting an 81%-FAIL run read MIXED
instead of PESSIMISTIC. The score now uses a pass RATIO over DISPATCHED checks
(BLOCKED excluded). Killswitch reverts to the legacy >=1 rule.
"""
from __future__ import annotations

from src.api.routers.execution import _r213k2_k6_healthy


def test_not_dispatched_is_healthy():
    assert _r213k2_k6_healthy(k6_total=0, k6_pass=0, k6_blocked=0) is True


def test_real_run_857ce1_is_unhealthy():
    # 6 PASS / 17 FAIL / 9 BLOCKED / 32 → 6/(32-9)=26% < 50% → NOT healthy
    assert _r213k2_k6_healthy(k6_total=32, k6_pass=6, k6_blocked=9) is False


def test_majority_pass_is_healthy():
    # 12 PASS / 20 dispatched = 60% → healthy
    assert _r213k2_k6_healthy(k6_total=24, k6_pass=12, k6_blocked=4) is True


def test_blocked_excluded_from_denominator():
    # 5 PASS, 5 dispatched (5 blocked) = 100% → healthy even though total=10
    assert _r213k2_k6_healthy(k6_total=10, k6_pass=5, k6_blocked=5) is True


def test_all_blocked_is_not_healthy():
    # everything blocked → 0 dispatched → cannot claim health
    assert _r213k2_k6_healthy(k6_total=8, k6_pass=0, k6_blocked=8) is False


def test_killswitch_reverts_to_ge1(monkeypatch):
    monkeypatch.setenv("ARTA_K6_HEALTHY_RATIO_DISABLE", "1")
    # legacy rule: a single PASS marks healthy even at 26%
    assert _r213k2_k6_healthy(k6_total=32, k6_pass=6, k6_blocked=9) is True
    assert _r213k2_k6_healthy(k6_total=32, k6_pass=0, k6_blocked=9) is False
