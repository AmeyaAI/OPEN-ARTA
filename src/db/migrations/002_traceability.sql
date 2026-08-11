-- Migration 002: Add traceability + analytics fields to test_cases.
-- Phase Gap-1.1: Without these columns, the in-memory fields (trace_id,
-- model_version, prompt_version, red_phase_status, judge_score, dataset_version,
-- analytics_layer, tier, error_message) are lost on every container restart.
--
-- Idempotent: every ALTER uses `ADD COLUMN IF NOT EXISTS`.

ALTER TABLE test_cases
    ADD COLUMN IF NOT EXISTS trace_id          UUID,
    ADD COLUMN IF NOT EXISTS model_version     TEXT,
    ADD COLUMN IF NOT EXISTS prompt_version    TEXT,
    ADD COLUMN IF NOT EXISTS red_phase_status  TEXT DEFAULT 'PENDING_VERIFICATION',
    ADD COLUMN IF NOT EXISTS judge_score       NUMERIC(4,3),
    ADD COLUMN IF NOT EXISTS dataset_version   TEXT,
    ADD COLUMN IF NOT EXISTS analytics_layer   TEXT,
    ADD COLUMN IF NOT EXISTS tier              SMALLINT,
    ADD COLUMN IF NOT EXISTS fixture           JSONB,
    ADD COLUMN IF NOT EXISTS eval_rubric       JSONB,
    ADD COLUMN IF NOT EXISTS adversarial_input TEXT,
    ADD COLUMN IF NOT EXISTS error_message     TEXT,
    ADD COLUMN IF NOT EXISTS generation_source TEXT;

COMMENT ON COLUMN test_cases.trace_id        IS 'UUID linking all tests created in a single generation request';
COMMENT ON COLUMN test_cases.model_version   IS 'LLM model used (e.g. arta-qwen-pro:latest)';
COMMENT ON COLUMN test_cases.prompt_version  IS 'SHA-256 prefix of prompt templates at generation time';
COMMENT ON COLUMN test_cases.red_phase_status IS 'ATDD red-phase: RED | GREEN_UNEXPECTED | ERROR | PENDING_VERIFICATION';
COMMENT ON COLUMN test_cases.judge_score     IS 'LLM-as-judge score 0.0-1.0 (analytics narrative tests only)';
COMMENT ON COLUMN test_cases.dataset_version IS 'SHA-256 of frozen fixture (analytics tests only)';
COMMENT ON COLUMN test_cases.analytics_layer IS 'nl_to_query | query_to_result | result_to_insight | insight_to_narrative | e2e';
COMMENT ON COLUMN test_cases.tier            IS '1=commit, 2=PR, 3=nightly';

-- Useful indexes for debugging workflows
CREATE INDEX IF NOT EXISTS idx_tc_trace_id         ON test_cases(trace_id);
CREATE INDEX IF NOT EXISTS idx_tc_red_phase        ON test_cases(red_phase_status);
CREATE INDEX IF NOT EXISTS idx_tc_analytics_layer  ON test_cases(analytics_layer) WHERE analytics_layer IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tc_tier             ON test_cases(tier) WHERE tier IS NOT NULL;


-- ── healing_proposals: table + HealingProposalRepo already exist in main schema.
-- Gap-1.3: Add missing columns the repository uses but the schema lacked
-- (failure_signature for I10 feedback corpus; approved_by for audit).
-- Also ensure the approve/reject path writes via the existing repo.

ALTER TABLE healing_proposals
    ADD COLUMN IF NOT EXISTS failure_signature TEXT,
    ADD COLUMN IF NOT EXISTS approved_by       TEXT;
