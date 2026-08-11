"""R134.D tests — shared `extract_json_from_llm_output` SSoT extractor.

Pre-R134.D: 4+ agent files (strategy_architect, defect_intel, dataset_recipe,
self_healing) each had their own JSON-extraction copy. Ollama outputs
wrapped in ```json fences caused the greedy regex to either swallow
surrounding prose OR silently return {}/[]. Post-R134.D: one canonical
helper at `src/agents/json_extract.py` consumed by all live extractors.

Five cases lock the SSoT contract.
"""
from __future__ import annotations

from src.agents.json_extract import extract_json_from_llm_output


def test_r134_d_strips_json_fence_with_lang_tag():
    """Ollama-style: prose + ```json [...] ``` + conclusion prose."""
    text = (
        "Here is the analysis:\n\n"
        "```json\n"
        '[{"id": "AC-001", "statement": "user logs in"}]\n'
        "```\n\n"
        "Done."
    )
    result = extract_json_from_llm_output(text, default=[])
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["id"] == "AC-001"


def test_r134_d_strips_bare_fence():
    """Some Ollama outputs use bare ``` without `json` lang tag."""
    text = "Result:\n```\n{\"key\": \"value\"}\n```\nThank you."
    result = extract_json_from_llm_output(text, default={})
    assert isinstance(result, dict)
    assert result["key"] == "value"


def test_r134_d_bare_json_unchanged():
    """Direct JSON without fences (Claude convention) still parses cleanly.
    Regression guard: R134.D fence-strip is additive — bare JSON path
    unchanged."""
    result = extract_json_from_llm_output('[{"id": "AC-001"}]', default=[])
    assert isinstance(result, list)
    assert result[0]["id"] == "AC-001"


def test_r134_d_prose_only_returns_default():
    """Pure prose (no JSON, no fences) returns the caller-supplied default.
    Per-caller defaults: extractors that want `raise` instead pass
    default=None and check + raise themselves (see strategy_architect /
    defect_intel / dataset_recipe wrappers)."""
    result = extract_json_from_llm_output("This response has no JSON.", default=[])
    assert result == []
    result_dict = extract_json_from_llm_output("This response has no JSON.", default={})
    assert result_dict == {}
    # No-default-supplied path → empty dict per fail-soft contract
    result_none = extract_json_from_llm_output("plain prose")
    assert result_none == {}


def test_r134_d_strips_think_block_before_fence():
    """Qwen/DeepSeek `<think>...</think>` reasoning strips FIRST so the
    fence-strip can find the canonical JSON block. Combined with R134.D's
    fence-strip, this handles the dominant Ollama-output pattern."""
    text = (
        "<think>\n"
        "Let me consider {invalid: json: brackets:} here.\n"
        "</think>\n"
        "```json\n"
        '[{"id": "CLEAN"}]\n'
        "```"
    )
    result = extract_json_from_llm_output(text, default=[])
    assert isinstance(result, list)
    assert result[0]["id"] == "CLEAN"
