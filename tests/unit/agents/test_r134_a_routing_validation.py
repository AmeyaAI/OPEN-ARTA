"""R134.A KEYSTONE tests — provider routing validation + provider-aware model defaults.

Six cases lock the contract:
  1. Unknown provider raises ValueError naming supported set
  2. OLLAMA without explicit model gets env-or-arta-qwen-pro:latest default
  3. ANTHROPIC without explicit model gets claude-sonnet-4-6 default
  4. GOOGLE_GEMINI without explicit model gets gemini-2.0-flash default
     (regression guard — previously got 'claude-sonnet-4-6' silently)
  5. Backward compat: explicit model in dict overrides default
  6. Empty-string model in dict treated as missing → uses default

R134.A.1 closes the silent LiteLLM→OpenAI fallback (violates "small Ollama
on-prem non-negotiable"); R134.A.2 stops provider/model mismatches.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from src.agents.llm_client import _R134_A_KNOWN_PROVIDERS, create_llm_client
from src.models.llm_config import LLMConfig, LLMProvider


def test_r134_a_1_unknown_provider_raises_with_supported_set():
    """R134.A.1 — unknown provider must raise ValueError naming the
    supported set. NEVER silently fall back to LiteLLM.

    Constructs LLMConfig + monkey-patches the provider field to a
    string that's not in the enum (simulates a future enum extension
    OR a hand-built config bypassing the runtime check)."""
    cfg = LLMConfig(provider=LLMProvider.ANTHROPIC, model="x")
    cfg.provider = "not-a-real-provider"   # type: ignore[assignment]
    with pytest.raises(ValueError) as excinfo:
        create_llm_client(cfg)
    msg = str(excinfo.value)
    assert "R134.A.1" in msg
    assert "unknown LLM provider" in msg
    # The error must list the supported providers so the operator can
    # correct the config.
    for known in _R134_A_KNOWN_PROVIDERS:
        assert known.value in msg
    # Mission contract reference in error message.
    assert "small Ollama on-prem non-negotiable" in msg


def test_r134_a_2_ollama_default_model_honors_env():
    """R134.A.2 — Ollama provider without explicit model gets the
    ARTA_PRIMARY_MODEL env var when set, else the canonical default."""
    with patch.dict(os.environ, {"ARTA_PRIMARY_MODEL": "test-qwen:custom"}, clear=False):
        cfg = LLMConfig.from_dict({"provider": "ollama"})
        assert cfg.model == "test-qwen:custom"


def test_r134_a_2_ollama_default_model_fallback():
    """R134.A.2 — Ollama provider without env override gets
    arta-qwen-pro:latest (matches PROVIDER_PRESETS recommended)."""
    env = {k: v for k, v in os.environ.items() if k != "ARTA_PRIMARY_MODEL"}
    with patch.dict(os.environ, env, clear=True):
        cfg = LLMConfig.from_dict({"provider": "ollama"})
        assert cfg.model == "arta-qwen-pro:latest"


def test_r134_a_2_anthropic_default_model_unchanged():
    """R134.A.2 — Anthropic still defaults to claude-sonnet-4-6.
    Regression guard: pre-R134.A.2 behavior preserved for the canonical
    Claude path."""
    cfg = LLMConfig.from_dict({"provider": "anthropic"})
    assert cfg.model == "claude-sonnet-4-6"


def test_r134_a_2_google_gemini_default_model():
    """R134.A.2 — Gemini provider must NOT default to claude-sonnet-4-6
    (the pre-R134.A.2 bug). Now defaults to gemini-2.0-flash from
    PROVIDER_PRESETS."""
    cfg = LLMConfig.from_dict({"provider": "google_gemini"})
    assert cfg.model == "gemini-2.0-flash"
    assert "claude" not in cfg.model.lower()


def test_r134_a_2_explicit_model_overrides_default():
    """R134.A.2 — when the dict explicitly carries a model, the default
    map is bypassed (operator choice always wins). Both truthy and
    empty-string cases tested: empty string treated as missing per
    `data.get("model") or _r134_a_default_model` semantics."""
    # Explicit truthy value wins
    cfg = LLMConfig.from_dict({"provider": "ollama", "model": "custom-model"})
    assert cfg.model == "custom-model"
    # Empty string treated as missing → default applied
    cfg_empty = LLMConfig.from_dict({"provider": "anthropic", "model": ""})
    assert cfg_empty.model == "claude-sonnet-4-6"
