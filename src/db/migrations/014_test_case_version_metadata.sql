-- Test-case versioning: snapshot the per-test traceability metadata too.
--
-- Problem: test_case_versions snapshots gherkin_snapshot + script_snapshot only.
-- A test's `metadata` (test_cases.metadata) holds the per-test traceability spine
-- (source_components / data_objects / workflows / authz / grounded_by). Without
-- snapshotting it, a revert restores the OLD gherkin+script but leaves the NEW
-- gen's lineage on it — an inconsistent, untraceable test. This column lets a
-- snapshot capture the metadata so a revert restores the test's OWN lineage.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS), applied at startup by session.py.
-- Backfill-safe: nullable; existing version rows keep NULL.

ALTER TABLE test_case_versions ADD COLUMN IF NOT EXISTS metadata_snapshot JSONB;
