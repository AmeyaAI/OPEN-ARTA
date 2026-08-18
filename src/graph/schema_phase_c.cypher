// Phase C4 — CallChain / Endpoint / EnvVar schema fragment.
//
// This file SUPPLEMENTS the canonical schema.cypher (which is permissioned
// to the operator that originally provisioned the database). Operators
// running migrations on Neo4j 5+ should apply both files via cypher-shell
// or the Neo4j Browser:
//
//   cat src/graph/schema.cypher src/graph/schema_phase_c.cypher | cypher-shell -u neo4j
//
// All constraints + indexes are IF NOT EXISTS so re-runs are idempotent.

// ── Node uniqueness constraints ───────────────────────────────────────────

CREATE CONSTRAINT endpoint_key_unique IF NOT EXISTS
  FOR (e:Endpoint) REQUIRE e.endpoint_key IS UNIQUE;

CREATE CONSTRAINT envvar_unique IF NOT EXISTS
  FOR (v:EnvVar) REQUIRE (v.project_id, v.name) IS UNIQUE;

CREATE CONSTRAINT chain_id_unique IF NOT EXISTS
  FOR (c:CallChain) REQUIRE c.chain_id IS UNIQUE;

// ── Indexes for common query patterns ─────────────────────────────────────

CREATE INDEX endpoint_method_path IF NOT EXISTS
  FOR (e:Endpoint) ON (e.method, e.path_template);

CREATE INDEX chain_project IF NOT EXISTS
  FOR (c:CallChain) ON (c.project_id);

CREATE INDEX chain_semantic IF NOT EXISTS
  FOR (c:CallChain) ON (c.semantic_hash);

// ── Reference query — DEPENDS_ON derivation (also in writer.py) ───────────
//
// Operators can run this manually for ad-hoc checks:
//
// MATCH (t1:TestCase)-[:OBSERVED_IN]->(c1:CallChain)
//        -[:CONTAINS]->(e1:Endpoint)
//        -[:PROVIDES]->(v:EnvVar)
// MATCH (t2:TestCase)-[:OBSERVED_IN]->(c2:CallChain)
//        -[:CONTAINS]->(e2:Endpoint)
//        -[:CONSUMES]->(v)
// WHERE t1 <> t2
// MERGE (t2)-[r:DEPENDS_ON {via_var: v.name}]->(t1);

// ── Reference query — provider→consumer trace for debugging ──────────────
//
// Show every provider/consumer pair for a project's env vars:
//
// MATCH (provider:Endpoint)-[p:PROVIDES]->(v:EnvVar {project_id: $pid})
// OPTIONAL MATCH (consumer:Endpoint)-[c:CONSUMES]->(v)
// RETURN v.name, provider.endpoint_key, p.jsonpath,
//        collect(consumer.endpoint_key) AS consumers
// ORDER BY v.name;

// ── R330 P5 — traceability-spine key-space constraints (from the R311 Stage C
// migration; applied at startup so the writer.py MERGE duplicate-node race
// cannot recur in a default deployment). If one of these FAILS at startup the
// database already holds duplicate spine nodes — run the dedup stages of
// src/graph/r311_island_unify_migration.cypher (manual, APOC) first.

CREATE CONSTRAINT r311_tc_test_id IF NOT EXISTS
  FOR (t:TestCase) REQUIRE t.test_id IS UNIQUE;

CREATE CONSTRAINT r311_req_req_id IF NOT EXISTS
  FOR (r:Requirement) REQUIRE r.req_id IS UNIQUE;

CREATE CONSTRAINT r311_ac_ac_id IF NOT EXISTS
  FOR (a:AcceptanceCriteria) REQUIRE a.ac_id IS UNIQUE;
