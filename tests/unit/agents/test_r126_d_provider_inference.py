"""R126.D — model-name fallback when client.provider attribute is missing.

Live bug from production logs: "R125.L: prompt is ~8752 tokens —
provider=unknown model=arta-qwen-pro:latest budget=16000". The model name
clearly identifies Ollama, but the client was constructed without setting
`.provider` (regen-consumer path), so R125.L fell back to the 16K default
budget instead of Ollama's 28K. This is a 43% budget loss for a known
provider — and it cascades into R126.A's prompt manifest, which would
NOT apply the Ollama trim path either.

R126.D adds `_r126_d_infer_provider_from_model()` invoked from
`llm_provider_tag` when the explicit provider attr is missing. Maps known
model-name patterns (arta-qwen-pro, qwen3, claude-sonnet, etc.) to their
provider so all downstream budget + manifest decisions land correctly.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.agents.llm_client import (
    _r126_d_infer_provider_from_model,
    llm_provider_tag,
)
from src.models.llm_config import LLMProvider


def test_r126d_arta_qwen_pro_inferred_ollama():
    """The exact production-bug model name → ollama (not unknown)."""
    assert _r126_d_infer_provider_from_model("arta-qwen-pro:latest") == "ollama"
    assert _r126_d_infer_provider_from_model("arta-qwen-pro") == "ollama"


def test_r126d_other_ollama_models_inferred():
    """All known Ollama model patterns resolve."""
    cases = {
        "qwen3:8b": "ollama",
        "qwen3:32b": "ollama",
        "qwen2.5:32b": "ollama",
        "qwen2": "ollama",
        "llama3.3:latest": "ollama",
        "llama2": "ollama",
        "mistral:latest": "ollama",
        "mixtral:8x7b": "ollama",
        "phi3:mini": "ollama",
        "gemma:latest": "ollama",
        "deepseek-r1:70b": "ollama",
        "codellama:latest": "ollama",
    }
    for model, expected in cases.items():
        assert _r126_d_infer_provider_from_model(model) == expected, (
            f"{model} should map to {expected}"
        )


def test_r126d_anthropic_models_inferred():
    """Claude model names → anthropic."""
    assert _r126_d_infer_provider_from_model("claude-sonnet-4-6") == "anthropic"
    assert _r126_d_infer_provider_from_model("claude-opus-4-6") == "anthropic"
    assert _r126_d_infer_provider_from_model("claude-haiku-4-5-20251001") == "anthropic"
    assert _r126_d_infer_provider_from_model("claude-3-5-sonnet") == "anthropic"


def test_r126d_unknown_model_returns_none():
    """Unrecognized model returns None — caller falls back to 'unknown'."""
    assert _r126_d_infer_provider_from_model("acme-llm:v1") is None
    assert _r126_d_infer_provider_from_model("") is None
    assert _r126_d_infer_provider_from_model(None) is None


def test_r126d_provider_attr_wins_over_inference():
    """When explicit provider IS set, model-name inference must not override.

    Critical: if the operator explicitly configured Anthropic but the model
    name happens to contain 'qwen' (e.g., a Claude vNext model), the explicit
    provider must remain authoritative.
    """
    c = MagicMock()
    c.provider = LLMProvider.ANTHROPIC
    c.model = "qwen-mystery-model"  # name SUGGESTS ollama
    tag = llm_provider_tag(c)
    assert tag["provider"] == "anthropic", (
        "explicit provider must win over model-name inference"
    )


def test_r126d_missing_provider_attr_uses_inference():
    """The production-bug repro: client has no .provider attr but model is qwen.

    Pre-R126.D this returned provider='unknown' → R125.L budget=16K.
    Post-R126.D this returns provider='ollama' → R125.L budget=28K.
    """
    c = MagicMock()
    c.provider = None  # regen-consumer path constructs this way
    c.model = "arta-qwen-pro:latest"
    tag = llm_provider_tag(c)
    assert tag["provider"] == "ollama", (
        f"R126.D fallback must infer ollama from arta-qwen-pro; got {tag['provider']}"
    )
    assert tag["strategy"] == "sequential", (
        "ollama provider implies sequential strategy"
    )


def test_r126d_unknown_provider_string_triggers_inference():
    """When provider attr is the literal string 'unknown' (legacy edge case),
    fall back to model-name inference."""
    c = MagicMock()
    c.provider = "unknown"
    c.model = "claude-sonnet-4-6"
    tag = llm_provider_tag(c)
    assert tag["provider"] == "anthropic"


def test_r126d_no_inference_when_model_also_missing():
    """When neither provider nor model is known, return 'unknown' gracefully."""
    # Use spec to prevent MagicMock auto-creating ._model attribute
    c = MagicMock(spec=["provider", "model"])
    c.provider = None
    c.model = None
    tag = llm_provider_tag(c)
    assert tag["provider"] == "unknown"
    assert tag["model"] == "unknown"
