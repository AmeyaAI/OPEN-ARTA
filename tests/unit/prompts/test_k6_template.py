"""F12-7: regression test for the k6 prompt template brace-leak.

The K6_GENERATION template was using `{{{{endpoint:X}}}}` which Python's
`.format()` collapses to `{{endpoint:X}}` (double brace) — but k6's
threshold key syntax requires SINGLE braces (`{endpoint:X}`). The
double-brace example in the prompt taught the LLM to emit double braces
in actual k6 code, which silently no-op'd every per-endpoint SLA check.

This test renders the K6_GENERATION prompt with the same args
`automation_engineer.py` passes, and asserts:
  - `{endpoint:` appears (correct single-brace form)
  - `{{endpoint:` does NOT appear (the leak)
"""
from __future__ import annotations

from src.prompts.tea_prompts import K6_GENERATION


def _render() -> str:
    return K6_GENERATION.format(
        performance_requirement="dummy gherkin",
        sla_threshold="3000",
        endpoint_thresholds_block="dummy thresholds",
    )


class TestK6PromptBraces:

    def test_renders_without_format_error(self):
        # Will raise KeyError if any unescaped `{name}` slipped through
        out = _render()
        assert isinstance(out, str) and len(out) > 200

    def test_threshold_example_has_single_braces(self):
        out = _render()
        # k6-correct form must appear at least twice (login + checkout examples)
        assert out.count("{endpoint:") >= 2

    def test_threshold_example_has_no_double_braces(self):
        out = _render()
        # The leak that broke every per-endpoint SLA in production
        assert "{{endpoint:" not in out, (
            "K6 prompt regressed to double-brace form — every per-endpoint "
            "threshold will silently no-op in generated k6 scripts"
        )

    def test_scenario_object_literals_are_single_brace(self):
        out = _render()
        # The example k6 options object uses {} for JS object literals;
        # double braces would break JS syntax in the LLM-emitted file.
        assert "scenarios: {" in out
        assert "scenarios: {{" not in out
