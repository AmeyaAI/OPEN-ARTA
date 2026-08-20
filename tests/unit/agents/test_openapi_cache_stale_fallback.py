"""R67.A unblock — a TTL-stale but VALID OpenAPI cache must not be discarded when
the live re-fetch fails (SUTs whose spec is source-derived, not served at a live
swagger path). Discarding it made Newman gen refuse (openapi_cache_empty) despite
real SUT understanding sitting on disk."""
import json
import os
import time

import pytest

from src.agents import openapi_cache as oc


REAL_SPEC = {"openapi": "3.0.0", "paths": {
    "/api/x/{id}": {"get": {"parameters": [{"name": "id", "in": "path"}]}}}}


def _write(tmp_path, monkeypatch, spec, age_sec):
    monkeypatch.setattr(oc, "_CACHE_DIR", tmp_path)
    p = tmp_path / "pid1.json"
    p.write_text(json.dumps(spec))
    old = time.time() - age_sec
    os.utime(p, (old, old))
    return p


def test_read_cache_ttl_hides_stale_but_allow_stale_returns_it(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, REAL_SPEC, age_sec=oc._TTL_SEC + 3600)  # 1h past TTL
    assert oc._read_cache("pid1") is None                    # TTL hides it
    assert oc._read_cache("pid1", allow_stale=True) == REAL_SPEC  # explicit stale read


def test_allow_stale_still_rejects_all_stub_doc(tmp_path, monkeypatch):
    stub = {"paths": {"/a": {"get": {"x-arta-stub": True}}}}
    _write(tmp_path, monkeypatch, stub, age_sec=oc._TTL_SEC + 3600)
    assert oc._read_cache("pid1", allow_stale=True) is None  # all-stub → miss even stale


@pytest.mark.asyncio
async def test_fetch_falls_back_to_stale_when_live_probe_fails(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, REAL_SPEC, age_sec=oc._TTL_SEC + 3600)
    # base_url with no reachable swagger path → every live probe fails →
    # must serve the stale on-disk cache instead of None (which would refuse gen).
    got = await oc.fetch_openapi("https://sut.invalid.example", "pid1")
    assert got == REAL_SPEC
