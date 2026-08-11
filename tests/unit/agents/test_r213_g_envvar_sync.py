"""R213.G — the discovery harvest→env_block sync must call
`bulk_add_environment_variables(project_id, env_name, body)` with the CANONICAL
3-arg signature and a real BulkAddVariablesBody, so harvested ids actually
overwrite REPLACE_ME placeholders.

Regression: the pre-R213.G caller passed `(project_id=…, body=<dict>)` (missing
env_name, dict not model) → TypeError on every discovery run → harvested ids
never reached the env_block → Newman/k6 path-param items BLOCKED forever.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from src.agents import discovery_executor as DE


def test_sync_calls_handler_with_canonical_signature(monkeypatch):
    captured = {}

    async def _fake_handler(project_id, env_name, body):
        captured["project_id"] = project_id
        captured["env_name"] = env_name
        captured["values"] = dict(getattr(body, "values", None) or {})
        return {"added": list(captured["values"].keys())}

    from src.api.routers import projects as projects_router
    monkeypatch.setattr(projects_router, "bulk_add_environment_variables", _fake_handler)

    asyncio.run(DE._bulk_set_envvars(
        "proj-1", {"collection_id": "abc-123", "container_name": "real-box"},
        har_path=Path("/tmp/x.har"), env_name="staging",
    ))
    assert captured["project_id"] == "proj-1"
    assert captured["env_name"] == "staging"          # env_name threaded (was missing)
    assert captured["values"]["collection_id"] == "abc-123"
    assert captured["values"]["container_name"] == "real-box"


def test_sync_killswitch(monkeypatch):
    called = {"n": 0}

    async def _fake_handler(project_id, env_name, body):
        called["n"] += 1

    from src.api.routers import projects as projects_router
    monkeypatch.setattr(projects_router, "bulk_add_environment_variables", _fake_handler)
    monkeypatch.setenv("ARTA_R213_G_ENVVAR_SYNC_DISABLE", "1")
    asyncio.run(DE._bulk_set_envvars(
        "proj-1", {"collection_id": "abc"}, har_path=Path("/tmp/x.har"), env_name="staging",
    ))
    assert called["n"] == 0


def test_empty_values_is_noop(monkeypatch):
    called = {"n": 0}

    async def _fake_handler(project_id, env_name, body):
        called["n"] += 1

    from src.api.routers import projects as projects_router
    monkeypatch.setattr(projects_router, "bulk_add_environment_variables", _fake_handler)
    asyncio.run(DE._bulk_set_envvars("proj-1", {}, har_path=Path("/tmp/x.har"), env_name="staging"))
    assert called["n"] == 0
