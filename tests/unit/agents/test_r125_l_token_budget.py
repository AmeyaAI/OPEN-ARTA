"""R125.L — provider-aware max_tokens + truncation detector.

Per the user directive: "The ARTA performance and accuracy should be same
irrespective whether I choose claude code cli or ollama."

Pre-R125.L: `_llm_with_growing_budget` grew max_tokens 4K → 8K → 16K
unconditionally. For Ollama qwen3 (32K context window total), a 28K input
prompt + an 8K output request silently truncates output mid-spec. For Claude
(200K context), the same input is fine.

R125.L caps max_tokens to fit within provider context budget AND surfaces
a WARN when input alone exceeds 50% of budget so operators see the risk
BEFORE the LLM call.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.agents.automation_engineer import AutomationEngineerAgent
from src.models.llm_config import LLMProvider


def _make_agent(client_mock):
    """Construct an agent with a mock LLM client."""
    agent = AutomationEngineerAgent(client=client_mock)
    return agent


def test_r125l_claude_large_prompt_no_clamp():
    """Claude has 200K context — a 30K-char prompt + 8K target should pass through."""
    client = MagicMock()
    client.provider = LLMProvider.CLAUDE_CODE
    client.model = "claude-sonnet-4-6"
    agent = _make_agent(client)
    # 30K chars ≈ 7.5K tokens; budget 180K; headroom huge
    clamped = agent._r125_l_compute_max_tokens(prompt_len_chars=30_000, target_max=8_000)
    assert clamped == 8_000, f"Claude should not clamp at 30K input; got {clamped}"


def test_r125l_ollama_large_prompt_clamped():
    """Ollama qwen-pro has 32K context → 28K-char prompt + 8K target gets clamped."""
    client = MagicMock()
    client.provider = LLMProvider.OLLAMA
    client.model = "arta-qwen-pro:latest"
    agent = _make_agent(client)
    # 100K chars ≈ 25K tokens; budget 28K; headroom = max(2000, 28K - 25K) = 3K
    clamped = agent._r125_l_compute_max_tokens(prompt_len_chars=100_000, target_max=16_000)
    assert clamped < 16_000, (
        f"Ollama with large input must clamp target; got clamped={clamped}"
    )
    assert clamped >= 2000, (
        f"Clamp must leave a useful headroom (≥2K); got {clamped}"
    )


def test_r125l_warning_when_input_exceeds_50pct_budget(caplog):
    """Input >50% of provider budget → WARN log line surfaces."""
    import logging
    client = MagicMock()
    client.provider = LLMProvider.OLLAMA
    client.model = "arta-qwen-pro:latest"
    agent = _make_agent(client)
    with caplog.at_level(logging.WARNING, logger="arta"):
        # 60K chars ≈ 15K tokens, > 50% of 28K budget
        agent._r125_l_compute_max_tokens(prompt_len_chars=60_000, target_max=4_000)
    warn_lines = [r for r in caplog.records if "R125.L" in r.message and "truncation risk" in r.message]
    assert len(warn_lines) >= 1, (
        f"R125.L: expected truncation-risk WARN log; got {[r.message for r in caplog.records]}"
    )


def test_r125l_unknown_provider_uses_default_budget():
    """Provider not in budget map → default 16K budget."""
    client = MagicMock()
    client.provider = "exotic-provider"
    client.model = "exotic-model"
    agent = _make_agent(client)
    # Small input + small target → no clamp needed
    clamped = agent._r125_l_compute_max_tokens(prompt_len_chars=10_000, target_max=4_000)
    assert clamped == 4_000

    # Large input under default budget → clamp
    clamped = agent._r125_l_compute_max_tokens(prompt_len_chars=60_000, target_max=16_000)
    assert clamped < 16_000


def test_r125l_truncation_detector_clean_spec_not_flagged():
    """A well-formed spec ending in `});` should not be flagged as truncated."""
    content = """
import { test, expect } from '@playwright/test';
test('foo', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveURL(/.*/);
});
"""
    assert AutomationEngineerAgent._r125_l_detect_output_truncation(content) is False


def test_r125l_truncation_detector_unbalanced_braces_flagged():
    """A spec ending mid-block (unbalanced braces) is truncation."""
    content = """
import { test } from '@playwright/test';
test('foo', async ({ page }) => {
  await page.goto('/');
  const data = {
    a: 1,
    b: 2,
"""  # ← unbalanced; missing closing braces
    assert AutomationEngineerAgent._r125_l_detect_output_truncation(content) is True


def test_r125l_truncation_detector_ellipsis_flagged():
    """LLM emitting `// ...` ellipsis → truncation indicator."""
    content = """
import { test } from '@playwright/test';
test('foo', async ({ page }) => {
  await page.goto('/');
  // ...
"""
    assert AutomationEngineerAgent._r125_l_detect_output_truncation(content) is True


def test_r125l_truncation_detector_empty_content_not_flagged():
    """Empty content is its own failure mode; not a truncation."""
    assert AutomationEngineerAgent._r125_l_detect_output_truncation("") is False
    assert AutomationEngineerAgent._r125_l_detect_output_truncation("   \n\n  ") is False


def test_r125l_clamp_never_below_2k_headroom():
    """Even with HUGE input (above budget), clamp leaves ≥2K headroom for
    output — caller should retry at smaller budget OR split the request."""
    client = MagicMock()
    client.provider = LLMProvider.OLLAMA
    client.model = "arta-qwen-pro:latest"
    agent = _make_agent(client)
    # Prompt EXCEEDS the budget
    clamped = agent._r125_l_compute_max_tokens(prompt_len_chars=200_000, target_max=16_000)
    assert clamped >= 2000, (
        f"Clamp must leave minimum 2K headroom; got {clamped}"
    )
