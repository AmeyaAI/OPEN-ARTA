"""G2.4 — Cache helper with Redis primary + in-memory TTL fallback.

Used by hot read endpoints (/dashboard/trends, /gates/nfr) to avoid repeatedly
running expensive SQL aggregations / percentile computations.

Usage:
    from src.observability.cache import cache

    cached = await cache.get(key)
    if cached is not None:
        return cached
    fresh = await compute_expensive_thing()
    await cache.set(key, fresh, ttl_seconds=60)
    return fresh
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

log = logging.getLogger("arta.cache")


class _TTLMemoryCache:
    """Fallback in-memory cache with per-key TTL."""

    def __init__(self, max_entries: int = 512) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._max = max_entries
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.monotonic():
                self._data.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: Any, ttl: float) -> None:
        async with self._lock:
            # Evict if over capacity (simple: drop oldest by expiry)
            if len(self._data) >= self._max:
                try:
                    oldest_key = min(self._data.items(), key=lambda kv: kv[1][0])[0]
                    self._data.pop(oldest_key, None)
                except ValueError:
                    pass
            self._data[key] = (time.monotonic() + float(ttl), value)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)


class _Cache:
    """Redis-primary, memory-fallback cache. Transparent to callers."""

    def __init__(self) -> None:
        self._memory = _TTLMemoryCache()
        self._redis = None
        self._redis_probed = False

    def _get_redis(self):
        """Lazy-import redis and connect; None if unavailable."""
        if self._redis_probed:
            return self._redis
        self._redis_probed = True
        url = os.environ.get("REDIS_URL")
        if not url:
            return None
        try:
            import redis.asyncio as aredis  # type: ignore
            self._redis = aredis.from_url(url, decode_responses=True)
            return self._redis
        except Exception as exc:
            log.debug("Cache: Redis unavailable (%s), using memory fallback", exc)
            return None

    async def get(self, key: str) -> Any | None:
        r = self._get_redis()
        if r is not None:
            try:
                raw = await r.get(f"arta:cache:{key}")
                if raw is not None:
                    return json.loads(raw)
            except Exception as exc:
                log.debug("Cache GET Redis error (%s): falling back to memory", exc)
        return await self._memory.get(key)

    async def set(self, key: str, value: Any, ttl_seconds: float = 60.0) -> None:
        r = self._get_redis()
        if r is not None:
            try:
                await r.set(f"arta:cache:{key}", json.dumps(value, default=str), ex=int(ttl_seconds))
                # Also populate memory for fast hot-path access
                await self._memory.set(key, value, ttl_seconds)
                return
            except Exception as exc:
                log.debug("Cache SET Redis error (%s): falling back to memory", exc)
        await self._memory.set(key, value, ttl_seconds)

    async def delete(self, key: str) -> None:
        r = self._get_redis()
        if r is not None:
            try:
                await r.delete(f"arta:cache:{key}")
            except Exception:
                pass
        await self._memory.delete(key)


# Singleton
cache = _Cache()
