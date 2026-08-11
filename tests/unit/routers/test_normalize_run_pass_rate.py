"""WS2 — `_normalize_run` must emit a consistent `pass_rate` so the in-memory
run path matches the DB path (and the run-history detail / dashboard / summary
all read ONE pass-rate instead of recomputing client-side)."""
from __future__ import annotations

from src.api.routers.execution import _normalize_run


def test_pass_rate_fallback_computed_from_passed_executed():
    # R306.A — pass_rate is OVER EXECUTED (passed + failed), NOT total. Here
    # executed = 9 + 1 = 10 → 90%.
    r = _normalize_run({"passed": 9, "failed": 1, "total": 10})
    assert r["pass_rate"] == 90.0


def test_pass_rate_excludes_blocked_and_skip_from_denominator():
    # R306.A — total=15 carries 5 blocked/skip; executed = 9 + 1 = 10 → 90%,
    # NOT 9/15 = 60%. This is the run-26aa5f discrepancy (50.6% vs 55.4%) fixed.
    r = _normalize_run({"passed": 9, "failed": 1, "skipped": 2, "total": 15})
    assert r["pass_rate"] == 90.0


def test_r306_a_recomputes_over_stale_stored_pass_rate():
    # R306.A — a PRE-R306.A run persisted a total-based pass_rate; _normalize_run
    # CANONICALLY recomputes executed-based so run-history matches the summary
    # report retroactively. 9 pass / 1 fail → 90%, overriding the stored 60.0.
    r = _normalize_run({"passed": 9, "failed": 1, "total": 15, "pass_rate": 60.0})
    assert r["pass_rate"] == 90.0


def test_explicit_pass_rate_preserved_only_when_executed_underivable():
    # No passed+failed to derive executed → keep the caller's explicit value.
    r = _normalize_run({"total": 10, "pass_rate": 42.5})
    assert r["pass_rate"] == 42.5


def test_zero_total_does_not_divide_by_zero():
    r = _normalize_run({"passed": 0, "total": 0})
    assert r["pass_rate"] == 0.0


def test_killswitch_skips_fallback(monkeypatch):
    monkeypatch.setenv("ARTA_WS2_PASSRATE_FALLBACK_DISABLE", "1")
    r = _normalize_run({"passed": 9, "total": 10})
    assert r.get("pass_rate") is None
