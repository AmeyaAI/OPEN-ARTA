"""I8 — Circuit Breaker for LLM provider calls.

Prevents the cascade failure where a degraded LLM provider causes 21 requirements
to each chew through 3 × 180s retries (~3+ hours wasted).

State machine:
  CLOSED   — normal operation; failures counted in a sliding window
  OPEN     — too many failures; reject calls immediately for cooldown_secs
  HALF_OPEN — cooldown elapsed; allow one trial call to probe recovery

F6-10 — additionally, every breaker.call() now waits on a global semaphore
sized by `ARTA_LLM_MAX_CONCURRENCY` (default 4). This caps the total number of
in-flight LLM calls across all agents, so a single Generate-All can't spawn
hundreds of parallel Anthropic requests until rate-limited.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")
log = logging.getLogger("arta.circuit_breaker")


# F6-10: Process-wide cap on outstanding LLM calls. Lazily created so we pick
# up the env var at first use rather than at import time (which is hostile to
# tests that override it). Tune via ARTA_LLM_MAX_CONCURRENCY (1–32 reasonable).
_LLM_SEMAPHORE: asyncio.Semaphore | None = None


def _get_llm_semaphore() -> asyncio.Semaphore:
    global _LLM_SEMAPHORE
    if _LLM_SEMAPHORE is None:
        try:
            cap = max(1, min(32, int(os.environ.get("ARTA_LLM_MAX_CONCURRENCY", "4"))))
        except (TypeError, ValueError):
            cap = 4
        _LLM_SEMAPHORE = asyncio.Semaphore(cap)
        log.info("LLM concurrency cap: %d in-flight calls (set via ARTA_LLM_MAX_CONCURRENCY)", cap)
    return _LLM_SEMAPHORE


class CircuitBreaker:
    """Per-provider circuit breaker. Use one instance per provider name."""

    def __init__(
        self,
        name: str,
        fail_threshold: int = 5,
        window_secs: float = 60.0,
        cooldown_secs: float = 30.0,
    ):
        self.name = name
        self.fail_threshold = fail_threshold
        self.window_secs = window_secs
        self.cooldown_secs = cooldown_secs

        self._failure_times: deque[float] = deque()
        self._state: str = "CLOSED"  # CLOSED | OPEN | HALF_OPEN
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    def _prune_old_failures(self) -> None:
        cutoff = time.monotonic() - self.window_secs
        while self._failure_times and self._failure_times[0] < cutoff:
            self._failure_times.popleft()

    async def _maybe_transition_to_half_open(self) -> None:
        if self._state == "OPEN":
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.cooldown_secs:
                self._state = "HALF_OPEN"
                log.info("circuit_breaker[%s]: cooldown elapsed (%.1fs) — HALF_OPEN trial",
                         self.name, elapsed)

    async def call(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Invoke `func(*args, **kwargs)` through the circuit breaker.

        Raises CircuitOpenError immediately if circuit is OPEN.

        F6-10: Acquires the global LLM-concurrency semaphore so a Generate-All
        that fans out (5 tools × 21 reqs) can't open hundreds of parallel
        Anthropic connections.
        """
        async with self._lock:
            await self._maybe_transition_to_half_open()
            if self._state == "OPEN":
                remaining = self.cooldown_secs - (time.monotonic() - self._opened_at)
                raise CircuitOpenError(
                    f"Circuit '{self.name}' is OPEN — cooldown {remaining:.0f}s remaining"
                )

        # Hold the global LLM cap for the duration of the actual call. The
        # circuit-breaker lock above is short-lived; this one wraps the await.
        sem = _get_llm_semaphore()
        await sem.acquire()
        # F7-4: each LLM call becomes a child span so distributed traces show
        # provider, model, and the full latency of every breaker.call().
        try:
            from ..observability.tracing import span as _ts
        except Exception:
            _ts = None  # type: ignore[assignment]
        try:
            cm = _ts(f"llm.call.{self.name}",
                     provider=self.name,
                     model=str(kwargs.get("model", "unknown"))) if _ts else None
            if cm is not None:
                with cm:
                    result = await func(*args, **kwargs)
            else:
                result = await func(*args, **kwargs)
        except Exception:
            async with self._lock:
                now = time.monotonic()
                self._failure_times.append(now)
                self._prune_old_failures()
                if self._state == "HALF_OPEN":
                    self._state = "OPEN"
                    self._opened_at = now
                    log.warning("circuit_breaker[%s]: HALF_OPEN trial failed — re-OPEN",
                                self.name)
                elif len(self._failure_times) >= self.fail_threshold:
                    self._state = "OPEN"
                    self._opened_at = now
                    log.error("circuit_breaker[%s]: %d failures in %.0fs — OPEN for %.0fs",
                              self.name, len(self._failure_times),
                              self.window_secs, self.cooldown_secs)
            raise

        finally:
            sem.release()  # F6-10

        # Success — reset on HALF_OPEN
        async with self._lock:
            if self._state == "HALF_OPEN":
                self._state = "CLOSED"
                self._failure_times.clear()
                log.info("circuit_breaker[%s]: HALF_OPEN trial succeeded — CLOSED",
                         self.name)
                # R125.J — auto-queue rebuild of reqs that failed during the
                # gen_source=failed by Claude Code auth lapse, they stayed
                # broken until the operator manually re-triggered each one.
                # Post-R125.J: circuit recovery automatically emits regen
                # markers via the recovery_queue module. R30.3 still gates
                # (operator_review + sut_regression NOT auto-healed).
                try:
                    from ..api.services.recovery_queue import on_circuit_recovered
                    on_circuit_recovered(self.name)
                except Exception as _r125_j_exc:
                    log.debug(
                        "R125.J: recovery callback skipped for %s: %s",
                        self.name, _r125_j_exc,
                    )
        return result


class CircuitOpenError(RuntimeError):
    """Raised when a CircuitBreaker rejects a call due to OPEN state."""


# Per-provider singletons
_BREAKERS: dict[str, CircuitBreaker] = {}


def get_breaker(provider: str) -> CircuitBreaker:
    """Return (and lazily create) the circuit breaker for a provider name."""
    if provider not in _BREAKERS:
        _BREAKERS[provider] = CircuitBreaker(name=provider)
    return _BREAKERS[provider]


def get_breakers_snapshot() -> dict:
    """R90.7 — snapshot of all known breakers for operator visibility.

    Returns one entry per provider that has been touched (lazy-created
    on first call to get_breaker). Each entry has state, failure count
    in current window, cooldown remaining (if OPEN), and opened_at.

    Used by /api/health/llm-circuit-breaker. Operators see this when k6
    or other gen consistently fails — circuit-open is the canonical
    signal that the LLM is degraded, not ARTA.
    """
    out: dict = {}
    now = time.monotonic()
    for name, br in _BREAKERS.items():
        # Prune failures in a thread-safe-ish way: we can't easily take
        # the lock here (we're not in an async context). Read state
        # without mutation; counts may be slightly stale but that's OK
        # for a status endpoint.
        cutoff = now - br.window_secs
        recent_failures = sum(1 for t in br._failure_times if t >= cutoff)
        cooldown_remaining = 0.0
        if br._state == "OPEN" and br._opened_at:
            cooldown_remaining = max(0.0, br.cooldown_secs - (now - br._opened_at))
        out[name] = {
            "state": br._state,
            "failure_count_in_window": recent_failures,
            "fail_threshold": br.fail_threshold,
            "window_secs": br.window_secs,
            "cooldown_secs": br.cooldown_secs,
            "cooldown_remaining_secs": round(cooldown_remaining, 1),
            "opened_at_monotonic": br._opened_at if br._state in ("OPEN", "HALF_OPEN") else None,
        }
    return out
