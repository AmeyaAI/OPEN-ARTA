"""_save_projects merge-preserve: a partial in-memory project must not clobber
richer on-disk config (e.g. an env-var PUT on project A dropping project B's
discovery_settings via the whole-file rewrite)."""
import json
from pathlib import Path

import src.api.routers.projects as P


def test_on_disk_field_survives_partial_in_memory_write(monkeypatch, tmp_path):
    f = tmp_path / "projects.json"
    monkeypatch.setattr(P, "_PROJECTS_FILE", Path(f))
    PID_A = "aaaaaaaa-0000-4000-8000-000000000001"
    PID_B = "bbbbbbbb-0000-4000-8000-000000000002"
    # On disk: B carries rich discovery_settings.
    f.write_text(json.dumps({
        PID_B: {"id": PID_B, "name": "B", "discovery_settings": {"app_entry_routes": ["/x", "/y"]}},
    }))
    # In memory: A is new; B is a PARTIAL entry (DB-hydrated, no discovery_settings).
    monkeypatch.setattr(P, "_PROJECTS", {
        PID_A: {"id": PID_A, "name": "A", "environments": {"e": {"variables": {"t": "1"}}}},
        PID_B: {"id": PID_B, "name": "B", "environments": {}},
    })
    P._save_projects()
    saved = json.loads(f.read_text())
    assert saved[PID_A]["name"] == "A"                     # new project written
    assert saved[PID_B]["discovery_settings"] == {"app_entry_routes": ["/x", "/y"]}, \
        "B's on-disk discovery_settings must survive A's write"


def test_in_memory_value_wins_on_conflict(monkeypatch, tmp_path):
    f = tmp_path / "projects.json"
    monkeypatch.setattr(P, "_PROJECTS_FILE", Path(f))
    PID = "cccccccc-0000-4000-8000-000000000003"
    f.write_text(json.dumps({PID: {"id": PID, "name": "old", "keep": 1}}))
    monkeypatch.setattr(P, "_PROJECTS", {PID: {"id": PID, "name": "new"}})
    P._save_projects()
    saved = json.loads(f.read_text())[PID]
    assert saved["name"] == "new"   # in-memory wins on conflict
    assert saved["keep"] == 1       # on-disk-only key preserved


def test_killswitch_reverts_to_whole_replace(monkeypatch, tmp_path):
    f = tmp_path / "projects.json"
    monkeypatch.setattr(P, "_PROJECTS_FILE", Path(f))
    monkeypatch.setenv("ARTA_PROJECTS_MERGE_PRESERVE_DISABLE", "1")
    PID = "dddddddd-0000-4000-8000-000000000004"
    f.write_text(json.dumps({PID: {"id": PID, "gone": 1}}))
    monkeypatch.setattr(P, "_PROJECTS", {PID: {"id": PID, "name": "x"}})
    P._save_projects()
    saved = json.loads(f.read_text())[PID]
    assert "gone" not in saved   # killswitch = old whole-replace behaviour
