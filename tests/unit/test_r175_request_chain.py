"""R175 — runtime request-chaining for Newman collections."""
from __future__ import annotations

from src.agents.request_chain import (
    apply_request_chaining, is_id_var, _path_var_count, _consumed_id_vars, _HARVEST_SCRIPT,
)


def test_is_id_var():
    assert is_id_var("collection_id") and is_id_var("file_uuid") and is_id_var("id")
    assert not is_id_var("name") and not is_id_var("base_url") and not is_id_var("")


def _item(name, path):
    return {"name": name, "request": {"method": "GET",
            "url": {"raw": "{{base_url}}/" + "/".join(path), "host": ["{{base_url}}"], "path": path}}}


def test_path_var_count_proxies_depth():
    assert _path_var_count(_item("list", ["api", "collections"])) == 0
    assert _path_var_count(_item("detail", ["api", "collections", "{{collection_id}}", "items", "{{item_id}}"])) == 2


def test_consumed_id_vars_detects_resource_ids():
    coll = {"item": [_item("d", ["api", "x", "{{collection_id}}", "y", "{{subscriber_id}}", "z", "{{name}}"])]}
    got = _consumed_id_vars(coll)
    assert "collection_id" in got and "subscriber_id" in got
    assert "name" not in got


def test_apply_chaining_injects_harvest_orders_and_seeds():
    coll = {"item": [
        _item("detail", ["api", "collection", "{{collection_id}}", "modules", "{{module_id}}"]),
        _item("list", ["api", "collection"]),
    ]}
    out, info = apply_request_chaining(coll, already_resolved={"subscriber_id"})
    # harvest event injected at collection level
    assert any(e.get("listen") == "test" and "R175" in " ".join(e["script"]["exec"])
               for e in out["event"])
    # list endpoint (0 path-vars) ordered BEFORE the detail (2 path-vars)
    assert out["item"][0]["name"] == "list"
    # consumed resource-id vars seeded as empty collection variables
    seeded_keys = {v["key"] for v in out["variable"]}
    assert {"collection_id", "module_id"} <= seeded_keys
    assert "collection_id" in info["chained_vars"]


def test_apply_chaining_idempotent_harvest():
    coll = {"item": [_item("a", ["x", "{{collection_id}}"])]}
    out, _ = apply_request_chaining(coll)
    out, _ = apply_request_chaining(out)
    test_events = [e for e in out["event"] if e.get("listen") == "test"
                   and "R175" in " ".join(e["script"]["exec"])]
    assert len(test_events) == 1  # not duplicated on re-apply


def test_already_resolved_not_seeded():
    coll = {"item": [_item("d", ["api", "{{account_id}}", "x", "{{collection_id}}"])]}
    out, info = apply_request_chaining(coll, already_resolved={"account_id"})
    seeded = {v["key"] for v in out.get("variable", [])}
    assert "account_id" not in seeded       # already a real value — don't seed empty
    assert "collection_id" in seeded
