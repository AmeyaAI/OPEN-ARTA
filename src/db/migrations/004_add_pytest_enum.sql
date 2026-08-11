-- Migration 004: Add 'pytest' to the automation_tool enum.
-- F20-18: ARTA's pytest-analytics runner ([_run_pytest_analytics](src/api/routers/execution.py#L1256))
-- stamps result rows with `automation_tool='pytest'`. The PG enum was
-- {playwright, selenium, cypress, appium, newman, k6, zap, manual, axe}
-- (axe added by migration 003 for F20-13). The pytest gap surfaced live in
-- run-c5ed67: every analytics test failed to persist with
--   invalid input value for enum automation_tool: "pytest"
-- and because the persist function ran every row in a single transaction,
-- the FIRST bad row aborted the txn and EVERY subsequent statement raised
-- InFailedSQLTransactionError. Net effect: status stayed 'running', 0 rows
-- in execution_results — even though the in-memory _REAL_RESULTS held
-- 712 failures. F20-20 separately splits the persist function into per-
-- bulk-write transactions so future enum mismatches don't lose all rows.
--
-- Idempotent: `IF NOT EXISTS` (Postgres 12+) so re-runs are safe.

ALTER TYPE automation_tool ADD VALUE IF NOT EXISTS 'pytest';
