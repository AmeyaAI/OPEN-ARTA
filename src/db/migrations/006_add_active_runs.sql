-- Fix PPP — DB-backed run registry checkpoint table.
--
-- The in-memory _REAL_RUNS dict in src/api/routers/execution.py remains the
-- primary; this table is a periodic snapshot so a container restart can
-- rehydrate runs in flight (heartbeat < 5 min old). State drift is bounded
-- by the checkpoint cadence (30s + on stage transitions + on terminal).
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, indexes likewise.
CREATE TABLE IF NOT EXISTS active_runs (
    run_id          TEXT PRIMARY KEY,
    project_id      UUID,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    heartbeat_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stage           TEXT,
    state           JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_active_runs_heartbeat ON active_runs(heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_active_runs_project ON active_runs(project_id);
