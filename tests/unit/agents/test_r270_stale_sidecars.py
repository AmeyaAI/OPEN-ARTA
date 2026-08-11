"""R270 — DOM sidecars are per-run scratch, not immortal history.

`ingest_dom_snapshots` globs every `dom*.json` in the HAR dir with no notion of
age, and nothing ever deleted them. When R180 skips a route it writes no
sidecar -- so the PREVIOUS run's sidecar survived and was re-ingested as if
freshly captured. Measured live:

    dom_portal.json                          08:41   <- this run
    dom_portal_remote_AlarmReeferReport.json 02:34   <- SIX HOURS stale,
                                                        still feeding 'Sign In'

This also made R268 structurally UNABLE to fire: its "no fresh capture" test is
`route not in routes`, but the stale sidecar kept putting the route IN routes.
Login chrome captured once was immortal, and every later run re-ingested it.

The durable store is dom_catalog.json (R203's merge still protects good history);
only the scratch is cleared.
"""
import json
from pathlib import Path

from src.agents.api_discovery import ingest_dom_snapshots

PROBE = Path("src/automation/playwright/discovery_probe.spec.ts")

LOGIN_CHROME = [
    {"tag": "form", "role": "form", "text": "Sign In"},
    {"tag": "input", "role": "textbox", "text": "E-mail"},
]
REAL = [{"tag": "button", "role": "button", "text": "Lease Management"}]


# ── probe-side: sidecars cleared at start ───────────────────────────────────

def test_probe_clears_stale_sidecars_before_walking():
    ts = PROBE.read_text()
    assert "R270" in ts
    assert "fs.unlinkSync(path.join(HAR_DIR, f))" in ts
    assert "/^dom.*\\.json$/.test(f)" in ts


def test_probe_never_deletes_the_durable_catalog():
    """dom_catalog.json lives in the same dir and is the DURABLE store —
    deleting it would throw away all history the merge depends on."""
    ts = PROBE.read_text()
    assert "f !== 'dom_catalog.json'" in ts


def test_probe_clean_has_killswitch():
    assert "ARTA_R270_SIDECAR_CLEAN_DISABLE" in PROBE.read_text()


def test_clean_happens_before_the_walk():
    """Clearing AFTER the walk would delete this run's own captures."""
    ts = PROBE.read_text()
    assert ts.index("R270: cleared") < ts.index("const sidecarPath = path.join(HAR_DIR,")


# ── the eviction R270 unblocks (R268 can finally fire) ──────────────────────

def test_stale_sidecar_no_longer_resurrects_login_chrome(tmp_path, monkeypatch):
    """End-to-end of the bug: with the stale sidecar GONE (as R270 now
    guarantees), a skipped route has no fresh capture, so R268 evicts the
    catalog's login chrome instead of preserving it forever."""
    import src.agents.api_discovery as ad
    monkeypatch.setattr(ad, "_DOM_CATALOG_DIR", tmp_path / "cat")
    (tmp_path / "cat" / "p").mkdir(parents=True)
    (tmp_path / "cat" / "p" / "dom_catalog.json").write_text(json.dumps(
        {"routes": {"/portal/remote/X": LOGIN_CHROME}}))

    har = tmp_path / "har" / "discovery.har"
    har.parent.mkdir(parents=True)
    # R270 cleared the stale sidecar, so only THIS run's capture exists:
    (har.parent / "dom_portal.json").write_text(
        json.dumps({"route": "/portal", "elements": REAL}))

    cat = ingest_dom_snapshots("p", har)
    assert "/portal/remote/X" not in cat["routes"], \
        "stale login chrome must not survive when no fresh capture exists"
    assert "/portal" in cat["routes"]


def test_without_r270_the_stale_sidecar_would_win(tmp_path, monkeypatch):
    """Documents the exact pre-R270 failure: the old sidecar still on disk is
    re-ingested as fresh, so R268's `route not in routes` never fires."""
    import src.agents.api_discovery as ad
    monkeypatch.setattr(ad, "_DOM_CATALOG_DIR", tmp_path / "cat")
    (tmp_path / "cat" / "p").mkdir(parents=True)
    (tmp_path / "cat" / "p" / "dom_catalog.json").write_text(json.dumps({"routes": {}}))

    har = tmp_path / "har" / "discovery.har"
    har.parent.mkdir(parents=True)
    # the stale sidecar R270 now deletes:
    (har.parent / "dom_portal_remote_X.json").write_text(
        json.dumps({"route": "/portal/remote/X", "elements": LOGIN_CHROME}))

    cat = ingest_dom_snapshots("p", har)
    # it IS ingested (that is the bug) — pinned so the behaviour is explicit
    assert "/portal/remote/X" in cat["routes"]
