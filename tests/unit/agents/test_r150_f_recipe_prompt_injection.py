"""R150.F — inject recipe.expected_outputs as HARD CONSTRAINT in PW prompt.

Pre-R150.F: the PW gen LLM had ZERO upstream signal about what response
field paths the SUT actually returns OR what values the recipe declared.
It invented assertions like `expect(json.insight.metric).toEqual('sales')`
based on Gherkin keywords alone. Live Iter 9 evidence: 23 ×
`waitForResponse` timeouts + ~41 × `locator.click` timeouts traced to
this class.

Post-R150.F: when a recipe has `expected_outputs`, a HARD CONSTRAINT
block listing the EXACT declared field paths + values is prepended into
the PW gen prompt. The LLM grounds assertions against this on attempt 1.
R150.E validator catches residuals via R57.1 retry-with-hint → R102.A
stamp on exhaust → R102.C dispatch BLOCK.

Killswitch: `ARTA_R150_F_RECIPE_PROMPT_DISABLE=1` reverts to no
injection (Iter 9 behavior).
"""
from __future__ import annotations

import os

from src.agents.automation_engineer import AutomationEngineerAgent


# ─── _r150_f_compose_expected_outputs_block helper unit tests ───────────────


def test_r150_f_none_risk_returns_empty():
    """Defensive: callers without a risk dict (unit-test invocations) get
    empty string back, NOT a crash."""
    assert AutomationEngineerAgent._r150_f_compose_expected_outputs_block(None) == ""


def test_r150_f_non_dict_risk_returns_empty():
    """Defensive: malformed risk objects fall through cleanly."""
    # Python coerces to dict-like check; isinstance fails → empty
    assert AutomationEngineerAgent._r150_f_compose_expected_outputs_block(
        "not_a_dict"  # type: ignore[arg-type]
    ) == ""
    assert AutomationEngineerAgent._r150_f_compose_expected_outputs_block(
        ["list", "not", "dict"]  # type: ignore[arg-type]
    ) == ""


def test_r150_f_empty_expected_outputs_returns_empty():
    """When risk has no expected_outputs OR empty dict, block is omitted
    — no signal to inject."""
    assert AutomationEngineerAgent._r150_f_compose_expected_outputs_block({}) == ""
    assert AutomationEngineerAgent._r150_f_compose_expected_outputs_block(
        {"expected_outputs": {}}
    ) == ""
    assert AutomationEngineerAgent._r150_f_compose_expected_outputs_block(
        {"expected_outputs": None}
    ) == ""


def test_r150_f_renders_expected_outputs_block_with_kv_pairs():
    """KEYSTONE — when recipe expected_outputs is populated, the HARD
    CONSTRAINT block lists each field path + value verbatim."""
    risk = {
        "expected_outputs": {
            "data.records[0].metric_name": "sales_revenue",
            "data.records[0].value": 125.5,
            "meta.total_count": 100,
        }
    }
    out = AutomationEngineerAgent._r150_f_compose_expected_outputs_block(risk)
    assert "[R150.F" in out
    # Each field path surfaces
    assert "data.records[0].metric_name" in out
    assert "data.records[0].value" in out
    assert "meta.total_count" in out
    # Values surface with repr (so strings get quoted, numbers don't)
    assert "'sales_revenue'" in out
    assert "125.5" in out
    assert "100" in out
    # HARD CONSTRAINT footer present
    assert "HARD CONSTRAINT" in out
    # Cites the downstream enforcement chain
    assert "R150.E" in out
    assert "R102.A" in out or "R102.C" in out


def test_r150_f_block_under_char_budget_for_normal_recipes():
    """Char budget contract: block stays ≤2400 for typical recipes
    (≤20 expected_outputs entries). Operators can trust it doesn't bloat
    the prompt unboundedly."""
    risk = {
        "expected_outputs": {
            f"data.records[0].field_{i}": f"value_{i}" for i in range(15)
        }
    }
    out = AutomationEngineerAgent._r150_f_compose_expected_outputs_block(risk)
    assert len(out) <= 2400, f"R150.F block oversized: {len(out)} chars"


def test_r150_f_block_char_budget_enforced_via_omission():
    """When expected_outputs has many entries, char budget caps and an
    explicit `// +N more entries omitted` marker surfaces."""
    risk = {
        "expected_outputs": {
            f"data.records[0].field_with_a_longer_name_for_budget_test_{i}":
            f"a_longer_value_for_budget_test_{i}"
            for i in range(60)
        }
    }
    out = AutomationEngineerAgent._r150_f_compose_expected_outputs_block(risk)
    assert len(out) <= 2400
    assert "more entries omitted" in out


def test_r150_f_killswitch_disables_injection():
    """`ARTA_R150_F_RECIPE_PROMPT_DISABLE=1` reverts to no injection
    (Iter 9 behavior)."""
    risk = {"expected_outputs": {"insight.metric": "sales"}}
    os.environ["ARTA_R150_F_RECIPE_PROMPT_DISABLE"] = "1"
    try:
        out = AutomationEngineerAgent._r150_f_compose_expected_outputs_block(risk)
    finally:
        del os.environ["ARTA_R150_F_RECIPE_PROMPT_DISABLE"]
    assert out == "", "killswitch must yield empty output"


def test_r150_f_block_cites_downstream_validation_chain():
    """The HARD CONSTRAINT footer instructs the LLM how to handle dynamic
    assertions properly — names the `waitForResponse + .json()` pattern."""
    risk = {"expected_outputs": {"x.y": "value"}}
    out = AutomationEngineerAgent._r150_f_compose_expected_outputs_block(risk)
    # Footer cites the canonical PW response-assertion pattern
    assert "waitForResponse" in out
    assert "json" in out.lower()


def test_r150_f_repr_distinguishes_string_from_number():
    """repr() output makes string-vs-number ambiguity vanish for the
    LLM. `'100'` vs `100` is the right disambiguation signal."""
    risk = {
        "expected_outputs": {
            "string_field": "100",   # string '100'
            "number_field": 100,     # int 100
        }
    }
    out = AutomationEngineerAgent._r150_f_compose_expected_outputs_block(risk)
    # String value gets quotes via repr; number doesn't
    assert "'100'" in out   # string variant
    # Number representation
    assert ": 100" in out


def test_r150_f_one_entry_recipe_renders_correctly():
    """Single-entry recipe is still rendered with full HARD CONSTRAINT
    block — no special-case empty handling."""
    risk = {"expected_outputs": {"only_key": "only_value"}}
    out = AutomationEngineerAgent._r150_f_compose_expected_outputs_block(risk)
    assert "only_key" in out
    assert "'only_value'" in out
    assert "HARD CONSTRAINT" in out
