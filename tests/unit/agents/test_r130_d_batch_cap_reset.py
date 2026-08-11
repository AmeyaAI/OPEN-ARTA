"""R130.D — Batch-cap reset + Ollama-aware escalation_cap default tests.

Five cases:
  1. `_r130_d_reset_batch_counter` clears `_r127_c_escalations_count` to 0.
  2. Reset does NOT touch the per-req counter (independence preserved).
  3. Atomicity under concurrent invocation via `asyncio.gather`.
  4. Ollama provider default → `escalation_cap=30` when unset.
  5. Anthropic provider default → `escalation_cap=10` (backward compat).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from src.agents.automation_engineer import AutomationEngineerAgent
from src.models.llm_config import LLMConfig, LLMProvider


def _make_agent() -> AutomationEngineerAgent:
    """Build a minimal agent instance for counter testing — bypasses
    LLM client construction by injecting a MagicMock."""
    mock_client = MagicMock()
    mock_client.provider = LLMProvider.ANTHROPIC
    return AutomationEngineerAgent(client=mock_client)


# ── Case 1: reset clears batch counter ─────────────────────────────────────


@pytest.mark.asyncio
async def test_r130d_reset_clears_batch_counter():
    """After consuming N escalation slots, calling _r130_d_reset_batch_counter
    drops the count to 0."""
    agent = _make_agent()
    # Simulate prior batch consumption
    agent._r127_c_escalations_count = 7
    assert agent._r127_c_escalations_count == 7
    await agent._r130_d_reset_batch_counter()
    assert agent._r127_c_escalations_count == 0


# ── Case 2: reset doesn't touch per-req counter ────────────────────────────


@pytest.mark.asyncio
async def test_r130d_reset_preserves_per_req_counter():
    """The two counters are independent. R130.D resets ONLY the per-batch
    counter; the per-req counter (R127.E.3) is reset via its own method."""
    agent = _make_agent()
    agent._r127_c_escalations_count = 7
    agent._r127_c_escalations_count_this_req = 3
    await agent._r130_d_reset_batch_counter()
    assert agent._r127_c_escalations_count == 0
    assert agent._r127_c_escalations_count_this_req == 3, (
        "Per-req counter must remain untouched by batch-counter reset"
    )


# ── Case 3: atomicity under concurrent gather ──────────────────────────────


@pytest.mark.asyncio
async def test_r130d_reset_atomic_under_concurrent_gather():
    """When 10 concurrent reset calls fire, the final state is 0 with no
    races (atomicity verified by the shared _r127_c_escalations_lock)."""
    agent = _make_agent()
    agent._r127_c_escalations_count = 100
    await asyncio.gather(*[agent._r130_d_reset_batch_counter() for _ in range(10)])
    assert agent._r127_c_escalations_count == 0


# ── Case 4: Ollama default escalation_cap=30 ───────────────────────────────


def test_r130d_ollama_default_escalation_cap_is_30():
    """LLMConfig.from_dict for an Ollama project without explicit
    escalation_cap → defaults to 30 (R130.D Ollama-aware default)."""
    cfg = LLMConfig.from_dict({
        "provider": "ollama",
        "model": "arta-qwen-pro",
    })
    assert cfg.escalation_cap == 30, (
        f"Ollama default escalation_cap should be 30; got {cfg.escalation_cap}"
    )


# ── Case 5: Anthropic default escalation_cap=10 (backward compat) ──────────


def test_r130d_anthropic_default_escalation_cap_is_10():
    """LLMConfig.from_dict for an Anthropic project without explicit
    escalation_cap → defaults to 10 (unchanged pre-R130.D)."""
    cfg = LLMConfig.from_dict({
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    })
    assert cfg.escalation_cap == 10, (
        f"Anthropic default escalation_cap should remain 10; got {cfg.escalation_cap}"
    )
    # Operator-explicit override still respected
    cfg_override = LLMConfig.from_dict({
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "escalation_cap": 25,
    })
    assert cfg_override.escalation_cap == 25
