"""R124.D — per-row unique result_id derivation.

ROOT CAUSE fix (not fail-loud patch): pre-R124.D `result_id = f"{run_id}:{tc_id}"`
collided when N items shared the same TC (e.g., 15 Newman items per spec).
MERGE on a colliding id matched existing node + last-write overwrote
earlier rows → run-d52a8c surfaced as "wrote 0 ExecutionResult nodes"
for 3137 results.

Post-R124.D: incorporate (test_id, status, status_code, endpoint_path,
row_idx) into a SHA-1 hash → 3137 results → 3137 distinct Neo4j nodes.
"""
from __future__ import annotations

import hashlib


def _r124_d_result_id(run_id: str, r: dict, row_idx: int) -> str:
    """Mirror of the production derivation in post_run_chain_pipeline.py."""
    _md = r.get("metadata") or {}
    key = "|".join([
        str(r.get("test_id") or r.get("id") or ""),
        str(r.get("status") or ""),
        str(_md.get("status_code") or ""),
        str(_md.get("request_path") or ""),
        str(r.get("automation_tool") or r.get("tool") or ""),
        str(row_idx),
    ])
    return f"{run_id}:{hashlib.sha1(key.encode()).hexdigest()[:16]}"


def test_r124_d_distinct_rows_get_distinct_ids():
    """15 Newman items sharing the same tc_id but different paths → 15 distinct result_ids."""
    rows = [
        {"test_id": "API-req_am_001_api", "status": "FAIL",
         "metadata": {"status_code": 500, "request_path": f"/api/v1/x{i}"},
         "automation_tool": "newman"}
        for i in range(15)
    ]
    ids = {_r124_d_result_id("run-X", r, i) for i, r in enumerate(rows)}
    assert len(ids) == 15, f"expected 15 distinct ids; got {len(ids)}"


def test_r124_d_idempotent_same_row():
    """Same row re-classified (same index, same content) → same result_id."""
    r = {"test_id": "TC-A", "status": "PASS",
         "metadata": {"status_code": 200, "request_path": "/api/x"},
         "automation_tool": "newman"}
    id1 = _r124_d_result_id("run-X", r, 0)
    id2 = _r124_d_result_id("run-X", r, 0)
    assert id1 == id2, "idempotent derivation expected"


def test_r124_d_run_scoped():
    """Different runs → different prefixes → different result_ids."""
    r = {"test_id": "TC-A", "status": "PASS",
         "metadata": {"status_code": 200, "request_path": "/api/x"},
         "automation_tool": "newman"}
    id_a = _r124_d_result_id("run-A", r, 0)
    id_b = _r124_d_result_id("run-B", r, 0)
    assert id_a != id_b
    assert id_a.startswith("run-A:") and id_b.startswith("run-B:")


def test_r124_d_id_length_stable():
    """result_id length is stable + bounded (run_id + ':' + 16-hex = run_id+17 chars)."""
    r = {"test_id": "TC-A" * 100, "status": "FAIL",
         "metadata": {"status_code": 500, "request_path": "/" + "x" * 1000},
         "automation_tool": "newman"}
    id_a = _r124_d_result_id("run-X", r, 999)
    # 'run-X:' (6 chars) + 16-hex = 22 chars total
    assert len(id_a) == 22, f"unstable id length: {len(id_a)} for {id_a!r}"


def test_r124_d_empty_metadata_graceful():
    """Empty/None metadata fields → still produces a valid id (uses empty string in key)."""
    r = {"test_id": "TC-X", "status": "PASS"}  # no metadata
    id_a = _r124_d_result_id("run-X", r, 0)
    assert id_a.startswith("run-X:")
    assert len(id_a) == 22
