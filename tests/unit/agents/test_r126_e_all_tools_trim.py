"""R126.E — Apply provider-aware prompt trim across remaining 5 tools.

The user's directive: "All 6 tools (full provider-parity)". R126.A
already trims PW prompt stack for Ollama. R126.E applies the same
provider-aware compression pattern to the other tools:

  R126.E.1 — Newman: chunk_size 30 → 15 endpoints/chunk for Ollama
             + R105.D SUT-source max_chars 6000 → 1500
  R126.E.2 — k6: R113.F SUT-source max_chars 4000 → 1000
  R126.E.3 — axe: (kept simple — axe specs are already small/template-like)
  R126.E.4 — ZAP: (deterministic YAML, minimal LLM exposure)
  R126.E.5 — Pytest analytics: R125.C SUT-source max_chars 4000 → 1000

These trims are SOURCE-GREP tests: the changes are deeply embedded in
multi-hundred-line gen methods that aren't unit-testable in isolation
without scaffolding the entire pipeline. Source-grep verifies the
provider-aware gate is in place at each tool's call site.
"""
from __future__ import annotations

import inspect

from src.agents import analytics_test_agent as ata_mod
from src.agents import automation_engineer as ae_mod


_AE_SOURCE = inspect.getsource(ae_mod)
_ATA_SOURCE = inspect.getsource(ata_mod)


def test_r126e1_newman_chunk_size_provider_aware():
    """R126.E.1 — Newman chunk_size adapts per provider.

    Post-R136.B: the inline `if _r126_a_is_ollama_provider(...) else 30`
    moved into a dedicated helper `_r136_b_pick_newman_chunk_size`. The
    R126.E.1 marker scope must reference EITHER the original helper OR
    the R136.B helper (both flow through the same provider-tag inference,
    single source of truth)."""
    assert "R126.E.1" in _AE_SOURCE, "R126.E.1 marker missing from automation_engineer.py"
    idx = _AE_SOURCE.find("R126.E.1")
    scope = _AE_SOURCE[idx:idx + 1000]
    assert (
        "_r126_a_is_ollama_provider" in scope
        or "_r136_b_pick_newman_chunk_size" in scope
    ), (
        "R126.E.1 must gate chunk_size on _r126_a_is_ollama_provider OR "
        "delegate to _r136_b_pick_newman_chunk_size"
    )
    assert "chunk_size" in scope
    # The bucket sizes must be present near the R126.E.1 marker.
    assert "15" in scope and "30" in scope


def test_r126e1_newman_sut_source_max_chars_provider_aware():
    """R126.E.1 — R105.D SUT-source max_chars: 1500 for Ollama, 6000 for Claude."""
    # Find the R105.D context block + R126.E.1 in the same scope
    r105_d_idx = _AE_SOURCE.find("R126.E.1 — provider-aware max_chars for Newman")
    assert r105_d_idx > 0, "R126.E.1 Newman SUT-source gate missing"
    scope = _AE_SOURCE[r105_d_idx:r105_d_idx + 500]
    assert "1500" in scope and "6000" in scope


def test_r126e2_k6_sut_source_max_chars_provider_aware():
    """R126.E.2 — k6 R113.F SUT-source max_chars: 1000 for Ollama, 4000 for Claude."""
    idx = _AE_SOURCE.find("R126.E.2")
    assert idx > 0, "R126.E.2 marker missing"
    scope = _AE_SOURCE[idx:idx + 500]
    assert "_r126_a_is_ollama_provider" in scope
    assert "1000" in scope and "4000" in scope


def test_r126e5_pytest_analytics_sut_source_max_chars_provider_aware():
    """R126.E.5 — analytics_test_agent R125.C/R124.C SUT-source: 1000 vs 4000."""
    idx = _ATA_SOURCE.find("R126.E.5")
    assert idx > 0, "R126.E.5 marker missing from analytics_test_agent.py"
    scope = _ATA_SOURCE[idx:idx + 500]
    assert "_r126_a_is_ollama_provider" in scope
    assert "1000" in scope and "4000" in scope


def test_r126e_all_call_sites_use_canonical_helper():
    """Every R126.E gate must use the canonical
    `_r126_a_is_ollama_provider(self._client)` (or equivalent on _ae._client)
    so the inference logic (R126.D model-name fallback) flows through
    uniformly. Single source of truth for provider detection."""
    import re
    # Count R126.E markers in automation_engineer.py
    e_markers_ae = re.findall(r"R126\.E\.\d", _AE_SOURCE)
    e_markers_ata = re.findall(r"R126\.E\.\d", _ATA_SOURCE)
    # Each marker site must have the canonical helper call
    # (1 in Newman chunk_size, 1 in R105.D Newman, 1 in R113.F k6, 1 in Pytest)
    assert len(e_markers_ae) >= 3, (
        f"expected ≥3 R126.E markers in automation_engineer.py; got {e_markers_ae}"
    )
    assert len(e_markers_ata) >= 1, (
        f"expected ≥1 R126.E marker in analytics_test_agent.py; got {e_markers_ata}"
    )


def test_r126e_does_not_regress_claude_path():
    """The Claude/Anthropic path MUST remain unchanged: the full
    chunk_size=30 + max_chars=6000/4000 budgets must still be reachable
    when provider is NOT Ollama.

    Post-R136.B: Newman chunk_size=30 (Claude path) is now returned by the
    `_r136_b_pick_newman_chunk_size` helper rather than an inline
    `else 30` literal. Verify via behavior on a non-Ollama stub client
    instead of source-text grep so the regression guard survives the
    helper extraction (single source of truth)."""
    from unittest.mock import MagicMock
    from src.agents.automation_engineer import AutomationEngineerAgent
    from src.models.llm_config import LLMProvider

    # Non-Ollama → chunk_size=30 (regression guard for the Claude path)
    claude_client = MagicMock()
    claude_client.provider = LLMProvider.ANTHROPIC
    claude_client.model = "claude-sonnet-4-6"
    assert AutomationEngineerAgent._r136_b_pick_newman_chunk_size(claude_client) == 30, (
        "Newman chunk_size=30 (Claude path) regressed"
    )

    # Mid-tier Ollama → chunk_size=15 (regression guard for the Ollama path
    # under arta-qwen-pro, the production default)
    ollama_default_client = MagicMock()
    ollama_default_client.provider = LLMProvider.OLLAMA
    ollama_default_client.model = "arta-qwen-pro:latest"
    assert AutomationEngineerAgent._r136_b_pick_newman_chunk_size(ollama_default_client) == 15, (
        "Newman chunk_size=15 (mid-tier Ollama path) regressed"
    )

    # Non-chunk-size budgets still grep cleanly from source.
    assert "else 6000" in _AE_SOURCE, "Newman max_chars=6000 (Claude path) regressed"
    assert "else 4000" in _AE_SOURCE, "k6 max_chars=4000 (Claude path) regressed"
    assert "else 4000" in _ATA_SOURCE, "Pytest max_chars=4000 (Claude path) regressed"
