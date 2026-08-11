"""R113.C — verify per-spec dispatch logging lines fire.

Pre-R113.C: only the aggregate "Playwright completed for run X: N results
across M specs" log line surfaced. Operators couldn't tell which specs were
R30.5-blocked, R102.C-blocked, or actually dispatched. R113.C adds 4 INFO
lines:
  1. R113.C dispatch inventory  (total / a11y / non-a11y counts)
  2. R113.C spec=X blocked_by=R30.5 unresolved_vars=...
  3. R113.C spec=X blocked_by=R102.C violation_kinds=...
  4. R113.C spec=X dispatched returncode=Y tests=N pass=A fail=B skip=C

This test verifies the log format strings are present in execution.py at
the right call sites (source-level verification — actual dispatch is
tested end-to-end via the smoke harness).
"""
from __future__ import annotations

import re
from pathlib import Path


_EXECUTION_PY = Path(__file__).resolve().parents[3] / "src" / "api" / "routers" / "execution.py"


def test_r113_c_inventory_log_format_present():
    """Verify the R113.C dispatch inventory log line is present in source."""
    content = _EXECUTION_PY.read_text()
    assert "R113.C dispatch inventory" in content, (
        "R113.C inventory log format string missing from execution.py"
    )
    # The format string should include key fields
    pattern = re.compile(
        r"R113\.C dispatch inventory.*total specs.*TARGET_TEST_MATCH.*a11y.*non-a11y",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R113.C inventory log missing required fields (total/TARGET_TEST_MATCH/a11y/non-a11y)"
    )


def test_r113_c_per_spec_r30_5_log_format_present():
    """Verify per-spec R30.5 log line `R113.C spec=X blocked_by=R30.5 ...`."""
    content = _EXECUTION_PY.read_text()
    pattern = re.compile(r'"R113\.C spec=%s blocked_by=R30\.5 unresolved_vars=%s"')
    assert pattern.search(content), (
        "R113.C per-spec R30.5 log format string missing from execution.py"
    )


def test_r113_c_per_spec_r102_c_log_format_present():
    """Verify per-spec R102.C log line `R113.C spec=X blocked_by=R102.C violation_kinds=...`."""
    content = _EXECUTION_PY.read_text()
    pattern = re.compile(r'"R113\.C spec=%s blocked_by=R102\.C violation_kinds=%s"')
    assert pattern.search(content), (
        "R113.C per-spec R102.C log format string missing from execution.py"
    )


def test_r113_c_per_spec_dispatch_outcome_log_format_present():
    """Verify per-spec dispatch outcome log `R113.C spec=X dispatched returncode=Y ...`."""
    content = _EXECUTION_PY.read_text()
    pattern = re.compile(
        r'"R113\.C spec=%s dispatched returncode=%s tests=%d pass=%d fail=%d skip=%d other=%d"'
    )
    assert pattern.search(content), (
        "R113.C per-spec dispatch outcome log format string missing from execution.py"
    )
