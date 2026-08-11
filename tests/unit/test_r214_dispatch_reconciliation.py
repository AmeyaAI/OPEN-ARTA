"""R214 — dispatch-boundary invariant: every SCHEDULED tool must produce >=1
result row. _r214_reconcile_dispatched_tools backfills a truthful BLOCKED/SKIP
row for any tool in the dispatch manifest that produced ZERO rows, so a tool can
never silently vanish from a run (run-6459b6: axe+pytest scheduled, 0 rows, no
BLOCKED/FAIL → operator saw a clean-looking run that had dropped 2 pillars).
"""
from __future__ import annotations

from src.api.routers.execution import _r214_reconcile_dispatched_tools, _REAL_RESULTS


def _rows(run_id):
    return [r for r in _REAL_RESULTS.get(run_id, []) if isinstance(r, dict)]


def _seed(run_id, rows):
    _REAL_RESULTS[run_id] = list(rows)


def test_scheduled_tool_with_zero_rows_gets_blocked_backfill():
    run = "run-r214-a"
    _seed(run, [{"automation_tool": "newman", "status": "PASS"}])
    n = _r214_reconcile_dispatched_tools(run, {"newman": 5, "axe": 20}, [])
    assert n == 1
    backfill = [r for r in _rows(run) if r.get("test_id", "").startswith("R214-axe")]
    assert len(backfill) == 1
    row = backfill[0]
    assert row["status"] == "BLOCKED"
    assert row["automation_tool"] == "axe"
    assert row["metadata"]["blocked_reason"] == "dispatch_produced_no_results"
    assert row["metadata"]["spec_count"] == 20


def test_zero_spec_count_yields_skip_not_blocked():
    run = "run-r214-b"
    _seed(run, [])
    _r214_reconcile_dispatched_tools(run, {"pytest": 0}, [])
    row = [r for r in _rows(run) if r["automation_tool"] == "pytest"][0]
    assert row["status"] == "SKIP"
    assert row["metadata"]["skip_reason"] == "no_specs_scheduled"


def test_tool_that_produced_rows_is_not_backfilled():
    run = "run-r214-c"
    _seed(run, [{"automation_tool": "k6", "status": "PASS"},
                {"automation_tool": "k6", "status": "FAIL"}])
    n = _r214_reconcile_dispatched_tools(run, {"k6": 12}, [])
    assert n == 0
    assert not any(r.get("test_id", "").startswith("R214-") for r in _rows(run))


def test_execution_error_cause_is_surfaced():
    run = "run-r214-d"
    _seed(run, [])
    # task name format is "<tool>-<run_id>"
    _r214_reconcile_dispatched_tools(
        run, {"axe": 20}, [f"axe-{run}: RuntimeError boom in axe subprocess"],
    )
    row = [r for r in _rows(run) if r["automation_tool"] == "axe"][0]
    assert row["status"] == "BLOCKED"
    assert "RAISED/was CANCELLED" in row["error_message"]
    assert "boom" in row["error_message"]


def test_no_cause_message_points_at_source():
    run = "run-r214-e"
    _seed(run, [])
    _r214_reconcile_dispatched_tools(run, {"pytest": 7}, [])
    row = [r for r in _rows(run) if r["automation_tool"] == "pytest"][0]
    assert "_run_pytest" in row["error_message"]


def test_killswitch_disables_reconciliation(monkeypatch):
    run = "run-r214-f"
    _seed(run, [])
    monkeypatch.setenv("ARTA_R214_RECONCILE_DISABLE", "1")
    n = _r214_reconcile_dispatched_tools(run, {"axe": 20, "pytest": 7}, [])
    assert n == 0
    assert _rows(run) == []


def test_multiple_missing_tools_each_get_one_row():
    run = "run-r214-g"
    _seed(run, [{"automation_tool": "playwright", "status": "PASS"}])
    n = _r214_reconcile_dispatched_tools(
        run, {"playwright": 18, "axe": 20, "pytest": 7}, [])
    assert n == 2
    tools = {r["automation_tool"] for r in _rows(run) if r.get("test_id", "").startswith("R214-")}
    assert tools == {"axe", "pytest"}
