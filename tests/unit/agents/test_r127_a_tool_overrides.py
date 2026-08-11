"""R127.A — per-tool LLM provider override unit tests.

8 cases covering the LLMConfig.tool_overrides + resolve_tool_config +
resolve_tool_client primitives. These lock down the single-source-of-truth
contract: empty/missing overrides → object identity preserved; explicit
overrides → distinct client construction with shared cache de-dupe.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from src.models.llm_config import LLMConfig, LLMProvider, resolve_tool_config


# ── R127.A — resolve_tool_config (model-level merging) ──────────────────────


def test_r127a_empty_overrides_returns_base_unchanged():
    """Empty tool_overrides dict → resolve returns the SAME object (identity)."""
    base = LLMConfig(provider=LLMProvider.OLLAMA, model="arta-qwen-pro")
    resolved = resolve_tool_config(base, "playwright")
    assert resolved is base, "Empty overrides must preserve object identity for fast-path"


def test_r127a_missing_tool_key_returns_base_unchanged():
    """Override exists for OTHER tool but not for the requested one → base unchanged."""
    base = LLMConfig(
        provider=LLMProvider.OLLAMA,
        model="arta-qwen-pro",
        tool_overrides={"newman": {"provider": "anthropic", "model": "claude-sonnet-4-6"}},
    )
    resolved = resolve_tool_config(base, "playwright")
    assert resolved is base


def test_r127a_explicit_override_merges_provider_and_model():
    """Override with provider+model → resolved carries override values; api_key inherits."""
    base = LLMConfig(
        provider=LLMProvider.OLLAMA,
        model="arta-qwen-pro",
        api_key="base-key-abc",
        tool_overrides={
            "playwright": {"provider": "anthropic", "model": "claude-sonnet-4-6"}
        },
    )
    resolved = resolve_tool_config(base, "playwright")
    assert resolved is not base
    assert resolved.provider == LLMProvider.ANTHROPIC
    assert resolved.model == "claude-sonnet-4-6"
    # api_key inherits from base when override api_key is empty
    assert resolved.api_key == "base-key-abc"


def test_r127a_partial_override_inherits_temperature_and_max_tokens():
    """Override without temperature/max_tokens → resolved inherits both from base."""
    base = LLMConfig(
        provider=LLMProvider.OLLAMA,
        model="arta-qwen-pro",
        temperature=0.4,
        max_tokens=12000,
        tool_overrides={
            "k6": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}
        },
    )
    resolved = resolve_tool_config(base, "k6")
    assert resolved.temperature == 0.4
    assert resolved.max_tokens == 12000


def test_r127a_unknown_provider_falls_back_to_base():
    """Invalid provider string in override → safely falls back to base provider."""
    base = LLMConfig(provider=LLMProvider.OLLAMA, model="arta-qwen-pro")
    base.tool_overrides = {"playwright": {"provider": "frobnitz", "model": "x"}}
    resolved = resolve_tool_config(base, "playwright")
    # Fallback: provider stays as base (Ollama) but model takes override value
    assert resolved.provider == LLMProvider.OLLAMA
    assert resolved.model == "x"


def test_r127a_to_dict_masks_override_api_keys():
    """to_dict(mask_keys=True) masks api_keys INSIDE tool_overrides too."""
    base = LLMConfig(
        provider=LLMProvider.OLLAMA,
        model="arta-qwen-pro",
        api_key="base-secret",
        tool_overrides={
            "playwright": {"provider": "anthropic", "model": "claude-sonnet-4-6",
                           "api_key": "override-secret"},
        },
    )
    d = base.to_dict(mask_key=True)
    assert d["api_key"] == "***"
    assert d["tool_overrides"]["playwright"]["api_key"] == "***"


def test_r127a_from_dict_backward_compat_no_tool_overrides_key():
    """Project JSON without tool_overrides/escalation_threshold/escalation_cap
    fields loads cleanly with defaults (zero-impact backward compat)."""
    legacy_data = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "api_key": "k",
        "base_url": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    cfg = LLMConfig.from_dict(legacy_data)
    assert cfg.tool_overrides == {}
    assert cfg.escalation_threshold == 0.5
    assert cfg.escalation_cap == 10


# ── R127.A — resolve_tool_client (factory cache) ────────────────────────────


def test_r127a_resolve_tool_client_caches_per_provider_model():
    """Two calls with overrides resolving to the same (provider, model)
    tuple share one constructed client via the cache."""
    # Two tools with identical overrides → same cached client
    base = LLMConfig(
        provider=LLMProvider.OLLAMA,
        model="arta-qwen-pro",
        tool_overrides={
            "playwright":           {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "playwright_escalation": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        },
    )
    cache: dict = {}
    fake_client = MagicMock()
    fake_client.provider = LLMProvider.ANTHROPIC
    with patch("src.agents.llm_client.create_llm_client", return_value=fake_client) as mock_factory:
        from src.agents.llm_client import resolve_tool_client
        c1 = resolve_tool_client(base, "playwright", cache)
        c2 = resolve_tool_client(base, "playwright_escalation", cache)
    assert c1 is c2, "Cache must dedupe identical (provider, model) tuples"
    assert mock_factory.call_count == 1
