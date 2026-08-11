"""R130.C — PLAYWRIGHT_GENERATION_OLLAMA constraint enrichment tests.

Five cases lock down the Ollama-compressed prompt template contract:
the three constraint blocks (R127.D.6.B + R114.C + R112.E) are inlined,
the sub_flows import is added to REQUIRED IMPORTS, the total stays under
6000 chars (qwen-pro context envelope), and `.format(...)` still renders
without unescaped-brace errors.
"""
from __future__ import annotations

from src.prompts.tea_prompts import PLAYWRIGHT_GENERATION_OLLAMA


# ── Case 1: R127.D.6.B authenticate() ban present in template ─────────────


def test_r130c_r127_d6_b_authenticate_ban_present():
    """Monolithic-fallback path must also see the authenticate() ban so
    qwen-pro doesn't emit `await authenticate(page)` when chunked path
    falls through to monolithic gen."""
    assert "[R127.D.6.B" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "DO NOT emit" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "authenticate(page)" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "storageState" in PLAYWRIGHT_GENERATION_OLLAMA


# ── Case 2: R114.C SPA hydration constraint present ───────────────────────


def test_r130c_r114_c_spa_hydration_present():
    """The Ollama monolithic prompt now teaches waitForSPAReady() after
    every page.goto on authenticated routes."""
    assert "[R114.C" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "waitForSPAReady(page)" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "SPA HYDRATION" in PLAYWRIGHT_GENERATION_OLLAMA


# ── Case 3: R112.E auth-verify constraint present ─────────────────────────


def test_r130c_r112_e_auth_verify_present():
    """The Ollama prompt teaches skipIfAuthStale() at the start of every
    auth-dependent test() block."""
    assert "[R112.E" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "skipIfAuthStale(page)" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "AUTH-STATE VERIFICATION" in PLAYWRIGHT_GENERATION_OLLAMA


# ── Case 4: sub_flows import added to REQUIRED IMPORTS ────────────────────


def test_r130c_sub_flows_import_added_to_required_imports():
    """The REQUIRED IMPORTS block must include the canonical sub_flows
    import so the generated spec can call waitForSPAReady + skipIfAuthStale
    at runtime (R116.B single-source-of-truth)."""
    assert "from '../common/sub_flows'" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "waitForSPAReady" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "skipIfAuthStale" in PLAYWRIGHT_GENERATION_OLLAMA


# ── Case 5: Total chars ≤ 6000 + .format() render succeeds ────────────────


def test_r130c_total_chars_within_budget_and_format_renders():
    """qwen-pro context budget consideration: total template ≤ 6000 chars
    leaves room for R98.3 captured_endpoints + R47.1b DOM catalog + Gherkin
    in the assembled prompt. AND `.format(gherkin_scenario=...)` must
    render without stray unescaped braces."""
    assert len(PLAYWRIGHT_GENERATION_OLLAMA) < 6000, (
        f"Template size {len(PLAYWRIGHT_GENERATION_OLLAMA)} exceeds 6000 char budget"
    )
    # Verify .format() renders without raising
    rendered = PLAYWRIGHT_GENERATION_OLLAMA.format(
        gherkin_scenario="Scenario: dummy\n  Given X\n  When Y\n  Then Z"
    )
    # The rendered prompt should NOT contain leftover `{{` or `}}` from
    # incorrect escaping
    assert "{{" not in rendered, "Found unescaped `{{` in rendered template"
    assert "}}" not in rendered, "Found unescaped `}}` in rendered template"
    # The Gherkin substitution worked
    assert "Scenario: dummy" in rendered
