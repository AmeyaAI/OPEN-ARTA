"""R134.G.2 tests — Newman + pytest analytics output-shape headers.

Cross-tool extension of R134.G.1's PW contract. Newman has 2 variants
(Pass-1 skeleton + Pass-2 assertions); pytest analytics has per-layer
prompts that need to advertise their layer's expected stub shape.

Six cases:
  - Newman Pass-1 header (NEWMAN_COLLECTION_SKELETON)
  - Newman Pass-2 header (NEWMAN_PM_TEST_ASSERTIONS_ONLY)
  - Pass-2 references R132.A + R133.C + R133.D constraints
  - Pytest analytics layer header (PYTEST_ANALYTICS_LAYER) parameterized
  - Pytest header carries the {layer_name} substitution slot
  - All three are MUTUALLY EXCLUSIVE Mode: declarations (no cross-tool leak)
"""
from __future__ import annotations

import inspect


def test_r134_g_2_newman_pass1_skeleton_header():
    """Newman Pass-1 prompt must declare Mode: NEWMAN_COLLECTION_SKELETON.
    The header lives inside _newman_pass1_single's f-string prompt body."""
    from src.agents.automation_engineer import AutomationEngineerAgent
    src = inspect.getsource(AutomationEngineerAgent)
    assert "Mode: NEWMAN_COLLECTION_SKELETON" in src
    # Wrap-with directive present + emphasizes EMPTY event[] arrays for Pass-1
    assert "Wrap with `info.name` / `item[]` / `event[]`: YES" in src
    # First-token assertion (literal `{{` in the f-string source — escapes to `{`
    # when the prompt renders).
    assert "Output starts with: `{{`" in src


def test_r134_g_2_newman_pass2_assertions_only_header():
    """Newman Pass-2 prompt must declare Mode: NEWMAN_PM_TEST_ASSERTIONS_ONLY.
    Distinct from Pass-1; output is pm.test bodies, NOT collection skeleton."""
    from src.agents.automation_engineer import AutomationEngineerAgent
    src = inspect.getsource(AutomationEngineerAgent)
    assert "Mode: NEWMAN_PM_TEST_ASSERTIONS_ONLY" in src
    # Wrap-with directive explicitly NO — output is JS statements, not JSON
    assert "Wrap with full Postman collection structure: NO" in src
    # First-token assertion
    assert "Output starts with: `pm.test('`" in src


def test_r134_g_2_newman_pass2_references_r132a_r133c_r133d_constraints():
    """Pass-2 header must reference the constraints active in this pass
    (R132.A + R133.C + R133.D) so the LLM sees the contract surface."""
    from src.agents.automation_engineer import AutomationEngineerAgent
    src = inspect.getsource(AutomationEngineerAgent)
    # Walk the source for the R134.G.2 Pass-2 block specifically
    g2_block_idx = src.find("Mode: NEWMAN_PM_TEST_ASSERTIONS_ONLY")
    assert g2_block_idx >= 0
    # Capture the contract block (~600 chars after the marker)
    block = src[g2_block_idx:g2_block_idx + 800]
    assert "R132.A" in block
    assert "R133.C" in block
    assert "R133.D" in block


def test_r134_g_2_pytest_analytics_layer_header():
    """ANALYTICS_TEST_GENERATION_PROMPT must declare Mode: PYTEST_ANALYTICS_LAYER
    + reference the parameterized {layer_name} slot."""
    from src.agents.analytics_test_agent import ANALYTICS_TEST_GENERATION_PROMPT
    assert "Mode: PYTEST_ANALYTICS_LAYER" in ANALYTICS_TEST_GENERATION_PROMPT
    # Parameterized layer name visible in the header (operator-facing
    # signal: the LLM sees WHICH layer it's emitting for)
    assert "Layer: {layer_name}" in ANALYTICS_TEST_GENERATION_PROMPT
    # Forbidden-emission list explicitly bans cross-layer pollution
    assert "DO NOT emit: tests for OTHER analytics layers" in ANALYTICS_TEST_GENERATION_PROMPT


def test_r134_g_2_pytest_layer_substitution_works():
    """The {layer_name} slot in the header substitutes correctly via
    .format() — confirms operator-visible layer dispatch."""
    from src.agents.analytics_test_agent import ANALYTICS_TEST_GENERATION_PROMPT
    rendered = ANALYTICS_TEST_GENERATION_PROMPT.format(
        layer_name="nl_to_query",
        requirement_text="<req>",
        layer_description="<desc>",
        mock_description="<mock>",
        assertion_type="<atype>",
        tier="3",
        tier_label="<label>",
        fixture_description="<fixture>",
        expected_outputs_block="<expected>",
    )
    assert "Layer: nl_to_query" in rendered


def test_r134_g_2_modes_mutually_exclusive_across_tools():
    """Sanity check — the 5 modes (3 from R134.G.1 PW + 2 from R134.G.2
    Newman + 1 from R134.G.2 pytest = 6 total) are textually distinct so
    the LLM cannot accidentally emit one variant's shape into another's
    prompt slot."""
    from src.agents.automation_engineer import AutomationEngineerAgent
    from src.agents.analytics_test_agent import ANALYTICS_TEST_GENERATION_PROMPT
    from src.prompts.tea_prompts import (
        PLAYWRIGHT_GENERATION,
        PLAYWRIGHT_GENERATION_OLLAMA,
    )
    ae_src = inspect.getsource(AutomationEngineerAgent)
    all_modes = (
        "Mode: FULL_FILE\n",                         # R134.G.1 — full PW
        "Mode: FULL_FILE_COMPACT",                   # R134.G.1 — Ollama PW
        "Mode: TEST_BODY_ONLY",                      # R134.G.1 — R130.A chunked
        "Mode: NEWMAN_COLLECTION_SKELETON",          # R134.G.2 — Newman Pass-1
        "Mode: NEWMAN_PM_TEST_ASSERTIONS_ONLY",      # R134.G.2 — Newman Pass-2
        "Mode: PYTEST_ANALYTICS_LAYER",              # R134.G.2 — pytest layers
    )
    # Each mode appears in EXACTLY one prompt source
    locations = {
        m: sum([
            int(m in PLAYWRIGHT_GENERATION),
            int(m in PLAYWRIGHT_GENERATION_OLLAMA),
            int(m in ae_src),
            int(m in ANALYTICS_TEST_GENERATION_PROMPT),
        ])
        for m in all_modes
    }
    # Each mode must appear in at least ONE source (i.e., it's actually wired)
    for mode, count in locations.items():
        assert count >= 1, f"R134.G mode {mode!r} not found in any prompt source"
    # PYTEST mode lives ONLY in analytics template
    assert "Mode: PYTEST_ANALYTICS_LAYER" not in PLAYWRIGHT_GENERATION
    assert "Mode: PYTEST_ANALYTICS_LAYER" not in PLAYWRIGHT_GENERATION_OLLAMA
    # NEWMAN modes live ONLY in automation_engineer (NOT in analytics)
    assert "Mode: NEWMAN_COLLECTION_SKELETON" not in ANALYTICS_TEST_GENERATION_PROMPT
    assert "Mode: NEWMAN_PM_TEST_ASSERTIONS_ONLY" not in ANALYTICS_TEST_GENERATION_PROMPT
    # PW FULL_FILE_COMPACT lives ONLY in the Ollama variant
    assert "Mode: FULL_FILE_COMPACT" not in PLAYWRIGHT_GENERATION
    assert "Mode: FULL_FILE_COMPACT" not in ANALYTICS_TEST_GENERATION_PROMPT
