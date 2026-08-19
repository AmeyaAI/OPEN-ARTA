"""grounding_coverage.protocols — surfaces the SUT's non-REST surface (durable
per-endpoint protocol tag + discovered gRPC surface) so the multi-protocol
understanding is observable in the SUT-Understanding panel."""
import asyncio
import json

import src.agents.api_discovery as ad
import src.agents.grpc_stub_gen as gsg
from src.api.routers.discovery import grounding_coverage


def _seed_captured(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_CAPTURED_DIR", tmp_path)
    (tmp_path / "pid.json").write_text(json.dumps([
        {"method": "GET", "path": "/api/v1/chat/event/response-stream",
         "evidence_count": 3, "source": "har"},
        {"method": "GET", "path": "/api/v1/users", "evidence_count": 3, "source": "har"},
        {"method": "POST", "path": "/api/v1/orders", "evidence_count": 3, "source": "har"},
    ]))
    # a project with a token so the endpoint doesn't fall through to the DB path
    from src.api.routers import projects as _projects
    monkeypatch.setitem(_projects._PROJECTS, "pid",
                        {"id": "pid", "integrations": {"github_token": "t"}})


def test_protocols_reports_sse_and_rest(tmp_path, monkeypatch):
    _seed_captured(tmp_path, monkeypatch)
    out = asyncio.run(grounding_coverage("pid"))
    p = out["protocols"]
    assert p["by_protocol"]["sse"] == 1                # the streaming endpoint
    assert p["by_protocol"]["rest"] == 2               # /users + /orders
    assert p["non_rest"] == 1
    assert "grpc_services" not in p                     # no gRPC surface persisted


def test_protocols_includes_grpc_surface(tmp_path, monkeypatch):
    _seed_captured(tmp_path, monkeypatch)
    monkeypatch.setattr(gsg, "_GRPC_SURFACE_DIR", tmp_path / "grpc")
    gsg.persist_grpc_surface("pid", gsg.build_grpc_surface([{
        "path": "auth.proto",
        "text": "service AuthService { rpc GetToken (R) returns (T); }\n"}]))
    out = asyncio.run(grounding_coverage("pid"))
    assert out["protocols"]["grpc_services"] == 1
    assert out["protocols"]["grpc_methods"] == 1


def test_protocols_killswitch(tmp_path, monkeypatch):
    _seed_captured(tmp_path, monkeypatch)
    monkeypatch.setenv("ARTA_PROTOCOL_SUMMARY_DISABLE", "1")
    assert asyncio.run(grounding_coverage("pid"))["protocols"] == {}
