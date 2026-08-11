"""R136.B — small-Ollama Newman chunk_size lowering.

3 cases: small-Ollama → 8; mid-tier Ollama → 15; non-Ollama → 30.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.agents.automation_engineer import AutomationEngineerAgent
from src.models.llm_config import LLMProvider


def _make_client(provider: LLMProvider | None, model: str) -> MagicMock:
    """Build a stub LLM client that satisfies `llm_provider_tag`'s duck-type
    contract: it reads `client.provider` (LLMProvider enum) + `client.model`.
    """
    client = MagicMock()
    client.provider = provider
    client.model = model
    return client


def test_r136b1_small_ollama_qwen3_8b_picks_chunk_size_8():
    """R136.B — qwen3:8b is the canonical small-Ollama target; chunk_size=8."""
    client = _make_client(LLMProvider.OLLAMA, "qwen3:8b")
    assert AutomationEngineerAgent._r136_b_pick_newman_chunk_size(client) == 8


def test_r136b2_mid_tier_ollama_qwen3_32b_picks_chunk_size_15():
    """R136.B — qwen3:32b is mid/large-Ollama; keeps the R126.E.1 default of 15."""
    client = _make_client(LLMProvider.OLLAMA, "qwen3:32b")
    assert AutomationEngineerAgent._r136_b_pick_newman_chunk_size(client) == 15


def test_r136b3_arta_qwen_pro_picks_chunk_size_15():
    """R136.B — arta-qwen-pro (production default) is NOT small-Ollama;
    keeps chunk_size=15 (mid/large-Ollama band)."""
    client = _make_client(LLMProvider.OLLAMA, "arta-qwen-pro:latest")
    assert AutomationEngineerAgent._r136_b_pick_newman_chunk_size(client) == 15


def test_r136b4_anthropic_picks_chunk_size_30():
    """R136.B — non-Ollama providers (Anthropic/Claude) use the unchanged
    default of 30 items/chunk."""
    client = _make_client(LLMProvider.ANTHROPIC, "claude-sonnet-4-6")
    assert AutomationEngineerAgent._r136_b_pick_newman_chunk_size(client) == 30


def test_r136b5_missing_client_falls_back_to_30():
    """R136.B defensive default — any detection error falls through to the
    large-provider default of 30, not the small-Ollama 8. Avoids accidentally
    throttling a healthy provider when the provider-tag inference fails."""
    # llm_provider_tag(None) returns {"provider": "unknown", ...} — not "ollama"
    # → R136.B should return 30.
    assert AutomationEngineerAgent._r136_b_pick_newman_chunk_size(None) == 30


def test_r136b6_phi4_14b_picks_chunk_size_8():
    """R136.B — phi4:14b is in the small-Ollama pattern set (R136.A); chunk_size=8."""
    client = _make_client(LLMProvider.OLLAMA, "phi4:14b")
    assert AutomationEngineerAgent._r136_b_pick_newman_chunk_size(client) == 8
