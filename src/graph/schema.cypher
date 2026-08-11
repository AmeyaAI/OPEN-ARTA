// ══════════════════════════════════════════════════════════════════════════════
// ARTA Traceability Graph Schema — Neo4j
// TEA Layer 7: Requirements → AC → Scenarios → Tests → Results → Defects
// ══════════════════════════════════════════════════════════════════════════════

// ── Constraints ───────────────────────────────────────────────────────────────

CREATE CONSTRAINT req_id_unique    IF NOT EXISTS FOR (r:Requirement)        REQUIRE r.id IS UNIQUE;
CREATE CONSTRAINT ac_id_unique     IF NOT EXISTS FOR (a:AcceptanceCriteria) REQUIRE a.id IS UNIQUE;
CREATE CONSTRAINT tc_id_unique     IF NOT EXISTS FOR (t:TestCase)           REQUIRE t.id IS UNIQUE;
CREATE CONSTRAINT result_id_unique IF NOT EXISTS FOR (r:ExecutionResult)    REQUIRE r.id IS UNIQUE;
CREATE CONSTRAINT defect_id_unique IF NOT EXISTS FOR (d:Defect)             REQUIRE d.id IS UNIQUE;
CREATE CONSTRAINT run_id_unique    IF NOT EXISTS FOR (r:TestRun)            REQUIRE r.id IS UNIQUE;

// ── Indexes ───────────────────────────────────────────────────────────────────

CREATE INDEX req_priority     IF NOT EXISTS FOR (r:Requirement) ON (r.priority);
CREATE INDEX req_sprint       IF NOT EXISTS FOR (r:Requirement) ON (r.sprint_id);
CREATE INDEX tc_type          IF NOT EXISTS FOR (t:TestCase)    ON (t.automation_type);
CREATE INDEX result_status    IF NOT EXISTS FOR (r:ExecutionResult) ON (r.status);
CREATE INDEX defect_severity  IF NOT EXISTS FOR (d:Defect) ON (d.severity);
CREATE INDEX defect_status    IF NOT EXISTS FOR (d:Defect) ON (d.status);

// ══════════════════════════════════════════════════════════════════════════════
// QUERIES
// ══════════════════════════════════════════════════════════════════════════════

// ── Coverage Report by Requirement ───────────────────────────────────────────

// Full coverage per requirement
MATCH (r:Requirement)-[:HAS_AC]->(ac:AcceptanceCriteria)
OPTIONAL MATCH (ac)<-[:COVERS]-(tc:TestCase)-[:PRODUCED]->(res:ExecutionResult)
WITH r,
     count(DISTINCT ac)                               AS total_ac,
     count(DISTINCT CASE WHEN res IS NOT NULL
           THEN tc END)                               AS covered_ac,
     count(DISTINCT CASE WHEN res.status = 'PASS'
           THEN tc END)                               AS passing_tc,
     count(DISTINCT tc)                               AS total_tc
RETURN
  r.id           AS requirement_id,
  r.title        AS title,
  r.priority     AS priority,
  r.risk_score   AS risk_score,
  total_ac,
  covered_ac,
  total_tc,
  passing_tc,
  CASE WHEN total_ac > 0
       THEN round(toFloat(covered_ac) / total_ac * 100, 1)
       ELSE 0 END AS coverage_pct
ORDER BY r.priority ASC, r.risk_score DESC;


// ── Find Coverage Gaps (uncovered ACs) ───────────────────────────────────────

MATCH (r:Requirement)-[:HAS_AC]->(ac:AcceptanceCriteria)
WHERE NOT (ac)<-[:COVERS]-(:TestCase)
RETURN
  r.id       AS requirement_id,
  r.priority AS priority,
  r.title    AS requirement_title,
  collect({
    id:        ac.id,
    statement: ac.statement
  }) AS uncovered_criteria
ORDER BY r.priority ASC;


// ── Orphaned Tests (no requirement link) ─────────────────────────────────────

MATCH (tc:TestCase)
WHERE NOT ()-[:COVERS]->(tc)
   OR NOT (tc)-[:COVERS]->(:AcceptanceCriteria)
RETURN tc.id AS test_id, tc.title AS title, tc.automation_type AS type
ORDER BY tc.id;


// ── Defect Impact Radius ──────────────────────────────────────────────────────

MATCH (d:Defect)<-[:TRIGGERED]-(res:ExecutionResult)<-[:PRODUCED]-(tc:TestCase)
      -[:COVERS]->(ac:AcceptanceCriteria)<-[:HAS_AC]-(r:Requirement)
WHERE d.status = 'open'
RETURN
  d.id        AS defect_id,
  d.severity  AS severity,
  d.title     AS title,
  collect(DISTINCT r.id)  AS impacted_requirements,
  collect(DISTINCT tc.id) AS failing_tests
ORDER BY d.severity ASC;


// ── Risk Heatmap Query ────────────────────────────────────────────────────────

MATCH (r:Requirement)
OPTIONAL MATCH (r)-[:HAS_AC]->(ac)<-[:COVERS]-(tc)-[:PRODUCED]->(res)
WITH r,
     count(DISTINCT CASE WHEN res.status = 'FAIL' THEN tc END) AS failing_tests,
     count(DISTINCT tc) AS total_tests
RETURN
  r.id          AS requirement_id,
  r.priority    AS priority,
  r.risk_score  AS risk_score,
  r.title       AS title,
  failing_tests,
  total_tests,
  CASE WHEN total_tests > 0
       THEN round(toFloat(failing_tests) / total_tests * 100, 1)
       ELSE 0 END AS failure_rate_pct
ORDER BY r.risk_score DESC;


// ── Sprint Coverage Summary ───────────────────────────────────────────────────

MATCH (r:Requirement {sprint_id: $sprint_id})-[:HAS_AC]->(ac)
OPTIONAL MATCH (ac)<-[:COVERS]-(tc)-[:PRODUCED]->(res:ExecutionResult {run_id: $run_id})
RETURN
  r.priority AS priority,
  count(DISTINCT r) AS requirement_count,
  count(DISTINCT ac) AS total_ac,
  count(DISTINCT CASE WHEN res IS NOT NULL THEN ac END) AS covered_ac,
  count(DISTINCT CASE WHEN res.status = 'PASS' THEN tc END) AS passing,
  count(DISTINCT CASE WHEN res.status = 'FAIL' THEN tc END) AS failing
ORDER BY priority;


// ── Create Full Traceability Chain ────────────────────────────────────────────
// Example: Link a test result to requirement through AC

MERGE (r:Requirement {id: $req_id})
  ON CREATE SET r.title = $req_title, r.priority = $priority,
                r.risk_score = $risk_score, r.created_at = datetime()

MERGE (ac:AcceptanceCriteria {id: $ac_id})
  ON CREATE SET ac.statement = $ac_statement, ac.covered = false

MERGE (tc:TestCase {id: $tc_id})
  ON CREATE SET tc.title = $tc_title, tc.automation_type = $automation_type,
                tc.created_at = datetime()

MERGE (res:ExecutionResult {id: $result_id})
  ON CREATE SET res.status = $status, res.duration_ms = $duration_ms,
                res.executed_at = datetime()

MERGE (r)-[:HAS_AC]->(ac)
MERGE (tc)-[:COVERS]->(ac)
MERGE (tc)-[:PRODUCED]->(res)

WITH ac, tc, res
WHERE res.status = 'PASS'
SET ac.covered = true

RETURN r, ac, tc, res;
