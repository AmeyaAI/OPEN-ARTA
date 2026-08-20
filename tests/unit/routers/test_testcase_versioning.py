"""Test-case versioning helpers — the no-DB-required correctness points of the
revert feature: (1) a snapshot captures the CANONICAL disk script, not the stale
DB cache; (2) the revert disk write is traversal-guarded to src/automation/;
(3) the killswitch/empty-input short-circuits before any DB work."""
import os

import pytest

from src.api.routers.tests_versions import (
    _canonical_script,
    _resolve_under_automation,
    _AUTOMATION_ROOT,
    snapshot_test_cases,
)


def test_canonical_prefers_disk_over_cache(tmp_path):
    f = tmp_path / "spec.ts"
    f.write_text("DISK CONTENT — the executable truth")
    # disk present → disk wins over the stale DB cache
    assert _canonical_script(str(f), "stale cache") == "DISK CONTENT — the executable truth"


def test_canonical_falls_back_to_cache_when_no_file(tmp_path):
    missing = tmp_path / "gone.ts"
    assert _canonical_script(str(missing), "cache fallback") == "cache fallback"
    assert _canonical_script(None, "cache fallback") == "cache fallback"
    assert _canonical_script(None, None) == ""


def test_traversal_guard_allows_under_automation():
    ok = _resolve_under_automation("src/automation/playwright/kui_1.spec.ts")
    assert ok is not None and str(ok).startswith(str(_AUTOMATION_ROOT))


def test_traversal_guard_refuses_escape():
    # a DB-sourced path must not write outside the automation tree
    assert _resolve_under_automation("../../etc/passwd") is None
    assert _resolve_under_automation("/etc/passwd") is None
    assert _resolve_under_automation("src/automation/../../../../tmp/evil") is None


def test_snapshot_hash_includes_metadata_and_scaffold():
    # M4 — the dedup gate must fold metadata + scaffold, else a metadata-only or
    # priority/tags-only change is skipped as a "duplicate" and the resurrection
    # scaffold goes stale.
    from src.api.routers.tests_versions import _snapshot_hash
    base = _snapshot_hash("g", "s", {"a": 1}, {"priority": "P2"})
    assert _snapshot_hash("g", "s", {"a": 2}, {"priority": "P2"}) != base   # metadata-only
    assert _snapshot_hash("g", "s", {"a": 1}, {"priority": "P0"}) != base   # scaffold-only
    assert _snapshot_hash("g", "s", {"a": 1}, {"priority": "P2"}) == base   # identical → dedup
    # key order is stable (dict serialization sorted)
    assert _snapshot_hash("g", "s", {"a": 1, "b": 2}, {}) == _snapshot_hash("g", "s", {"b": 2, "a": 1}, {})


def test_version_model_carries_resurrection_columns():
    # guards migration↔model drift: revert restores the traceability spine
    # (metadata_snapshot) and can RESURRECT a deleted row (row_snapshot).
    from src.db.models import TestCaseVersion
    cols = TestCaseVersion.__table__.columns.keys()
    assert "metadata_snapshot" in cols
    assert "row_snapshot" in cols


@pytest.mark.asyncio
async def test_snapshot_killswitch_and_empty_shortcircuit():
    # both must return 0 WITHOUT touching the (None) db — proves the guard runs first
    os.environ["ARTA_TESTCASE_VERSIONING_DISABLE"] = "1"
    try:
        assert await snapshot_test_cases(None, ["TC-X-1"], "b1") == 0
    finally:
        del os.environ["ARTA_TESTCASE_VERSIONING_DISABLE"]
    assert await snapshot_test_cases(None, [], "b1") == 0
    assert await snapshot_test_cases(None, None, "b1") == 0
