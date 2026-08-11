-- R30.8 — promote triage signal from metadata JSONB to dedicated columns.
--
-- Pre-R30.8 (= R30.7-B): triage_category/confidence/signals lived inside
-- the defects.metadata JSONB blob. The triage queue (/api/triage) had
-- to scan EVERY defect row, parse JSONB, then post-filter — O(N) per
-- list call. As defect count grows beyond a few thousand this becomes
-- the dominant cost in the triage page render.
--
-- Post-R30.8: dedicated columns let the triage queue and Defect
-- Intelligence page run an indexed query. Both columns AND the JSONB
-- stay populated for backward compatibility — older callers reading
-- from metadata still work.
--
-- Data preservation: backfills the new columns from the existing JSONB
-- so prior runs' triage classifications stay readable. Non-destructive:
-- only adds columns + populates them; no existing data is altered or
-- dropped.
--
-- Rollback: DROP the columns + index. The metadata JSONB still carries
-- the same data (R30.7-B writes both paths), so nothing is lost.

BEGIN;

-- Add the three triage columns. Nullable because legacy rows pre-R30.7-B
-- never had triage data; they stay NULL. New rows post-R30.8 populate
-- both the columns AND the metadata JSONB.
ALTER TABLE defects
    ADD COLUMN IF NOT EXISTS triage_category TEXT NULL;

ALTER TABLE defects
    ADD COLUMN IF NOT EXISTS triage_confidence DOUBLE PRECISION NULL;

ALTER TABLE defects
    ADD COLUMN IF NOT EXISTS triage_signals TEXT[] DEFAULT ARRAY[]::TEXT[];

-- Backfill from metadata JSONB. R30.7-B started writing these fields
-- so any rows post-R30.7-B already have data inside metadata. This
-- one-shot UPDATE moves them into the columns. Idempotent — re-running
-- does nothing because the WHERE clause filters non-null targets.
UPDATE defects
   SET triage_category   = metadata->>'triage_category',
       triage_confidence = (metadata->>'triage_confidence')::float,
       triage_signals    = ARRAY(SELECT jsonb_array_elements_text(
                             COALESCE(metadata->'triage_signals', '[]'::jsonb)))
 WHERE triage_category IS NULL
   AND metadata ? 'triage_category';

-- Index for /api/triage queries that filter by category.
-- triage_category has low cardinality (4 values: test_gen_bug,
-- sut_regression, sut_contract_change, operator_review) but the
-- triage queue specifically wants `WHERE triage_category =
-- 'operator_review'` — partial index would also work but a plain
-- btree is simplest.
CREATE INDEX IF NOT EXISTS idx_def_triage_cat
    ON defects (triage_category)
    WHERE triage_category IS NOT NULL;

-- Composite index for the common gate query: "all unresolved
-- operator_review defects for this project". Covers the typical
-- triage page filter shape.
CREATE INDEX IF NOT EXISTS idx_def_triage_proj_status
    ON defects (project_id, triage_category, status)
    WHERE triage_category IS NOT NULL;

COMMIT;
