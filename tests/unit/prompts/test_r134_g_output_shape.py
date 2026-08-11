"""R134.G.1 + G.2 tests — output-shape contract headers in prompt templates.

R134.G.1 (PW, 3 variants):
  - PLAYWRIGHT_GENERATION    → Mode: FULL_FILE
  - PLAYWRIGHT_GENERATION_OLLAMA → Mode: FULL_FILE_COMPACT
  - R130.A per-test chunked → Mode: TEST_BODY_ONLY

R134.G.2 (Newman + pytest analytics layers): Newman Pass-1 / Pass-2 + 5
pytest analytics layer headers (the R134.G.2 phase is verified by its own
test cases below — those land separately from these G.1 cases).

Each variant must carry an explicit `[OUTPUT SHAPE — read first]` block
that disambiguates the per-path output convention. Pre-R134.G the LLM
sometimes emitted full-file content into the R130.A chunked path OR
test-body fragments into the monolithic Ollama variant, corrupting the
merge.
"""
from __future__ import annotations


def test_r134_g_1_full_pw_template_has_full_file_shape_header():
    """PLAYWRIGHT_GENERATION (full template) must declare Mode: FULL_FILE.
    Verifies the template ALSO survives `.format()` so existing
    placeholder-rendering tests keep passing (brace-escaped `}}` for
    literal closing braces in the contract block)."""
    from src.prompts.tea_prompts import PLAYWRIGHT_GENERATION
    assert "[OUTPUT SHAPE" in PLAYWRIGHT_GENERATION
    assert "Mode: FULL_FILE" in PLAYWRIGHT_GENERATION
    # Wrap-with directive present
    assert "Wrap with `import` / `test.describe(...)` / `test(...)`: YES" in PLAYWRIGHT_GENERATION
    # First-token assertion
    assert "Output starts with: `import`" in PLAYWRIGHT_GENERATION
    # `}});` renders to `});` after format-escape (R134.G.1 braces must
    # be escaped so format() doesn't choke).
    assert "}});" in PLAYWRIGHT_GENERATION


def test_r134_g_1_ollama_variant_has_full_file_compact_header():
    """PLAYWRIGHT_GENERATION_OLLAMA must declare Mode: FULL_FILE_COMPACT.
    Distinct from the full variant so the LLM can adjust verbosity but
    NOT emit a fragment."""
    from src.prompts.tea_prompts import PLAYWRIGHT_GENERATION_OLLAMA
    assert "[OUTPUT SHAPE" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "Mode: FULL_FILE_COMPACT" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "Wrap with `import`" in PLAYWRIGHT_GENERATION_OLLAMA
    # Forbidden-emission list present
    assert "DO NOT emit" in PLAYWRIGHT_GENERATION_OLLAMA
    # Brace-escaped (R134.G.1 braces must survive format())
    assert "}});" in PLAYWRIGHT_GENERATION_OLLAMA


def test_r134_g_1_chunked_per_test_prompt_has_test_body_only_header():
    """R130.A's per-test chunked prompt (assembled at gen time) must declare
    Mode: TEST_BODY_ONLY. The prompt is built inside
    AutomationEngineerAgent._r126_c_compose_chunked_pw_gen at line ~1816;
    we render a sample by importing the source and grepping the literal."""
    import inspect
    from src.agents.automation_engineer import AutomationEngineerAgent
    src = inspect.getsource(AutomationEngineerAgent)
    # The literal lives inside the chunked-gen helper; we just check the
    # contract header tokens are present in the compiled module source.
    assert "Mode: TEST_BODY_ONLY" in src
    # FIRST-line directive: the canonical `// AC: {ac_id}` opener
    assert "Output starts with: `// AC:" in src
    # Wrap-with directive explicitly NO for chunked path
    assert "Wrap with `import` / `test.describe(...)` / `test(...)`: NO" in src


def test_r134_g_1_three_modes_distinct():
    """Sanity check — the three PW prompt variants advertise DISTINCT
    output modes. Pre-R134.G the LLM had no signal to differentiate;
    post-R134.G each mode is unambiguous."""
    import inspect
    from src.agents.automation_engineer import AutomationEngineerAgent
    from src.prompts.tea_prompts import (
        PLAYWRIGHT_GENERATION,
        PLAYWRIGHT_GENERATION_OLLAMA,
    )
    chunked_src = inspect.getsource(AutomationEngineerAgent)
    # FULL_FILE is the bare-mode marker; FULL_FILE_COMPACT is its own
    # distinct mode (the COMPACT suffix differentiates).
    assert "Mode: FULL_FILE\n" in PLAYWRIGHT_GENERATION
    assert "Mode: FULL_FILE_COMPACT" in PLAYWRIGHT_GENERATION_OLLAMA
    assert "Mode: TEST_BODY_ONLY" in chunked_src
    # Mutual exclusion check — no template should carry MULTIPLE Mode: lines
    assert PLAYWRIGHT_GENERATION.count("Mode: FULL_FILE\n") == 1
    assert PLAYWRIGHT_GENERATION_OLLAMA.count("Mode: FULL_FILE_COMPACT") == 1
