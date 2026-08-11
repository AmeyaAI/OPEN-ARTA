"""R127.D.6.E — shared `grounding_stamp` module tests.

The shared module is the single-source-of-truth for R102.A-style stamps
across PW + Pytest merge paths. These tests pin:
  - PW stamp shape (TS comments, `// _dispatch_block_kind: playwright_grounding_violation`)
  - Pytest stamp shape (Python comments, `# _dispatch_block_kind: pytest_grounding_violation`)
  - Idempotency on both
  - JSON-line shape matches R102.C dispatch-reader regex at execution.py:3353
  - Empty violations list returns content unchanged
  - Dataclass-style violations + dict-style violations both serialize
"""
from __future__ import annotations

import json
import re

from src.agents.grounding_stamp import (
    PW_BLOCK_KIND,
    PYTEST_BLOCK_KIND,
    _serialize_violation,
    stamp_pw_violations,
    stamp_pytest_violations,
)


# Regex from execution.py:3353 — used by R102.C dispatch reader to extract
# violation JSON from comment headers. Must match the same pattern produced
# by the stamp helpers.
_R102_C_PW_RE = re.compile(r"^//\s+(\{[^\n]+\})\s*$", re.MULTILINE)
_R102_C_PYTEST_RE = re.compile(r"^#\s+(\{[^\n]+\})\s*$", re.MULTILINE)


class _FakeViolation:
    """Mimics src/agents/grounding_validator.py:GroundingViolation."""

    def __init__(self, kind: str, symbol: str = "<sym>", hint: str = "<hint>"):
        self.kind = kind
        self.symbol = symbol
        self.hint = hint


def test_pw_stamp_empty_violations_returns_unchanged():
    content = "import { test } from '@playwright/test';\ntest('foo', async () => {});\n"
    out = stamp_pw_violations(content, [])
    assert out == content, "empty violations list must NOT prepend stamp"


def test_pw_stamp_prepends_canonical_header():
    content = "test('foo', async () => {});\n"
    viols = [_FakeViolation("merged_paren_imbalance", "<spec>", "paren delta +3")]
    out = stamp_pw_violations(content, viols)
    head = out.split("\n", 6)
    assert head[0] == "// ── ARTA _grounding_violations stamp (R102.A) ──"
    assert head[1] == f"// _dispatch_block_kind: {PW_BLOCK_KIND}"
    assert head[2] == "// _grounding_violations:"
    # JSON line per violation
    json_lines = _R102_C_PW_RE.findall(out)
    assert len(json_lines) == 1
    payload = json.loads(json_lines[0])
    assert payload["kind"] == "merged_paren_imbalance"
    assert payload["symbol"] == "<spec>"
    assert "paren delta +3" in payload["hint"]
    # Original content preserved at tail
    assert out.endswith(content)


def test_pw_stamp_idempotent_on_double_call():
    content = "test('foo', async () => {});\n"
    viols = [_FakeViolation("merged_paren_imbalance")]
    first = stamp_pw_violations(content, viols)
    second = stamp_pw_violations(first, viols)
    assert first == second, "second call must NOT re-stamp when block-kind already present"


def test_pytest_stamp_uses_python_comments():
    content = "import pytest\n\ndef test_foo():\n    pass\n"
    viols = [_FakeViolation("pytest_syntax_error_indent", "line 5:0", "unexpected indent")]
    out = stamp_pytest_violations(content, viols)
    head = out.split("\n", 6)
    assert head[0] == "# ── ARTA _grounding_violations stamp (R127.D.6.D) ──"
    assert head[1] == f"# _dispatch_block_kind: {PYTEST_BLOCK_KIND}"
    assert head[2] == "# _grounding_violations:"
    # No `//` TypeScript comments leaked
    assert "//" not in out.split("\n\n", 1)[0]
    json_lines = _R102_C_PYTEST_RE.findall(out)
    assert len(json_lines) == 1
    payload = json.loads(json_lines[0])
    assert payload["kind"] == "pytest_syntax_error_indent"


def test_pytest_stamp_idempotent_on_double_call():
    content = "def test_x():\n    pass\n"
    viols = [_FakeViolation("pytest_syntax_error_eof")]
    first = stamp_pytest_violations(content, viols)
    second = stamp_pytest_violations(first, viols)
    assert first == second


def test_stamp_accepts_both_dataclass_and_dict_violations():
    """R127.D.6.A + R127.D.6.D pass plain dicts; automation_engineer passes
    GroundingViolation dataclass instances. The shared helper must accept
    both shapes without raising."""
    content_pw = "test('x', async () => {});\n"
    dict_viol = {"kind": "merged_paren_imbalance", "symbol": "s", "hint": "h"}
    dc_viol = _FakeViolation("merged_paren_imbalance", "s2", "h2")
    out = stamp_pw_violations(content_pw, [dict_viol, dc_viol])
    json_lines = _R102_C_PW_RE.findall(out)
    assert len(json_lines) == 2
    payloads = [json.loads(line) for line in json_lines]
    assert {p["symbol"] for p in payloads} == {"s", "s2"}
