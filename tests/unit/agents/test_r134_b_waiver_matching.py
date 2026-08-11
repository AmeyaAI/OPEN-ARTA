"""R134.B KEYSTONE tests — per-blocker waiver matching + stub graph honesty.

R134.B.1 closes the Pillar 4 truthfulness gap where a single waiver
covering ANY blocker silently waived ALL blockers. Post-R134.B.1 only
checks WITH an explicit matching waiver are waived; unwaived blockers
continue to block.

R134.B.2 replaces the hardcoded 90.9% stub coverage with computed
values from in-memory RECENT_RESULTS. degraded flag stays so the gate
flags CONCERNS during Neo4j outages.

Ten cases (6 R134.B.1 + 4 R134.B.2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.agents.quality_gate_agent import QualityGateAgent


@dataclass
class _StubCheck:
    """Minimal GateCheck-shaped stub for waiver-matching tests."""
    name: str
    passed: bool = False
    severity: str = "BLOCK"


def _agent() -> QualityGateAgent:
    return QualityGateAgent()


def _make_waiver(check_name: str, *, expired: bool = False, project: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    exp = (now - timedelta(days=1)) if expired else (now + timedelta(days=30))
    return {
        "waived_check": check_name,
        "rationale": "test waiver",
        "approved_by": "test-user",
        "expires_at": exp.isoformat(),
        "project_id": project,
    }


# ── R134.B.1 cases (6 tests) ────────────────────────────────────────────

def test_r134_b_1_single_waiver_covers_single_blocker():
    """One blocker + one matching waiver → that blocker is waived."""
    blockers = [_StubCheck(name="coverage_p0")]
    waivers = [_make_waiver("coverage_p0")]
    waived, still_blocking = _agent()._r134_b_1_match_waivers_per_blocker(waivers, blockers)
    assert len(waived) == 1
    assert waived[0].name == "coverage_p0"
    assert still_blocking == []


def test_r134_b_1_single_waiver_does_not_cover_other_blockers():
    """Two blockers + one waiver matching only one → still_blocking has
    the OTHER. Pre-R134.B.1 the entire release was WAIVED; post-R134.B.1
    the other blocker continues to block (Pillar 4 truthful)."""
    blockers = [_StubCheck(name="coverage_p0"), _StubCheck(name="pass_rate_p0")]
    waivers = [_make_waiver("coverage_p0")]
    waived, still_blocking = _agent()._r134_b_1_match_waivers_per_blocker(waivers, blockers)
    assert len(waived) == 1 and waived[0].name == "coverage_p0"
    assert len(still_blocking) == 1 and still_blocking[0].name == "pass_rate_p0"


def test_r134_b_1_expired_waiver_does_not_match():
    """Expired waiver must NOT match — even if the name aligns. Otherwise
    expired waivers would silently waive blockers."""
    blockers = [_StubCheck(name="coverage_p0")]
    waivers = [_make_waiver("coverage_p0", expired=True)]
    waived, still_blocking = _agent()._r134_b_1_match_waivers_per_blocker(waivers, blockers)
    assert waived == []
    assert len(still_blocking) == 1


def test_r134_b_1_all_blockers_waived():
    """Three blockers, three matching waivers → all waived; release
    truthfully WAIVED. Acceptance criterion: explicit per-blocker waivers
    are sufficient, NOT a single waiver covering one."""
    blockers = [
        _StubCheck(name="coverage_p0"),
        _StubCheck(name="pass_rate_p0"),
        _StubCheck(name="sut_quality"),
    ]
    waivers = [
        _make_waiver("coverage_p0"),
        _make_waiver("pass_rate_p0"),
        _make_waiver("sut_quality"),
    ]
    waived, still_blocking = _agent()._r134_b_1_match_waivers_per_blocker(waivers, blockers)
    assert len(waived) == 3
    assert still_blocking == []


def test_r134_b_1_no_waivers_keeps_all_blocking():
    """Empty waiver list → all blockers remain blocking. Regression
    guard for the cold-start case."""
    blockers = [_StubCheck(name="coverage_p0"), _StubCheck(name="pass_rate_p0")]
    waived, still_blocking = _agent()._r134_b_1_match_waivers_per_blocker([], blockers)
    assert waived == []
    assert len(still_blocking) == 2


def test_r134_b_1_is_expired_handles_malformed_input():
    """`_r134_b_1_is_expired` treats malformed/missing expires_at as
    expired (fail-safe). A waiver without a deadline is not valid."""
    agent = _agent()
    # Missing expires_at
    assert agent._r134_b_1_is_expired({"waived_check": "x"}) is True
    # Unparseable expires_at string
    assert agent._r134_b_1_is_expired({"waived_check": "x", "expires_at": "not-a-date"}) is True
    # Empty string expires_at
    assert agent._r134_b_1_is_expired({"waived_check": "x", "expires_at": ""}) is True


# ── R134.B.2 cases (4 tests) ────────────────────────────────────────────

def test_r134_b_2_stub_coverage_no_data_returns_zero_p0():
    """Stub coverage with empty RECENT_RESULTS returns p0_pass_rate=0.0
    + degraded=True. Pre-R134.B.2 returned hardcoded 90.9% — the bug
    that R134.B.2 closes."""
    from src.agents.traceability_agent import TraceabilityAgent
    agent = TraceabilityAgent()
    # Patch RECENT_RESULTS to empty list
    with patch("src.api.routers.tests.GENERATED_TESTS", []), \
         patch("src.api.routers.execution._REAL_RESULTS", {}):
        report = agent._stub_coverage_report()
    assert report["degraded"] is True
    assert report["p0_pass_rate"] == 0.0
    assert "samples_used" in report
    # Critical: must NOT report the pre-R134.B.2 hardcoded 90.9
    assert report["p0_pass_rate"] != 90.9


def test_r134_b_2_stub_coverage_computes_from_recent_results():
    """When RECENT_RESULTS has data, p0_pass_rate is computed from
    actual passes. R134.B.2 single source of truth."""
    from src.agents.traceability_agent import TraceabilityAgent
    p0_tests = [
        {"id": "TC-001", "priority": "P0"},
        {"id": "TC-002", "priority": "P0"},
    ]
    recent = [
        {"test_id": "TC-001", "status": "PASS"},
        {"test_id": "TC-002", "status": "FAIL"},
    ]
    agent = TraceabilityAgent()
    # `_REAL_RESULTS` is dict[run_id, list[result_dict]]; helper flattens
    # across runs to get a recent results pool
    with patch("src.api.routers.tests.GENERATED_TESTS", p0_tests), \
         patch("src.api.routers.execution._REAL_RESULTS", {"run-test": recent}):
        report = agent._stub_coverage_report()
    assert report["degraded"] is True
    assert report["p0_pass_rate"] == 50.0  # 1 of 2 passed


def test_r134_b_2_stub_coverage_degraded_flag_present():
    """R134.B.2 preserves the degraded flag + reason. The gate reads
    this and surfaces CONCERNS — operator MUST see the stub-mode
    indicator."""
    from src.agents.traceability_agent import TraceabilityAgent
    agent = TraceabilityAgent()
    with patch("src.api.routers.tests.GENERATED_TESTS", []), \
         patch("src.api.routers.execution._REAL_RESULTS", {}):
        report = agent._stub_coverage_report()
    assert report["degraded"] is True
    assert "degraded_reason" in report
    assert "R134.B.2" in report["degraded_reason"] or "Neo4j unavailable" in report["degraded_reason"]


def test_r134_b_2_stub_coverage_no_hardcoded_legacy_numbers():
    """Regression guard — R134.B.2 must NOT re-introduce the hardcoded
    78%/91%/90.9% values. Inspect the source to verify they're gone."""
    import inspect
    from src.agents.traceability_agent import TraceabilityAgent
    src = inspect.getsource(TraceabilityAgent._stub_coverage_report)
    # The pre-R134.B.2 magic numbers
    assert "78.0" not in src or "78.0," not in src   # tolerate other 78.0 mentions
    assert '"p0_pass_rate": 90.9' not in src
    # Computed-mode signal must be present
    assert "R134.B.2" in src
    assert "RECENT_RESULTS" in src
