"""R213 A2 — gen-time deterministic path regrounding for the storage/extraction
contract class (the real AM-008 fix). Proves the snapper completes a
hallucinated-but-close path onto the real captured surface so the collection
grounds instead of hitting R55.1 BLOCK."""
from __future__ import annotations

from src.agents.endpoint_grounding import build_grounding_index, reground_collection_paths


def _collection(path_segs):
    return {"item": [{"name": "blob meta", "request": {
        "method": "GET",
        "url": {"raw": "{{base_url}}/" + "/".join(path_segs), "path": path_segs},
    }}]}


def test_storage_path_completion_snaps_to_captured_surface():
    # real captured storage endpoint (the AM-008 family)
    captured = [
        {"method": "GET", "path": "/api/storage/default/blob/{container_name}/metadata"},
        {"method": "GET", "path": "/api/extraction/{account_id}/{subscriber_id}/{schema_id}"},
    ]
    index = build_grounding_index(captured, {})
    # LLM hallucinated a CLOSE path missing the middle `default` segment (same
    # `api` family) — the realistic R174 completion case.
    coll = _collection(["api", "storage", "blob", "{{container_name}}", "metadata"])
    coll, n_rg, n_un = reground_collection_paths(coll, index, {})
    assert n_rg == 1, f"expected the storage path to snap, got rg={n_rg} un={n_un}"
    new_path = coll["item"][0]["request"]["url"]["path"]
    assert new_path == ["api", "storage", "default", "blob", "{{container_name}}", "metadata"]


def test_far_off_path_left_as_is_not_mis_snapped():
    # a semantically-different invented path must NOT be force-snapped (safety:
    # completion-only, never a sibling swap) — it stays for the grounding gate
    # to flag truthfully.
    captured = [{"method": "GET", "path": "/api/storage/default/blob/{container_name}/metadata"}]
    index = build_grounding_index(captured, {})
    coll = _collection(["api", "datasource", "connections"])
    coll, n_rg, n_un = reground_collection_paths(coll, index, {})
    assert n_rg == 0   # not a completion → left as-is (truthful violation downstream)
