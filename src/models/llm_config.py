"""
ARTA LLM Provider Configuration
Supports: Anthropic, Google Gemini, Ollama (local), OpenAI-compatible endpoints.
Each project configures its own LLM provider and model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LLMProvider(str, Enum):
    ANTHROPIC     = "anthropic"
    CLAUDE_CODE   = "claude_code"
    GOOGLE_GEMINI = "google_gemini"
    OLLAMA        = "ollama"
    OPENAI        = "openai"
    AZURE_OPENAI  = "azure_openai"


@dataclass
class LLMConfig:
    """Per-project LLM configuration."""

    provider:    LLMProvider = LLMProvider.ANTHROPIC
    model:       str         = "claude-sonnet-4-6"
    api_key:     str         = ""    # empty = fall back to environment variable
    base_url:    str         = ""    # Ollama: "http://localhost:11434"; Azure: custom endpoint
    temperature: float       = 0.2
    max_tokens:  int         = 4096

    # R127.A — per-tool overrides + escalation knobs.
    # tool_overrides keys: "playwright", "newman", "k6", "axe", "zap", "pytest",
    # plus escalation variants like "playwright_escalation" (R127.C).
    # Empty dict → no overrides, all tools use the base provider/model
    # (zero-impact backward compatibility for existing projects).
    tool_overrides:        dict[str, dict] = field(default_factory=dict)
    escalation_threshold:  float           = 0.5   # R127.C — R126.Q aggregate score below this triggers escalation
    # R127.C original `escalation_cap` is the PER-BATCH ceiling (the
    # operator's true cost knob — applies across all reqs in one gen run).
    # R127.E.3 added the PER-REQ cap below: a fresh budget per req so a
    # dense 16-sub req can't single-handedly exhaust the batch budget +
    # leave subsequent reqs uncovered. Both caps are checked atomically;
    # whichever is reached first emits `capped_batch` OR `capped_req`.
    escalation_cap:          int           = 10    # R127.C / R127.E.3 per-batch ceiling
    escalation_cap_per_req:  int           = 10    # R127.E.3 — fresh budget per req (resets in _r127_b_generate_decomposed)
    # R128.B — LLM output caching (Redis-backed via observability/cache.py).
    # Pre-R128.B: every LLM call (regen verification, R97.D bulk regen, etc.)
    # paid full Ollama / Claude Code cost. Post-R128.B: identical request
    # signatures replay from cache at zero token cost. Opt-in via
    # `cache_enabled=True`; defaults preserve current behavior so existing
    # projects' configs continue to function unchanged.
    cache_enabled:           bool          = False  # opt-in; default False = current behavior
    cache_ttl_seconds:       int           = 604800  # 7 days
    cache_temperature_max:   float         = 0.3    # bypass cache above this temp (variance protection)

    @property
    def litellm_model(self) -> str:
        """Return the litellm-compatible model identifier."""
        if self.provider in (LLMProvider.ANTHROPIC, LLMProvider.CLAUDE_CODE):
            return self.model
        elif self.provider == LLMProvider.GOOGLE_GEMINI:
            return f"gemini/{self.model}"
        elif self.provider == LLMProvider.OLLAMA:
            return f"ollama/{self.model}"
        elif self.provider == LLMProvider.AZURE_OPENAI:
            return f"azure/{self.model}"
        # OpenAI-compatible (pass model name as-is, e.g. "gpt-4o")
        return self.model

    @classmethod
    def from_dict(cls, data: dict) -> "LLMConfig":
        provider = LLMProvider(data.get("provider", "anthropic"))
        # R134.A.2 — provider-aware model default. Pre-R134.A.2: any project
        # config without an explicit `model` got "claude-sonnet-4-6"
        # regardless of provider → Ollama clients got constructed with a
        # Claude model name + then the OllamaDirectClient route ignored it
        # in favor of env defaults → silent provider/model mismatch in logs.
        # Post-R134.A.2: each provider gets the canonical recommended model
        # from PROVIDER_PRESETS (matches the UI dropdown's recommended row).
        # Ollama specifically honors ARTA_PRIMARY_MODEL env override so
        # operator config (e.g., arta-qwen-pro:latest) is respected when
        # no explicit model is set.
        import os as _r134_a_os
        _R134_A_2_PROVIDER_MODEL_DEFAULTS = {
            LLMProvider.ANTHROPIC:     "claude-sonnet-4-6",
            LLMProvider.CLAUDE_CODE:   "claude-sonnet-4-6",
            LLMProvider.OLLAMA:        _r134_a_os.environ.get("ARTA_PRIMARY_MODEL", "arta-qwen-pro:latest"),
            LLMProvider.GOOGLE_GEMINI: "gemini-2.0-flash",
            LLMProvider.OPENAI:        "gpt-4o",
            LLMProvider.AZURE_OPENAI:  "gpt-4o",
        }
        _r134_a_default_model = _R134_A_2_PROVIDER_MODEL_DEFAULTS.get(provider, "claude-sonnet-4-6")
        # R127.A — parse tool_overrides + escalation knobs (defaults preserve
        # behaviour for projects.json files predating R127).
        tool_overrides_raw = data.get("tool_overrides") or {}
        tool_overrides: dict[str, dict] = {}
        if isinstance(tool_overrides_raw, dict):
            for tool_name, override in tool_overrides_raw.items():
                if isinstance(override, dict):
                    tool_overrides[str(tool_name)] = {
                        "provider":    str(override.get("provider", "") or ""),
                        "model":       str(override.get("model", "") or ""),
                        "api_key":     str(override.get("api_key", "") or ""),
                        "base_url":    str(override.get("base_url", "") or ""),
                        "temperature": override.get("temperature"),
                        "max_tokens":  override.get("max_tokens"),
                    }
        # R130.D — Ollama provider gets a higher default escalation_cap
        # because qwen-pro 32B's intrinsic escalation rate is empirically
        # 2-3× Claude's. Operator-overridable via projects.json. Backward
        # compat preserved: Anthropic / others keep the original cap=10.
        _default_escalation_cap = 30 if provider == LLMProvider.OLLAMA else 10
        return cls(
            provider=provider,
            model=data.get("model") or _r134_a_default_model,   # R134.A.2 provider-aware default
            api_key=data.get("api_key", ""),
            base_url=data.get("base_url", ""),
            temperature=float(data.get("temperature", 0.2)),
            max_tokens=int(data.get("max_tokens", 4096)),
            tool_overrides=tool_overrides,
            escalation_threshold=float(data.get("escalation_threshold", 0.5)),
            escalation_cap=int(data.get("escalation_cap", _default_escalation_cap)),
            # R127.E.3 — backward compat: missing key → default 10.
            escalation_cap_per_req=int(data.get("escalation_cap_per_req", 10)),
            # R128.B — backward compat: missing keys → defaults preserve
            # current behavior (cache_enabled=False = no behavior change).
            cache_enabled=bool(data.get("cache_enabled", False)),
            cache_ttl_seconds=int(data.get("cache_ttl_seconds", 604800)),
            cache_temperature_max=float(data.get("cache_temperature_max", 0.3)),
        )

    def to_dict(self, mask_key: bool = True) -> dict:
        # R127.A — mask api_keys inside tool_overrides too.
        masked_overrides: dict[str, dict] = {}
        for tool_name, override in (self.tool_overrides or {}).items():
            masked: dict = {}
            for k, v in (override or {}).items():
                if k == "api_key" and mask_key and v:
                    masked[k] = "***"
                else:
                    masked[k] = v
            masked_overrides[tool_name] = masked
        return {
            "provider":              self.provider.value,
            "model":                 self.model,
            "api_key":               "***" if mask_key and self.api_key else self.api_key,
            "base_url":              self.base_url,
            "temperature":           self.temperature,
            "max_tokens":            self.max_tokens,
            "tool_overrides":         masked_overrides,
            "escalation_threshold":   self.escalation_threshold,
            "escalation_cap":         self.escalation_cap,
            "escalation_cap_per_req": self.escalation_cap_per_req,
            # R128.B — LLM output cache knobs
            "cache_enabled":          self.cache_enabled,
            "cache_ttl_seconds":      self.cache_ttl_seconds,
            "cache_temperature_max":  self.cache_temperature_max,
        }


# ── R127.A — resolve_tool_config (single source of truth for tool→config merge) ──

def resolve_tool_config(base: LLMConfig, tool_name: str) -> LLMConfig:
    """R127.A — return base merged with tool_overrides[tool_name].

    Missing override OR empty override returns base unchanged (idempotent;
    object-identity preserved so the caller can compare `result is base`).

    Override semantics: any field with a truthy value in the override
    replaces the base; falsy/missing fields inherit from base. api_key
    inherits from base when override api_key is empty (matches the
    existing Anthropic env-fallback pattern at llm_client.py:248).
    """
    overrides = (base.tool_overrides or {}).get(tool_name)
    if not overrides:
        return base
    # Treat empty-string + None as "inherit"; anything else overrides.
    override_provider = overrides.get("provider") or ""
    override_model    = overrides.get("model") or ""
    if not override_provider and not override_model:
        # All-empty override → no-op; return base for object identity.
        return base
    try:
        new_provider = LLMProvider(override_provider) if override_provider else base.provider
    except ValueError:
        # Unknown provider in override → safely fall back to base
        new_provider = base.provider
    new_model    = override_model or base.model
    new_api_key  = overrides.get("api_key") or base.api_key
    new_base_url = overrides.get("base_url") or base.base_url
    override_temp = overrides.get("temperature")
    new_temp     = float(override_temp) if override_temp is not None else base.temperature
    override_max = overrides.get("max_tokens")
    new_max      = int(override_max) if override_max is not None else base.max_tokens
    return LLMConfig(
        provider=new_provider,
        model=new_model,
        api_key=new_api_key,
        base_url=new_base_url,
        temperature=new_temp,
        max_tokens=new_max,
        # Resolved tool configs do NOT carry nested tool_overrides — they
        # represent a single resolved (provider, model) pair for one tool.
        tool_overrides={},
        escalation_threshold=base.escalation_threshold,
        escalation_cap=base.escalation_cap,
        escalation_cap_per_req=base.escalation_cap_per_req,
        # R128.B — propagate cache knobs to resolved tool configs so per-tool
        # overrides (e.g., playwright_escalation client) inherit caching.
        cache_enabled=base.cache_enabled,
        cache_ttl_seconds=base.cache_ttl_seconds,
        cache_temperature_max=base.cache_temperature_max,
    )


# ── Provider / Model Presets ──────────────────────────────────────────────────
# Surfaced in the UI for project configuration.

PROVIDER_PRESETS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic",
        "icon":  "🟣",
        "description": "Claude — state-of-the-art reasoning and coding",
        "requires_base_url": False,
        "models": [
            {"id": "claude-sonnet-4-6",        "name": "Claude Sonnet 4.6",  "recommended": True,  "tier": "balanced"},
            {"id": "claude-opus-4-6",           "name": "Claude Opus 4.6",    "recommended": False, "tier": "powerful"},
            {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5",   "recommended": False, "tier": "fast"},
        ],
    },
    "google_gemini": {
        "label": "Google Gemini",
        "icon":  "🔵",
        "description": "Gemini — multimodal, long context, Google ecosystem",
        "requires_base_url": False,
        "models": [
            {"id": "gemini-2.0-flash",    "name": "Gemini 2.0 Flash",  "recommended": True,  "tier": "fast"},
            {"id": "gemini-1.5-pro",      "name": "Gemini 1.5 Pro",    "recommended": False, "tier": "powerful"},
            {"id": "gemini-1.5-flash",    "name": "Gemini 1.5 Flash",  "recommended": False, "tier": "fast"},
            {"id": "gemini-2.0-pro-exp",  "name": "Gemini 2.0 Pro",    "recommended": False, "tier": "powerful"},
        ],
    },
    "ollama": {
        "label": "Ollama (Local)",
        "icon":  "🦙",
        "description": "Run models locally — no data leaves your machine",
        "requires_base_url": True,
        "base_url_placeholder": "http://host.docker.internal:11434",
        "models": [
            {"id": "arta-qwen-pro",          "name": "ARTA Qwen Pro (Upskilled)", "recommended": True,  "tier": "reasoning"},
            {"id": "qwen2.5:32b",            "name": "Qwen 2.5 32B",          "recommended": False, "tier": "balanced"},
            {"id": "qwen3:32b",              "name": "Qwen3 32B",              "recommended": False, "tier": "powerful"},
            {"id": "qwen3:8b",               "name": "Qwen3 8B",               "recommended": False, "tier": "fast"},
            {"id": "qwen2.5-coder:32b",      "name": "Qwen 2.5 Coder 32B",    "recommended": False, "tier": "balanced"},
            {"id": "llama3.3:70b",           "name": "LLaMA 3.3 70B",         "recommended": False, "tier": "powerful"},
            {"id": "deepseek-r1:70b",        "name": "DeepSeek R1 70B",        "recommended": False, "tier": "reasoning"},
            {"id": "mixtral:8x7b",           "name": "Mixtral 8x7B",           "recommended": False, "tier": "balanced"},
            {"id": "codestral:22b",          "name": "Codestral 22B",          "recommended": False, "tier": "balanced"},
            {"id": "deepseek-coder-v2:16b",  "name": "DeepSeek Coder v2 16B", "recommended": False, "tier": "fast"},
            {"id": "mistral:7b",             "name": "Mistral 7B",             "recommended": False, "tier": "fast"},
            {"id": "phi4:14b",               "name": "Phi-4 14B",              "recommended": False, "tier": "fast"},
        ],
    },
    "openai": {
        "label": "OpenAI",
        "icon":  "⚫",
        "description": "GPT models — widely used, strong general-purpose performance",
        "requires_base_url": False,
        "models": [
            {"id": "gpt-4o",         "name": "GPT-4o",       "recommended": True,  "tier": "balanced"},
            {"id": "gpt-4o-mini",    "name": "GPT-4o Mini",  "recommended": False, "tier": "fast"},
            {"id": "o3-mini",        "name": "o3-mini",      "recommended": False, "tier": "reasoning"},
            {"id": "o1",             "name": "o1",           "recommended": False, "tier": "reasoning"},
        ],
    },
    "claude_code": {
        "label": "Claude Code",
        "icon":  "🔮",
        "description": "Use your Claude Code API token — same models as Anthropic",
        "requires_base_url": False,
        "models": [
            {"id": "claude-sonnet-4-6",        "name": "Claude Sonnet 4.6",  "recommended": True,  "tier": "balanced"},
            {"id": "claude-opus-4-6",           "name": "Claude Opus 4.6",    "recommended": False, "tier": "powerful"},
            {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5",   "recommended": False, "tier": "fast"},
        ],
    },
    "azure_openai": {
        "label": "Azure OpenAI",
        "icon":  "☁️",
        "description": "OpenAI models hosted on Microsoft Azure — enterprise compliance",
        "requires_base_url": True,
        "base_url_placeholder": "https://<resource>.openai.azure.com",
        "models": [
            {"id": "gpt-4o",      "name": "GPT-4o (Azure)",      "recommended": True,  "tier": "balanced"},
            {"id": "gpt-4",       "name": "GPT-4 (Azure)",       "recommended": False, "tier": "powerful"},
            {"id": "gpt-35-turbo","name": "GPT-3.5 Turbo (Azure)","recommended": False, "tier": "fast"},
        ],
    },
}

# Default config used when no project context is available
DEFAULT_LLM_CONFIG = LLMConfig(
    provider=LLMProvider.OLLAMA,
    model="qwen2.5:32b",
    base_url="http://host.docker.internal:11434",
)
