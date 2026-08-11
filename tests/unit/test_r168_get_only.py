"""R168 — read-only (GET) Newman contract suite + R154 parity.

R142.B contract gen emits ALL methods; pre-R168 mutating POST/PUT/DELETE ran
with synthetic bodies → 500s masquerading as SUT bugs, and bypassed the
Playwright-only R154 gate. R168 drops non-GET at dispatch (BLOCKED) unless
destructive testing is explicitly enabled.
"""
from __future__ import annotations

import os
import pytest

from src.api.routers.execution import (
    _r168_partition_get_only,
    _r154_newman_destructive_allowed,
)


def _item(name, method, path):
    return {"name": name, "request": {"method": method,
            "url": {"path": path.strip("/").split("/")}}}


def test_partition_keeps_get_blocks_mutations():
    coll = {"info": {"name": "c"}, "item": [
        _item("list", "GET", "/api/collection/list"),
        _item("create", "POST", "/api/collection"),
        _item("update", "PUT", "/api/collection/1"),
        _item("remove", "DELETE", "/api/collection/1"),
        _item("head", "HEAD", "/api/collection/ping"),
    ]}
    get_coll, blocked = _r168_partition_get_only(coll)
    kept = [i["name"] for i in get_coll["item"]]
    assert set(kept) == {"list", "head"}            # GET + HEAD kept
    blocked_methods = sorted(m for _, m, _ in blocked)
    assert blocked_methods == ["DELETE", "POST", "PUT"]


def test_r217_blocks_get_on_action_endpoints(monkeypatch):
    """R217 — a GET on an ACTION path (/publish, /generate-upload-url) is a
    mutation route exposed as a path; GET-ing it 500s + reports no SUT quality.
    Block it as GET-ACTION instead of dispatching (live: 17/50 newman FAILs)."""
    monkeypatch.delenv("ARTA_R217_R168_ACTION_FILTER_DISABLE", raising=False)
    coll = {"item": [
        _item("read-list", "GET", "/api/collection/list"),
        _item("publish", "GET", "/api/collection/fieldset/publish"),
        _item("gen-url", "GET", "/api/media/generate-upload-url"),
        _item("versions", "GET", "/api/storage/installer/versions"),
    ]}
    get_coll, blocked = _r168_partition_get_only(coll)
    kept = {i["name"] for i in get_coll["item"]}
    assert kept == {"read-list", "versions"}              # genuine reads kept
    blocked_names = {n for n, m, _ in blocked if m == "GET-ACTION"}
    assert blocked_names == {"publish", "gen-url"}         # action GETs blocked


def test_r217_action_filter_killswitch(monkeypatch):
    """Killswitch reverts to pre-R217 (GET-on-action kept)."""
    monkeypatch.setenv("ARTA_R217_R168_ACTION_FILTER_DISABLE", "1")
    coll = {"item": [_item("publish", "GET", "/api/collection/fieldset/publish")]}
    get_coll, blocked = _r168_partition_get_only(coll)
    assert [i["name"] for i in get_coll["item"]] == ["publish"]
    assert not [m for _, m, _ in blocked if m == "GET-ACTION"]


def _item_raw(name, method, raw):
    """item with an explicit raw URL (to test unresolved-template detection)."""
    return {"name": name, "request": {"method": method, "url": {"raw": raw,
            "path": [s for s in raw.split("/") if s]}}}


def test_r217_blocks_unresolved_template_and_synthetic(monkeypatch):
    """R217 — block GETs whose URL still has an unresolved single-brace template
    ({account_id} / %7Baccount_id%7D) or an arta-synthetic placeholder; KEEP
    legitimate {{newman_vars}} (the dispatcher resolves those)."""
    monkeypatch.delenv("ARTA_R217_UNRESOLVED_BLOCK_DISABLE", raising=False)
    monkeypatch.delenv("ARTA_R217_R168_ACTION_FILTER_DISABLE", raising=False)
    coll = {"item": [
        _item_raw("ok-newman-var", "GET", "{{base_url}}/{{account_id}}/api/collection/x"),
        _item_raw("single-brace", "GET", "{{base_url}}/{account_id}/api/collection/x"),
        _item_raw("url-encoded", "GET", "{{base_url}}/%7Baccount_id%7D/api/collection/x"),
        _item_raw("synthetic", "GET", "{{base_url}}/api/storage/blob/arta-synthetic-container"),
    ]}
    get_coll, blocked = _r168_partition_get_only(coll)
    kept = {i["name"] for i in get_coll["item"]}
    assert kept == {"ok-newman-var"}                      # only the resolvable one
    blocked_unres = {n for n, m, _ in blocked if m == "UNRESOLVED-PARAM"}
    assert blocked_unres == {"single-brace", "url-encoded", "synthetic"}


def test_r217_unresolved_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R217_UNRESOLVED_BLOCK_DISABLE", "1")
    coll = {"item": [_item_raw("single-brace", "GET", "{{base_url}}/{account_id}/x")]}
    get_coll, blocked = _r168_partition_get_only(coll)
    assert [i["name"] for i in get_coll["item"]] == ["single-brace"]


def test_partition_recurses_folders():
    coll = {"item": [
        {"name": "folder", "item": [
            _item("g", "GET", "/a"),
            _item("p", "POST", "/a"),
        ]},
        _item("top", "GET", "/b"),
    ]}
    get_coll, blocked = _r168_partition_get_only(coll)
    # folder retained with only its GET child; POST surfaced as blocked
    assert len(blocked) == 1 and blocked[0][1] == "POST"
    folder = next(i for i in get_coll["item"] if i.get("name") == "folder")
    assert [c["name"] for c in folder["item"]] == ["g"]


def test_partition_all_get_no_blocks():
    coll = {"item": [_item("a", "GET", "/a"), _item("b", "GET", "/b")]}
    get_coll, blocked = _r168_partition_get_only(coll)
    assert blocked == [] and len(get_coll["item"]) == 2


def test_destructive_gate_requires_both_env(monkeypatch):
    monkeypatch.delenv("ARTA_R154_ALLOW_DESTRUCTIVE_TESTS", raising=False)
    monkeypatch.delenv("SUT_TEST_DATA_NAMESPACE", raising=False)
    assert _r154_newman_destructive_allowed() is False
    monkeypatch.setenv("ARTA_R154_ALLOW_DESTRUCTIVE_TESTS", "1")
    assert _r154_newman_destructive_allowed() is False  # namespace still missing
    monkeypatch.setenv("SUT_TEST_DATA_NAMESPACE", "arta-sandbox-x")
    assert _r154_newman_destructive_allowed() is True
