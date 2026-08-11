"""H3 (R218) — the discover_endpoints in-memory cache is now TTL-bounded. Pre-H3
it was returned for the whole process lifetime (only a manual .clear()), so a
long-running process served a stale discovery surface after re-discovery.
"""
from __future__ import annotations

import time

from src.agents import api_discovery as ad


def _reset():
    ad._ENDPOINT_CACHE.clear()
    ad._ENDPOINT_CACHE_TS.clear()


def test_h3_miss_when_uncached(monkeypatch):
    _reset()
    assert ad._endpoint_cache_fresh("pX") is False


def test_h3_fresh_after_set(monkeypatch):
    _reset()
    monkeypatch.setenv("ARTA_ENDPOINT_CACHE_TTL_S", "600")
    ad._endpoint_cache_set("pX", [{"path": "/a"}])
    assert ad._endpoint_cache_fresh("pX") is True


def test_h3_stale_after_ttl(monkeypatch):
    _reset()
    monkeypatch.setenv("ARTA_ENDPOINT_CACHE_TTL_S", "600")
    ad._endpoint_cache_set("pX", [{"path": "/a"}])
    # Simulate the entry being older than the TTL.
    ad._ENDPOINT_CACHE_TS["pX"] = time.monotonic() - 700
    assert ad._endpoint_cache_fresh("pX") is False


def test_h3_ttl_zero_caches_forever(monkeypatch):
    _reset()
    monkeypatch.setenv("ARTA_ENDPOINT_CACHE_TTL_S", "0")
    ad._endpoint_cache_set("pX", [{"path": "/a"}])
    ad._ENDPOINT_CACHE_TS["pX"] = time.monotonic() - 99999
    assert ad._endpoint_cache_fresh("pX") is True  # legacy: never expires
