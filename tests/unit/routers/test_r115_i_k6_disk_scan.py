"""R115.I.2 — k6 dispatch disk-scan fallback when GENERATED_TESTS is empty.

Pre-R115.I.2: bulk-regen wrote k6 specs to disk but didn't register them in
the in-memory GENERATED_TESTS inventory. Result: k6 dispatch saw 0 entries
and emitted BLOCKED row with `no_k6_specs_in_inventory` reason even though
18 valid req_am_*.js files were on disk (run-8da91d evidence).

R115.I.2 adds a fallback: when GENERATED_TESTS has 0 k6 entries for a
project AND the disk has *.js files matching the R-PWProjectFilter prefix,
auto-synthesize transient entries from disk for THIS run.

These tests verify the source-level wiring of the R115.I.2 fallback.
"""
from __future__ import annotations

import re
from pathlib import Path


_EXECUTION_PY = Path(__file__).resolve().parents[3] / "src" / "api" / "routers" / "execution.py"


def test_r115_i_2_disk_scan_branch_present():
    """Source check: R115.I.2 disk-scan fallback branch exists in execution.py."""
    content = _EXECUTION_PY.read_text()
    # Look for the R115.I.2 marker comment + the disk-scan logic
    pattern = re.compile(
        r"R115\.I\.2.*disk-scan.*?k6_dir\.glob\(\"\*\.js\"\)",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R115.I.2: disk-scan fallback branch missing from execution.py"
    )


def test_r115_i_2_validates_content_before_adding():
    """Source check: R115.I.2 reuses the R90.5 verdict helper to skip stub files."""
    content = _EXECUTION_PY.read_text()
    # Find the R115.I.2 block specifically
    r115_block_start = content.find("R115.I.2 — disk-scan fallback")
    assert r115_block_start > 0, "R115.I.2 block not found"
    # Within the block, _r90_5_k6_verdict must be called
    block_window = content[r115_block_start:r115_block_start + 2500]
    assert "_r90_5_k6_verdict" in block_window, (
        "R115.I.2: should call _r90_5_k6_verdict before adding to inventory"
    )


def test_r115_i_2_synthesizes_transient_entry_shape():
    """Source check: synthesized entries carry _arta_source='r115_i_2_disk_scan'."""
    content = _EXECUTION_PY.read_text()
    assert '"_arta_source": "r115_i_2_disk_scan"' in content, (
        "R115.I.2: synthesized entry missing _arta_source marker"
    )
    # And carries the canonical fields
    assert '"automation_tool": "k6"' in content
    assert '"script_path"' in content


def test_r115_i_2_does_not_mutate_GENERATED_TESTS():
    """Source check: R115.I.2 appends to LOCAL `project_k6_entries`, not GENERATED_TESTS."""
    content = _EXECUTION_PY.read_text()
    r115_block_start = content.find("R115.I.2 — disk-scan fallback")
    block_window = content[r115_block_start:r115_block_start + 2500]
    # Must append to project_k6_entries (local var), NOT GENERATED_TESTS
    assert "project_k6_entries.append" in block_window, (
        "R115.I.2: should append to project_k6_entries local var"
    )
    assert "GENERATED_TESTS.append" not in block_window, (
        "R115.I.2: must NOT mutate GENERATED_TESTS global (keeps in-memory state clean)"
    )
