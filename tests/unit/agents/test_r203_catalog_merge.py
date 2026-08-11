"""R203 — non-destructive catalog write (keep-richer merge).

Root cause it fixes: `ingest_dom_snapshots` always overwrote the catalog with
the current capture (no merge, no keep-richer guard). A sparse probe window (or
the R117.F boot rebuild picking an arbitrary HAR) therefore CLOBBERED a rich
catalog with a sparse one — observed 30→2 testids between gen and run. R203
merges per-route, keeping the richer entry, and never lets a sparser capture
replace a richer route.
"""
from __future__ import annotations

import json
from pathlib import Path

import src.agents.api_discovery as ad
from src.agents.api_discovery import ingest_dom_snapshots, load_dom_catalog


def _write_har_with_sidecar(har_dir: Path, route: str, testids: list[str]):
    har_dir.mkdir(parents=True, exist_ok=True)
    (har_dir / "discovery.har").write_text("{}")
    (har_dir / "dom_0.json").write_text(json.dumps({
        "route": route,
        "elements": [{"testid": t, "tag": "button", "text": t} for t in testids],
    }))


def _seed_existing_catalog(tmp_path, route: str, testids: list[str]):
    d = tmp_path / "pid"
    d.mkdir(parents=True, exist_ok=True)
    (d / "dom_catalog.json").write_text(json.dumps({
        "project_id": "pid",
        "routes": {route: [{"testid": t, "tag": "button", "text": t} for t in testids]},
        "testid_count": len(testids),
    }))


def test_r203_sparse_capture_does_not_clobber_richer(monkeypatch, tmp_path):
    monkeypatch.setattr(ad, "_DOM_CATALOG_DIR", tmp_path)
    # Existing: /x has 30 testids.
    _seed_existing_catalog(tmp_path, "/x", [f"t{i}" for i in range(30)])
    # New sparse capture: /x has only 2.
    har = tmp_path / "har"
    _write_har_with_sidecar(har, "/x", ["t0", "t1"])
    ingest_dom_snapshots("pid", str(har / "discovery.har"))
    cat = load_dom_catalog("pid")
    assert cat["testid_count"] == 30, "sparse capture must NOT clobber the richer catalog"


def test_r203_new_route_is_additive(monkeypatch, tmp_path):
    monkeypatch.setattr(ad, "_DOM_CATALOG_DIR", tmp_path)
    _seed_existing_catalog(tmp_path, "/x", [f"t{i}" for i in range(30)])
    har = tmp_path / "har"
    _write_har_with_sidecar(har, "/y", ["y0", "y1", "y2", "y3", "y4"])
    ingest_dom_snapshots("pid", str(har / "discovery.har"))
    cat = load_dom_catalog("pid")
    assert set(cat["routes"]) == {"/x", "/y"}
    assert cat["testid_count"] == 35


def test_r203_richer_capture_replaces(monkeypatch, tmp_path):
    monkeypatch.setattr(ad, "_DOM_CATALOG_DIR", tmp_path)
    _seed_existing_catalog(tmp_path, "/x", [f"t{i}" for i in range(30)])
    har = tmp_path / "har"
    _write_har_with_sidecar(har, "/x", [f"t{i}" for i in range(40)])
    ingest_dom_snapshots("pid", str(har / "discovery.har"))
    cat = load_dom_catalog("pid")
    assert cat["testid_count"] == 40, "a richer capture SHOULD replace the existing route"


def test_r203_killswitch_restores_destructive(monkeypatch, tmp_path):
    monkeypatch.setattr(ad, "_DOM_CATALOG_DIR", tmp_path)
    monkeypatch.setenv("ARTA_R203_CATALOG_MERGE_DISABLE", "1")
    _seed_existing_catalog(tmp_path, "/x", [f"t{i}" for i in range(30)])
    har = tmp_path / "har"
    _write_har_with_sidecar(har, "/x", ["t0", "t1"])
    ingest_dom_snapshots("pid", str(har / "discovery.har"))
    cat = load_dom_catalog("pid")
    assert cat["testid_count"] == 2, "killswitch must restore legacy destructive overwrite"
