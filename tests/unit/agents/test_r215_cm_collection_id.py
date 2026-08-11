"""R215 Item 3 — cm collection_id capture. The cm list response puts the
collection id as a UUID-shaped KEY ({"<uuid>": [...]}), invisible to standard
dataflow/jsonpath. _walk_leaves emits it at a sentinel jsonpath; the Newman
capture template scans for the (dynamic) UUID key at runtime."""
from __future__ import annotations

from src.agents.call_chain import _walk_leaves, _R215_is_uuid_key
from src.agents.chain_aware_newman import _TEST_TEMPLATE

_RESP = {"Collection": "x", "last_evaluated_key": None,
         "16dc887c-1259-4bba-d322-b5bcfd7aca56": [{"payload": {"id": "abc"}}]}


def test_uuid_key_detection():
    assert _R215_is_uuid_key("16dc887c-1259-4bba-d322-b5bcfd7aca56")
    assert not _R215_is_uuid_key("Collection")
    assert not _R215_is_uuid_key("last_evaluated_key")
    assert not _R215_is_uuid_key("published_collections_detail")
    assert not _R215_is_uuid_key("not-a-uuid")


def test_walk_leaves_emits_collection_key_sentinel():
    leaves = _walk_leaves(_RESP, "$")
    sentinel = [(p, v) for p, v in leaves if p == "$.__cm_collection_key__"]
    assert sentinel == [("$.__cm_collection_key__", "16dc887c-1259-4bba-d322-b5bcfd7aca56")]


def test_walk_leaves_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R215_CM_COLLECTION_ID_DISABLE", "1")
    leaves = _walk_leaves(_RESP, "$")
    assert not any(p == "$.__cm_collection_key__" for p, _ in leaves)


def test_newman_template_has_cm_key_scan():
    # the capture template must scan Object.keys for a UUID key on the sentinel
    assert "__cm_collection_key__" in _TEST_TEMPLATE
    assert "Object.keys(_body)" in _TEST_TEMPLATE


def test_non_cm_response_no_sentinel():
    # a normal response (no UUID-keyed list) emits no sentinel
    leaves = _walk_leaves({"data": [{"id": "x"}], "total": 5}, "$")
    assert not any(p == "$.__cm_collection_key__" for p, _ in leaves)
