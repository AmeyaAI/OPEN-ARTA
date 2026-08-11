"""R141.A — configurable --max-turns for Claude CLI client.

Pre-R141.A: `--max-turns 1` was hardcoded at claude_cli_client.py:161.
R135 evidence: "Reached max turns (1)" repeated per cluster across 2
iters; defect classifier produced 0 defects from 522+224 failures.
Pillar 4 mission violation.

Post-R141.A: configurable via ARTA_CLAUDE_CLI_MAX_TURNS env (default 2
per Plan-agent critique point 3 — 3 triples per-cluster wallclock and
may convert turn-exhaust into TimeoutError). Cost ceiling: clamped to
[1, 10] to prevent runaway spend.
"""
from __future__ import annotations

import os

from src.agents.claude_cli_client import (
    _r141_a_0_count_turns_in_output,
    _r141_a_max_turns_default,
)


def test_r141_a_default_max_turns_is_1(monkeypatch):
    """R215 Item 1: default 2→1 — ARTA's CLI calls are single-shot structured
    output; a 2nd turn is pure latency (contributed to the 209s risk-scoring
    timeouts). Env-overridable for the rare multi-turn call."""
    monkeypatch.delenv("ARTA_CLAUDE_CLI_MAX_TURNS", raising=False)
    assert _r141_a_max_turns_default() == 1


def test_r141_a_env_override_respected(monkeypatch):
    monkeypatch.setenv("ARTA_CLAUDE_CLI_MAX_TURNS", "5")
    assert _r141_a_max_turns_default() == 5


def test_r141_a_clamps_to_safe_range(monkeypatch):
    """Cost ceiling: clamped to [1, 10] to prevent runaway spend."""
    monkeypatch.setenv("ARTA_CLAUDE_CLI_MAX_TURNS", "0")
    assert _r141_a_max_turns_default() == 1
    monkeypatch.setenv("ARTA_CLAUDE_CLI_MAX_TURNS", "100")
    assert _r141_a_max_turns_default() == 10
    monkeypatch.setenv("ARTA_CLAUDE_CLI_MAX_TURNS", "-5")
    assert _r141_a_max_turns_default() == 1


def test_r141_a_invalid_value_falls_back_to_1(monkeypatch):
    monkeypatch.setenv("ARTA_CLAUDE_CLI_MAX_TURNS", "not-an-int")
    assert _r141_a_max_turns_default() == 1


def test_r141_a_0_count_turns_no_markers():
    """When CLI output has no Turn markers → single-turn execution (count=1)."""
    assert _r141_a_0_count_turns_in_output("Some plain output without markers") == 1
    assert _r141_a_0_count_turns_in_output("") == 0


def test_r141_a_0_count_turns_with_markers():
    """When CLI emits Turn markers → count them."""
    output = (
        "--- Turn 1 ---\n"
        "Some thinking...\n"
        "--- Turn 2 ---\n"
        "More work\n"
    )
    assert _r141_a_0_count_turns_in_output(output) == 2
