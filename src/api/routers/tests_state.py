"""F7-2 (M2 partial) — shared in-memory state for the tests router family.

Carved out of the 3,994-line `tests.py` so:
  1. Cross-router imports (healing, execution, projects, requirements,
     traceability, main) target a small, stable surface instead of the
     monolith — a partial regression in `tests.py` no longer puts the entire
     project at risk during a refactor.
  2. Future per-feature splits (versions, fixtures, review) can read/write
     the same state without circular imports back into `tests.py`.

The `tests.py` module re-exports these symbols for backward compatibility,
so existing `from .tests import GENERATED_TESTS` call sites keep working.
New code should prefer `from .tests_state import GENERATED_TESTS`.
"""
from __future__ import annotations


# ── In-memory test entries (project_id-aware) ────────────────────────────────
# Source-of-truth for tests is PostgreSQL when available; this list is the
# fallback when the DB is unavailable AND the working cache for hot lookups.
GENERATED_TESTS: list[dict] = []


# ── Generate-All bulk-job state ──────────────────────────────────────────────
# Keyed by job_id (8-char UUID prefix). Persisted to .arta/generate_all_jobs.json
# on every status update so survives container restart.
_GENERATE_ALL_JOBS: dict[str, dict] = {}


# ── Pending human-review queue (Feature 4) ───────────────────────────────────
# Workflow_id → review payload. Cleared when reviewer approves / rejects.
_PENDING_REVIEWS: dict[str, dict] = {}


# ── Mock version history (used when DB is unavailable) ───────────────────────
# Keyed by test_id → list of {version, date, reason, by, gherkin}.
MOCK_VERSIONS: dict[str, list[dict]] = {}


__all__ = [
    "GENERATED_TESTS",
    "_GENERATE_ALL_JOBS",
    "_PENDING_REVIEWS",
    "MOCK_VERSIONS",
]
