"""POST /api/admin/generate-grpc-tests — thin operator trigger over the
deterministic gRPC gen chain. Direct-call unit test (no HTTP/network)."""
import asyncio

import src.agents.grpc_stub_gen as gsg
from src.api.main import generate_grpc_tests_endpoint


def test_endpoint_resolves_project_and_returns_result(monkeypatch):
    captured = {}

    async def _fake_gen(project, **kw):
        captured["project"] = project
        return {"proto_count": 3, "read_tests": 1, "errors": []}

    monkeypatch.setattr(gsg, "generate_project_grpc_tests", _fake_gen)
    from src.api.routers import projects as _projects
    monkeypatch.setitem(_projects._PROJECTS, "pid",
                        {"id": "pid", "integrations": {"repositories": [{"repo": "O/r"}]}})

    out = asyncio.run(generate_grpc_tests_endpoint("pid"))
    assert out["read_tests"] == 1 and out["proto_count"] == 3
    # the configured project dict (with integrations) was passed through
    assert captured["project"]["integrations"]["repositories"] == [{"repo": "O/r"}]


def test_endpoint_unknown_project_falls_back(monkeypatch):
    async def _fake_gen(project, **kw):
        return {"proto_count": 0, "read_tests": 0, "errors": ["no_proto_reachable"],
                "_pid": project.get("id")}

    monkeypatch.setattr(gsg, "generate_project_grpc_tests", _fake_gen)
    out = asyncio.run(generate_grpc_tests_endpoint("no-such"))
    assert out["proto_count"] == 0 and out["_pid"] == "no-such"   # {"id": pid} fallback
