// Phase AD — Architecture Discovery schema fragment.
//
// SUPPLEMENTS schema.cypher + schema_phase_c.cypher. Adds node types for the
// consolidated SUT graphs (Service / Token / Claim / Scenario / Step). The
// api/dependency/data-flow graphs reuse the existing Endpoint + EnvVar nodes
// from schema_phase_c.cypher, so no new constraints are needed for those.
//
// All constraints + indexes are IF NOT EXISTS so re-runs are idempotent.

CREATE CONSTRAINT arch_service_unique IF NOT EXISTS
  FOR (s:Service) REQUIRE (s.project_id, s.repo) IS UNIQUE;

CREATE CONSTRAINT arch_token_unique IF NOT EXISTS
  FOR (t:Token) REQUIRE (t.project_id, t.kind) IS UNIQUE;

CREATE CONSTRAINT arch_scenario_unique IF NOT EXISTS
  FOR (w:Scenario) REQUIRE w.scenario_id IS UNIQUE;

CREATE INDEX arch_service_project IF NOT EXISTS FOR (s:Service) ON (s.project_id);

CREATE INDEX arch_token_project IF NOT EXISTS FOR (t:Token) ON (t.project_id);
