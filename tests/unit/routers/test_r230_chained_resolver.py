"""R330.CR — chained-resource param resolver. A nested id whose LIST endpoint
has a parent placeholder (`/collections/{collection_id}/items`) must resolve by
substituting the parent the flat pass already found — turning a truthful BLOCK
into a real value (BLOCK→PASS)."""
import httpx
import pytest

from src.api.routers.execution import _r230_probe


class _Resp:
    def __init__(self, payload): self._p = payload; self.status_code = 200
    def json(self): return self._p


def _fake_get_factory(routes):
    def _get(url, headers=None, timeout=None, verify=None):
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                return _Resp(payload)
        return _Resp([])
    return _get


def test_chained_resolves_nested_id(monkeypatch):
    routes = {
        "/collections": [{"collection_id": "c-1"}],
        "/collections/c-1/items": [{"collection_item_id": "i-9"}],
    }
    monkeypatch.setattr(httpx, "get", _fake_get_factory(routes))
    eps = [
        {"method": "GET", "path": "/collections"},
        {"method": "GET", "path": "/collections/{collection_id}/items"},
    ]
    out: dict = {}
    _r230_probe(eps, {"collection_id", "collection_item_id"}, out,
                "https://sut.example", {})
    assert out.get("collection_id") == "c-1"          # flat pass
    assert out.get("collection_item_id") == "i-9"     # chained pass (was structurally BLOCKED)


def test_chained_falls_back_to_block_when_parent_unresolved(monkeypatch):
    # No /collections list → parent collection_id never resolves → the nested id
    # must NOT be fabricated; it stays unresolved (→ truthful BLOCK downstream).
    routes = {"/collections/c-1/items": [{"collection_item_id": "i-9"}]}
    monkeypatch.setattr(httpx, "get", _fake_get_factory(routes))
    eps = [{"method": "GET", "path": "/collections/{collection_id}/items"}]
    out: dict = {}
    _r230_probe(eps, {"collection_id", "collection_item_id"}, out,
                "https://sut.example", {})
    assert "collection_item_id" not in out
    assert "collection_id" not in out


def test_chained_killswitch(monkeypatch):
    routes = {
        "/collections": [{"collection_id": "c-1"}],
        "/collections/c-1/items": [{"collection_item_id": "i-9"}],
    }
    monkeypatch.setattr(httpx, "get", _fake_get_factory(routes))
    monkeypatch.setenv("ARTA_R330_CHAIN_RESOLVE_DISABLE", "1")
    eps = [
        {"method": "GET", "path": "/collections"},
        {"method": "GET", "path": "/collections/{collection_id}/items"},
    ]
    out: dict = {}
    _r230_probe(eps, {"collection_id", "collection_item_id"}, out,
                "https://sut.example", {})
    assert out.get("collection_id") == "c-1"          # flat pass still runs
    assert "collection_item_id" not in out            # chained pass disabled
