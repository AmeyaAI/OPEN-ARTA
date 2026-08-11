-- Migration 001: Initial ARTA schema
-- Apply with: psql $DATABASE_URL -f src/db/migrations/001_initial_schema.sql
-- Or via Alembic: alembic upgrade head

-- This migration applies the full schema defined in src/db/schema.sql
-- Run schema.sql directly for fresh installs; use this for tracked migrations.

\i src/db/schema.sql
