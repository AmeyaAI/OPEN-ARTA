-- Migration 003: Add 'axe' to the automation_tool enum.
-- F20-13: F20-12 started routing accessibility tests as tool='axe' in the
-- Python layer, but the PostgreSQL enum `automation_tool` was still the
-- original set {playwright, selenium, cypress, appium, newman, k6, zap,
-- manual}. Persist failed for every requirement whose risk profile
-- included Accessibility:
--   invalid input value for enum automation_tool: "axe"
-- The batch INSERT for the whole req then rolled back, fell through to
-- F8-7's 3-retry-then-in-memory fallback, and the req showed up as
-- Partial in the Generation Results modal (playwright marked failed
-- because its tests never landed in DB either). Verified live in a
-- generation job where all 12 tests (playwright:5 + newman:3 +
-- k6:3 + axe:1... etc.) failed to persist because of the enum rejection.
--
-- Idempotent: `IF NOT EXISTS` (Postgres 12+) so re-running this migration
-- on a container that already has 'axe' doesn't error.

ALTER TYPE automation_tool ADD VALUE IF NOT EXISTS 'axe';
