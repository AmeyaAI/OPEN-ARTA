"""R126.C — Per-test() chunked Ollama PW gen.

The keystone wire that combines R126.A (manifest), R126.B (scaffolder),
R126.T (partial-failure semantics), and R126.W (hint filter) into a
functional alternative to monolithic PW gen for Ollama provider.

Architecture:
  1. Build R126.B skeleton (deterministic 80%)
  2. Parse scenarios from combined Gherkin
  3. For each scenario, call LLM with small per-test prompt
  4. Extract test body from LLM response (defensive)
  5. Splice bodies into skeleton via R126.B
  6. R126.T classifies failures: clean → ship, partial → ship w/ placeholders,
     fatal → return None (caller falls back to monolithic path)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.automation_engineer import AutomationEngineerAgent
from src.models.llm_config import LLMProvider


def _make_ollama_agent(canned_response: str = ""):
    """Construct an agent with a stub Ollama client returning canned LLM output."""
    client = MagicMock()
    client.provider = LLMProvider.OLLAMA
    client.model = "arta-qwen-pro:latest"
    agent = AutomationEngineerAgent(client=client)
    # Stub _llm_with_growing_budget to return the canned content
    async def _stub_llm(_kwargs):
        msg = MagicMock()
        msg.content = [MagicMock(text=canned_response)]
        return msg
    agent._llm_with_growing_budget = AsyncMock(side_effect=_stub_llm)
    agent._model = "arta-qwen-pro:latest"
    return agent


SAMPLE_GHERKIN = """\
Feature: Dashboard

  Scenario: AC-001 — User views dashboard
    Given user is logged in
    When the user navigates to /dashboard
    Then the dashboard renders

  Scenario: AC-002 — User logs out
    Given user is on the dashboard
    When the user clicks Logout
    Then the login page appears
"""


# ── Body extraction ──

def test_r126c_extract_body_from_bare_text():
    """LLM emits just the body content → returned as-is."""
    raw = "await page.goto('/dashboard');\nawait expect(page.getByRole('main')).toBeVisible();"
    body = AutomationEngineerAgent._r126_c_extract_test_body(raw)
    assert "await page.goto('/dashboard')" in body
    assert "toBeVisible" in body


def test_r126c_extract_body_strips_test_wrapper():
    """LLM emits a full test() wrapper despite instructions → strip it."""
    raw = """\
test('AC-001', async ({ page }) => {
  await page.goto('/login');
  await expect(page).toHaveURL(/login/);
});
"""
    body = AutomationEngineerAgent._r126_c_extract_test_body(raw)
    # The test() wrapper should be gone, only the body remains
    assert "test(" not in body or body.count("test(") == 0
    assert "page.goto" in body
    assert "toHaveURL" in body


def test_r126c_extract_body_strips_import_lines():
    """LLM emits imports → strip them."""
    raw = """\
import { test, expect } from '@playwright/test';
await page.goto('/');
await expect(page).toHaveURL(/.*/);
"""
    body = AutomationEngineerAgent._r126_c_extract_test_body(raw)
    assert "import " not in body
    assert "page.goto" in body


def test_r126c_extract_body_strips_code_fences():
    """LLM may wrap output in ```typescript ... ``` fences → strip."""
    raw = "```typescript\nawait page.goto('/');\n```"
    body = AutomationEngineerAgent._r126_c_extract_test_body(raw)
    assert "```" not in body
    assert "page.goto" in body


def test_r126c_extract_body_handles_empty():
    """Empty input → empty output (graceful)."""
    assert AutomationEngineerAgent._r126_c_extract_test_body("") == ""


# ── End-to-end chunked gen ──

@pytest.mark.asyncio
async def test_r126c_chunked_gen_succeeds_with_filled_bodies():
    """All LLM calls succeed → R126.C returns a complete spliced spec."""
    canned = "await page.goto('/');\nawait expect(page).toHaveURL(/.*/);"
    agent = _make_ollama_agent(canned_response=canned)
    content = await agent._r126_c_generate_chunked_pw_ollama(
        req_id="REQ-TEST-001",
        priority="P1",
        catalog_prefix="# DOM CATALOG: ...\n",
        gherkin_text=SAMPLE_GHERKIN,
        dom_catalog={"role_names": [("button", "Logout")]},
        captured_endpoints=[{"method": "GET", "path": "/api/v1/dashboard"}],
    )
    assert content is not None, "chunked gen must return a spec when all calls succeed"
    # Should be a complete spec with imports + describe + tests
    assert "@playwright/test" in content
    assert "test.describe" in content
    assert "R126.B SKELETON SCAFFOLDED" in content
    # Both ACs filled
    assert "await page.goto" in content
    # No LLM_FILL markers left (all spliced)
    assert "R126.B LLM_FILL_START" not in content


@pytest.mark.asyncio
async def test_r126c_fatal_failure_returns_none():
    """≥50% LLM calls fail → return None (caller falls back to monolithic)."""
    agent = _make_ollama_agent()
    # Replace _llm_with_growing_budget with one that always raises
    async def _always_fail(_kwargs):
        raise RuntimeError("simulated rate limit")
    agent._llm_with_growing_budget = AsyncMock(side_effect=_always_fail)
    content = await agent._r126_c_generate_chunked_pw_ollama(
        req_id="REQ-TEST-002",
        priority="P1",
        catalog_prefix="",
        gherkin_text=SAMPLE_GHERKIN,
        dom_catalog=None,
        captured_endpoints=None,
    )
    assert content is None, "fatal classification (100% failures) must return None"


@pytest.mark.asyncio
async def test_r126c_partial_failure_returns_content_with_skip_placeholders():
    """1/2 failures = 50% — at the boundary (≥50% = fatal). Test the <50% path
    with a 1/3 failure scenario."""
    # Build a Gherkin with 3 scenarios
    g = SAMPLE_GHERKIN + """
  Scenario: AC-003 — User changes settings
    Given user is logged in
    When the user navigates to /settings
    Then the settings panel opens
"""
    # Call counter — fail the 3rd, succeed the first 2
    call_counter = {"n": 0}
    async def _selective(_kwargs):
        call_counter["n"] += 1
        if call_counter["n"] == 3:
            raise RuntimeError("simulated timeout on 3rd call")
        msg = MagicMock()
        msg.content = [MagicMock(text="await expect(page).toHaveURL(/.*/);")]
        return msg
    agent = _make_ollama_agent()
    agent._llm_with_growing_budget = AsyncMock(side_effect=_selective)
    content = await agent._r126_c_generate_chunked_pw_ollama(
        req_id="REQ-TEST-003",
        priority="P1",
        catalog_prefix="",
        gherkin_text=g,
        dom_catalog=None,
        captured_endpoints=None,
    )
    # 1/3 = 33% failure → partial → content returned with placeholder
    assert content is not None, "partial outcome must ship a spec (not fatal)"
    # Successful ACs have filled bodies
    assert "await expect(page).toHaveURL" in content
    # Failed AC has a test.skip placeholder per R126.T
    assert "test.skip" in content
    assert "simulated timeout" in content or "LLM_FILL failed" in content


@pytest.mark.asyncio
async def test_r126c_assembled_spec_passes_no_lingering_markers():
    """Assembled spec has no LLM_FILL markers left — all spliced or replaced."""
    agent = _make_ollama_agent(canned_response="await page.click('button');")
    content = await agent._r126_c_generate_chunked_pw_ollama(
        req_id="REQ-TEST-004",
        priority="P1",
        catalog_prefix="",
        gherkin_text=SAMPLE_GHERKIN,
        dom_catalog=None,
        captured_endpoints=None,
    )
    assert content is not None
    # Markers should NOT be in the final spec content
    assert "R126.B LLM_FILL_START" not in content
    assert "LLM_FILL_END" not in content
