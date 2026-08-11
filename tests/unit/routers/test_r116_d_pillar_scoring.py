"""R116.D — Pillar 1, 1b, 4 scoring truthfulness fixes.

Pre-R116.D scoring bugs (confirmed via edge-case probing):
  - Pillar 1: 200×400 + 200×404 hallucinated clusters + pass≥15% graded
    MIXED. Truthful: PESSIMISTIC (gen-quality crisis).
  - Pillar 1b: pytest_blocked=10 + pw_blocked=0 graded MIXED. Truthful:
    PESSIMISTIC (pytest pillar broken).
  - Pillar 4: 0 defects graded PESSIMISTIC. Truthful: CLEAN when newman
    actually ran (genuinely bug-free SUT report). MIXED when no signal.

Tests are source-checks (parallel to R115.G/R116.C pattern): pin the
code path without DB seeding.
"""
from __future__ import annotations

import re
from pathlib import Path


_EXECUTION_PY = Path(__file__).resolve().parents[3] / "src" / "api" / "routers" / "execution.py"


def test_r116_d_pillar1_cluster_ratio_gate():
    """Source check: Pillar 1 uses cluster_ratio = (400+404)/total threshold."""
    content = _EXECUTION_PY.read_text()
    p1_start = content.find("# Pillar 1 — test cases (Newman gen quality)")
    assert p1_start > 0, "Pillar 1 scoring block not found"
    window = content[p1_start:p1_start + 2500]
    assert "R116.D" in window, "R116.D marker missing in Pillar 1"
    assert "cluster_ratio" in window, "R116.D: cluster_ratio derivation missing"
    # The crisis gate: cluster_ratio >= 0.25 → PESSIMISTIC
    pattern = re.compile(
        r"cluster_ratio\s*>=\s*0\.25",
        re.DOTALL,
    )
    assert pattern.search(window), (
        "R116.D: cluster_ratio >= 0.25 → PESSIMISTIC gate missing "
        "(gen-quality crisis must override high pass rate)"
    )


def test_r116_d_pillar1_high_pass_rate_no_longer_masks_clusters():
    """Source check: CLEAN requires BOTH pass≥50% AND cluster_ratio<10%."""
    content = _EXECUTION_PY.read_text()
    p1_start = content.find("# Pillar 1 — test cases (Newman gen quality)")
    window = content[p1_start:p1_start + 2500]
    assert "newman_pass_rate >= 50 and cluster_ratio < 0.10" in window, (
        "R116.D: CLEAN must require pass≥50 AND cluster_ratio<10%"
    )


def test_r116_d_pillar1b_pytest_blocked_counts():
    """Source check: Pillar 1b sums pw_blocked + pytest_blocked symmetrically."""
    content = _EXECUTION_PY.read_text()
    p1b_start = content.find("# Pillar 1b — test scripts")
    assert p1b_start > 0, "Pillar 1b scoring block not found"
    window = content[p1b_start:p1b_start + 2500]
    assert "R116.D" in window, "R116.D marker missing in Pillar 1b"
    # Symmetric: total = pw_blocked + pytest_blocked
    assert "total_blocked_1b" in window, (
        "R116.D: total_blocked_1b sum missing"
    )
    assert "pw_blocked + pytest_blocked" in window, (
        "R116.D: must sum pw_blocked + pytest_blocked for the threshold"
    )


def test_r116_d_pillar4_zero_defects_clean_when_healthy():
    """Source check: Pillar 4 grades 0 defects + healthy run as CLEAN."""
    content = _EXECUTION_PY.read_text()
    p4_start = content.find("# Pillar 4 — report SUT quality")
    assert p4_start > 0, "Pillar 4 scoring block not found"
    window = content[p4_start:p4_start + 2500]
    assert "R116.D" in window, "R116.D marker missing in Pillar 4"
    # Bug-free SUT path: total_defects == 0 AND healthy execution → CLEAN
    assert "total_defects == 0" in window, (
        "R116.D: zero-defects branch missing"
    )
    assert "newman_total >= 50 and newman_pass_rate >= 30" in window, (
        "R116.D: healthy-run threshold for zero-defects CLEAN missing"
    )


def test_r116_d_pillar4_zero_defects_mixed_when_no_signal():
    """Source check: Pillar 4 falls back to MIXED when 0 defects but no execution signal."""
    content = _EXECUTION_PY.read_text()
    p4_start = content.find("# Pillar 4 — report SUT quality")
    window = content[p4_start:p4_start + 2500]
    # The else branch under `if total_defects == 0:` must yield MIXED
    pattern = re.compile(
        r"if\s+total_defects\s*==\s*0\s*:.*?pillar_4_score\s*=\s*\"MIXED\"",
        re.DOTALL,
    )
    assert pattern.search(window), (
        "R116.D: zero-defects + no-signal fallback to MIXED missing"
    )
