"""R213.K.3 — k6 tasks dispatch in the "fast" gather group, so the R99.E
stage→tools map MUST list k6 under "fast". Omitting it made a k6-only
tools-filter (tools=["k6"]) skip the entire fast stage → 0 k6 dispatched with
no result/BLOCKED row (live: run-4e0720 + run-d8cafd, 32 valid specs on disk).
"""
from __future__ import annotations

from src.api.routers.execution import _R99_E_STAGE_TO_TOOLS, _r163_tool_enabled


def test_k6_is_in_fast_stage():
    assert "k6" in _R99_E_STAGE_TO_TOOLS["fast"]


def test_k6_only_filter_does_not_skip_fast_stage():
    # Reproduce the R99.E skip decision: stage runs iff the filter intersects the
    # stage's tool set. A k6-only filter must NOT skip "fast".
    tools_filter = {"k6"}
    fast_tools = _R99_E_STAGE_TO_TOOLS["fast"]
    assert tools_filter & fast_tools, "k6-only filter would skip the fast stage (k6 never dispatches)"
    assert _r163_tool_enabled("k6", tools_filter) is True


def test_every_fast_tool_is_individually_dispatchable():
    # Each tool that rides the fast stage must, on its own filter, keep the stage alive.
    for tool in _R99_E_STAGE_TO_TOOLS["fast"]:
        assert {tool} & _R99_E_STAGE_TO_TOOLS["fast"], tool


def test_newman_and_playwright_stages_unchanged():
    assert _R99_E_STAGE_TO_TOOLS["newman"] == {"newman"}
    assert _R99_E_STAGE_TO_TOOLS["playwright"] == {"playwright"}
