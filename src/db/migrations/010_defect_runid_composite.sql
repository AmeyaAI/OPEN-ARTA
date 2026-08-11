-- R28.3 — replace defects.defect_id-only unique constraint with composite (defect_id, run_id).
--
-- Pre-fix: `defect_id TEXT UNIQUE NOT NULL` rejected the same DEF-HTTP_500
-- cluster on its second appearance. R-DefectIdempotent's
-- ON CONFLICT DO NOTHING masked this — the SECOND run silently lost
-- defect rows because they collided on defect_id with the FIRST run's row.
-- Result: operator could only see the FIRST run's defects, not subsequent
-- runs (the user's "navigate back to prior runs" flow was broken).
--
-- Post-fix: defect_id is no longer globally unique; the (defect_id, run_id)
-- pair IS. Each run can independently produce DEF-HTTP_500 / DEF-AUTH_401
-- clusters and they coexist as separate rows scoped to their run.
--
-- Data preservation: existing rows with run_id IS NULL are unaffected
-- (Postgres treats NULL-bearing tuples as distinct in unique constraints,
-- so legacy rows with NULL run_id can still collide on defect_id alone —
-- but that matches pre-R28.3 behavior, no regression).
--
-- Rollback: re-add `UNIQUE (defect_id)` and drop the composite. Operator
-- is responsible for de-duping rows with shared defect_id BEFORE rollback.

BEGIN;

-- Drop the legacy auto-generated unique constraint on defect_id.
-- The constraint name follows Postgres's default naming for
-- `column UNIQUE` on table create.
ALTER TABLE defects
    DROP CONSTRAINT IF EXISTS defects_defect_id_key;

-- Add composite unique on (defect_id, run_id). NULL run_id values
-- still allow legacy rows; new R20a/R-DefectFallback rows always
-- carry run_id, so the composite enforces per-run isolation.
ALTER TABLE defects
    ADD CONSTRAINT uq_def_did_runid UNIQUE (defect_id, run_id);

-- Helpful index for the new GET /api/defects?run_id= query path
-- (R28.4). idx_def_run already exists on run_id alone — leave it.
-- This composite index speeds the common operator query
-- "list defects for this run, by priority".
CREATE INDEX IF NOT EXISTS idx_def_run_priority
    ON defects (run_id, priority)
    WHERE run_id IS NOT NULL;

COMMIT;
