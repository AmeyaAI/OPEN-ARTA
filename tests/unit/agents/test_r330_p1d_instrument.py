"""R330 P1d — the measuring instrument itself must be correct.

Covers the two spots the P1 review found broken and untested:
- per-test grounded_by derivation (evidence-preserving; was rebuilt from a
  synthetic {"source": best} that lost the HAR-evidence branch)
- provenance rank/bucket for r212_probe_response_capture (was unlabeled)
- sediment prune + read_traceability aggregation (source_grounding statuses,
  distinct needs_attention_count)
"""
import json

import pytest

from src.agents import traceability_gate as tg
from src.agents.api_discovery import endpoint_provenance
from src.agents.traceability_gate import derive_grounded_by, prune_traceability, read_traceability


# ── endpoint_provenance / rank ───────────────────────────────────────────────

def test_r212_probe_capture_is_observed():
    assert endpoint_provenance({"source": "r212_probe_response_capture"}) == "observed"


def test_har_evidence_without_source_is_observed():
    assert endpoint_provenance({"source_har": "run-x.har"}) == "observed"
    assert endpoint_provenance({"discovered_at": "2026-08-01"}) == "observed"


# ── derive_grounded_by ───────────────────────────────────────────────────────

CAP = {
    "GET:/api/a": {"source": "openapi", "path": "/api/a"},
    "GET:/api/b": {"source": "network", "path": "/api/b"},
    # evidence-only: no `source`, but real HAR capture stamps — the old synthetic
    # rebuild classified this as guess; it IS observed traffic.
    "GET:/api/c": {"source_har": "run-y.har", "path": "/api/c"},
    "GET:/api/d": {"path": "/api/d"},  # truly unlabeled
}


def test_ui_when_no_endpoints():
    assert derive_grounded_by(0, [], CAP) == "ui"


def test_guess_when_no_matched_keys():
    assert derive_grounded_by(3, [], CAP) == "guess"


def test_strongest_provenance_wins():
    assert derive_grounded_by(2, ["GET:/api/a", "GET:/api/b"], CAP) == "source_grounded"


def test_evidence_only_endpoint_counts_as_observed():
    assert derive_grounded_by(1, ["GET:/api/c"], CAP) == "observed"


def test_unlabeled_only_maps_to_guess():
    assert derive_grounded_by(1, ["GET:/api/d"], CAP) == "guess"


def test_unknown_key_is_guess():
    assert derive_grounded_by(1, ["GET:/api/never-captured"], CAP) == "guess"


# ── prune + aggregation ──────────────────────────────────────────────────────

def _write_row(root, pid, test_id, req_id, **extra):
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{test_id}.json").write_text(json.dumps(
        {"test_id": test_id, "req_id": req_id, **extra}))


@pytest.fixture()
def trace_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "_TRACE_DIR", tmp_path)
    return tmp_path


def test_prune_removes_only_this_requirements_stale_rows(trace_dir):
    _write_row(trace_dir, "p1", "T-1", "REQ-1", traceable=True)
    _write_row(trace_dir, "p1", "T-stale", "REQ-1", traceable=True)
    _write_row(trace_dir, "p1", "T-other-req", "REQ-2", traceable=True)
    removed = prune_traceability("p1", "REQ-1", {"T-1"})
    assert removed == 1
    kept = {json.loads(p.read_text())["test_id"] for p in (trace_dir / "p1").glob("*.json")}
    assert kept == {"T-1", "T-other-req"}


def test_prune_killswitch(trace_dir, monkeypatch):
    monkeypatch.setenv("ARTA_TRACE_PRUNE_DISABLE", "1")
    _write_row(trace_dir, "p1", "T-stale", "REQ-1", traceable=True)
    assert prune_traceability("p1", "REQ-1", set()) == 0


def test_read_traceability_aggregates_p1d_fields(trace_dir):
    # guess AND untraceable — must count ONCE in needs_attention (old sum = 2)
    _write_row(trace_dir, "p1", "T-1", "REQ-1", traceable=False, grounded_by="guess",
               source_grounding="unavailable:no_github_token")
    _write_row(trace_dir, "p1", "T-2", "REQ-1", traceable=True, grounded_by="observed")
    out = read_traceability("p1")
    assert out["test_count"] == 2
    assert out["needs_attention_count"] == 1
    assert out["grounded_by"] == {"guess": 1, "observed": 1}
    assert out["source_grounding"] == {"unavailable:no_github_token": 1}
