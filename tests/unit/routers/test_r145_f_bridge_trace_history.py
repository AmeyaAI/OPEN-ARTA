"""R145.F — multi-run bridge-trace history aggregation tests.

Validates that the helper walks `.arta/runs/*/r145_c_bridge_trace.jsonl`
correctly, filters by project_id, sorts by sidecar mtime desc, caps to
the requested limit, and detects regression drift when the latest run's
delivery_break_point differs from the prior 3 consecutive runs.

Mission contract: operators must see WHETHER the bridge is delivering
consistently across smokes (trend visibility) AND whether the latest run
regressed from prior consistent behavior (drift WARN).
"""
from __future__ import annotations
import json
import time
from pathlib import Path

from src.api.routers.projects import _r145_f_load_bridge_trace_history


def _write_sidecar(tmp_path: Path, run_id: str, events: list[dict],
                   mtime: float | None = None) -> Path:
    """Helper to write a synthetic R145.C sidecar at the expected path."""
    run_dir = tmp_path / ".arta" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    sidecar = run_dir / "r145_c_bridge_trace.jsonl"
    sidecar.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    if mtime is not None:
        import os
        os.utime(sidecar, (mtime, mtime))
    return sidecar


def test_r145_f_empty_history_no_runs(tmp_path, monkeypatch):
    """No sidecars present → empty entries, no drift."""
    monkeypatch.chdir(tmp_path)
    result = _r145_f_load_bridge_trace_history("proj-abc")
    assert result["entries"] == []
    assert result["drift_detected"] is False
    assert result["drift_message"] is None


def test_r145_f_filters_by_project_id(tmp_path, monkeypatch):
    """Only sidecars whose first project_id_stamped event matches the
    project_id are included. Other projects' sidecars must not leak into
    this project's dashboard tile."""
    monkeypatch.chdir(tmp_path)
    _write_sidecar(tmp_path, "run-proj-a", [
        {"event": "project_id_stamped", "run_id": "run-proj-a",
         "project_id": "proj-a"},
        {"event": "r143_d_state_stamped", "should_bridge": True,
         "sut_host": "sandbox.test.ai"},
    ])
    _write_sidecar(tmp_path, "run-proj-b", [
        {"event": "project_id_stamped", "run_id": "run-proj-b",
         "project_id": "proj-b"},
    ])
    result_a = _r145_f_load_bridge_trace_history("proj-a")
    assert len(result_a["entries"]) == 1
    assert result_a["entries"][0]["run_id"] == "run-proj-a"
    assert result_a["entries"][0]["sut_host"] == "sandbox.test.ai"


def test_r145_f_sorts_by_mtime_desc_and_caps_limit(tmp_path, monkeypatch):
    """Multiple runs sorted by mtime desc; limit respected (most recent
    `limit` runs returned)."""
    monkeypatch.chdir(tmp_path)
    now = time.time()
    # Write 12 sidecars with increasing mtime
    for i in range(12):
        _write_sidecar(tmp_path, f"run-{i:03d}", [
            {"event": "project_id_stamped", "project_id": "p"},
        ], mtime=now - (12 - i) * 60)
    result = _r145_f_load_bridge_trace_history("p", limit=5)
    assert len(result["entries"]) == 5
    # Most recent run (run-011) is first
    assert result["entries"][0]["run_id"] == "run-011"
    assert result["entries"][-1]["run_id"] == "run-007"


def test_r145_f_drift_detection_fires_on_regression(tmp_path, monkeypatch):
    """When the most recent run's delivery_break_point differs from the
    prior 3 consecutive runs (all 'delivered'), drift fires with an
    actionable message naming the regressed run_id."""
    monkeypatch.chdir(tmp_path)
    now = time.time()
    # Prior 3 runs: all delivered
    for i in range(3):
        _write_sidecar(tmp_path, f"run-prior-{i}", [
            {"event": "project_id_stamped", "project_id": "p"},
            {"event": "r143_d_state_stamped", "should_bridge": True},
            {"event": "chromium_bridge_env_set"},
            {"event": "pw_subprocess_spawn", "has_resolver_rules_env": True},
            {"event": "chromium_launch_args", "resolver_rules_present": True},
        ], mtime=now - (10 - i) * 60)
    # Latest run: env was dropped (subprocess didn't see it)
    _write_sidecar(tmp_path, "run-regressed", [
        {"event": "project_id_stamped", "project_id": "p"},
        {"event": "r143_d_state_stamped", "should_bridge": True},
        {"event": "chromium_bridge_env_set"},
        {"event": "pw_subprocess_spawn", "has_resolver_rules_env": False},
    ], mtime=now)
    result = _r145_f_load_bridge_trace_history("p", limit=10)
    assert result["drift_detected"] is True
    assert "run-regressed" in result["drift_message"]
    assert "delivered" in result["drift_message"]
    assert "subprocess_env" in result["drift_message"]
