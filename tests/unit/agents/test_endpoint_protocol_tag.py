"""Durable per-endpoint protocol tagging at _load_captured_endpoints — so the
SSE/gRPC/GraphQL classification reaches GENERATION (previously it lived only in
the staleable architecture api_graph, so gen read protocol=None → defaulted to
REST and the non-REST gen paths never fired)."""
import json

import src.agents.api_discovery as ad


def _load(tmp_path, monkeypatch, endpoints):
    monkeypatch.setattr(ad, "_CAPTURED_DIR", tmp_path)
    (tmp_path / "pid.json").write_text(json.dumps(endpoints))
    return ad._load_captured_endpoints("pid")


def test_sse_endpoint_gets_protocol_tag(tmp_path, monkeypatch):
    eps = _load(tmp_path, monkeypatch, [
        {"method": "GET", "path": "/api/v1/chat/event/response-stream",
         "evidence_count": 3, "source": "har"},
        {"method": "GET", "path": "/api/v1/chat/event/query",
         "evidence_count": 3, "source": "har"},
    ])
    by = {e["path"]: e for e in eps}
    # the streaming endpoint is tagged sse; its sibling REST /event/ route is not
    assert by["/api/v1/chat/event/response-stream"]["protocol"] == "sse"
    assert by["/api/v1/chat/event/query"]["protocol"] == "rest"


def test_existing_protocol_not_overridden(tmp_path, monkeypatch):
    eps = _load(tmp_path, monkeypatch, [
        {"method": "POST", "path": "/graphql", "protocol": "graphql",
         "evidence_count": 3, "source": "har"},
    ])
    assert eps[0]["protocol"] == "graphql"   # pre-set tag preserved


def test_tag_killswitch(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTA_ENDPOINT_PROTOCOL_TAG_DISABLE", "1")
    eps = _load(tmp_path, monkeypatch, [
        {"method": "GET", "path": "/api/v1/chat/event/response-stream",
         "evidence_count": 3, "source": "har"},
    ])
    assert "protocol" not in eps[0]
