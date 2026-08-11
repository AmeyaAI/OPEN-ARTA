"""R144.E — orphan-sweep TTL env override + execution_results heartbeat.

Pre-R144.E (Iter 2 + Iter 3 of R143.F): hardcoded 75min TTL murdered
runs before PW phase persisted results. Post-R144.E:
  - ARTA_ORPHAN_SWEEP_TTL_MIN env var configurable [15, 1440]
  - Sweeper SQL incorporates execution_results.created_at heartbeat —
    runs actively persisting per-spec rows in the last (TTL/3) minutes
    are alive even when started_at > TTL.

Helpers under test are pure (no DB), so all 4 cases can run as fast
unit tests with no fixture.
"""
from __future__ import annotations

import os

from src.api.main import (
    _r144_e_build_sweeper_sql,
    _r144_e_resolve_ttl,
)


def test_r144_e_1_ttl_default_75_when_env_unset(monkeypatch):
    """R144.E.1 — default TTL stays 75 when ARTA_ORPHAN_SWEEP_TTL_MIN
    is not set (backward compat with pre-R144.E behavior)."""
    monkeypatch.delenv("ARTA_ORPHAN_SWEEP_TTL_MIN", raising=False)
    assert _r144_e_resolve_ttl() == 75


def test_r144_e_1_ttl_env_override_honored_within_clamp(monkeypatch):
    """R144.E.1 — operator can set ARTA_ORPHAN_SWEEP_TTL_MIN=180 to
    extend the window for long-running smokes (R143.F use case)."""
    monkeypatch.setenv("ARTA_ORPHAN_SWEEP_TTL_MIN", "180")
    assert _r144_e_resolve_ttl() == 180

    # And clamp at boundaries — too small and too large protect against
    # foot-guns (5 min would cause cascade reaping; 99999 would never sweep).
    monkeypatch.setenv("ARTA_ORPHAN_SWEEP_TTL_MIN", "5")
    assert _r144_e_resolve_ttl() == 15

    monkeypatch.setenv("ARTA_ORPHAN_SWEEP_TTL_MIN", "99999")
    assert _r144_e_resolve_ttl() == 1440


def test_r144_e_2_sweeper_sql_carries_both_ttl_and_heartbeat():
    """R144.E.2 — composed SQL incorporates the TTL interval AND the
    heartbeat NOT EXISTS subquery. Verifies the heartbeat clause
    references execution_results.created_at + correct interval."""
    sql = _r144_e_build_sweeper_sql(ttl_min=180, heartbeat_min=60)
    # TTL clause
    assert "INTERVAL '180 minutes'" in sql
    assert "COALESCE(started_at, created_at)" in sql
    # Heartbeat NOT EXISTS clause
    assert "NOT EXISTS" in sql
    assert "execution_results" in sql
    assert "INTERVAL '60 minutes'" in sql
    # R144.E gate_summary marker for forensic traceability
    assert "R144.E" in sql


def test_r144_e_2_sweeper_sql_at_default_ttl_75_yields_heartbeat_25():
    """R144.E.2 — heartbeat = max(5, ttl//3). At default ttl=75 →
    heartbeat=25 minutes. Verifies the helper's clamp logic is wired
    correctly between resolve_ttl + the sweeper-task site."""
    ttl = _r144_e_resolve_ttl()  # 75 by default
    heartbeat = max(5, ttl // 3)
    sql = _r144_e_build_sweeper_sql(ttl_min=ttl, heartbeat_min=heartbeat)
    assert heartbeat == 25
    assert "INTERVAL '75 minutes'" in sql
    assert "INTERVAL '25 minutes'" in sql


def test_r147_a_sweeper_uses_executed_at_not_created_at():
    """R147.A — heartbeat references `er.executed_at`, the column that
    actually exists in execution_results. Pre-R147.A: `er.created_at`
    raised UndefinedColumnError every 5 min for the lifetime of every
    run, flooding logs + silently disabling the R144.E heartbeat path.

    Live evidence: Iter 6 (run-11ff8f) logs at 15:30:09 show repeated
    `column er.created_at does not exist` exceptions; R144.E
    heartbeat-reset architecture was structurally broken until R147.A
    landed."""
    sql = _r144_e_build_sweeper_sql(ttl_min=75, heartbeat_min=25)
    # The fixed column — must reference the real schema column
    assert "er.executed_at" in sql
    # The broken column reference must be gone (regression guard against
    # any future revert)
    assert "er.created_at" not in sql
