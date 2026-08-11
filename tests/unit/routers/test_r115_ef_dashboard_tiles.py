"""R115.E + R115.F — dashboard truthfulness tiles.

R115.E: per-run vs all-time P0 split. Pre-R115.E the tile fell back to
project-wide P0 count when run-scoped returned 0 → operator confused
"did THIS run create 107 P0?" when 107 was historical.

R115.F: self-heal queue ETA tile. Surfaces queue_depth + ETA in minutes
so operator can plan when to expect 2049 queued items to drain.
"""
from __future__ import annotations

import re
from pathlib import Path


_FRONTEND_TSX = (
    Path(__file__).resolve().parents[3] / "frontend" / "src" / "app" / "run-history" / "RunDetailContent.tsx"
)


def test_r115_e_run_scoped_count_no_fallback():
    """Source check: run-scoped query no longer falls through to project query."""
    content = _FRONTEND_TSX.read_text()
    # Confirm R115.E comment + the no-fallback logic
    assert "R115.E" in content
    pattern = re.compile(
        r"R115\.E.*?ALWAYS set run-scoped count.*?No more.*?fall-through",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R115.E: run-scoped query must NOT fall through to project query for primary tile"
    )


def test_r115_e_alltime_p0_separate_query():
    """Source check: all-time P0 has its own query + state."""
    content = _FRONTEND_TSX.read_text()
    assert "allTimeP0Defects" in content, "R115.E: allTimeP0Defects state missing"
    assert "setAllTimeP0Defects" in content


def test_r115_e_two_distinct_tile_rows():
    """Source check: 'P0 from this run' + 'All-time open P0' tiles both present."""
    content = _FRONTEND_TSX.read_text()
    assert "P0 defects from THIS run" in content, (
        "R115.E: per-run tile label missing"
    )
    assert "All-time open P0" in content, (
        "R115.E: all-time tile label missing"
    )


def test_r115_f_queue_eta_state():
    """Source check: regenQueueDepth + regenQueueEta state hooks present."""
    content = _FRONTEND_TSX.read_text()
    assert "regenQueueDepth" in content, "R115.F: regenQueueDepth state missing"
    assert "regenQueueEta" in content, "R115.F: regenQueueEta state missing"
    assert "/api/health/regen-consumer" in content


def test_r115_f_eta_formula_correct():
    """Source check: ETA = depth / drain × cycleSec / 60 (minutes)."""
    content = _FRONTEND_TSX.read_text()
    # Look for the formula: depth / drain * cycleSec / 60
    pattern = re.compile(
        r"depth\s*/\s*drain\s*\*\s*cycleSec\s*/\s*60",
    )
    assert pattern.search(content), (
        "R115.F: ETA formula `depth / drain × cycleSec / 60` missing"
    )


def test_r115_f_tile_renders_when_queue_nonzero():
    """Source check: queue ETA tile renders only when queue_depth > 0."""
    content = _FRONTEND_TSX.read_text()
    pattern = re.compile(
        r"regenQueueDepth\s*!==\s*null\s*&&\s*regenQueueDepth\s*>\s*0",
    )
    assert pattern.search(content), (
        "R115.F: tile gate (queue_depth > 0) missing"
    )
