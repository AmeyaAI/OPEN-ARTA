"""R134.C tests — single-source-of-truth retry tuple covers httpx network errors.

Pre-R134.C: every agent (8 files) used the literal tuple
`(anthropic.RateLimitError, anthropic.APIConnectionError, RuntimeError)`
which DID NOT include httpx.ConnectError / ReadTimeout / RemoteProtocolError.
Transient Ollama daemon hiccups fell through unretried → gen failed
permanently on what should have been recoverable network blips.

Post-R134.C: `LLM_RETRYABLE_EXC` from `src/agents/retry_policy.py` is
the single tuple imported by all 8 agents. Adding/removing retryable
exceptions lands in one place.
"""
from __future__ import annotations

import asyncio

import anthropic
import httpx
import pytest

from src.agents.retry_policy import (
    LLM_RETRYABLE_EXC,
    _R134_C_RETRYABLE_NETWORK_EXC,
)


def test_r134_c_includes_anthropic_sdk_errors():
    """Regression guard — pre-R134.C tuple's anthropic.* errors must
    remain in the new SSoT tuple."""
    assert anthropic.RateLimitError in LLM_RETRYABLE_EXC
    assert anthropic.APIConnectionError in LLM_RETRYABLE_EXC


def test_r134_c_includes_runtime_error():
    """Regression guard — RuntimeError preserved for ClaudeCLIClient /
    OllamaDirectClient subprocess failures (F5-4 + Gap-2 contract)."""
    assert RuntimeError in LLM_RETRYABLE_EXC


def test_r134_c_widened_with_httpx_network_errors():
    """R134.C target — httpx async network errors NOW retryable.
    Pre-R134.C: transient Ollama daemon network blips broke gen.
    Post-R134.C: retried via the exponential-backoff @retry decorator."""
    assert httpx.ConnectError in LLM_RETRYABLE_EXC
    assert httpx.ReadTimeout in LLM_RETRYABLE_EXC
    assert httpx.RemoteProtocolError in LLM_RETRYABLE_EXC
    assert httpx.ConnectTimeout in LLM_RETRYABLE_EXC
    assert httpx.PoolTimeout in LLM_RETRYABLE_EXC
    assert asyncio.TimeoutError in LLM_RETRYABLE_EXC


def test_r134_c_does_not_include_non_retryable_errors():
    """Non-network programmer errors (ValueError, TypeError) must NOT
    be retried — those indicate a permanent bug, not a transient blip.
    Retrying would mask the bug behind 3× exponential backoff."""
    assert ValueError not in LLM_RETRYABLE_EXC
    assert TypeError not in LLM_RETRYABLE_EXC
    assert KeyError not in LLM_RETRYABLE_EXC
    # Specific anthropic permanent errors NOT in retry set
    assert anthropic.AuthenticationError not in LLM_RETRYABLE_EXC


def test_r134_c_all_eight_agents_import_shared_tuple():
    """SSoT contract — every @retry call site across 8 agents must import
    LLM_RETRYABLE_EXC from retry_policy (not redefine the tuple inline).

    This regression guard catches the case where a future contributor
    re-introduces the inline `(anthropic.RateLimitError, ...)` literal
    by mistake. We do a direct module-import check to ensure no agent
    has dropped the SSoT pattern."""
    from src.agents import (
        analytics_test_agent,
        atdd_designer,
        automation_engineer,
        dataset_recipe,
        defect_intel,
        requirement_intel,
        self_healing,
        strategy_architect,
    )
    # Each module must have the symbol from retry_policy in scope
    for module in (
        analytics_test_agent, atdd_designer, automation_engineer,
        dataset_recipe, defect_intel, requirement_intel,
        self_healing, strategy_architect,
    ):
        assert hasattr(module, "LLM_RETRYABLE_EXC"), (
            f"Module {module.__name__} missing R134.C LLM_RETRYABLE_EXC import"
        )
        assert getattr(module, "LLM_RETRYABLE_EXC") is LLM_RETRYABLE_EXC, (
            f"Module {module.__name__} has a DIFFERENT LLM_RETRYABLE_EXC "
            "than the SSoT (R134.C broken — re-check import path)"
        )
