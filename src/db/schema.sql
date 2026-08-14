-- ARTA Platform — PostgreSQL Schema
-- BMAD TEA Methodology: Requirements → ACs → Tests → Runs → Results → Defects
-- PostgreSQL 16+

-- ── Extensions ────────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Enums ─────────────────────────────────────────────────────────────────────

CREATE TYPE risk_priority     AS ENUM ('P0', 'P1', 'P2', 'P3');
CREATE TYPE req_status        AS ENUM ('draft', 'approved', 'in_progress', 'done', 'deprecated');
CREATE TYPE test_type         AS ENUM ('unit', 'integration', 'ui', 'api', 'performance', 'security', 'accessibility');
-- F20-15: `axe` added for Accessibility tests. Migration 003_add_axe_enum.sql
-- does `ALTER TYPE ... ADD VALUE IF NOT EXISTS 'axe'` for existing deployments;
-- new deployments need it here so the enum is correct from the first boot.
-- F20-18: `pytest` added for analytics-layer + adversarial test family.
-- Migration 004_add_pytest_enum.sql backfills existing deployments.
CREATE TYPE automation_tool   AS ENUM ('playwright', 'selenium', 'cypress', 'appium', 'newman', 'k6', 'zap', 'manual', 'axe', 'pytest');
CREATE TYPE execution_status  AS ENUM ('PASS', 'FAIL', 'SKIP', 'FLAKY', 'PENDING', 'RUNNING');
CREATE TYPE run_status        AS ENUM ('queued', 'running', 'completed', 'failed', 'cancelled');
CREATE TYPE gate_decision     AS ENUM ('PASS', 'CONCERNS', 'FAIL', 'WAIVED');
CREATE TYPE coverage_level    AS ENUM ('FULL', 'PARTIAL', 'NONE');
CREATE TYPE defect_severity   AS ENUM ('critical', 'high', 'medium', 'low');
CREATE TYPE defect_status     AS ENUM ('open', 'in_progress', 'resolved', 'wont_fix', 'duplicate');
CREATE TYPE source_type       AS ENUM ('jira', 'github_issue', 'confluence', 'openapi', 'plain_text', 'db_schema');

-- ── Table: requirements ────────────────────────────────────────────────────────

CREATE TABLE requirements (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    req_id          TEXT NOT NULL UNIQUE,              -- e.g. REQ-017
    title           TEXT NOT NULL,
    description     TEXT,
    source_type     source_type NOT NULL DEFAULT 'plain_text',
    source_url      TEXT,
    priority        risk_priority NOT NULL DEFAULT 'P2',
    risk_score      INTEGER CHECK (risk_score BETWEEN 1 AND 9),  -- BMAD TEA: Impact(1-3) × Probability(1-3)
    impact          INTEGER CHECK (impact BETWEEN 1 AND 3),      -- 1=Minor, 2=Degraded, 3=Critical
    probability     INTEGER CHECK (probability BETWEEN 1 AND 3), -- 1=Unlikely, 2=Possible, 3=Likely
    status          req_status NOT NULL DEFAULT 'draft',
    sprint_id       TEXT,                              -- e.g. SPRINT-42
    epic_id         TEXT,                              -- e.g. EPIC-005
    owner           TEXT,
    tags            TEXT[] DEFAULT '{}',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE requirements IS 'TEA Layer 1: Ingested and structured requirements';
COMMENT ON COLUMN requirements.risk_score IS 'BMAD TEA: Impact(1-3) × Probability(1-3) = 1-9. Score 9 = auto gate FAIL';

-- ── Table: acceptance_criteria ────────────────────────────────────────────────

CREATE TABLE acceptance_criteria (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ac_id           TEXT NOT NULL UNIQUE,              -- e.g. AC-017-001
    requirement_id  UUID NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    given           TEXT,                              -- BDD Given clause
    when_           TEXT,                              -- BDD When clause (when is reserved)
    then_           TEXT,                              -- BDD Then clause
    priority        risk_priority NOT NULL DEFAULT 'P2',
    is_covered      BOOLEAN NOT NULL DEFAULT FALSE,    -- Updated by TraceabilityAgent
    coverage_level  coverage_level NOT NULL DEFAULT 'NONE', -- BMAD TEA: FULL/PARTIAL/NONE
    coverage_pct    NUMERIC(5,2) DEFAULT 0,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE acceptance_criteria IS 'TEA Layer 3: Given/When/Then acceptance criteria per requirement';
CREATE INDEX idx_ac_requirement ON acceptance_criteria(requirement_id);
CREATE INDEX idx_ac_priority    ON acceptance_criteria(priority);
CREATE INDEX idx_ac_covered     ON acceptance_criteria(is_covered);

-- ── Table: test_cases ─────────────────────────────────────────────────────────

CREATE TABLE test_cases (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    test_id             TEXT NOT NULL UNIQUE,          -- e.g. TC-124
    title               TEXT NOT NULL,
    requirement_id      UUID REFERENCES requirements(id) ON DELETE SET NULL,
    ac_id               UUID REFERENCES acceptance_criteria(id) ON DELETE SET NULL,
    test_type           test_type NOT NULL DEFAULT 'integration',
    automation_tool     automation_tool NOT NULL DEFAULT 'playwright',
    priority            risk_priority NOT NULL DEFAULT 'P2',
    gherkin_scenario    TEXT,                          -- Full Gherkin Feature/Scenario block
    script_path         TEXT,                          -- Relative path to automation script
    script_content      TEXT,                          -- Cached script content (for UI viewer)
    is_automated        BOOLEAN NOT NULL DEFAULT FALSE,
    is_flaky            BOOLEAN NOT NULL DEFAULT FALSE,
    flaky_rate          NUMERIC(5,2) DEFAULT 0,        -- 0–100 percentage
    last_status         execution_status DEFAULT 'PENDING',
    last_run_at         TIMESTAMPTZ,
    avg_duration_ms     INTEGER,
    tags                TEXT[] DEFAULT '{}',
    metadata            JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE test_cases IS 'TEA Layers 3–4: ATDD-designed test cases with automation scripts';
CREATE INDEX idx_tc_requirement ON test_cases(requirement_id);
CREATE INDEX idx_tc_priority    ON test_cases(priority);
CREATE INDEX idx_tc_status      ON test_cases(last_status);
CREATE INDEX idx_tc_tool        ON test_cases(automation_tool);
CREATE INDEX idx_tc_flaky       ON test_cases(is_flaky) WHERE is_flaky = TRUE;

-- ── Table: test_runs ──────────────────────────────────────────────────────────

CREATE TABLE test_runs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id          TEXT NOT NULL UNIQUE,              -- e.g. run-487
    build_id        TEXT NOT NULL,                     -- CI build identifier
    environment     TEXT NOT NULL DEFAULT 'staging',
    branch          TEXT,
    commit_sha      TEXT,
    triggered_by    TEXT,                              -- user, ci, schedule
    status          run_status NOT NULL DEFAULT 'queued',
    gate_decision   gate_decision,
    gate_summary    TEXT,
    total_tests     INTEGER DEFAULT 0,
    passed          INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    skipped         INTEGER DEFAULT 0,
    flaky           INTEGER DEFAULT 0,
    pass_rate       NUMERIC(5,2),
    duration_ms     INTEGER,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE test_runs IS 'TEA Layer 5: One record per CI/CD triggered test execution';
CREATE INDEX idx_run_build     ON test_runs(build_id);
CREATE INDEX idx_run_status    ON test_runs(status);
CREATE INDEX idx_run_env       ON test_runs(environment);
CREATE INDEX idx_run_created   ON test_runs(created_at DESC);

-- ── Table: execution_results ──────────────────────────────────────────────────

CREATE TABLE execution_results (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id          UUID NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    test_case_id    UUID REFERENCES test_cases(id) ON DELETE SET NULL,
    test_id         TEXT NOT NULL,                     -- Denormalised for fast lookup
    title           TEXT NOT NULL,
    status          execution_status NOT NULL,
    automation_tool automation_tool,
    duration_ms     INTEGER,
    error_message   TEXT,
    stack_trace     TEXT,
    screenshot_url  TEXT,
    video_url       TEXT,
    retry_count     INTEGER DEFAULT 0,
    is_flaky        BOOLEAN DEFAULT FALSE,
    browser         TEXT,                              -- chromium, firefox, webkit
    metadata        JSONB DEFAULT '{}',
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE execution_results IS 'TEA Layer 5–6: Per-test pass/fail evidence';
CREATE INDEX idx_er_run_id    ON execution_results(run_id);
CREATE INDEX idx_er_test_id   ON execution_results(test_id);
CREATE INDEX idx_er_status    ON execution_results(status);
CREATE INDEX idx_er_browser   ON execution_results(browser);
CREATE INDEX idx_er_executed  ON execution_results(executed_at DESC);

-- ── Table: defects ────────────────────────────────────────────────────────────

CREATE TABLE defects (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    defect_id           TEXT NOT NULL UNIQUE,          -- e.g. DEF-089
    title               TEXT NOT NULL,
    description         TEXT,
    severity            defect_severity NOT NULL DEFAULT 'medium',
    priority            risk_priority NOT NULL DEFAULT 'P2',
    status              defect_status NOT NULL DEFAULT 'open',
    requirement_id      UUID REFERENCES requirements(id) ON DELETE SET NULL,
    test_case_id        UUID REFERENCES test_cases(id) ON DELETE SET NULL,
    run_id              UUID REFERENCES test_runs(id) ON DELETE SET NULL,
    root_cause          TEXT,
    root_cause_category TEXT,                          -- race_condition, timing, external_dep, etc.
    suggested_fix       TEXT,                          -- AI-generated fix suggestion
    impacted_files      TEXT[] DEFAULT '{}',
    jira_key            TEXT,                          -- External Jira ticket key
    jira_url            TEXT,
    assignee            TEXT,
    reporter            TEXT DEFAULT 'ARTA',
    is_flaky            BOOLEAN NOT NULL DEFAULT FALSE,
    is_known            BOOLEAN NOT NULL DEFAULT FALSE,
    tags                TEXT[] DEFAULT '{}',
    metadata            JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE defects IS 'TEA Layer 8: AI-analysed defects with root cause and fix suggestions';
CREATE INDEX idx_def_severity    ON defects(severity);
CREATE INDEX idx_def_priority    ON defects(priority);
CREATE INDEX idx_def_status      ON defects(status);
CREATE INDEX idx_def_requirement ON defects(requirement_id);
CREATE INDEX idx_def_run         ON defects(run_id);
CREATE INDEX idx_def_flaky       ON defects(is_flaky) WHERE is_flaky = TRUE;

-- ── Table: quality_gates ──────────────────────────────────────────────────────

CREATE TABLE quality_gates (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id                      UUID NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    build_id                    TEXT NOT NULL,
    environment                 TEXT NOT NULL DEFAULT 'staging',
    decision                    gate_decision NOT NULL,
    summary                     TEXT,
    blocking_checks             JSONB DEFAULT '[]',
    warnings                    JSONB DEFAULT '[]',
    passed_checks               JSONB DEFAULT '[]',
    p0_coverage_pct             NUMERIC(5,2),
    p1_coverage_pct             NUMERIC(5,2),
    overall_coverage_pct        NUMERIC(5,2),
    p0_pass_rate_pct            NUMERIC(5,2),
    overall_pass_rate_pct       NUMERIC(5,2),
    open_p0_defects             INTEGER,
    performance_p95_ms          INTEGER,
    security_critical_findings  INTEGER,
    evidence_package_id         TEXT,
    assessed_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE quality_gates IS 'TEA Layer 9: Evidence-based PASS/CONCERNS/FAIL/WAIVED gate decisions';

-- ── Table: gate_waivers ─────────────────────────────────────────────────────

CREATE TABLE gate_waivers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    gate_id         UUID NOT NULL REFERENCES quality_gates(id) ON DELETE CASCADE,
    waived_check    TEXT NOT NULL,                     -- Name of the check being waived
    rationale       TEXT NOT NULL,                     -- Why this waiver is justified
    approved_by     UUID,                                -- User who approved the waiver
    expires_at      TIMESTAMPTZ NOT NULL,              -- Waivers must have an expiry date
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE gate_waivers IS 'BMAD TEA: Authorized exceptions for gate checks — requires rationale + expiry';
CREATE INDEX idx_gate_waivers_gate_id ON gate_waivers(gate_id);
CREATE INDEX idx_gate_waivers_expires ON gate_waivers(expires_at);
CREATE INDEX idx_gate_run        ON quality_gates(run_id);
CREATE INDEX idx_gate_build      ON quality_gates(build_id);
CREATE INDEX idx_gate_decision   ON quality_gates(decision);
CREATE INDEX idx_gate_assessed   ON quality_gates(assessed_at DESC);

-- ── Updated-at trigger ────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_requirements_updated_at
    BEFORE UPDATE ON requirements
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_ac_updated_at
    BEFORE UPDATE ON acceptance_criteria
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_test_cases_updated_at
    BEFORE UPDATE ON test_cases
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_defects_updated_at
    BEFORE UPDATE ON defects
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── Views ─────────────────────────────────────────────────────────────────────

CREATE VIEW v_coverage_summary AS
SELECT
    r.req_id,
    r.title,
    r.priority,
    r.risk_score,
    COUNT(ac.id)                                                    AS total_acs,
    COUNT(ac.id) FILTER (WHERE ac.is_covered)                       AS covered_acs,
    ROUND(COUNT(ac.id) FILTER (WHERE ac.is_covered) * 100.0
          / NULLIF(COUNT(ac.id), 0), 1)                             AS coverage_pct,
    COUNT(tc.id)                                                    AS total_tests,
    COUNT(tc.id) FILTER (WHERE tc.last_status = 'PASS')             AS passing_tests,
    COUNT(tc.id) FILTER (WHERE tc.is_flaky)                         AS flaky_tests
FROM requirements r
LEFT JOIN acceptance_criteria ac ON ac.requirement_id = r.id
LEFT JOIN test_cases tc ON tc.requirement_id = r.id
GROUP BY r.id, r.req_id, r.title, r.priority, r.risk_score;

CREATE VIEW v_run_summary AS
SELECT
    tr.*,
    COUNT(er.id)                                            AS result_count,
    COUNT(er.id) FILTER (WHERE er.status = 'PASS')          AS pass_count,
    COUNT(er.id) FILTER (WHERE er.status = 'FAIL')          AS fail_count,
    COUNT(er.id) FILTER (WHERE er.is_flaky)                 AS flaky_count,
    AVG(er.duration_ms)::INTEGER                            AS avg_duration_ms
FROM test_runs tr
LEFT JOIN execution_results er ON er.run_id = tr.id
GROUP BY tr.id;

-- ── Seed Data ─────────────────────────────────────────────────────────────────

-- Requirements: REQ-017 to REQ-020

INSERT INTO requirements (req_id, title, description, source_type, priority, risk_score, impact, probability, status, sprint_id, owner, tags) VALUES
(
    'REQ-017',
    'Checkout Payment Processing',
    'Users must be able to complete checkout using Visa, Mastercard, or PayPal. Payment must process within 3 seconds (p95). The system must prevent double-charges via idempotency keys. PCI DSS Level 1 compliance required — no raw card data stored.',
    'plain_text', 'P0', 9, 3, 3, 'in_progress', 'SPRINT-42', 'alice@example.com',
    ARRAY['payment', 'pci', 'checkout', 'critical']
),
(
    'REQ-018',
    'User Authentication & Session Management',
    'Users can register, login, and maintain sessions. Support OAuth2 (Google, GitHub). Account lockout after 5 failed attempts. Passwords hashed with bcrypt (cost 12). JWT tokens expire after 24 hours. Refresh tokens valid for 30 days.',
    'plain_text', 'P0', 9, 3, 3, 'approved', 'SPRINT-42', 'bob@example.com',
    ARRAY['auth', 'security', 'session', 'critical']
),
(
    'REQ-019',
    'Refund & Order Cancellation',
    'Users can request full or partial refunds within 30 days. Refunds processed within 5 business days. Automatic refund for cancelled orders. Audit trail required for all refund actions.',
    'plain_text', 'P1', 6, 3, 2, 'draft', 'SPRINT-43', 'carol@example.com',
    ARRAY['refund', 'orders', 'financial']
),
(
    'REQ-020',
    'Product Search & Filtering',
    'Full-text search across product catalogue (< 200ms p95). Filters: price range, category, rating, availability. Search results ranked by relevance score. Support autocomplete suggestions (< 50ms). ElasticSearch 8.x backend.',
    'plain_text', 'P1', 6, 2, 3, 'approved', 'SPRINT-42', 'dave@example.com',
    ARRAY['search', 'elasticsearch', 'performance']
);

-- Acceptance Criteria for REQ-017

INSERT INTO acceptance_criteria (ac_id, requirement_id, title, given, when_, then_, priority, is_covered) VALUES
(
    'AC-017-001',
    (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
    'Successful Visa payment completes within 3s',
    'a registered user with a valid Visa card in their cart with items totalling ≥ £1',
    'the user submits the checkout form with correct card details',
    'the order status is "confirmed", an order_id is returned, and response time is < 3000ms',
    'P0', TRUE
),
(
    'AC-017-002',
    (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
    'Expired card returns 422 with card_expired error code',
    'a user has a card with expiry date in the past',
    'the user attempts checkout with that card',
    'the API returns HTTP 422 with error.code = "card_expired" and no order_id is created',
    'P0', TRUE
),
(
    'AC-017-003',
    (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
    'Duplicate request with same idempotency key does not double-charge',
    'a successful payment has been made with idempotency key K',
    'the same request is replayed with key K',
    'the API returns HTTP 200 with the same order_id and transaction_count = 1',
    'P0', TRUE
),
(
    'AC-017-004',
    (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
    'Unauthenticated request returns 401',
    'no Authorization header is present',
    'a checkout request is made',
    'the API returns HTTP 401 with a WWW-Authenticate header',
    'P0', TRUE
),
(
    'AC-017-005',
    (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
    'SQL injection in card fields is rejected safely',
    'an attacker sends SQL injection strings in card.number',
    'the request is processed',
    'the API returns 400 or 422, no SQL error leaks in the response, and the database is unaffected',
    'P0', TRUE
),
(
    'AC-017-006',
    (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
    'PCI compliance: no raw card data in order response',
    'a payment is processed successfully',
    'the order details are retrieved via GET /api/orders/:id',
    'the response contains no card number, CVV, or any 13–16 digit PAN',
    'P0', TRUE
);

-- Acceptance Criteria for REQ-018

INSERT INTO acceptance_criteria (ac_id, requirement_id, title, given, when_, then_, priority, is_covered) VALUES
(
    'AC-018-001',
    (SELECT id FROM requirements WHERE req_id = 'REQ-018'),
    'Successful login with valid credentials',
    'a registered user with email and valid password',
    'the user submits the login form',
    'a JWT access token and refresh token are returned with HTTP 200',
    'P0', TRUE
),
(
    'AC-018-002',
    (SELECT id FROM requirements WHERE req_id = 'REQ-018'),
    'Account locked after 5 failed login attempts',
    'a user has failed login 4 times in the last 15 minutes',
    'they attempt login again with any password',
    'HTTP 423 is returned and no further attempts are processed for 30 minutes',
    'P0', TRUE
),
(
    'AC-018-003',
    (SELECT id FROM requirements WHERE req_id = 'REQ-018'),
    'Login error does not reveal whether email exists',
    'an attacker provides an email that is not registered',
    'they submit the login form',
    'the error message is identical to the wrong-password message (user enumeration prevention)',
    'P0', FALSE
);

-- Acceptance Criteria for REQ-019

INSERT INTO acceptance_criteria (ac_id, requirement_id, title, given, when_, then_, priority, is_covered) VALUES
(
    'AC-019-001',
    (SELECT id FROM requirements WHERE req_id = 'REQ-019'),
    'Full refund processed within 5 business days',
    'an order was placed within the last 30 days and is in "delivered" status',
    'the user requests a full refund',
    'the refund is initiated within 24 hours and completed within 5 business days',
    'P1', FALSE
),
(
    'AC-019-002',
    (SELECT id FROM requirements WHERE req_id = 'REQ-019'),
    'Refund request older than 30 days is rejected',
    'an order was placed more than 30 days ago',
    'the user requests a refund',
    'HTTP 422 is returned with error.code = "refund_window_expired"',
    'P1', FALSE
);

-- Acceptance Criteria for REQ-020

INSERT INTO acceptance_criteria (ac_id, requirement_id, title, given, when_, then_, priority, is_covered) VALUES
(
    'AC-020-001',
    (SELECT id FROM requirements WHERE req_id = 'REQ-020'),
    'Full-text search returns results within 200ms',
    'the product catalogue contains ≥ 10,000 products',
    'a user searches for a term present in at least 5 products',
    'results are returned in < 200ms (p95) with relevance ranking',
    'P1', TRUE
),
(
    'AC-020-002',
    (SELECT id FROM requirements WHERE req_id = 'REQ-020'),
    'Empty search term returns 400 validation error',
    'the search endpoint is called',
    'the query parameter is empty or whitespace',
    'HTTP 400 is returned with a validation error message',
    'P1', FALSE
);

-- Test cases (sample: TC-124 to TC-131 mapped to REQ-017)

INSERT INTO test_cases (test_id, title, requirement_id, ac_id, test_type, automation_tool, priority, is_automated, last_status) VALUES
('TC-124', 'Checkout with valid Visa card — happy path',
 (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
 (SELECT id FROM acceptance_criteria WHERE ac_id = 'AC-017-001'),
 'ui', 'playwright', 'P0', TRUE, 'PASS'),
('TC-125', 'Checkout SLA — response time < 3000ms under normal load',
 (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
 (SELECT id FROM acceptance_criteria WHERE ac_id = 'AC-017-001'),
 'performance', 'k6', 'P0', TRUE, 'PASS'),
('TC-126', 'Payment timeout handling — gateway 30s timeout',
 (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
 (SELECT id FROM acceptance_criteria WHERE ac_id = 'AC-017-001'),
 'api', 'newman', 'P0', TRUE, 'FAIL'),
('TC-127', 'Expired card returns 422 card_expired',
 (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
 (SELECT id FROM acceptance_criteria WHERE ac_id = 'AC-017-002'),
 'api', 'newman', 'P0', TRUE, 'PASS'),
('TC-128', 'Idempotency — duplicate request not double-charged',
 (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
 (SELECT id FROM acceptance_criteria WHERE ac_id = 'AC-017-003'),
 'api', 'newman', 'P0', TRUE, 'PASS'),
('TC-129', 'Unauthorized checkout returns 401',
 (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
 (SELECT id FROM acceptance_criteria WHERE ac_id = 'AC-017-004'),
 'api', 'newman', 'P0', TRUE, 'PASS'),
('TC-130', 'SQL injection in card fields safely rejected',
 (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
 (SELECT id FROM acceptance_criteria WHERE ac_id = 'AC-017-005'),
 'security', 'newman', 'P0', TRUE, 'PASS'),
('TC-131', 'PCI: card number absent from order response',
 (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
 (SELECT id FROM acceptance_criteria WHERE ac_id = 'AC-017-006'),
 'api', 'newman', 'P0', TRUE, 'PASS');

-- Test run: build 487 (BLOCK) — matches mock gate data

INSERT INTO test_runs (run_id, build_id, environment, branch, commit_sha, triggered_by,
                       status, gate_decision, gate_summary,
                       total_tests, passed, failed, skipped,
                       pass_rate, duration_ms, started_at, completed_at)
VALUES (
    'run-487', '487', 'staging', 'feature/checkout-v2', 'abc1234def5678',
    'ci',
    'completed', 'FAIL',
    'RELEASE FAILED — 1 critical check failed: p0_pass_rate (90.9% vs =100%)',
    22, 20, 2, 0,
    90.91, 184000,
    '2026-03-11T14:20:00Z', '2026-03-11T14:34:30Z'
);

-- Test run: build 486 (GO)

INSERT INTO test_runs (run_id, build_id, environment, branch, commit_sha, triggered_by,
                       status, gate_decision, gate_summary,
                       total_tests, passed, failed, skipped,
                       pass_rate, duration_ms, started_at, completed_at)
VALUES (
    'run-486', '486', 'staging', 'main', 'f9e8d7c6b5a4',
    'ci',
    'completed', 'PASS',
    'RELEASE APPROVED — 11/11 checks passed',
    22, 22, 0, 0,
    100.00, 176000,
    '2026-03-11T10:05:00Z', '2026-03-11T10:19:45Z'
);

-- Execution results for run-487

INSERT INTO execution_results (run_id, test_id, title, status, automation_tool, duration_ms)
SELECT
    (SELECT id FROM test_runs WHERE run_id = 'run-487'),
    tc.test_id, tc.title,
    CASE tc.test_id
        WHEN 'TC-126' THEN 'FAIL'::execution_status
        ELSE 'PASS'::execution_status
    END,
    tc.automation_tool,
    CASE tc.test_id
        WHEN 'TC-124' THEN 2840
        WHEN 'TC-125' THEN 2210
        WHEN 'TC-126' THEN 30001
        WHEN 'TC-127' THEN 412
        WHEN 'TC-128' THEN 887
        WHEN 'TC-129' THEN 203
        WHEN 'TC-130' THEN 318
        WHEN 'TC-131' THEN 295
        ELSE 500
    END
FROM test_cases tc
WHERE tc.test_id IN ('TC-124','TC-125','TC-126','TC-127','TC-128','TC-129','TC-130','TC-131');

-- Defects

INSERT INTO defects (defect_id, title, description, severity, priority, status,
                     requirement_id, test_case_id, run_id,
                     root_cause, root_cause_category, suggested_fix, impacted_files, reporter)
VALUES (
    'DEF-089',
    'Payment timeout causes race condition in order state machine',
    'When the payment gateway returns a timeout after 30s, the order is left in "processing" state. A retry then creates a second charge. TC-126 consistently fails under load.',
    'critical', 'P0', 'open',
    (SELECT id FROM requirements WHERE req_id = 'REQ-017'),
    (SELECT id FROM test_cases WHERE test_id = 'TC-126'),
    (SELECT id FROM test_runs WHERE run_id = 'run-487'),
    'Missing idempotency guard in the timeout-retry code path. The order state machine transitions to "failed" locally but the payment gateway has already captured the amount.',
    'race_condition',
    'Add distributed lock (Redis SETNX) around payment capture. Check gateway status before retry. Implement compensating transaction to void the original charge on timeout.',
    ARRAY['src/services/payment.ts', 'src/services/order-state-machine.ts', 'src/workers/payment-retry.ts'],
    'ARTA'
);

-- Quality gate records

INSERT INTO quality_gates (run_id, build_id, environment, decision, summary,
                            blocking_checks, warnings, passed_checks,
                            p0_coverage_pct, overall_coverage_pct,
                            p0_pass_rate_pct, overall_pass_rate_pct,
                            open_p0_defects, performance_p95_ms, security_critical_findings,
                            evidence_package_id)
VALUES (
    (SELECT id FROM test_runs WHERE run_id = 'run-487'),
    '487', 'staging', 'FAIL',
    'RELEASE FAILED — 1 critical check failed: p0_pass_rate (90.9% vs =100%)',
    '[{"name":"p0_pass_rate","actual":"90.9%","expected":"=100%","detail":"TC-126 failing under load"}]'::jsonb,
    '[{"name":"coverage_REQ-019","actual":"0%","expected":"≥90%","detail":"Refund flow has zero test coverage"}]'::jsonb,
    '[{"name":"p0_coverage","actual":"87%","expected":"≥100%"},{"name":"performance_p95","actual":"2.8s","expected":"≤3s"},{"name":"security_findings","actual":"0","expected":"≤0"}]'::jsonb,
    87.0, 78.0, 90.91, 95.45, 1, 2800, 0,
    'ep-487-staging'
),
(
    (SELECT id FROM test_runs WHERE run_id = 'run-486'),
    '486', 'staging', 'PASS',
    'RELEASE APPROVED — 11/11 checks passed',
    '[]'::jsonb, '[]'::jsonb,
    '[{"name":"p0_pass_rate","actual":"100%","expected":"=100%"},{"name":"overall_coverage","actual":"76.5%","expected":"≥80%"},{"name":"open_p0_defects","actual":"0","expected":"≤0"}]'::jsonb,
    100.0, 76.5, 100.0, 100.0, 0, 2100, 0,
    'ep-486-staging'
);


-- ══════════════════════════════════════════════════════════════════════════════
-- PROJECTS — Multi-project support with per-project LLM configuration
-- ══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS projects (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name          VARCHAR(255) NOT NULL,
    description   TEXT         DEFAULT '',
    color         VARCHAR(7)   DEFAULT '#6366f1',
    icon          VARCHAR(50)  DEFAULT '🧪',
    -- LLM provider config: {provider, model, api_key, base_url, temperature, max_tokens}
    llm_config    JSONB        NOT NULL DEFAULT '{"provider":"anthropic","model":"claude-sonnet-4-6","api_key":"","base_url":"","temperature":0.2,"max_tokens":4096}',
    -- Quality gate overrides (empty = use platform defaults from arta.config.yaml)
    quality_gates JSONB        DEFAULT '{}',
    -- Per-project integration endpoints: {jira_project, github_repo, slack_channel, base_url}
    integrations  JSONB        DEFAULT '{}',
    -- Framework setup fields
    engagement_model TEXT,                     -- 'tea_solo' | 'tea_lite' | 'integrated'
    stack_type       TEXT,                     -- 'frontend' | 'backend' | 'fullstack'
    is_greenfield    BOOLEAN    DEFAULT TRUE,
    created_at    TIMESTAMPTZ  DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_projects_name ON projects (name);

-- Add project scope to all main data tables (nullable — existing rows have no project)
ALTER TABLE requirements  ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE test_cases    ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE test_runs     ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE defects       ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE quality_gates ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_requirements_project  ON requirements  (project_id);
CREATE INDEX IF NOT EXISTS idx_test_cases_project    ON test_cases    (project_id);
CREATE INDEX IF NOT EXISTS idx_test_runs_project     ON test_runs     (project_id);
CREATE INDEX IF NOT EXISTS idx_defects_project       ON defects       (project_id);

-- Trigger: auto-update updated_at on projects
CREATE TRIGGER set_updated_at_projects
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── Seed: default project for existing demo data ──────────────────────────

INSERT INTO projects (id, name, description, color, icon, llm_config, integrations)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'E-Commerce Platform',
    'Checkout, Cart, Refund, and Search for the ARTA demo — REQ-017 to REQ-020.',
    '#6366f1',
    '🛒',
    '{"provider":"anthropic","model":"claude-sonnet-4-6","api_key":"","base_url":"","temperature":0.2,"max_tokens":4096}',
    '{"github_repo":"","jira_project":"EP","slack_channel":"#qa-alerts","base_url":"http://localhost:3000"}'
) ON CONFLICT (id) DO NOTHING;

INSERT INTO projects (id, name, description, color, icon, llm_config)
VALUES (
    '00000000-0000-0000-0000-000000000002',
    'Mobile Banking App',
    'iOS/Android banking feature tests — login, transfers, statements.',
    '#8b5cf6',
    '🏦',
    '{"provider":"google_gemini","model":"gemini-2.0-flash","api_key":"","base_url":"","temperature":0.2,"max_tokens":4096}'
) ON CONFLICT (id) DO NOTHING;

INSERT INTO projects (id, name, description, color, icon, llm_config)
VALUES (
    '00000000-0000-0000-0000-000000000003',
    'Internal Tooling',
    'Developer portal, admin dashboard, and CI/CD toolchain tests.',
    '#06b6d4',
    '⚙️',
    '{"provider":"ollama","model":"qwen2.5:32b","api_key":"","base_url":"http://localhost:11434","temperature":0.2,"max_tokens":4096}'
) ON CONFLICT (id) DO NOTHING;

-- ── Feature 1: Test Data Fixtures ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS test_data_fixtures (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    test_id     TEXT NOT NULL,
    fixture_id  TEXT NOT NULL,              -- e.g. "TD-124"
    description TEXT,
    columns     JSONB NOT NULL DEFAULT '[]',
    rows        JSONB NOT NULL DEFAULT '[]',
    examples    TEXT,                        -- Gherkin Examples: table as text
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_test_data_fixtures_test_id ON test_data_fixtures(test_id);

-- ── Feature 3: Test Case Versions ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS test_case_versions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    test_id          TEXT NOT NULL,
    version          INTEGER NOT NULL,        -- 1, 2, 3, …
    change_reason    TEXT,
    gherkin_snapshot TEXT,
    script_snapshot  TEXT,
    changed_by       TEXT DEFAULT 'arta-agent',
    created_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE(test_id, version)
);

-- ── Authentication: Users, Roles, Refresh Tokens ──────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT UNIQUE NOT NULL,
    full_name       TEXT NOT NULL,
    avatar_url      TEXT,                          -- Gravatar or OAuth provider URL
    hashed_password TEXT,                          -- NULL if social-OAuth-only account
    is_active       BOOLEAN DEFAULT TRUE,
    is_admin        BOOLEAN DEFAULT FALSE,         -- Platform-level admin
    created_at      TIMESTAMPTZ DEFAULT now(),
    last_login      TIMESTAMPTZ,
    oauth_provider  TEXT,                          -- 'google' | 'github' | NULL for password users
    oauth_id        TEXT,                          -- Provider-specific unique ID
    UNIQUE(oauth_provider, oauth_id)
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
COMMENT ON TABLE users IS 'Platform users with JWT + OAuth authentication';

-- Per-project roles: viewer < developer < tester < qa_lead < test_architect < admin
CREATE TABLE IF NOT EXISTS project_roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('admin', 'test_architect', 'qa_lead', 'tester', 'developer', 'viewer')),
    granted_by  UUID REFERENCES users(id),
    granted_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_project_roles_user_id    ON project_roles(user_id);
CREATE INDEX IF NOT EXISTS idx_project_roles_project_id ON project_roles(project_id);
COMMENT ON TABLE project_roles IS 'Per-project RBAC: viewer | tester | qa_lead | admin';

-- JWT refresh tokens (stored as bcrypt hash for security)
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id ON refresh_tokens(user_id);

-- No seed admin: the first admin is created at startup by ensure_bootstrap_admin()
-- when the users table is empty and ARTA_BOOTSTRAP_ADMIN_EMAIL/PASSWORD are set
-- (or use ARTA_DEMO_MODE=1 for a no-DB demo login). A NULL-password seed row here
-- would leave the table non-empty and defeat that bootstrap.
CREATE INDEX IF NOT EXISTS idx_test_case_versions_test_id ON test_case_versions(test_id);

-- ── Requirement Versioning & Change Detection ───────────────────────────

ALTER TABLE requirements ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE requirements ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;
CREATE INDEX IF NOT EXISTS idx_requirements_content_hash ON requirements(content_hash);

CREATE TABLE IF NOT EXISTS requirement_versions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    req_id               TEXT NOT NULL,
    version              INTEGER NOT NULL,
    title_snapshot       TEXT,
    description_snapshot TEXT,
    ac_snapshot          JSONB DEFAULT '[]',
    content_hash         TEXT NOT NULL,
    change_type          TEXT NOT NULL CHECK (change_type IN ('created', 'modified', 'deleted')),
    change_reason        TEXT,
    changed_by           TEXT DEFAULT 'arta-agent',
    created_at           TIMESTAMPTZ DEFAULT now(),
    UNIQUE(req_id, version)
);

CREATE INDEX IF NOT EXISTS idx_requirement_versions_req_id ON requirement_versions(req_id);
COMMENT ON TABLE requirement_versions IS 'Audit trail for requirement changes — snapshots content at each version';

-- ── Exploratory Testing ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS exploratory_sessions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          TEXT UNIQUE NOT NULL,
    charter             TEXT NOT NULL,
    tagged_requirements JSONB DEFAULT '[]',
    time_limit_minutes  INTEGER DEFAULT 60,
    started_at          TIMESTAMPTZ DEFAULT now(),
    completed_at        TIMESTAMPTZ,
    status              TEXT DEFAULT 'active' CHECK (status IN ('active', 'completed', 'abandoned')),
    user_id             UUID REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_exploratory_sessions_status ON exploratory_sessions(status);

CREATE TABLE IF NOT EXISTS exploratory_findings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id          TEXT UNIQUE NOT NULL,
    session_id          TEXT NOT NULL,
    text                TEXT NOT NULL,
    severity            TEXT DEFAULT 'medium',
    screenshot_url      TEXT,
    converted_to_type   TEXT,      -- 'defect' | 'test_case' | NULL
    converted_to_id     TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_exploratory_findings_session ON exploratory_findings(session_id);

-- ── Reusable Test Blocks ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS test_blocks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    block_id        TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    steps           JSONB NOT NULL DEFAULT '[]',
    parameters      JSONB NOT NULL DEFAULT '[]',
    tags            TEXT[] DEFAULT '{}',
    usage_count     INTEGER DEFAULT 0,
    project_id      UUID REFERENCES projects(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_test_blocks_project ON test_blocks(project_id);

-- ── R320: Test-Case Refinement corrections ────────────────────────────────────
-- A tester's correction of an AI-generated test: the correction, its provenance
-- verdict (arta_knew vs human_knowledge), the grounding fact it produced, and the
-- verify result. Traceable + revertible; the audit spine for the refinement copilot.
CREATE TABLE IF NOT EXISTS test_corrections (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correction_id      TEXT UNIQUE NOT NULL,
    project_id         UUID REFERENCES projects(id) ON DELETE CASCADE,
    test_id            TEXT,
    requirement_id     TEXT,
    ac_id              TEXT,
    tool               TEXT,
    kind               TEXT NOT NULL,          -- endpoint | field_value | shape | intent
    field              TEXT,                   -- field name for field_value corrections
    correction_text    TEXT NOT NULL,
    from_value         TEXT,
    to_value           TEXT,
    verdict            TEXT,                   -- arta_knew | human_knowledge
    grounding_fact_ref TEXT,                   -- "METHOD:path" written to the store
    verify_status      TEXT,                   -- pending | passed | failed | not_run
    verify_run_id      TEXT,                   -- the run_id that verifies this correction
    corrected_by       TEXT,
    created_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_test_corrections_project ON test_corrections(project_id);
-- Idempotent column adds for DBs created before these columns existed.
ALTER TABLE test_corrections ADD COLUMN IF NOT EXISTS field TEXT;
ALTER TABLE test_corrections ADD COLUMN IF NOT EXISTS verify_run_id TEXT;

-- ── Self-Healing Proposals ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS healing_proposals (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id       TEXT UNIQUE NOT NULL,
    test_id           TEXT,
    defect_id         TEXT,
    strategy          TEXT NOT NULL,
    confidence        NUMERIC(5,2),
    ai_reasoning      TEXT,
    current_selector  TEXT,
    proposed_selector TEXT,
    diff_preview      TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    applied_selector  TEXT,
    pr_url            TEXT,
    project_id        UUID REFERENCES projects(id) ON DELETE CASCADE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ,
    resolved_by       TEXT
);
CREATE INDEX IF NOT EXISTS idx_healing_proposals_status ON healing_proposals(status);
CREATE INDEX IF NOT EXISTS idx_healing_proposals_project ON healing_proposals(project_id);

-- Platform-level configuration (gate thresholds, feature flags, etc.)
CREATE TABLE IF NOT EXISTS platform_config (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE platform_config IS 'Key-value config store for platform settings (gate thresholds, etc.)';

-- Backfill: assign orphan test_runs (NULL project_id) to the BugTrackr project if it exists
DO $$
BEGIN
  UPDATE test_runs
  SET project_id = (SELECT id FROM projects WHERE name ILIKE '%bugtrack%' LIMIT 1)
  WHERE project_id IS NULL
    AND EXISTS (SELECT 1 FROM projects WHERE name ILIKE '%bugtrack%');
EXCEPTION WHEN OTHERS THEN
  -- Ignore errors (e.g., no projects table yet)
  NULL;
END $$;
