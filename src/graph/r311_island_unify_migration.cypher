// R311 — Neo4j traceability island-unification migration (idempotent, one-shot).
//
// Problem: two graph writers stored the SAME value under different property names —
//   src/graph/writer.py (gen-time)      keys Requirement/AC/TestCase on req_id/ac_id/test_id
//   src/agents/traceability_agent.py    keys the same nodes on `id`
// so {id:"TC-X"} and {test_id:"TC-X"} were DISTINCT nodes → the Requirement→AC→TC→Run
// planning spine (writer.py) never connected to the TC→ExecutionResult→Defect layer
// (traceability_agent). Additionally, writer.py MERGE-ed without a uniqueness constraint
// (the race R232 warns about) and had accumulated duplicate nodes.
//
// This migration: (A) dedupes race-created duplicates, (B) merges each cross-island
// value-pair ({id:V} ⋈ {test_id:V}) into one node carrying both properties (relationships
// preserved), (C) adds the missing uniqueness constraints. Requires APOC.
//
// The DURABLE fix that keeps FUTURE writes unified lives in code:
//   - traceability_agent.py: adopt-id block + `ON CREATE SET` the writer.py property
//   - killswitch ARTA_R311_GRAPH_UNIFY_DISABLE=1
// The graph read layer surfaces the newly-connected Results per run:
//   - traceability.py `_full_chain_graph` run-scoped PRODUCED/TRIGGERED traversal
//   - killswitch ARTA_R311_GRAPH_RESULTS_DISABLE=1
//
// Run (idempotent): cat this file | cypher-shell -u neo4j -p <pw>

// ── Stage A — dedupe race-created duplicate nodes (same key value → one node) ──
MATCH (t:TestCase) WHERE t.test_id IS NOT NULL
WITH t.test_id AS k, collect(t) AS ns WHERE size(ns) > 1
CALL apoc.refactor.mergeNodes(ns, {properties:'discard', mergeRels:true}) YIELD node
RETURN count(*) AS testcase_dedup_groups;

MATCH (r:Requirement) WHERE r.req_id IS NOT NULL
WITH r.req_id AS k, collect(r) AS ns WHERE size(ns) > 1
CALL apoc.refactor.mergeNodes(ns, {properties:'discard', mergeRels:true}) YIELD node
RETURN count(*) AS requirement_dedup_groups;

MATCH (a:AcceptanceCriteria) WHERE a.ac_id IS NOT NULL
WITH a.ac_id AS k, collect(a) AS ns WHERE size(ns) > 1
CALL apoc.refactor.mergeNodes(ns, {properties:'discard', mergeRels:true}) YIELD node
RETURN count(*) AS ac_dedup_groups;

// ── Stage B — cross-island merge ({id:V} node ⋈ {alt:V} node → one node) ──
MATCH (a:TestCase) WHERE a.id IS NOT NULL AND a.test_id IS NULL
MATCH (b:TestCase) WHERE b.test_id = a.id AND b.id IS NULL
WITH a, collect(DISTINCT b) AS bs
CALL apoc.refactor.mergeNodes([a]+bs, {properties:'discard', mergeRels:true}) YIELD node
SET node.test_id = node.id
RETURN count(node) AS testcase_cross_merged;

MATCH (a:Requirement) WHERE a.id IS NOT NULL AND a.req_id IS NULL
MATCH (b:Requirement) WHERE b.req_id = a.id AND b.id IS NULL
WITH a, collect(DISTINCT b) AS bs
CALL apoc.refactor.mergeNodes([a]+bs, {properties:'discard', mergeRels:true}) YIELD node
SET node.req_id = node.id
RETURN count(node) AS requirement_cross_merged;

MATCH (a:AcceptanceCriteria) WHERE a.id IS NOT NULL AND a.ac_id IS NULL
MATCH (b:AcceptanceCriteria) WHERE b.ac_id = a.id AND b.id IS NULL
WITH a, collect(DISTINCT b) AS bs
CALL apoc.refactor.mergeNodes([a]+bs, {properties:'discard', mergeRels:true}) YIELD node
SET node.ac_id = node.id
RETURN count(node) AS ac_cross_merged;

// ── Stage C — uniqueness constraints (prevent the writer.py MERGE race recurring) ──
CREATE CONSTRAINT r311_tc_test_id  IF NOT EXISTS FOR (t:TestCase)          REQUIRE t.test_id IS UNIQUE;
CREATE CONSTRAINT r311_req_req_id  IF NOT EXISTS FOR (r:Requirement)       REQUIRE r.req_id  IS UNIQUE;
CREATE CONSTRAINT r311_ac_ac_id    IF NOT EXISTS FOR (a:AcceptanceCriteria) REQUIRE a.ac_id  IS UNIQUE;

// ── Verify — connected Requirement→AC→TestCase→ExecutionResult paths (was 0) ──
MATCH (r:Requirement)-[:HAS_AC]->(:AcceptanceCriteria)<-[:COVERS]-(tc:TestCase)-[:PRODUCED]->(:ExecutionResult)
RETURN count(*) AS connected_req_to_result_paths;
