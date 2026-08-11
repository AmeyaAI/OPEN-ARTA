"""R104.B regression tests — agent-owned MCP connection + SUT source-code
context injection into Playwright prompt.

Pre-R104.B: `github_context.py` spawned a NEW github-mcp-server subprocess
per call (one-shot ad-hoc invocation). High overhead + no shared state.

Post-R104.B: AutomationEngineerAgent OWNS a persistent MCP client. The PW
gen path injects SUT source code (frontend routes + backend route handlers)
into the prompt as a HARD CONSTRAINT block so the LLM stops hallucinating
`/v1/datasets`-style paths when the SUT serves `/api/v1/datasets`.

These tests verify:
1. connect_mcp() is idempotent (multiple calls don't re-spawn)
2. connect_mcp() graceful when GITHUB_TOKEN missing
3. disconnect_mcp() closes the client cleanly
4. _fetch_sut_source_context() returns "" when MCP unavailable (graceful degradation)
5. _fetch_sut_source_context() returns formatted HARD CONSTRAINT block when MCP has data
6. The block composes cleanly (no over-budget) with R47.1b + R98.3 in the prompt
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.automation_engineer import AutomationEngineerAgent


@pytest.fixture()
def agent():
    return AutomationEngineerAgent(client=AsyncMock())


# ── 1. connect_mcp idempotency ───────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_mcp_idempotent(agent: AutomationEngineerAgent, monkeypatch):
    """Multiple connect_mcp() calls should not spawn multiple subprocesses."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")

    spawn_count = 0

    class _FakeClient:
        def __init__(self, token):
            nonlocal spawn_count
            spawn_count += 1
        async def start(self):
            pass
        async def stop(self):
            pass

    with patch("src.agents.github_context.GitHubMCPClient", _FakeClient):
        await agent.connect_mcp(project={"integrations": {"github_token": "x"}})
        await agent.connect_mcp(project={"integrations": {"github_token": "x"}})
        await agent.connect_mcp(project={"integrations": {"github_token": "x"}})

    assert spawn_count == 1, f"Expected 1 spawn; got {spawn_count}"
    assert agent._mcp_connected is True


# ── 2. connect_mcp graceful without GITHUB_TOKEN ─────────────────────


@pytest.mark.asyncio
async def test_connect_mcp_no_token_graceful(agent: AutomationEngineerAgent, monkeypatch):
    """When GITHUB_TOKEN is missing AND project has no github_token,
    connect_mcp marks _mcp_connected=True (to avoid retry loop) but
    doesn't crash. PW gen will fall back to R98.3 only."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    await agent.connect_mcp(project={"integrations": {}})
    assert agent._mcp_connected is True
    assert agent._mcp_clients == {}   # no client registered


# ── 3. disconnect_mcp closes cleanly ─────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_mcp_calls_stop(agent: AutomationEngineerAgent, monkeypatch):
    """disconnect_mcp() invokes .stop() on every registered client."""
    stop_called = {"count": 0}

    class _FakeClient:
        def __init__(self, token):
            pass
        async def start(self):
            pass
        async def stop(self):
            stop_called["count"] += 1

    monkeypatch.setenv("GITHUB_TOKEN", "x")
    with patch("src.agents.github_context.GitHubMCPClient", _FakeClient):
        await agent.connect_mcp(project={"integrations": {"github_token": "x"}})
        assert "github" in agent._mcp_clients
        await agent.disconnect_mcp()

    assert stop_called["count"] == 1
    assert agent._mcp_clients == {}
    assert agent._mcp_connected is False


# ── 4. _fetch_sut_source_context graceful when MCP unavailable ───────


@pytest.mark.asyncio
async def test_fetch_sut_source_context_no_client_returns_empty(
    agent: AutomationEngineerAgent, monkeypatch,
):
    """When MCP can't connect (no token, no repos), _fetch returns "" so
    PW gen continues with existing R98.3 captured_endpoints only."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    out = await agent._fetch_sut_source_context(
        project={"integrations": {}},
        gherkin_text="As a user I want to view datasets",
    )
    assert out == ""


# ── 5. _fetch_sut_source_context returns formatted HARD CONSTRAINT block ─


@pytest.mark.asyncio
async def test_fetch_sut_source_context_formats_block(
    agent: AutomationEngineerAgent, monkeypatch,
):
    """When REST returns route data, the helper formats a HARD CONSTRAINT
    block with backend routes + frontend routes. R104.B uses direct REST
    (the npm github-mcp-server is a Git CLI tool, NOT the API MCP per
    github_context.py:223 TODO)."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake")

    # Mock extract_frontend_routes (also uses REST internally)
    async def _fake_fe_routes(project):
        return [{"route": "/dashboard", "component": "DashboardPage", "selectors_hint": ""}]

    # Mock the agent's REST-based backend-routes extractor
    async def _fake_be_routes(*a, **kw):
        return [
            {"method": "POST", "path": "/api/v1/datasets"},
            {"method": "GET",  "path": "/api/v1/projects"},
        ]

    with patch("src.agents.github_context.extract_frontend_routes", _fake_fe_routes), \
         patch.object(agent, "_r104_b_extract_backend_routes_rest", _fake_be_routes):
        out = await agent._fetch_sut_source_context(
            project={
                "integrations": {
                    "github_token": "x",
                    "repositories": [{"repo": "owner/sut-backend"}],
                },
            },
            gherkin_text="user views datasets",
        )

    assert out, f"Expected non-empty block; got {out!r}"
    assert "HARD CONSTRAINT" in out
    assert "/api/v1/datasets" in out
    assert "/dashboard" in out
    assert "[END SUT SOURCE CONTEXT]" in out


# ── 6. Source context block respects max_chars budget ────────────────


@pytest.mark.asyncio
async def test_fetch_sut_source_context_respects_max_chars(
    agent: AutomationEngineerAgent, monkeypatch,
):
    """Block must not exceed max_chars even with many routes."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake")

    async def _fake_fe_routes(project):
        return [
            {"route": f"/page-{i}", "component": f"Page{i}", "selectors_hint": "selector_x"}
            for i in range(200)
        ]

    async def _fake_be_routes(*a, **kw):
        return [
            {"method": "POST", "path": f"/api/v1/route_{i}"}
            for i in range(200)
        ]

    with patch("src.agents.github_context.extract_frontend_routes", _fake_fe_routes), \
         patch.object(agent, "_r104_b_extract_backend_routes_rest", _fake_be_routes):
        out = await agent._fetch_sut_source_context(
            project={
                "integrations": {
                    "github_token": "x",
                    "repositories": [{"repo": "owner/sut-backend"}],
                },
            },
            gherkin_text="test scenario",
            max_chars=2000,
        )

    assert 0 < len(out) <= 2000, f"Expected 0 < len ≤ 2000; got {len(out)}"
