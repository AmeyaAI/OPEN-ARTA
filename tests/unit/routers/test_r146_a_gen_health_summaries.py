"""R146.A — wire 4 r145_*_summary fields into gen-health response.

Pre-R146.A: gen-health endpoint returned `r145_f_bridge_trace_history`
only; the 4 per-current-run r145_*_summary fields were computed
elsewhere but not surfaced. Iter 5 dashboard showed `r145_a_summary=null`,
`r145_b_summary=null`, etc. Operators saw historical (R145.F) but not
the current-run signal.

R146.A ships 4 aggregator helpers + wires them into the response dict.
Each helper is best-effort (returns empty-shaped defaults on missing
data, no exceptions).
"""
from __future__ import annotations
import json
from pathlib import Path

from src.api.routers.projects import (
    _r146_a_summary_a_aggregate,
    _r146_a_summary_b_aggregate,
    _r146_a_summary_c_latest_run,
    _r146_a_summary_d_cascade,
    _r146_a_latest_run_id_for_project,
)


def test_r146_a_summary_a_aggregates_audit_entries(tmp_path, monkeypatch):
    """Summary A reads .arta/audit/r145_a3_autopurge.jsonl + sums
    items_substituted / items_blocked / files_scanned per project."""
    monkeypatch.chdir(tmp_path)
    audit_dir = tmp_path / ".arta" / "audit"
    audit_dir.mkdir(parents=True)
    audit = audit_dir / "r145_a3_autopurge.jsonl"
    audit.write_text("\n".join([
        json.dumps({"trigger": "startup", "project_id": "proj-a", "newman_files_scanned": 1000, "items_substituted": 5, "items_blocked": 2}),
        json.dumps({"trigger": "post_paste", "project_id": "proj-a", "newman_files_scanned": 1000, "items_substituted": 10, "items_blocked": 1}),
        json.dumps({"trigger": "startup", "project_id": "proj-OTHER", "newman_files_scanned": 500, "items_substituted": 99}),  # filtered out
    ]) + "\n")
    summary = _r146_a_summary_a_aggregate("proj-a")
    assert summary["total_items_substituted"] == 15
    assert summary["total_items_blocked"] == 3
    assert summary["total_items_scanned"] == 2000
    assert summary["audit_entries"] == 2
    assert "startup" in summary["triggers_seen"]
    assert "post_paste" in summary["triggers_seen"]


def test_r146_a_summary_a_returns_zeros_when_audit_missing(tmp_path, monkeypatch):
    """No audit file → empty-shaped defaults (no exception)."""
    monkeypatch.chdir(tmp_path)
    summary = _r146_a_summary_a_aggregate("proj-a")
    assert summary["audit_entries"] == 0
    assert summary["total_items_substituted"] == 0


def test_r146_a_summary_c_returns_empty_when_no_sidecars(tmp_path, monkeypatch):
    """No per-run sidecars → summarize returns empty-shaped defaults."""
    monkeypatch.chdir(tmp_path)
    summary = _r146_a_summary_c_latest_run("proj-a")
    assert summary.get("trace_events_present") is False


def test_r146_a_summary_c_loads_latest_run_sidecar(tmp_path, monkeypatch):
    """When a per-run sidecar matches the project_id, summary C loads +
    returns the computed bridge-trace summary."""
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / ".arta" / "runs" / "run-A"
    run_dir.mkdir(parents=True)
    (run_dir / "r145_c_bridge_trace.jsonl").write_text(
        json.dumps({
            "event": "project_id_stamped",
            "run_id": "run-A",
            "project_id": "proj-a",
        }) + "\n"
    )
    summary = _r146_a_summary_c_latest_run("proj-a")
    assert summary.get("trace_events_present") is True
    assert summary.get("run_id") == "run-A"
    assert "project_id_stamped" in (summary.get("latest_event_chain") or [])


def test_r146_a_summary_d_returns_empty_when_no_run(tmp_path, monkeypatch):
    """No per-run sidecar → summary D returns empty dict."""
    monkeypatch.chdir(tmp_path)
    summary = _r146_a_summary_d_cascade("proj-a")
    assert summary == {}


def test_r146_a_latest_run_id_filters_by_project(tmp_path, monkeypatch):
    """Helper returns most-recent run for THIS project, skips others."""
    monkeypatch.chdir(tmp_path)
    base = tmp_path / ".arta" / "runs"
    # proj-a run
    rd_a = base / "run-A"
    rd_a.mkdir(parents=True)
    (rd_a / "r145_c_bridge_trace.jsonl").write_text(
        json.dumps({"event": "project_id_stamped", "run_id": "run-A", "project_id": "proj-a"}) + "\n"
    )
    # proj-other run (should be filtered)
    rd_b = base / "run-B"
    rd_b.mkdir(parents=True)
    (rd_b / "r145_c_bridge_trace.jsonl").write_text(
        json.dumps({"event": "project_id_stamped", "run_id": "run-B", "project_id": "proj-other"}) + "\n"
    )
    assert _r146_a_latest_run_id_for_project("proj-a") == "run-A"
    assert _r146_a_latest_run_id_for_project("proj-other") == "run-B"
    assert _r146_a_latest_run_id_for_project("proj-c") is None
