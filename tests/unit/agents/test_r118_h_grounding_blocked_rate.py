"""R118.H regression tests for the grounding-blocked-rate gate metric.

Pre-R118.H: `gen_quality_first_try_pass_rate` measured whether the gen
pipeline COMPLETED (no `generation_failure` stamp) but did NOT count
specs whose generation completed AND passed validators BUT were R102.A-
stamped at retry exhaustion. Those specs are excluded from dispatch by
R102.C — invisible to the gate yet operator-actionable.

R118.H closes the visibility gap. The new check scans on-disk PW specs
for R102.A `_dispatch_block_kind: playwright_grounding_violation` stamps
and reports a severity-tiered ratio:
  - INFO ≤5% (acceptable noise)
  - WARN ≤15% (gen quality slipping)
  - BLOCK >15% (gen-quality crisis)
"""
from __future__ import annotations

import os
from pathlib import Path

from src.agents.quality_gate_agent import QualityGateAgent


def _make_specs(tmp_path: Path, total: int, stamped: int) -> Path:
    """Create `total` PW specs in tmp_path, of which `stamped` carry the
    R102.A comment-header. Returns the PW dir."""
    pw_dir = tmp_path / "src" / "automation" / "playwright"
    pw_dir.mkdir(parents=True)
    for i in range(total):
        spec = pw_dir / f"req_am_{i:03d}.spec.ts"
        if i < stamped:
            spec.write_text(
                "// ── ARTA _grounding_violations stamp (R102.A) ──\n"
                "// _dispatch_block_kind: playwright_grounding_violation\n"
                "// _grounding_violations:\n"
                "//   {\"kind\": \"bad_playwright_api\", \"symbol\": \"x\"}\n"
                "// ────────────────────────────────────────────────\n"
                "import { test, expect } from '@playwright/test';\n"
                f"test('foo {i}', async ({{ page }}) => {{ await page.goto('/'); }});\n"
            )
        else:
            spec.write_text(
                "import { test, expect } from '@playwright/test';\n"
                f"test('foo {i}', async ({{ page }}) => {{ await page.goto('/'); }});\n"
            )
    return pw_dir


def _run_check(tmp_path: Path) -> list:
    """Invoke _check_r102_a_stamp_rate with cwd switched to tmp_path so
    the relative Path('src/automation/playwright') resolves correctly."""
    agent = QualityGateAgent()
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        # project_id is mandatory per R75.5 scope guard
        return agent._check_r102_a_stamp_rate({"project_id": "test-pid"})
    finally:
        os.chdir(cwd)


def test_r118_h_zero_stamped_info_severity(tmp_path):
    """0 stamped out of 10 specs → 0% → INFO severity, gate PASSes."""
    _make_specs(tmp_path, total=10, stamped=0)
    checks = _run_check(tmp_path)
    assert len(checks) == 1
    c = checks[0]
    assert c.name == "r118_h_grounding_blocked_rate"
    assert c.severity == "INFO"
    assert c.passed is True
    assert "0/10" in c.actual


def test_r118_h_acceptable_band_info(tmp_path):
    """5% stamped (1 of 20) — at the boundary, should be INFO."""
    _make_specs(tmp_path, total=20, stamped=1)
    checks = _run_check(tmp_path)
    assert len(checks) == 1
    c = checks[0]
    assert c.severity == "INFO", (
        f"5% should be INFO (boundary at >5% triggers WARN); got {c.severity}"
    )
    assert "1/20" in c.actual
    assert "5.0%" in c.actual


def test_r118_h_slipping_warn(tmp_path):
    """10% stamped (2 of 20) → above 5%, below 15% → WARN."""
    _make_specs(tmp_path, total=20, stamped=2)
    checks = _run_check(tmp_path)
    assert len(checks) == 1
    c = checks[0]
    assert c.severity == "WARN", f"10% should be WARN; got {c.severity}"
    assert c.passed is False


def test_r118_h_crisis_block(tmp_path):
    """20% stamped (4 of 20) → above 15% → BLOCK (gen-quality crisis)."""
    _make_specs(tmp_path, total=20, stamped=4)
    checks = _run_check(tmp_path)
    assert len(checks) == 1
    c = checks[0]
    assert c.severity == "BLOCK", f"20% should be BLOCK; got {c.severity}"
    assert c.passed is False
    assert "4/20" in c.actual
