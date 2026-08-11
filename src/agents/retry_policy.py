"""R134.C — Single-source-of-truth retry policy for LLM call sites.

Pre-R134.C: every agent (8 files) duplicated the tuple
`(anthropic.RateLimitError, anthropic.APIConnectionError, RuntimeError)`.
RuntimeError was meant to catch Ollama/Claude CLI client failures, but
async network paths via `httpx` raise concrete httpx errors (ConnectError,
ReadTimeout, RemoteProtocolError) that ALSO need to be retried — those
flowed through unretried, breaking the "small Ollama on-prem" reliability
contract whenever the daemon hiccupped.

Post-R134.C: one canonical `LLM_RETRYABLE_EXC` tuple imported by all
8 agents. Any future addition (e.g., new Anthropic SDK error class)
lands in one place.
"""
from __future__ import annotations

import asyncio

import anthropic
import httpx


# ── Network errors raised by async httpx clients (Ollama, Anthropic SDK,
# Gemini, LiteLLM all use httpx underneath). Pre-R134.C: NONE of these
# were in the retry tuple → transient network failures masqueraded as
# permanent gen failures → R130.J carry-rate appeared lower than truth.
_R134_C_RETRYABLE_NETWORK_EXC: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    asyncio.TimeoutError,
)


# ── Canonical retry tuple. Shared across all 8 agents.
# RuntimeError stays for ClaudeCLIClient / OllamaDirectClient subprocess
# failures (existing R130.* + F5-4 contract); anthropic.* for Anthropic
# SDK; _R134_C_RETRYABLE_NETWORK_EXC for transient httpx network errors.
LLM_RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    RuntimeError,
) + _R134_C_RETRYABLE_NETWORK_EXC
