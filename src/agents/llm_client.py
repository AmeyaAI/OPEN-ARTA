"""
ARTA LLM Client Factory
Creates the appropriate LLM client based on per-project configuration.

Design:
  - Anthropic projects  → native AsyncAnthropic (preserves streaming + tenacity error types)
  - All other providers → LiteLLMClientAdapter (duck-types AsyncAnthropic interface)

All agents call `self._client.messages.create(...)` — the adapter matches this interface
exactly, so no agent code changes are required when switching providers.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, AsyncGenerator

from ..models.llm_config import LLMConfig, LLMProvider

_log = logging.getLogger("arta.llm")
log = _log  # Convenience alias for new R128.B code (parity with other modules)


def _r128_b_compute_cache_key(
    *,
    model: str,
    system: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
) -> str:
    """R128.B — stable hash of the full LLM request signature.

    Two calls with identical (model, system, messages, temperature,
    max_tokens) produce the same key; any change → different key →
    cache miss (no false hits). Cache value is the litellm response dict
    serialized; deserialized into `_LiteLLMResponse` on hit.

    Per the article: caching defeats its purpose on high-temperature
    randomized outputs (no determinism). Caller gates via
    `LLMConfig.cache_temperature_max` (default 0.3).
    """
    import hashlib
    import json
    canonical = json.dumps({
        "model": model,
        "system": system,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }, sort_keys=True, default=str)
    return f"r128_b:llm:{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"


# F10-1b: Set on first probe to avoid repeated stat() calls. We're in a container
# whenever /.dockerenv exists OR ARTA_IS_CONTAINER=1 is set explicitly.
_IS_CONTAINER: bool | None = None


def _is_container() -> bool:
    global _IS_CONTAINER
    if _IS_CONTAINER is None:
        _IS_CONTAINER = (
            Path("/.dockerenv").exists()
            or os.environ.get("ARTA_IS_CONTAINER") == "1"
        )
    return _IS_CONTAINER


# Track URLs we've already warned about so log isn't flooded — first call per
# (URL) per process is enough for operator visibility.
_LOCALHOST_WARNED: set[str] = set()


def _warn_if_localhost_inside_container(url: str) -> None:
    """F10-1b: Loud warning when an LLM base_url resolves to localhost while
    we're inside a container. Inside Docker, `localhost` is the container
    itself — there's almost never a useful service there. The original bug
    (which made every Generate-All fail in 17.0s) was exactly this.
    """
    if not url or url in _LOCALHOST_WARNED:
        return
    lowered = url.lower()
    if not _is_container():
        return
    if not any(host in lowered for host in ("localhost", "127.0.0.1", "0.0.0.0")):
        return
    _LOCALHOST_WARNED.add(url)
    _log.warning(
        "LLM base_url %s contains localhost while running inside a container. "
        "From inside Docker this points at the container itself, not the host. "
        "Use http://host.docker.internal:11434 (or empty string + OLLAMA_BASE_URL env) instead.",
        url,
    )


# ── Response Adapters ────────────────────────────────────────────────────────


class _ContentBlock:
    """Mimics anthropic.types.ContentBlock.text interface."""

    def __init__(self, text: str) -> None:
        self.text = text


class _LiteLLMResponse:
    """Wraps a litellm completion response to look like Anthropic MessageObject.

    Accepts EITHER a live litellm response object (pydantic-shaped with
    `.choices[0].message.content`) OR a plain dict (the R128.B cache
    payload after JSON round-trip through Redis). Dict-shape detection
    lets cached values rehydrate without losing the response interface.
    """

    def __init__(self, response: Any) -> None:
        if isinstance(response, dict):
            # R128.B cache rehydration path. Dict has either the full
            # litellm `.model_dump()` shape OR our compact 4-field shape
            # (text/finish_reason/model/usage) — both are handled.
            if "content_text" in response:
                raw_text = response.get("content_text") or ""
                finish = response.get("finish_reason") or "stop"
                model = response.get("model") or ""
                usage = response.get("usage")
            else:
                choices = response.get("choices") or [{}]
                first = choices[0] if choices else {}
                msg = first.get("message") or {}
                raw_text = msg.get("content") or ""
                finish = first.get("finish_reason") or "stop"
                model = response.get("model") or ""
                usage = response.get("usage")
            self.content = [_ContentBlock(raw_text)]
            self.stop_reason = finish
            self.model = model
            self.usage = usage
            return
        raw_text = response.choices[0].message.content or ""
        self.content = [_ContentBlock(raw_text)]
        self.stop_reason = response.choices[0].finish_reason
        self.model = response.model
        self.usage = getattr(response, "usage", None)


# ── Streaming Adapter ────────────────────────────────────────────────────────


class _LiteLLMStreamContext:
    """
    Async context manager that mimics `client.messages.stream(...)`.
    Yields text tokens via `.text_stream`.
    """

    def __init__(
        self,
        config: LLMConfig,
        messages: list[dict],
        system: str | None,
        max_tokens: int,
    ) -> None:
        self._config = config
        self._messages = messages
        self._system = system
        self._max_tokens = max_tokens
        self._response = None

    async def __aenter__(self) -> "_LiteLLMStreamContext":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    @property
    def text_stream(self) -> AsyncGenerator[str, None]:
        return self._stream_tokens()

    async def _stream_tokens(self) -> AsyncGenerator[str, None]:
        import litellm

        msgs = self._messages[:]
        if self._system:
            msgs = [{"role": "system", "content": self._system}] + msgs

        # Force Docker-to-Host bridge for Ollama if running in local docker container
        api_base = self._config.base_url
        if not api_base and self._config.provider == LLMProvider.OLLAMA:
            api_base = "http://host.docker.internal:11434"
        elif api_base and ("localhost" in api_base or "127.0.0.1" in api_base):
            api_base = api_base.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")

        stream = await litellm.acompletion(
            model=self._config.litellm_model,
            messages=msgs,
            max_tokens=self._max_tokens,
            temperature=self._config.temperature,
            api_key=self._config.api_key or None,
            api_base=api_base or None,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


# ── Messages Proxy ────────────────────────────────────────────────────────────


class _MessagesProxy:
    """
    Mimics the `client.messages` namespace of AsyncAnthropic.
    Supports `.create()` and `.stream()`.
    """

    def __init__(self, adapter: "LiteLLMClientAdapter") -> None:
        self._adapter = adapter

    async def create(
        self,
        model: str,
        max_tokens: int,
        messages: list[dict],
        **kwargs: Any,
    ) -> _LiteLLMResponse:
        import litellm

        # litellm accepts 'system' as a top-level param for some providers;
        # for others it goes into the messages list.
        system = kwargs.pop("system", None)
        msgs = messages[:]
        if system:
            msgs = [{"role": "system", "content": system}] + msgs

        # Force Docker-to-Host bridge for Ollama if running in local docker container
        api_base = self._adapter._config.base_url
        if not api_base and self._adapter._config.provider == LLMProvider.OLLAMA:
            api_base = "http://host.docker.internal:11434"
        elif api_base and ("localhost" in api_base or "127.0.0.1" in api_base):
            api_base = api_base.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")

        # R128.B — LLM output caching via Redis-primary + memory-fallback
        # `cache` singleton from observability/cache.py. Pre-R128.B every
        # LLM call paid full Ollama/Claude cost even on repeat regens
        # Post-R128.B identical (model, msgs, temperature, max_tokens)
        # calls return from cache at zero token cost. Aligned with the
        # standing "small Ollama on-prem non-negotiable" directive.
        #
        # Gates: opt-in via `cache_enabled` on the LLMConfig; bypassed when
        # `temperature > cache_temperature_max` (variance protection — caching
        # randomized outputs defeats the purpose). When cache is unavailable
        # OR not configured, falls through to direct call.
        _cfg = self._adapter._config
        _cache_enabled = bool(getattr(_cfg, "cache_enabled", False))
        _cache_temp_max = float(getattr(_cfg, "cache_temperature_max", 0.3))
        _cache_ttl = int(getattr(_cfg, "cache_ttl_seconds", 604800))  # 7d default
        _cache_temp = float(_cfg.temperature or 0.0)
        _r128_b_use_cache = (
            _cache_enabled and _cache_temp <= _cache_temp_max
        )
        _cache_key: str | None = None
        if _r128_b_use_cache:
            try:
                _cache_key = _r128_b_compute_cache_key(
                    model=_cfg.litellm_model,
                    system=system or "",
                    messages=msgs,
                    temperature=_cache_temp,
                    max_tokens=max_tokens,
                )
                from ..observability.cache import cache as _r128_b_cache
                cached = await _r128_b_cache.get(_cache_key)
                if cached is not None:
                    log.info(
                        "R128.B: LLM cache HIT for model=%s key=%s",
                        _cfg.litellm_model, _cache_key[:24],
                    )
                    return _LiteLLMResponse(cached)
            except Exception as exc:
                log.debug("R128.B: cache get skipped (%s)", exc)
                _cache_key = None  # disable cache write below

        response = await litellm.acompletion(
            model=self._adapter._config.litellm_model,
            messages=msgs,
            max_tokens=max_tokens,
            temperature=self._adapter._config.temperature,
            api_key=self._adapter._config.api_key or None,
            api_base=api_base or None,
            **kwargs,
        )

        # R128.B — store result in cache for replay on identical future calls.
        # Compact 4-field payload (text/finish/model/usage) — survives
        # JSON round-trip through Redis cleanly and rehydrates via
        # `_LiteLLMResponse(dict)` on the next HIT. Avoids dragging the
        # entire litellm pydantic shape (which can carry non-JSON-safe
        # extensions like streaming chunks) through the cache.
        if _r128_b_use_cache and _cache_key:
            try:
                from ..observability.cache import cache as _r128_b_cache
                _content_text = ""
                _finish = "stop"
                _model_str = ""
                _usage_payload: Any = None
                try:
                    _content_text = response.choices[0].message.content or ""
                    _finish = response.choices[0].finish_reason or "stop"
                    _model_str = getattr(response, "model", "") or ""
                    _usage_attr = getattr(response, "usage", None)
                    if _usage_attr is not None:
                        if hasattr(_usage_attr, "model_dump"):
                            _usage_payload = _usage_attr.model_dump()
                        else:
                            _usage_payload = getattr(_usage_attr, "__dict__", None) or str(_usage_attr)
                except Exception as _extract_exc:
                    log.debug("R128.B: cache payload extract skipped (%s)", _extract_exc)
                _to_cache = {
                    "content_text":  _content_text,
                    "finish_reason": _finish,
                    "model":         _model_str,
                    "usage":         _usage_payload,
                }
                await _r128_b_cache.set(
                    _cache_key, _to_cache, ttl_seconds=_cache_ttl,
                )
                log.info(
                    "R128.B: LLM cache MISS → stored model=%s key=%s ttl=%ds",
                    _cfg.litellm_model, _cache_key[:24], _cache_ttl,
                )
            except Exception as exc:
                log.debug("R128.B: cache set skipped (%s)", exc)

        return _LiteLLMResponse(response)

    def stream(
        self,
        model: str,
        max_tokens: int,
        messages: list[dict],
        **kwargs: Any,
    ) -> _LiteLLMStreamContext:
        system = kwargs.pop("system", None)
        return _LiteLLMStreamContext(
            self._adapter._config, messages, system, max_tokens
        )


# ── Main Adapter ──────────────────────────────────────────────────────────────


class LiteLLMClientAdapter:
    """
    Drop-in replacement for AsyncAnthropic.
    All existing agents work without modification.
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self.provider = config.provider
        self.messages = _MessagesProxy(self)


# ── Factory ───────────────────────────────────────────────────────────────────


# R134.A.1 KEYSTONE — explicit provider validation. Pre-R134.A.1 an unknown
# provider string (e.g., from a future enum extension OR a hand-built
# LLMConfig bypassing the enum's runtime check) silently routed to
# LiteLLMClientAdapter → litellm defaults to OpenAI/api.openai.com → on-prem
# mission violated. Post-R134.A.1 the factory entry-point asserts the
# provider is in the known set and raises with the supported list when
# not. Concrete providers (GOOGLE_GEMINI/OPENAI/AZURE_OPENAI) still go
# through LiteLLMClientAdapter — that's their canonical route, not a
# silent fallback. The fix targets only UNKNOWN values.
_R134_A_KNOWN_PROVIDERS = frozenset({
    LLMProvider.ANTHROPIC,
    LLMProvider.CLAUDE_CODE,
    LLMProvider.OLLAMA,
    LLMProvider.GOOGLE_GEMINI,
    LLMProvider.OPENAI,
    LLMProvider.AZURE_OPENAI,
})


def create_llm_client(config: LLMConfig) -> Any:
    """
    Return the appropriate LLM client for the given project config.

    - Claude Code → specialized CLI wrapper (subprocess)
    - Anthropic   → native AsyncAnthropic (HTTP)
    - Ollama      → OllamaDirectClient (model routing, JSON mode, thinking strip)
    - Google Gemini / OpenAI / Azure OpenAI → LiteLLMClientAdapter (canonical route)

    R134.A.1: raises ValueError when `config.provider` is not in
    `_R134_A_KNOWN_PROVIDERS`. Refuses to silently fall back to LiteLLM
    on unknown providers (which would default to OpenAI and violate the
    "small Ollama on-prem non-negotiable" mission directive).
    """
    import os

    if config.provider not in _R134_A_KNOWN_PROVIDERS:
        raise ValueError(
            f"R134.A.1: unknown LLM provider {config.provider!r}; "
            f"supported: {sorted(p.value for p in _R134_A_KNOWN_PROVIDERS)}. "
            f"Refusing to silently fall back to LiteLLM/OpenAI "
            f"(violates 'small Ollama on-prem non-negotiable')."
        )

    if config.provider == LLMProvider.CLAUDE_CODE:
        from .claude_cli_client import ClaudeCLIClient
        # Claude Code manages its own auth state; no API key passed to constructor
        cli_path = os.environ.get("CLAUDE_CLI_PATH", "claude")
        client = ClaudeCLIClient(cli_path=cli_path)
        client.provider = config.provider
        return client

    if config.provider == LLMProvider.ANTHROPIC:
        from anthropic import AsyncAnthropic
        # Standard API requires an API key
        api_key = config.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        client = AsyncAnthropic(api_key=api_key)
        client.provider = config.provider
        return client

    if config.provider == LLMProvider.OLLAMA:
        # Use OllamaDirectClient (with C1-C5 fixes) instead of LiteLLM, which
        # ignores per-call timeout and lacks JSON-mode auto-detection.
        from .ollama_client import OllamaDirectClient
        base_url = config.base_url or os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
        model = config.model or os.environ.get("ARTA_LLM_MODEL", "arta-qwen-pro:latest")
        # like `http://localhost:11434` resolves to the container itself when
        # ARTA runs in Docker. There's no Ollama there. Warn loudly so the
        # operator sees it before the circuit breaker trips on 5 connection
        # failures and hides the root cause.
        _warn_if_localhost_inside_container(base_url)
        client = OllamaDirectClient(base_url, model)
        client.provider = config.provider
        return client

    return LiteLLMClientAdapter(config)


# ── R127.A — resolve_tool_client (per-tool client factory) ────────────────────

import asyncio as _asyncio_r127  # noqa: E402  (kept local to R127 block)

_r127_a_resolver_lock = _asyncio_r127.Lock()
"""R127.B.2 — module-level async lock guarding the resolver cache so two
concurrent sub-req gen calls don't both miss + both construct the same
underlying client. Double-check-after-acquire pattern keeps the fast
path (cache hit) lock-free."""


def resolve_tool_client(
    base: LLMConfig,
    tool_name: str,
    cache: dict | None = None,
) -> Any:
    """R127.A — return the LLM client appropriate for *tool_name*.

    When ``base.tool_overrides`` has no entry for *tool_name* (or the
    entry is empty), the base client is returned via the cache keyed on
    the base (provider, model). When an override IS present, the merged
    config is resolved via ``resolve_tool_config`` and a (possibly
    distinct) client is constructed and cached per (provider, model).

    The cache parameter is a plain dict (caller-owned) keyed on
    ``(provider_value, model)``. ARTA passes one cache per gen request
    so all reqs in a batch share constructed clients (cheap, but not
    free given `LiteLLMClientAdapter` builds an internal proxy).

    Synchronous variant — safe to call from non-async code AND from
    async code that does NOT have concurrent sub-req gen running.
    See ``resolve_tool_client_async`` for the concurrent variant used
    by R127.B.2.
    """
    from ..models.llm_config import resolve_tool_config as _rtc
    resolved = _rtc(base, tool_name)
    # Cache key intentionally excludes api_key + base_url; if those
    # change, the operator must invalidate the cache (typically by
    # restarting the agent / batch). Keys are (provider value, model).
    cache_key = (resolved.provider.value, resolved.model)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    client = create_llm_client(resolved)
    if cache is not None:
        cache[cache_key] = client
    return client


async def resolve_tool_client_async(
    base: LLMConfig,
    tool_name: str,
    cache: dict | None = None,
) -> Any:
    """R127.B.2 — concurrent-safe variant of ``resolve_tool_client``.

    Used inside ``asyncio.gather``-wrapped per-sub-req gen loops. The
    fast path (cache hit) takes no lock; on miss, we acquire the
    module-level lock + double-check the cache (in case another
    coroutine populated it during the lock wait) before constructing
    the client. This ensures one client per (provider, model) tuple
    even under heavy concurrent gen.
    """
    from ..models.llm_config import resolve_tool_config as _rtc
    resolved = _rtc(base, tool_name)
    cache_key = (resolved.provider.value, resolved.model)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    async with _r127_a_resolver_lock:
        # Double-check after lock acquisition
        if cache is not None and cache_key in cache:
            return cache[cache_key]
        client = create_llm_client(resolved)
        if cache is not None:
            cache[cache_key] = client
        return client


# R126.D — model-name → provider inference patterns.
# Used when client.provider attribute is None/missing (regen-consumer path
# constructs clients without LLMProvider enum tagging). Live evidence from
# logs: "R125.L: prompt is ~8752 tokens — provider=unknown
# model=arta-qwen-pro:latest budget=16000" — the model name CONTAINS
# "arta-qwen-pro" (clearly Ollama) yet provider was tagged "unknown"
# because the attribute was missing, falling R125.L back to the 16K
# default budget instead of Ollama's 28K.
_R126_D_MODEL_PROVIDER_PATTERNS = (
    # (substring, inferred_provider) — order matters; first match wins.
    # Ollama models — match before Claude (some Ollama models use Claude-like
    # task names like "claude-sonnet-4-6" mapped via ollama_client routing,
    # but those are ALWAYS reached through an Ollama client w/ provider set;
    # the unknown-provider path is reached when client.provider is missing).
    ("arta-qwen-pro",        "ollama"),
    ("qwen3",                "ollama"),
    ("qwen2.5",              "ollama"),
    ("qwen2",                "ollama"),
    ("llama3",               "ollama"),
    ("llama2",               "ollama"),
    ("mistral",              "ollama"),
    ("mixtral",              "ollama"),
    ("phi3",                 "ollama"),
    ("gemma",                "ollama"),
    ("deepseek",             "ollama"),
    ("codellama",            "ollama"),
    # Anthropic models
    ("claude-sonnet",        "anthropic"),
    ("claude-opus",          "anthropic"),
    ("claude-haiku",         "anthropic"),
    ("claude-3-5",           "anthropic"),
)


def _r126_d_infer_provider_from_model(model_name: str) -> str | None:
    """R126.D — best-effort provider inference from model-name pattern.

    Used as fallback when `client.provider` attribute is missing. Returns
    None when no pattern matches (caller falls back to "unknown").
    """
    if not model_name:
        return None
    name_lower = str(model_name).lower()
    for pattern, inferred in _R126_D_MODEL_PROVIDER_PATTERNS:
        if pattern in name_lower:
            return inferred
    return None


# R125.K — single source of truth for provider tagging across gen paths.
def llm_provider_tag(client: Any) -> dict:
    """R125.K — canonical provider/model/strategy tag for _gen_metrics.

    Stamps the LLM provenance on every gen-emitted test row so the dashboard
    (R125.I gen-health endpoint) can compare quality side-by-side across
    providers AND R125.M can assert batch-vs-serial strategy equivalence.

    Strategy mapping mirrors the gating in automation_engineer at
    `_generate_all_tools` (line ~723): Ollama uses Strategy A (sequential,
    one LLM call per tool), everything else uses Strategy B (batch, one
    LLM call produces all tools with separator markers).

    R126.D — when client.provider attribute is missing (regen-consumer
    path), fall back to model-name pattern inference so R125.L budgeting
    + R126.A prompt-manifest gating get the right provider classification.

    Returns a JSON-serializable dict (no enum, no live client refs) so it
    can be persisted in metadata + read by the dashboard layer.

    Graceful: when client is None OR no inference possible, returns
    {"provider": "unknown", "model": "unknown", "strategy": "unknown"}.
    """
    if client is None:
        return {"provider": "unknown", "model": "unknown", "strategy": "unknown"}
    provider_attr = getattr(client, "provider", None)
    model_attr = (
        getattr(client, "model", None)
        or getattr(client, "_model", None)
        or "unknown"
    )
    # Normalise provider — could be the LLMProvider enum, its string value,
    # or None
    provider_str = "unknown"
    if provider_attr is not None:
        if hasattr(provider_attr, "value"):
            provider_str = str(provider_attr.value)
        else:
            provider_str = str(provider_attr)
    # R126.D — model-name fallback when explicit provider is missing
    if provider_str == "unknown" or provider_str == "None":
        inferred = _r126_d_infer_provider_from_model(str(model_attr))
        if inferred:
            provider_str = inferred
    # Strategy mirrors the gating in automation_engineer
    is_ollama = (
        provider_attr == LLMProvider.OLLAMA
        or provider_str.lower() == "ollama"
    )
    strategy = "sequential" if is_ollama else "batch"
    return {
        "provider": provider_str,
        "model": str(model_attr),
        "strategy": strategy,
    }


def default_client() -> Any:
    """Return the default Anthropic client from environment variables."""
    import os
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
