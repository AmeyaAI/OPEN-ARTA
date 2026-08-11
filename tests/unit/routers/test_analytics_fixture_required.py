"""F20-33: regression tests that confirm fixture materialisation is REQUIRED.

The original block silently swallowed both:
  - missing `fixture_info` (LLM forgot to emit metadata)
  - `materialise_fixture` raising any exception

…and then wrote the test code anyway. Every Run Suite produced files referencing
non-existent `fixtures/analytics/*.parquet` paths → uniform setup-time
FileNotFoundError at runtime.

These tests are source-grep style: the analytics-fixture block is deeply nested
inside the 3000-line `generate_tests` function and not unit-testable in
isolation without scaffolding the entire generation pipeline. Source-grep tests
protect the F20-33 control-flow shape from being reverted.
"""
from __future__ import annotations

import inspect

from src.api.routers import tests as tests_mod


# Pull the source of the module-level function once.
_SOURCE = inspect.getsource(tests_mod)


class TestFixtureWiringInvariants:

    async def test_log_warning_replaced_with_log_error_for_missing_fixture(self):
        """The pre-F20-33 code had `log.warning(...) ... could not materialise`.
        Post-F20-33 the same condition logs at ERROR level AND `continue`s the
        outer for-suite loop. Verify the NEW message is present and the OLD
        warning-only path is gone.
        """
        # New: error-level log when fixture metadata is missing
        assert "skipping suite" in _SOURCE, (
            "F20-33: must skip suite (continue outer loop) when fixture missing — "
            "the 'skipping suite' literal in the new error message is missing."
        )
        # New: error-level log on materialise failure (was log.warning)
        assert "Fixture materialisation failed" in _SOURCE, (
            "F20-33: error-level log on materialise failure missing."
        )

    async def test_old_silent_swallow_pattern_removed(self):
        """The old `log.warning("[%s] Could not materialise fixture: %s: %s", ...)`
        + fall-through to write-test-anyway is the bug we fixed. Make sure the
        exact warn-and-continue pattern is gone.
        """
        bad = 'log.warning(\n                            "[%s] Could not materialise fixture'
        assert bad not in _SOURCE, (
            "F20-33: stale warn-and-continue swallow pattern detected — "
            "fixture failures must skip the suite, not log+continue silently."
        )

    async def test_fixture_existence_check_after_materialise(self):
        """Defensive check: after materialise_fixture returns, verify the file
        actually exists on disk. Guards against the writer returning a Path
        without persisting (silent exception in pandas / race with cleanup)."""
        assert "materialised.exists()" in _SOURCE, (
            "F20-33: must verify materialised path exists on disk after "
            "materialise_fixture returns."
        )

    async def test_fixture_required_branch_continues(self):
        """When `not fixture_info`, the new code must `continue` the outer
        loop (skip writing any test files for the suite)."""
        # Find the F20-33 marker block and check it ends with `continue`
        marker = "F20-33"
        assert marker in _SOURCE, "F20-33 marker comment must be present"
        # Verify all three early-exit conditions use `continue`, not `pass`
        # or fallthrough. Count "continue" occurrences in the F20-33 region.
        f20_idx = _SOURCE.find("F20-33")
        # R92: window 2500 → 3700 chars. The third `continue` (missing-on-disk
        # check at tests.py:3423) sits at character offset ~3543 from the
        # F20-33 marker — outside the original 2500-char window. Production
        # code is correct (all 3 continue statements exist at lines 3381,
        # 3413, 3423); the test window was stale. 3700 gives ~150 chars of
        # margin against future inline edits within the same block.
        block = _SOURCE[f20_idx:f20_idx + 3700]
        continue_count = block.count("continue")
        assert continue_count >= 3, (
            f"F20-33: expected ≥3 `continue` statements in the fixture block "
            f"(missing fixture_info, materialise failure, missing on disk) — "
            f"found {continue_count}"
        )
