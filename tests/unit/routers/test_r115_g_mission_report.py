"""R115.G — ARTA Mission Outcome consolidated report endpoint.

Single GET /api/runs/{run_id}/mission-report endpoint aggregates per-pillar
scores (1, 1b, 2, 4) with sub-metric drilldown + actionable CTAs. Mission
contract: single-view operator dashboard for ARTA's autonomous ATDD
delivery against the SUT.
"""
from __future__ import annotations

import re
from pathlib import Path


_EXECUTION_PY = Path(__file__).resolve().parents[3] / "src" / "api" / "routers" / "execution.py"


def test_r115_g_endpoint_registered():
    """Source check: /runs/{run_id}/mission-report endpoint exists."""
    content = _EXECUTION_PY.read_text()
    pattern = re.compile(
        r"@router\.get\(\"/runs/\{run_id\}/mission-report\".*?async def get_mission_report",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R115.G: /runs/{run_id}/mission-report endpoint missing"
    )


def test_r115_g_returns_all_4_pillars():
    """Source check: response carries all 4 ARTA mission pillars."""
    content = _EXECUTION_PY.read_text()
    g_start = content.find("get_mission_report")
    assert g_start > 0
    # Window grew 14000→18000→21000→22500→27000 (a11y C2 Pillar-4 block + axe-meta query + WCAG aggregation added ~67 lines) (the
    # all_results DB-source fix + the B5 upstream_quality block added
    # ~35 lines before the actionable_ctas key, pushing it past 18000).
    # →34000: R259 fidelity query + R260 Pillar-4 re-basing added ~60 lines
    # ahead of the pillar keys.
    window = content[g_start:g_start + 34000]
    for pillar_key in (
        "pillar_1_test_case_quality",
        "pillar_1b_test_script_quality",
        "pillar_2_execute_flawlessly",
        "pillar_4_report_sut_quality",
    ):
        assert pillar_key in window, (
            f"R115.G: pillar key '{pillar_key}' missing from response"
        )


def test_r115_g_outcome_band_classification():
    """Source check: outcome_band derived from per-pillar scores."""
    content = _EXECUTION_PY.read_text()
    g_start = content.find("get_mission_report")
    # Window grew 14000→18000→21000→22500→27000 (a11y C2 Pillar-4 block + axe-meta query + WCAG aggregation added ~67 lines) (the
    # all_results DB-source fix + the B5 upstream_quality block added
    # ~35 lines before the actionable_ctas key, pushing it past 18000).
    window = content[g_start:g_start + 34000]
    assert 'outcome_band = "OPTIMISTIC"' in window
    assert 'outcome_band = "MIXED"' in window
    assert 'outcome_band = "PESSIMISTIC"' in window


def test_r115_g_actionable_ctas_present():
    """Source check: actionable_ctas list built from pillar signals."""
    content = _EXECUTION_PY.read_text()
    g_start = content.find("get_mission_report")
    # Window grew 14000→18000→21000→22500→27000 (a11y C2 Pillar-4 block + axe-meta query + WCAG aggregation added ~67 lines) (the
    # all_results DB-source fix + the B5 upstream_quality block added
    # ~35 lines before the actionable_ctas key, pushing it past 18000).
    # R218 F2 — the per-requirement query block (~30 lines) pushed it past 27000.
    # R302 — the row-level fidelity query (~30 lines) pushed it past 34000.
    window = content[g_start:g_start + 36000]
    assert "actionable_ctas" in window, "R115.G: actionable_ctas key missing"
    assert "Investigate Newman 400" in window or "newman_400" in window
    assert "auth-scope-summary" in window  # CTA links to R115.A.3 endpoint
    assert "sut_regression" in window
