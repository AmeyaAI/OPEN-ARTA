"""C — the mission-report Pillar 4 (report SUT quality) must surface the REAL
WCAG signal, truthfully. C1 stamps a11y_violations_* + a11y_scanned onto the
aggregated axe row metadata; C2 queries it and adds an `a11y` sub-dict whose
`status` is `clean` ONLY when axe actually scanned a real page (never on a
BLOCKED/SKIP vacuous pass). Source-asserted (mission-report is DB-backed).
"""
from __future__ import annotations

import inspect

from src.api.routers import execution as _exec


def test_c1_run_axe_stamps_wcag_metadata():
    src = inspect.getsource(_exec._run_axe)
    # aggregated verdict row carries the WCAG counts + the scanned marker
    assert "a11y_violations_critical" in src
    assert "a11y_scanned" in src
    assert "a11y_top_rules" in src


def test_c2_pillar4_queries_and_surfaces_a11y():
    src = inspect.getsource(_exec.get_mission_report)
    # queries the axe rows' metadata
    assert "axe_meta_rows" in src
    assert "automation_tool='axe'" in src
    # truthful status: clean only when scanned; not_assessed when blocked/skip
    assert "not_assessed" in src and "violations_found" in src
    assert '"a11y"' in src and '"status": _a11y_status' in src
    # WCAG violations escalate Pillar 4 CLEAN→MIXED; killswitch present
    assert "ARTA_AXE_PILLAR4_DISABLE" in src
    assert "_a11y_scanned and (_a11y_crit + _a11y_mod) > 0" in src


def test_c2_not_assessed_never_reads_clean():
    """The status logic: scanned==0 → not_assessed (never 'clean')."""
    src = inspect.getsource(_exec.get_mission_report)
    i = src.index("if _a11y_scanned == 0:")
    window = src[i:i + 200]
    assert 'not_assessed' in window
    # 'clean' only in the elif branch (scanned>0 AND 0 crit/mod)
    assert "elif (_a11y_crit + _a11y_mod) == 0:" in window
