"""R161 — claude_code CLI concurrency limiter (serialize the shared --continue session)."""
import asyncio, os
import pytest
import src.agents.claude_cli_client as cc


def test_max_concurrency_default_and_env(monkeypatch):
    monkeypatch.delenv("ARTA_CLAUDE_CLI_MAX_CONCURRENCY", raising=False)
    assert cc._cli_max_concurrency() == 1            # default serialize
    monkeypatch.setenv("ARTA_CLAUDE_CLI_MAX_CONCURRENCY", "3")
    assert cc._cli_max_concurrency() == 3
    monkeypatch.setenv("ARTA_CLAUDE_CLI_MAX_CONCURRENCY", "bogus")
    assert cc._cli_max_concurrency() == 1            # invalid → 1


def test_decorator_serializes_concurrent_calls(monkeypatch):
    monkeypatch.delenv("ARTA_CLAUDE_CLI_MAX_CONCURRENCY", raising=False)
    cc._CLI_SEM = None  # reset lazy semaphore
    state = {"active": 0, "max": 0}

    @cc._cli_serialized
    async def fake(self, *, tag):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.02)
        state["active"] -= 1
        return tag

    async def run():
        return await asyncio.gather(*[fake(None, tag=i) for i in range(5)])

    out = asyncio.run(run())
    assert sorted(out) == [0, 1, 2, 3, 4]
    assert state["max"] == 1     # default limiter serialized all 5


def test_create_is_wrapped():
    # the real create() carries the R161 wrapper
    assert getattr(cc.ClaudeCLIClient.create, "__wrapped__", None) is not None
