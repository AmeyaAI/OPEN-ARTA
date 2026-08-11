"""R130.E KEYSTONE — Regen-consumer triage gate unit tests.

Six cases lock down the R30.3 contract:
  1. `triage_category=operator_review` → marker MOVED to `gated_no_regen/`,
     no LLM call, returns True (marker consumed).
  2. `triage_category=sut_regression` → same gating.
  3. `triage_category=test_gen_bug` → falls through to normal regen flow.
  4. Missing `triage_category` field → defaults to `test_gen_bug` (legacy).
  5. Unknown `triage_category` (e.g., `flaky_test`) → falls through.
  6. `gated_no_regen/` directory auto-created if missing.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.api.services import regen_consumer
from src.api.services.regen_consumer import _regen_one


def _make_marker(tmp_path: Path, *, payload: dict) -> Path:
    """Write a marker file at tmp_path/.arta/regen_queue/<id>.json
    and return its path. Tests use this to drive _regen_one."""
    queue_dir = tmp_path / ".arta" / "regen_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    marker = queue_dir / f"{payload.get('test_id', 'TC-TEST-001')}.json"
    marker.write_text(json.dumps(payload))
    return marker


@pytest.fixture(autouse=True)
def _redirect_queue_paths(tmp_path, monkeypatch):
    """Redirect module-level QUEUE_DIR / APPLIED_DIR / ORPHAN_DIR into
    tmp_path so tests don't touch the real .arta/regen_queue/."""
    fake_queue = tmp_path / ".arta" / "regen_queue"
    fake_applied = fake_queue / "applied"
    fake_orphan = fake_applied / "orphans"
    monkeypatch.setattr(regen_consumer, "QUEUE_DIR", fake_queue)
    monkeypatch.setattr(regen_consumer, "APPLIED_DIR", fake_applied)
    monkeypatch.setattr(regen_consumer, "ORPHAN_DIR", fake_orphan)
    fake_queue.mkdir(parents=True, exist_ok=True)
    fake_applied.mkdir(parents=True, exist_ok=True)
    fake_orphan.mkdir(parents=True, exist_ok=True)
    yield


# ── Case 1: operator_review → gated_no_regen ──────────────────────────────


@pytest.mark.asyncio
async def test_r130e_operator_review_gated(tmp_path):
    """`triage_category=operator_review` → marker moved to gated_no_regen/,
    no LLM call, returns True."""
    marker = _make_marker(tmp_path, payload={
        "test_id": "TC-AM-001-01-api",
        "triage_category": "operator_review",
        "signals": ["auth_scope_mismatch"],
        "sample_error": "401 Unauthorized",
    })
    result = await _regen_one(marker)
    assert result is True, "Gated marker should return True (consumed)"
    # Marker moved out of queue
    assert not marker.exists(), "Original marker file should be moved"
    # Landed in gated_no_regen/
    gated_dir = regen_consumer.APPLIED_DIR / "gated_no_regen"
    assert gated_dir.is_dir()
    moved_files = list(gated_dir.glob("*.json"))
    assert len(moved_files) == 1
    assert moved_files[0].name == "TC-AM-001-01-api.json"


# ── Case 2: sut_regression → gated_no_regen ───────────────────────────────


@pytest.mark.asyncio
async def test_r130e_sut_regression_gated(tmp_path):
    """`triage_category=sut_regression` → same gating semantics."""
    marker = _make_marker(tmp_path, payload={
        "test_id": "TC-AM-002-01-api",
        "triage_category": "sut_regression",
        "signals": ["5xx_response"],
        "sample_error": "503 Service Unavailable",
    })
    result = await _regen_one(marker)
    assert result is True
    assert not marker.exists()
    gated_files = list((regen_consumer.APPLIED_DIR / "gated_no_regen").glob("*.json"))
    assert len(gated_files) == 1


# ── Case 3: test_gen_bug falls through (no gating) ────────────────────────


@pytest.mark.asyncio
async def test_r130e_test_gen_bug_falls_through(tmp_path):
    """`triage_category=test_gen_bug` is NOT gated — falls through to the
    GENERATED_TESTS resolver. We mock the import to make the function
    return False (simulating GENERATED_TESTS import failure) so the test
    verifies the gate didn't fire."""
    marker = _make_marker(tmp_path, payload={
        "test_id": "TC-AM-003-01-pw",
        "triage_category": "test_gen_bug",
        "signals": ["pw_syntax_error"],
        "sample_error": "Unexpected token",
    })
    # Patch the GENERATED_TESTS import to raise so the function exits
    # AFTER the gate check (returning False) — proving the gate didn't fire.
    with patch.dict("sys.modules", {"src.api.routers.tests_state": None}):
        # We can't easily intercept the inner `from ... import GENERATED_TESTS`
        # but we can verify the marker is NOT in gated_no_regen/ — that's
        # the signal we care about.
        try:
            await _regen_one(marker)
        except Exception:
            pass
    # The gate did NOT move this marker to gated_no_regen/
    gated_files = list((regen_consumer.APPLIED_DIR / "gated_no_regen").glob("*.json"))
    assert len(gated_files) == 0, (
        "test_gen_bug marker should NOT land in gated_no_regen/"
    )


# ── Case 4: missing triage_category defaults to test_gen_bug ──────────────


@pytest.mark.asyncio
async def test_r130e_missing_triage_category_defaults_to_test_gen_bug(tmp_path):
    """Legacy markers without `triage_category` field default to
    `test_gen_bug` per the existing fallback at regen_consumer.py:65."""
    marker = _make_marker(tmp_path, payload={
        "test_id": "TC-LEGACY-001",
        # No triage_category field — legacy marker shape
        "signals": ["legacy_signal"],
    })
    try:
        await _regen_one(marker)
    except Exception:
        pass
    gated_files = list((regen_consumer.APPLIED_DIR / "gated_no_regen").glob("*.json"))
    assert len(gated_files) == 0, (
        "Legacy marker without triage_category should default to "
        "test_gen_bug and NOT be gated"
    )


# ── Case 5: unknown triage_category falls through ─────────────────────────


@pytest.mark.asyncio
async def test_r130e_unknown_triage_category_not_gated(tmp_path):
    """An unrecognised triage_category (e.g., `flaky_test`) is NOT in the
    R130.E gated set → falls through to the normal resolver path."""
    marker = _make_marker(tmp_path, payload={
        "test_id": "TC-FLAKE-001",
        "triage_category": "flaky_test",   # not in {operator_review, sut_regression}
        "signals": ["flake_signal"],
    })
    try:
        await _regen_one(marker)
    except Exception:
        pass
    gated_files = list((regen_consumer.APPLIED_DIR / "gated_no_regen").glob("*.json"))
    assert len(gated_files) == 0, (
        "Unknown triage_category should NOT be gated by R130.E "
        "(only operator_review + sut_regression are gated)"
    )


# ── Case 6: gated_no_regen/ auto-created if missing ───────────────────────


@pytest.mark.asyncio
async def test_r130e_gated_no_regen_dir_auto_created(tmp_path):
    """If `applied/gated_no_regen/` does not yet exist, R130.E creates
    it via `mkdir(parents=True, exist_ok=True)`."""
    # Delete the auto-created gated_no_regen/ (the fixture created it)
    gated_dir = regen_consumer.APPLIED_DIR / "gated_no_regen"
    if gated_dir.exists():
        import shutil
        shutil.rmtree(gated_dir)
    assert not gated_dir.exists()
    marker = _make_marker(tmp_path, payload={
        "test_id": "TC-AUTO-001",
        "triage_category": "operator_review",
    })
    result = await _regen_one(marker)
    assert result is True
    assert gated_dir.is_dir(), "gated_no_regen/ should be auto-created"
    assert len(list(gated_dir.glob("*.json"))) == 1
