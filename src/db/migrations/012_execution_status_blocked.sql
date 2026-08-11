-- R33.1 — Add BLOCKED to the `execution_status` enum.
--
-- Pre-R33.1 R29.3a emitted `status: "BLOCKED"` for items whose required
-- env vars were unresolvable, but the Postgres enum only had {PASS,
-- FAIL, SKIP, FLAKY, PENDING, RUNNING}. asyncpg rejected every BLOCKED
-- INSERT with `InvalidTextRepresentationError`, the per-row try/rollback
-- silently dropped 71% of the rows in run-80b983 (2,655 of 3,701).
-- The dashboard then showed "Newman / k6 / axe / pytest not executed"
-- because their rows weren't in execution_results — even though the
-- runners ran fine.
--
-- Post-R33.1 BLOCKED is a first-class enum value. Both R29.3a's pre-
-- dispatch filter AND R30.5's tool-level pre-flight check can persist
-- their BLOCKED rows; quality_gate's R29.3d / R33.6 exclude them from
-- the effective pass-rate denominator (operator-actionable config gap,
-- not test failure).
--
-- Idempotent — re-running this migration after BLOCKED already exists
-- is a no-op (the IF NOT EXISTS check).
--
-- Rollback: enum values cannot be dropped in Postgres; rollback would
-- require recreating the enum from scratch + casting columns. Don't
-- roll this back.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum
         WHERE enumtypid = 'execution_status'::regtype
           AND enumlabel = 'BLOCKED'
    ) THEN
        -- Position after SKIP so dashboards/queries that ORDER BY enum
        -- groups status-classes naturally (PASS, FAIL, SKIP, BLOCKED, …).
        ALTER TYPE execution_status ADD VALUE 'BLOCKED' AFTER 'SKIP';
    END IF;
END$$;

COMMIT;
