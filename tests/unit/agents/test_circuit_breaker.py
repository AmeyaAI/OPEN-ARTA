"""F9-11: Cover the I8 circuit breaker — state transitions, fail-counting,
HALF_OPEN trial logic. Without these tests the F6-10 cascade-prevention
is one bad refactor away from silent breakage.
"""
from __future__ import annotations

import asyncio

import pytest

from src.agents.circuit_breaker import CircuitBreaker, CircuitOpenError, get_breaker


@pytest.fixture
def breaker():
    # Tight thresholds so tests don't sleep
    return CircuitBreaker(name="test", fail_threshold=3, window_secs=10.0, cooldown_secs=0.1)


async def _ok(*a, **kw):
    return "ok"


async def _fail(*a, **kw):
    raise RuntimeError("forced")


class TestCircuitBreaker:

    async def test_starts_closed(self, breaker):
        assert breaker.state == "CLOSED"

    async def test_successful_call_returns_result(self, breaker):
        assert await breaker.call(_ok) == "ok"
        assert breaker.state == "CLOSED"

    async def test_opens_after_threshold_failures(self, breaker):
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await breaker.call(_fail)
        assert breaker.state == "OPEN"

    async def test_open_circuit_rejects_calls_with_circuit_open_error(self, breaker):
        # Trip the circuit
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await breaker.call(_fail)
        # Next call rejected without invoking the wrapped fn
        with pytest.raises(CircuitOpenError):
            await breaker.call(_ok)

    async def test_recovers_to_closed_after_successful_half_open_trial(self, breaker):
        # Trip
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await breaker.call(_fail)
        # Wait past cooldown
        await asyncio.sleep(0.15)
        # First call after cooldown is HALF_OPEN trial — succeeds → CLOSED
        assert await breaker.call(_ok) == "ok"
        assert breaker.state == "CLOSED"

    async def test_failed_half_open_trial_re_opens(self, breaker):
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await breaker.call(_fail)
        await asyncio.sleep(0.15)
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
        assert breaker.state == "OPEN"

    async def test_get_breaker_returns_singleton_per_provider(self):
        a = get_breaker("provider-x")
        b = get_breaker("provider-x")
        c = get_breaker("provider-y")
        assert a is b
        assert a is not c
