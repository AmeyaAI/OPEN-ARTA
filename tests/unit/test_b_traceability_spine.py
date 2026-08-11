"""Phase 3 (charter conformance) — traceability spine walkable (minimal).

The charter's Requirement→AC→…→Endpoint half was written to Neo4j but NOT traversed by
the read query, and a space-vs-colon Endpoint-key split blocked the arch-graph↔spine
join. B1 adds a canonical key helper; B2 extends the read-path _CHAIN_EDGES to walk the
already-written IMPLEMENTED_BY/EXERCISES/INVOKES/AC-TESTED_BY edges; B3 persists api_graph
Endpoint nodes on the normalized (colon) key so arch-discovery joins the same nodes.
"""
from __future__ import annotations

import pytest

from src.graph import writer
from src.graph.writer import endpoint_key, normalize_endpoint_key
from src.api.routers import traceability as tr


# ── B1 ──────────────────────────────────────────────────────────────────────────

def test_b1_endpoint_key_colon():
    assert endpoint_key("post", "/api/x") == "POST:/api/x"
    assert endpoint_key(None, None) == "GET:/"


def test_b1_normalize_space_to_colon_idempotent():
    assert normalize_endpoint_key("POST /api/x") == "POST:/api/x"     # space -> colon
    assert normalize_endpoint_key("POST:/api/x") == "POST:/api/x"     # already colon
    assert normalize_endpoint_key(normalize_endpoint_key("GET /y")) == "GET:/y"  # idempotent
    assert normalize_endpoint_key("") == ""


# ── B2 ──────────────────────────────────────────────────────────────────────────

def test_b2_chain_edges_extension_present_by_default():
    rels = {e[2] for e in tr._CHAIN_EDGES}
    for r in ("IMPLEMENTED_BY", "EXERCISES", "INVOKES", "TESTED_BY"):
        assert r in rels, f"{r} missing from _CHAIN_EDGES"


def test_b2_extension_targets_endpoint_and_ac():
    ext = {(e[0], e[2], e[3]) for e in tr._CHAIN_EDGES_SPINE_EXT}
    assert ("Requirement", "IMPLEMENTED_BY", "Endpoint") in ext
    assert ("TestCase", "EXERCISES", "Endpoint") in ext
    assert ("FrontendRoute", "INVOKES", "Endpoint") in ext
    assert ("AcceptanceCriteria", "TESTED_BY", "TestCase") in ext
    # base list does NOT already contain them (the extension is what adds them)
    base_rels = {e[2] for e in tr._CHAIN_EDGES_BASE}
    assert "EXERCISES" not in base_rels and "IMPLEMENTED_BY" not in base_rels


# ── B3 (fake driver) ────────────────────────────────────────────────────────────

class _FakeSession:
    def __init__(self, sink):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def run(self, query, params=None):
        self._sink.append((query, params or {}))


class _FakeDriver:
    def __init__(self):
        self.queries: list = []

    def session(self):
        return _FakeSession(self.queries)


@pytest.mark.asyncio
async def test_b3_api_graph_endpoint_merges_on_colon_key():
    """A disk api_graph node keyed 'GET /api/x' (space) MERGEs Endpoint on 'GET:/api/x'."""
    d = _FakeDriver()
    graphs = {"api_graph": {"nodes": [
        {"kind": "endpoint", "id": "GET /api/x", "method": "GET",
         "path": "/api/x", "protocol": "rest"},
        {"kind": "endpoint", "id": "POST /soap/svc?wsdl", "method": "POST",
         "path": "/soap/svc", "protocol": "soap"},
    ]}}
    await writer.upsert_architecture_graphs(d, project_id="p1", graphs=graphs)
    endpoint_writes = [(q, p) for q, p in d.queries
                       if "MERGE (e:Endpoint {endpoint_key: $ek})" in q]
    assert len(endpoint_writes) == 2
    eks = {p["ek"] for _, p in endpoint_writes}
    assert eks == {"GET:/api/x", "POST:/soap/svc?wsdl"}, f"got {eks}"
    # protocol metadata carried through
    protos = {p["ek"]: p["protocol"] for _, p in endpoint_writes}
    assert protos["POST:/soap/svc?wsdl"] == "soap"


@pytest.mark.asyncio
async def test_b3_noop_on_none_driver():
    await writer.upsert_architecture_graphs(None, project_id="p", graphs={"api_graph": {}})  # no raise


# ── read-path traversal of the new EXERCISES edge ───────────────────────────────

class _ReadResult:
    def __init__(self, rows):
        self._rows = rows

    async def data(self):
        return self._rows


class _ReadSession:
    def __init__(self, rowmap):
        self._rowmap = rowmap

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def run(self, q, params=None):
        for marker, rows in self._rowmap.items():
            if marker in q:
                return _ReadResult(rows)
        return _ReadResult([])


class _ReadDriver:
    def __init__(self, rowmap):
        self._rowmap = rowmap

    def session(self):
        return _ReadSession(self._rowmap)


@pytest.mark.asyncio
async def test_full_chain_graph_walks_exercises_to_endpoint():
    """_full_chain_graph now returns an `endpoint` node + `exercises` edge (previously
    unreachable — the edge was written but never traversed)."""
    rowmap = {
        "EXERCISES": [{"sid": "TEST-AM-1", "tid": "GET:/api/x",
                       "ap": {"test_id": "TEST-AM-1"},
                       "bp": {"method": "GET", "path_template": "/api/x"}}],
    }
    out = await tr._full_chain_graph(_ReadDriver(rowmap), "p1", "run1")
    types = {n["type"] for n in out["nodes"]}
    assert "endpoint" in types and "test_case" in types
    assert any(e["type"] == "exercises" for e in out["edges"])
    ep = next(n for n in out["nodes"] if n["type"] == "endpoint")
    assert ep["label"] == "GET /api/x"  # friendly endpoint label
