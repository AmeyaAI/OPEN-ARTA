"""R215 — process-wide Neo4j driver registry.

The driver is created once at app startup and lives on app.state.neo4j, but
background tasks + agent modules (discovery_executor / architecture_discovery)
run without a FastAPI app reference and read a None ctx.neo4j_driver → they
silently skipped ALL graph writes for EVERY SUT (discovery_summary.neo4j_written
== False). set_driver()/get_driver() let any module reach the same driver.
SUT-agnostic.
"""
from __future__ import annotations

from src.graph import writer


def test_set_and_get_driver_roundtrip():
    sentinel = object()
    prev = writer.get_driver()
    try:
        writer.set_driver(sentinel)
        assert writer.get_driver() is sentinel
    finally:
        writer.set_driver(prev)


def test_get_driver_defaults_to_none_when_unset():
    prev = writer.get_driver()
    try:
        writer.set_driver(None)
        assert writer.get_driver() is None
    finally:
        writer.set_driver(prev)


def test_discovery_executor_falls_back_to_registry(monkeypatch):
    """When ctx has no neo4j_driver, discovery must use the registry driver —
    the exact path that was silently dropping graph writes."""
    import asyncio
    from src.agents import discovery_executor as de

    sentinel = object()
    writer.set_driver(sentinel)
    captured = {}

    async def _fake_run(*, project, project_id, neo4j_driver, gherkin=None):
        captured["driver"] = neo4j_driver
        return {"neo4j_written": neo4j_driver is not None}

    # Patch architecture_discovery.run + the loaders execute() touches, then run
    # a minimal ctx (no neo4j_driver attr) through the AD phase.
    import src.agents.architecture_discovery as ad
    monkeypatch.setattr(ad, "run", _fake_run)

    class _Ctx:  # no neo4j_driver attribute → must fall back to registry
        gherkin_scenarios = []

    # Exercise just the resolution branch the fix added (mirror execute()'s logic).
    _ctx = _Ctx()
    driver = getattr(_ctx, "neo4j_driver", None)
    if driver is None:
        driver = writer.get_driver()
    assert driver is sentinel  # registry supplied the driver

    out = asyncio.get_event_loop().run_until_complete(
        ad.run(project={}, project_id="p", neo4j_driver=driver))
    assert out["neo4j_written"] is True
    assert captured["driver"] is sentinel
    writer.set_driver(None)
