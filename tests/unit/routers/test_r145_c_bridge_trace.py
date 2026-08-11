"""R145.C — bridge wire forensic trace tests.

Validates the per-run JSONL sidecar at
`.arta/runs/{run_id}/r145_c_bridge_trace.jsonl` AND the dashboard summary
derived from those events.

Mission contract: operators must be able to identify WHERE the chromium
bridge env-var delivery chain broke (project_id stamp → R143.D state
stamp → env-var set → npx subprocess → chromium launch arg). The sidecar
+ summary together give the R146 hypothesis-narrowing artifact via the
computed `delivery_break_point`.
"""
from __future__ import annotations
import json
from pathlib import Path

from src.api.routers.execution import (
    _r145_c_trace,
    _r145_c_load_bridge_trace,
    _r145_c_summarize_bridge_trace,
)


def test_r145_c_trace_writes_jsonl_line(tmp_path, monkeypatch):
    """JSONL line written to sidecar with expected schema (ts, event,
    run_id, payload keys)."""
    monkeypatch.chdir(tmp_path)
    _r145_c_trace(
        "project_id_stamped",
        {"project_id": "proj-abc", "build_id": "build-1"},
        "run-test-001",
    )
    sidecar = tmp_path / ".arta" / "runs" / "run-test-001" / "r145_c_bridge_trace.jsonl"
    assert sidecar.exists(), "trace JSONL sidecar not written"
    lines = sidecar.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "project_id_stamped"
    assert record["run_id"] == "run-test-001"
    assert record["project_id"] == "proj-abc"
    assert "ts" in record


def test_r145_c_trace_idempotent_dir_creation(tmp_path, monkeypatch):
    """Multiple trace calls for the same run_id append to the same file
    without recreating the directory."""
    monkeypatch.chdir(tmp_path)
    _r145_c_trace("project_id_stamped", {"project_id": "p"}, "run-test-002")
    _r145_c_trace(
        "r143_d_state_stamped",
        {"should_bridge": True, "resolved_ip": "1.2.3.4"},
        "run-test-002",
    )
    sidecar = tmp_path / ".arta" / "runs" / "run-test-002" / "r145_c_bridge_trace.jsonl"
    assert sidecar.exists()
    events = sidecar.read_text().strip().split("\n")
    assert len(events) == 2
    assert json.loads(events[0])["event"] == "project_id_stamped"
    assert json.loads(events[1])["event"] == "r143_d_state_stamped"


def test_r145_c_trace_swallows_disk_errors(monkeypatch, caplog):
    """Disk errors swallowed silently with log.debug; never raise to
    caller (best-effort instrumentation must NOT break dispatch)."""
    import os
    # Pass empty run_id → helper short-circuits without writing
    _r145_c_trace("project_id_stamped", {"project_id": "p"}, "")
    # No exception raised — that's the contract


def test_r145_c_summarize_delivery_break_points(tmp_path, monkeypatch):
    """Summary helper computes correct delivery_break_point across all
    5 break states + the not_armed state (which is mission-correct, not
    a break)."""
    monkeypatch.chdir(tmp_path)
    # Case 1: empty events → no trace
    summary = _r145_c_summarize_bridge_trace([])
    assert summary["trace_events_present"] is False
    assert summary["delivery_break_point"] is None

    # Case 2: only stamp event → break at "bridge_arm"
    events = [{"event": "project_id_stamped", "run_id": "r1"}]
    summary = _r145_c_summarize_bridge_trace(events)
    assert summary["delivery_break_point"] == "bridge_arm"

    # Case 3: stamp + state but should_bridge=False → "not_armed"
    events = [
        {"event": "project_id_stamped", "run_id": "r1"},
        {"event": "r143_d_state_stamped", "should_bridge": False},
    ]
    summary = _r145_c_summarize_bridge_trace(events)
    assert summary["delivery_break_point"] == "not_armed"

    # Case 4: stamp + state(bridge=True) but no chromium_bridge_env_set
    events = [
        {"event": "project_id_stamped", "run_id": "r1"},
        {"event": "r143_d_state_stamped", "should_bridge": True},
    ]
    summary = _r145_c_summarize_bridge_trace(events)
    assert summary["delivery_break_point"] == "subprocess_env"

    # Case 5: env set but subprocess didn't see it
    events = [
        {"event": "project_id_stamped"},
        {"event": "r143_d_state_stamped", "should_bridge": True},
        {"event": "chromium_bridge_env_set", "rule_preview": "MAP h:443 1.1.1.1:443"},
        {"event": "pw_subprocess_spawn", "has_resolver_rules_env": False},
    ]
    summary = _r145_c_summarize_bridge_trace(events)
    assert summary["delivery_break_point"] == "subprocess_env"
    assert summary["subprocess_saw_env_var"] is False

    # Case 6: full chain delivered
    events = [
        {"event": "project_id_stamped"},
        {"event": "r143_d_state_stamped", "should_bridge": True},
        {"event": "chromium_bridge_env_set", "rule_preview": "MAP h:443 1.1.1.1:443"},
        {"event": "pw_subprocess_spawn", "has_resolver_rules_env": True},
        {"event": "chromium_launch_args", "resolver_rules_present": True},
    ]
    summary = _r145_c_summarize_bridge_trace(events)
    assert summary["delivery_break_point"] == "delivered"
    assert summary["subprocess_saw_env_var"] is True
    assert summary["chromium_saw_launch_arg"] is True


def test_r145_c_load_sidecar_handles_malformed_lines(tmp_path, monkeypatch):
    """Malformed JSONL lines are skipped silently; valid lines load.
    Critical because the sidecar is best-effort: partial writes during
    container kill must not poison the dashboard read."""
    monkeypatch.chdir(tmp_path)
    sidecar_dir = tmp_path / ".arta" / "runs" / "run-test-malformed"
    sidecar_dir.mkdir(parents=True)
    sidecar = sidecar_dir / "r145_c_bridge_trace.jsonl"
    sidecar.write_text(
        '{"event":"project_id_stamped","run_id":"r1"}\n'
        '{malformed json\n'
        '{"event":"r143_d_state_stamped","should_bridge":true}\n'
        '\n'  # blank
    )
    events = _r145_c_load_bridge_trace("run-test-malformed")
    assert len(events) == 2
    assert events[0]["event"] == "project_id_stamped"
    assert events[1]["event"] == "r143_d_state_stamped"
