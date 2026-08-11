-- Migration 005: Add 'preflight' to the automation_tool enum.
-- Pre-flight HTTP probe (executes/abort path in
-- src/api/routers/execution.py::_real_execution_inner) emits a result
-- row with automation_tool='preflight' so persist, dashboard, and
-- gate logic can recognise it as a non-test environment check —
-- distinct from playwright/newman/k6/zap/axe/pytest. Adding it to the
-- enum (rather than misusing 'newman') prevents pollution of API
-- pass-rate metrics, the SUT 5xx endpoint aggregator
-- (execution.py:2641), and Layer 7 TC-to-tool traceability edges.
-- Verified live in run-2cc854: persist dropped the row with
--   invalid input value for enum automation_tool: "preflight"
-- → 1/1 result rows skipped → DB has 0 rows even though summary.html
-- shows the abort row in memory.
--
-- Idempotent: IF NOT EXISTS (Postgres 12+) so re-runs are safe.

ALTER TYPE automation_tool ADD VALUE IF NOT EXISTS 'preflight';
