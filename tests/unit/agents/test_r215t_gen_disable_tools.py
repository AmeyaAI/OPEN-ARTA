"""R215.T (extended) — the ATDD + Playwright generators must pass `--tools none`
to the Claude Code CLI (disable_tools=True). Leaving tools exposed on a single-shot
gen prompt let the model attempt tool calls and burn the --max-turns budget →
empty/truncated output (the "ATDD attempt 1/3: empty" retries + PW paren-imbalance
quarantines). This locks the fix and its guard (CLI transport ONLY)."""
from __future__ import annotations

import pytest

from src.agents.atdd_designer import ATDDDesignerAgent
from src.agents.automation_engineer import AutomationEngineerAgent


class _FakeMessages:
    def __init__(self, sink):
        self._sink = sink

    async def create(self, **kwargs):
        self._sink.update(kwargs)
        # shape the response so _call_llm's stop_reason check passes
        return type("Msg", (), {"stop_reason": None, "done_reason": None,
                                "content": [type("C", (), {"text": "ok"})()]})()


class ClaudeCLIClient:  # noqa: N801 — name MUST match the guard's __name__ check
    provider = "claude_code"

    def __init__(self, sink):
        self.messages = _FakeMessages(sink)


class AnthropicSDKClient:  # a non-CLI transport (SDK) — must NOT get disable_tools
    provider = "anthropic"

    def __init__(self, sink):
        self.messages = _FakeMessages(sink)


@pytest.mark.asyncio
async def test_atdd_disables_tools_on_cli():
    sink: dict = {}
    agent = ATDDDesignerAgent(ClaudeCLIClient(sink))
    await agent._call_llm(model="m", max_tokens=100,
                          messages=[{"role": "user", "content": "gen"}])
    assert sink.get("disable_tools") is True


@pytest.mark.asyncio
async def test_atdd_does_not_disable_tools_on_sdk():
    sink: dict = {}
    agent = ATDDDesignerAgent(AnthropicSDKClient(sink))
    await agent._call_llm(model="m", max_tokens=100,
                          messages=[{"role": "user", "content": "gen"}])
    assert "disable_tools" not in sink   # SDK path rejects the unknown kwarg


@pytest.mark.asyncio
async def test_playwright_gen_disables_tools_on_cli():
    sink: dict = {}
    agent = AutomationEngineerAgent(ClaudeCLIClient(sink))
    await agent._call_llm(model="m", max_tokens=100,
                          messages=[{"role": "user", "content": "gen"}])
    assert sink.get("disable_tools") is True


@pytest.mark.asyncio
async def test_playwright_gen_does_not_disable_tools_on_sdk():
    sink: dict = {}
    agent = AutomationEngineerAgent(AnthropicSDKClient(sink))
    await agent._call_llm(model="m", max_tokens=100,
                          messages=[{"role": "user", "content": "gen"}])
    assert "disable_tools" not in sink


@pytest.mark.asyncio
async def test_caller_override_is_respected():
    # an explicit disable_tools=False from a caller must win (setdefault semantics)
    sink: dict = {}
    agent = AutomationEngineerAgent(ClaudeCLIClient(sink))
    await agent._call_llm(model="m", max_tokens=100, disable_tools=False,
                          messages=[{"role": "user", "content": "gen"}])
    assert sink.get("disable_tools") is False
