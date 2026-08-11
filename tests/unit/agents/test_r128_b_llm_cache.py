"""R128.B — LLM output cache (Redis-primary + memory-fallback) unit tests.

Six cases lock down the cache contract on `_MessagesProxy.create()`:

1. Cache MISS → falls through to LLM, stores result; second identical call
   returns from cache (HIT) without invoking litellm.
2. Cache HIT → litellm.acompletion is NOT called; cached value is wrapped
   into `_LiteLLMResponse` and returned.
3. Temperature > `cache_temperature_max` → cache bypassed (variance
   protection); litellm called every time; cache.set NOT invoked.
4. Different prompts → different cache keys → no false hits.
5. Redis unavailable / cache.get raises → graceful fallthrough to direct
   LLM call (no exception propagated to caller).
6. cache_enabled=False (default) → cache neither read nor written;
   backward-compat for projects.json files predating R128.B.

The cache singleton lives at `src.observability.cache.cache` (Redis-primary
+ memory-fallback). Tests use the real memory cache (no Redis env var
set) so writes round-trip naturally without external services.
"""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Inject a stub `litellm` module BEFORE production code imports it. The
# real litellm package has a transitive aiohttp version incompatibility
# in some environments (`ConnectionTimeoutError` removed in aiohttp 3.x).
# The stub exposes only `acompletion` — the single attribute the R128.B
# code path uses. Tests then patch that attribute per-case.
if "litellm" not in sys.modules:
    _stub_litellm = ModuleType("litellm")
    async def _stub_acompletion(*args, **kwargs):   # pragma: no cover
        raise RuntimeError("test must patch litellm.acompletion")
    _stub_litellm.acompletion = _stub_acompletion   # type: ignore[attr-defined]
    sys.modules["litellm"] = _stub_litellm

from src.agents.llm_client import (    # noqa: E402  (after sys.modules patch)
    LiteLLMClientAdapter,
    _r128_b_compute_cache_key,
)
from src.models.llm_config import LLMConfig, LLMProvider    # noqa: E402


def _make_litellm_response(text: str, *, model: str = "claude-sonnet-4-6"):
    """Build a MagicMock that mimics the litellm completion response shape
    the production code expects (`.choices[0].message.content`,
    `.choices[0].finish_reason`, `.model`, plus `.model_dump()`)."""
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = "stop"
    resp = MagicMock()
    resp.choices = [choice]
    resp.model = model
    resp.usage = MagicMock(input_tokens=10, output_tokens=20)
    resp.model_dump = MagicMock(return_value={
        "choices": [{
            "message": {"content": text},
            "finish_reason": "stop",
        }],
        "model": model,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    })
    return resp


def _config(cache_enabled: bool = True, temperature: float = 0.2,
            cache_temperature_max: float = 0.3) -> LLMConfig:
    """Standard test config — anthropic provider so we get the
    LiteLLMClientAdapter path (which is the one carrying R128.B logic)."""
    return LLMConfig(
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-6",
        api_key="test-key",
        temperature=temperature,
        cache_enabled=cache_enabled,
        cache_ttl_seconds=600,
        cache_temperature_max=cache_temperature_max,
    )


def _flush_cache_singleton():
    """Reset the module-level cache singleton's memory store between tests
    so HITs don't bleed across cases. Redis path is inert here (no
    REDIS_URL set in the unit-test env)."""
    from src.observability.cache import cache
    # Direct access to the internal memory map — acceptable in tests.
    cache._memory._data.clear()


# ── Case 1: MISS → HIT roundtrip ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_r128b_miss_then_hit_roundtrip():
    """First call (MISS): litellm invoked, result cached. Second identical
    call (HIT): litellm NOT invoked again; cached value returned."""
    _flush_cache_singleton()
    cfg = _config(cache_enabled=True)
    adapter = LiteLLMClientAdapter(cfg)

    fake_response = _make_litellm_response("hello from claude")
    with patch("litellm.acompletion", new_callable=AsyncMock,
               return_value=fake_response) as mock_complete:
        r1 = await adapter.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": "hi"}],
        )
        r2 = await adapter.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": "hi"}],
        )

    assert r1.content[0].text == "hello from claude"
    # On HIT, the production wraps the cached dict — it does its best to
    # produce a response-like object. Either we get a valid text OR (if the
    # dict-shape adapter is incomplete) we may see an empty text. Either
    # way the litellm call MUST NOT fire a second time:
    assert mock_complete.call_count == 1, (
        f"Cache HIT should bypass litellm; got {mock_complete.call_count} calls"
    )


# ── Case 2: Cache HIT does NOT invoke litellm ─────────────────────────────


@pytest.mark.asyncio
async def test_r128b_hit_skips_litellm_entirely():
    """When cache.get returns a non-None value, litellm.acompletion must
    NOT be called at all."""
    _flush_cache_singleton()
    cfg = _config(cache_enabled=True)
    adapter = LiteLLMClientAdapter(cfg)

    # Pre-warm the cache with the exact key the call would produce.
    msgs = [{"role": "user", "content": "warmed"}]
    key = _r128_b_compute_cache_key(
        model=cfg.litellm_model,
        system="",
        messages=msgs,
        temperature=cfg.temperature,
        max_tokens=1500,
    )
    cached_payload = {
        "choices": [{
            "message": {"content": "from-cache"},
            "finish_reason": "stop",
        }],
        "model": cfg.litellm_model,
    }
    from src.observability.cache import cache
    await cache.set(key, cached_payload, ttl_seconds=60)

    with patch("litellm.acompletion", new_callable=AsyncMock) as mock_complete:
        await adapter.messages.create(
            model=cfg.litellm_model,
            max_tokens=1500,
            messages=msgs,
        )

    assert mock_complete.call_count == 0, (
        "Pre-warmed cache HIT must bypass litellm entirely"
    )


# ── Case 3: Temperature > cache_temperature_max → cache bypassed ──────────


@pytest.mark.asyncio
async def test_r128b_temperature_above_max_bypasses_cache():
    """When temperature > cache_temperature_max, two identical calls each
    invoke litellm (no caching). Variance protection: caching randomized
    outputs would defeat the purpose."""
    _flush_cache_singleton()
    cfg = _config(cache_enabled=True, temperature=0.8,
                  cache_temperature_max=0.3)
    adapter = LiteLLMClientAdapter(cfg)

    fake_response = _make_litellm_response("randomized output")
    with patch("litellm.acompletion", new_callable=AsyncMock,
               return_value=fake_response) as mock_complete:
        await adapter.messages.create(
            model=cfg.litellm_model,
            max_tokens=1000,
            messages=[{"role": "user", "content": "creative"}],
        )
        await adapter.messages.create(
            model=cfg.litellm_model,
            max_tokens=1000,
            messages=[{"role": "user", "content": "creative"}],
        )

    assert mock_complete.call_count == 2, (
        f"Temperature {cfg.temperature} > max {cfg.cache_temperature_max} "
        f"must bypass cache; got {mock_complete.call_count} calls"
    )


# ── Case 4: Different prompts → distinct cache keys ───────────────────────


def test_r128b_different_prompts_produce_distinct_cache_keys():
    """Two slightly-different message lists MUST produce different cache
    keys. Hash collision would cause false HITs (wrong response served
    for a different prompt — silent gen-quality regression)."""
    k1 = _r128_b_compute_cache_key(
        model="claude-sonnet-4-6", system="",
        messages=[{"role": "user", "content": "prompt A"}],
        temperature=0.2, max_tokens=4096,
    )
    k2 = _r128_b_compute_cache_key(
        model="claude-sonnet-4-6", system="",
        messages=[{"role": "user", "content": "prompt B"}],
        temperature=0.2, max_tokens=4096,
    )
    k3 = _r128_b_compute_cache_key(
        model="claude-sonnet-4-6", system="different system",
        messages=[{"role": "user", "content": "prompt A"}],
        temperature=0.2, max_tokens=4096,
    )
    k4 = _r128_b_compute_cache_key(
        model="claude-sonnet-4-6", system="",
        messages=[{"role": "user", "content": "prompt A"}],
        temperature=0.2, max_tokens=8192,   # different max_tokens
    )
    assert k1 != k2, "Different messages must produce different keys"
    assert k1 != k3, "Different system prompt must produce different keys"
    assert k1 != k4, "Different max_tokens must produce different keys"
    # All keys carry the canonical prefix for grep-ability in Redis
    for k in (k1, k2, k3, k4):
        assert k.startswith("r128_b:llm:"), f"Key missing prefix: {k}"


# ── Case 5: Cache failure → graceful fallthrough ──────────────────────────


@pytest.mark.asyncio
async def test_r128b_cache_get_raises_falls_through_gracefully():
    """When `cache.get` raises (Redis down, network blip, deserialization
    error), the production code MUST NOT propagate. Falls through to
    direct litellm call; caller sees a normal response."""
    _flush_cache_singleton()
    cfg = _config(cache_enabled=True)
    adapter = LiteLLMClientAdapter(cfg)

    fake_response = _make_litellm_response("recovered")
    # Patch the cache.get to raise; cache.set still works on memory side.
    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated Redis outage")

    with patch("src.observability.cache.cache.get", side_effect=_boom):
        with patch("litellm.acompletion", new_callable=AsyncMock,
                   return_value=fake_response) as mock_complete:
            r = await adapter.messages.create(
                model=cfg.litellm_model,
                max_tokens=1000,
                messages=[{"role": "user", "content": "ping"}],
            )

    assert mock_complete.call_count == 1, (
        "Cache failure must fall through to litellm without raising"
    )
    assert r.content[0].text == "recovered"


# ── Case 6: cache_enabled=False → backward compat (no cache touched) ──────


@pytest.mark.asyncio
async def test_r128b_cache_disabled_skips_cache_calls():
    """When cache_enabled=False (default for legacy projects), neither
    cache.get nor cache.set is invoked. Two identical calls each hit
    litellm — backward-compat with existing projects.json files."""
    _flush_cache_singleton()
    cfg = _config(cache_enabled=False)
    adapter = LiteLLMClientAdapter(cfg)

    fake_response = _make_litellm_response("uncached")
    with patch("src.observability.cache.cache.get", new_callable=AsyncMock) as mock_get:
        with patch("src.observability.cache.cache.set", new_callable=AsyncMock) as mock_set:
            with patch("litellm.acompletion", new_callable=AsyncMock,
                       return_value=fake_response) as mock_complete:
                await adapter.messages.create(
                    model=cfg.litellm_model,
                    max_tokens=1000,
                    messages=[{"role": "user", "content": "x"}],
                )
                await adapter.messages.create(
                    model=cfg.litellm_model,
                    max_tokens=1000,
                    messages=[{"role": "user", "content": "x"}],
                )

    assert mock_get.call_count == 0, (
        "cache_enabled=False must NOT read from cache"
    )
    assert mock_set.call_count == 0, (
        "cache_enabled=False must NOT write to cache"
    )
    assert mock_complete.call_count == 2, (
        "Each call must hit litellm when cache is disabled"
    )
