-- Test-case versioning: capture the full row scaffold so a revert can RESURRECT
-- a test the regen deleted outright (not just restore content of one that still
-- exists).
--
-- Problem: restore-previous-generation restored the gherkin/script/metadata of
-- tests that still exist, but a force-regen that produces a DIFFERENT/smaller set
-- (e.g. drops the zap/axe variants) deletes rows entirely. Those can't be
-- re-INSERTed from gherkin+script alone — test_cases has NOT-NULL title /
-- test_type / automation_tool / priority. This column snapshots those scaffold
-- fields (title, tool, priority, type, requirement_id, ac_id, project_id,
-- script_path, tags) so "undo my force-generate" resurrects the whole prior gen.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS), applied at startup by session.py.
-- Backfill-safe: nullable; old version rows have NULL (content-only revert still
-- works for tests that still exist).

ALTER TABLE test_case_versions ADD COLUMN IF NOT EXISTS row_snapshot JSONB;
