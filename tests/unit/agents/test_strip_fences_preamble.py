"""R77.1.α — regression tests for `_strip_fences` JSON-with-preamble path.

Pre-R77.1.α the function handled `<think>` blocks + code fences but NOT
plain prose preceding/following the JSON. LLMs frequently produce output
like `Here is the Postman collection: {...}` even when the system prompt
says "output ONLY JSON" — the outer json.loads then fails with
JSONDecodeError on the very first character → all 3 R57.1 retries hit
the same preamble → Newman gen reports "LLM returned empty scripts".

The balanced-brace scanner finds the first `{` or `[` and walks until
its matching closer, properly handling strings + escapes.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.agents.automation_engineer import AutomationEngineerAgent


@pytest.fixture
def agent():
    # `_strip_fences` doesn't touch the LLM client but the agent's
    # __init__ requires one — pass an AsyncMock (same pattern as
    # tests/unit/agents/test_newman_validation.py).
    return AutomationEngineerAgent(client=AsyncMock())


def _parses(s: str) -> object:
    """Helper: assert the cleaned output round-trips through json.loads."""
    return json.loads(s)


# ── Happy path: bare JSON unchanged ─────────────────────────────────────

def test_bare_object_passes_through(agent):
    """No fences, no preamble — the function should leave it alone."""
    raw = '{"info": {"name": "X"}, "item": []}'
    out = agent._strip_fences(raw)
    assert out == raw
    assert _parses(out) == {"info": {"name": "X"}, "item": []}


def test_bare_array_passes_through(agent):
    """Same for top-level arrays."""
    raw = '[{"a": 1}, {"b": 2}]'
    out = agent._strip_fences(raw)
    assert _parses(out) == [{"a": 1}, {"b": 2}]


# ── R77.1.α: preamble handling ─────────────────────────────────────────

def test_strips_preamble_before_json_object(agent):
    """The keystone case: LLM emits prose then a JSON object."""
    raw = 'Here is the Postman collection:\n{"info": {"name": "X"}, "item": [{"name": "A"}]}'
    out = agent._strip_fences(raw)
    parsed = _parses(out)
    assert parsed["info"]["name"] == "X"
    assert len(parsed["item"]) == 1


def test_strips_preamble_before_json_array(agent):
    """Same for array-rooted JSON."""
    raw = "Sure! Here's the list: [1, 2, 3]"
    out = agent._strip_fences(raw)
    assert _parses(out) == [1, 2, 3]


def test_strips_both_preamble_and_trailing(agent):
    """LLM brackets the JSON with prose on BOTH sides. The leading-prose
    scanner trims the trailing prose too because balanced-brace walk
    terminates at the matching close brace."""
    raw = (
        "Here is the JSON output you requested:\n"
        '{"key": "value"}\n'
        "Hope this helps!"
    )
    out = agent._strip_fences(raw)
    assert _parses(out) == {"key": "value"}


def test_trailing_prose_without_preamble_unchanged(agent):
    """When the text starts with `{` AND has trailing prose, R77.1.α
    deliberately leaves it alone — running the balanced-brace scanner
    on text that's already JSON-shaped would mangle valid TypeScript /
    YAML output that contains arbitrary braces. The Newman caller's
    json.loads will surface a clear error on the trailing text; the
    R57.1 retry-with-hint loop nudges the LLM to drop the postscript."""
    raw = (
        '{"info": {"name": "Test"}, "item": [{"name": "GET /api/x"}]}\n'
        "Let me know if you need any changes!"
    )
    out = agent._strip_fences(raw)
    # Untouched (just whitespace-stripped) — the trailing prose remains.
    assert "Let me know" in out


# ── Edge cases: balanced-brace correctness ─────────────────────────────

def test_nested_braces_preserved(agent):
    """The scanner must walk the FULL depth, not stop at the first close."""
    raw = (
        'Output: {"outer": {"inner": {"deep": [1, 2, 3]}}, "tail": "end"}'
    )
    out = agent._strip_fences(raw)
    parsed = _parses(out)
    assert parsed["outer"]["inner"]["deep"] == [1, 2, 3]
    assert parsed["tail"] == "end"


def test_string_containing_braces_does_not_confuse_scanner(agent):
    """Braces INSIDE string literals must not be counted toward depth."""
    raw = 'Note: {"prompt": "Use {{var}} for placeholders", "ok": true}'
    out = agent._strip_fences(raw)
    parsed = _parses(out)
    assert parsed["prompt"] == "Use {{var}} for placeholders"
    assert parsed["ok"] is True


def test_escaped_quote_inside_string(agent):
    r"""A `\"` inside a string must not be treated as a string terminator."""
    raw = r'Result: {"msg": "He said \"hi\""}'
    out = agent._strip_fences(raw)
    parsed = _parses(out)
    assert parsed["msg"] == 'He said "hi"'


# ── Preserves existing fence + think-block behavior ────────────────────

def test_code_fence_path_unchanged(agent):
    """Existing fence-handling must still work — preamble-scan only runs
    when there are no fences."""
    raw = (
        "Here is the JSON:\n"
        "```json\n"
        '{"in_fence": true}\n'
        "```\n"
        "End."
    )
    out = agent._strip_fences(raw)
    assert _parses(out) == {"in_fence": True}


def test_think_block_path_unchanged(agent):
    """`<think>` reasoning blocks are still stripped before the new
    preamble scan fires."""
    raw = (
        "<think>let me design the collection</think>\n"
        "Here is the result: {\"thought_through\": true}"
    )
    out = agent._strip_fences(raw)
    assert _parses(out) == {"thought_through": True}


# ── Defensive: malformed input ─────────────────────────────────────────

def test_unbalanced_braces_returns_from_first_delimiter(agent):
    """An LLM that emits prose + an opening `{` but no closer should
    return everything from `{` onwards — letting the JSON parser report
    a clear, line-numbered error instead of silently truncating."""
    raw = 'Here: {"key": "value", "uncl'   # missing closing quote + brace
    out = agent._strip_fences(raw)
    assert out.startswith('{')
    assert "uncl" in out   # full tail preserved for the parser's error msg


def test_only_prose_no_json_returns_stripped(agent):
    """Pure prose with no `{` or `[` at all — return the original
    stripped text (downstream parsing will fail; we don't synthesize JSON)."""
    raw = "I'm sorry, I cannot fulfill that request."
    out = agent._strip_fences(raw)
    assert out == raw
