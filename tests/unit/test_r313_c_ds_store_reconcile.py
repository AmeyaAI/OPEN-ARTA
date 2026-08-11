"""R313.C — dual-store reconciliation. The executor must merge operator per-SUT
probe config from the file registry (.arta/projects.json) onto whatever project dict
a caller supplies (which, when DB-derived, lacks discovery_settings config), so
config like auth_liveness_path reaches the probe for EVERY caller — not only the
refresh_discovery path that already merges it (R221). File config wins; the passed
dict's runtime bookkeeping is preserved."""
from __future__ import annotations

import json

from src.agents.discovery_executor import _merge_file_discovery_settings

PID = "pid-abc"


def _write_store(tmp_path, monkeypatch, store):
    f = tmp_path / "projects.json"
    f.write_text(json.dumps(store))
    monkeypatch.setenv("ARTA_PROJECTS_FILE", str(f))
    return f


def test_file_config_reaches_db_derived_project_dict_keyed(tmp_path, monkeypatch):
    # projects.json keyed by id (the real shape)
    _write_store(tmp_path, monkeypatch, {
        PID: {"id": PID, "discovery_settings": {"auth_liveness_path": "/v1/regions",
                                                "skip_routes": ["/accounts"]}},
    })
    # a DB-derived project dict with only runtime bookkeeping, no operator config
    project = {"id": PID, "discovery_settings": {"discovery_pending": True}}
    out = _merge_file_discovery_settings(project, PID)
    ds = out["discovery_settings"]
    assert ds["auth_liveness_path"] == "/v1/regions"   # file config now present
    assert ds["skip_routes"] == ["/accounts"]
    assert ds["discovery_pending"] is True             # runtime key preserved


def test_file_config_reaches_project_list_shape(tmp_path, monkeypatch):
    # projects.json as a list of project dicts
    _write_store(tmp_path, monkeypatch, [
        {"id": "other", "discovery_settings": {}},
        {"id": PID, "discovery_settings": {"auth_liveness_path": "/whoami"}},
    ])
    out = _merge_file_discovery_settings({"id": PID}, PID)
    assert out["discovery_settings"]["auth_liveness_path"] == "/whoami"


def test_file_wins_for_overlapping_config_key(tmp_path, monkeypatch):
    _write_store(tmp_path, monkeypatch, {
        PID: {"id": PID, "discovery_settings": {"route_cap": 50}},
    })
    project = {"id": PID, "discovery_settings": {"route_cap": 10, "last_discovery_at": "t0"}}
    out = _merge_file_discovery_settings(project, PID)
    assert out["discovery_settings"]["route_cap"] == 50          # file wins
    assert out["discovery_settings"]["last_discovery_at"] == "t0"  # runtime preserved


def test_killswitch_disables_merge(tmp_path, monkeypatch):
    _write_store(tmp_path, monkeypatch, {
        PID: {"id": PID, "discovery_settings": {"auth_liveness_path": "/v1/regions"}},
    })
    monkeypatch.setenv("ARTA_DS_FILE_MERGE_DISABLE", "1")
    project = {"id": PID, "discovery_settings": {}}
    out = _merge_file_discovery_settings(project, PID)
    assert "auth_liveness_path" not in out["discovery_settings"]


def test_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTA_PROJECTS_FILE", str(tmp_path / "does-not-exist.json"))
    project = {"id": PID, "discovery_settings": {"x": 1}}
    out = _merge_file_discovery_settings(project, PID)
    assert out["discovery_settings"] == {"x": 1}
