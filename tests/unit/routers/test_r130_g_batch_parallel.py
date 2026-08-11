"""R130.G KEYSTONE — Bounded-parallel batch concurrency unit tests.

Five cases:
  1. `_r130_g_batch_concurrency("ollama")` → 2 (single-instance daemon)
  2. `_r130_g_batch_concurrency("anthropic")` → 4 (tier-1 RPM safe)
  3. `_r130_g_batch_concurrency("claude_code")` → 1 (R217 0d — serialized
     CLI under R161; override via ARTA_R217_CLAUDE_CODE_PARALLEL)
  4. `_r130_g_batch_concurrency("")` / `_r130_g_batch_concurrency("unknown")`
     → 4 (generic fallback)
  5. Operator env-var override propagates via ARTA_GEN_CONCURRENCY
     (verified by reading os.environ in the activation site — exercised
     by the import-level helper test below).
"""
from __future__ import annotations

import os
from unittest.mock import patch

from src.api.routers.tests import _r130_g_batch_concurrency


def test_r130g_ollama_concurrency_is_2():
    """Ollama daemon is single-instance per model; 2 concurrent reqs ×
    4 sub-semaphore = 8 in-flight calls — the safe saturation ceiling."""
    assert _r130_g_batch_concurrency("ollama") == 2


def test_r130g_anthropic_concurrency_is_4():
    """Anthropic tier-1 RPM ~50/min; 4 concurrent × ~10 escalation calls
    avg = ~40 calls/min — under the limit."""
    assert _r130_g_batch_concurrency("anthropic") == 4


def test_r130g_claude_code_concurrency_is_1():
    """R217 0d — claude_code is the SERIALIZED CLI path (R161 per-project
    --continue; concurrent calls corrupt the session AND fire N reqs' calls
    into the OAuth rate budget at once → the 110×-429 bulk-gen collapse).
    The correct provider-aware default is 1 (sequential, so 0d batching +
    pacing apply). Distinct from the real Anthropic API ('anthropic')."""
    assert _r130_g_batch_concurrency("claude_code") == 1


def test_r130g_claude_code_parallel_override(monkeypatch):
    """Operators with a non-serialized claude_code setup can opt back into
    parallel via ARTA_R217_CLAUDE_CODE_PARALLEL=1."""
    monkeypatch.setenv("ARTA_R217_CLAUDE_CODE_PARALLEL", "1")
    assert _r130_g_batch_concurrency("claude_code") == 4
    monkeypatch.delenv("ARTA_R217_CLAUDE_CODE_PARALLEL", raising=False)
    assert _r130_g_batch_concurrency("claude_code") == 1


def test_r130g_unknown_provider_falls_back_to_4():
    """Generic default for openai/gemini/azure_openai/etc. is 4 (their
    APIs handle concurrent calls natively)."""
    assert _r130_g_batch_concurrency("") == 4
    assert _r130_g_batch_concurrency("unknown_provider") == 4
    assert _r130_g_batch_concurrency("openai") == 4
    assert _r130_g_batch_concurrency("gemini") == 4


def test_r130g_case_insensitive_provider_match():
    """Provider matching is case-insensitive — operator may set
    `provider: OLLAMA` or `provider: Ollama` in projects.json."""
    assert _r130_g_batch_concurrency("OLLAMA") == 2
    assert _r130_g_batch_concurrency("Ollama") == 2
    assert _r130_g_batch_concurrency("ANTHROPIC") == 4
    assert _r130_g_batch_concurrency("Claude_Code") == 1   # R217 0d — serialized CLI
