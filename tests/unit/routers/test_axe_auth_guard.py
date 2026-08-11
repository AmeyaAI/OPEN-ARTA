"""A1 — the axe (a11y) dispatch must be guarded by the SAME auth pre-flight as
Playwright. Pre-A1 axe ran even when `auth_ok` was False → it scanned the SPA
login wall → vacuous "0 violations PASS". Source-asserted (the dispatch flow is
integration-level; mirrors the session's source-assert pattern).
"""
from __future__ import annotations

import inspect

from src.api.routers import execution as _exec


def test_axe_dispatch_guarded_by_auth_ok():
    src = inspect.getsource(_exec._real_execution_inner)
    # the axe block must check auth_ok + the killswitch, and SKIP truthfully
    assert "ARTA_AXE_AUTH_GUARD_DISABLE" in src
    assert "AXE-AUTH-SKIP" in src
    assert "auth_pre_flight_failed" in src
    # the SKIP is for the axe tool with a skip_reason
    i = src.index("AXE-AUTH-SKIP")
    window = src[i:i + 400]
    assert '"automation_tool": "axe"' in window
    assert "skip_reason" in window
    assert '"status": "SKIP"' in window


def test_axe_guard_uses_same_auth_signal_as_pw():
    src = inspect.getsource(_exec._real_execution_inner)
    # both PW and axe gate on `auth_ok`
    assert src.count("if not auth_ok") >= 2  # PW + axe (Newman uses its own branch)
