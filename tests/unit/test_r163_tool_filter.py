"""R163 — honor the request's `tools` filter at dispatch.

Pre-R163 the core PW/newman/k6 stages ran unconditionally; only the extra
tools (selenium/cypress/axe/pytest) checked the filter. So `tools:["newman"]`
still ran the full PW+ZAP+selenium tail. R163 gates every stage.
"""
from __future__ import annotations

from src.api.routers.execution import _r163_tool_enabled


def test_empty_filter_enables_all_tools():
    # Legacy behavior: no filter ⇒ everything dispatches.
    for t in ("playwright", "newman", "k6", "zap", "selenium"):
        assert _r163_tool_enabled(t, set()) is True


def test_filter_restricts_to_listed_tools():
    f = {"newman"}
    assert _r163_tool_enabled("newman", f) is True
    assert _r163_tool_enabled("playwright", f) is False
    assert _r163_tool_enabled("zap", f) is False
    assert _r163_tool_enabled("k6", f) is False


def test_multi_tool_filter():
    f = {"newman", "k6"}
    assert _r163_tool_enabled("newman", f) is True
    assert _r163_tool_enabled("k6", f) is True
    assert _r163_tool_enabled("playwright", f) is False
