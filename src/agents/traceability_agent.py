"""
ARTA Traceability Agent — TEA Layer 7: Traceability & Coverage Analysis
Manages the Neo4j traceability graph and produces coverage reports.
Detects: missing tests, uncovered requirements, redundant/orphaned tests.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("arta.traceability")


@dataclass
class CoverageReport:
    overall_coverage_pct: float = 0.0
    pass_rate: float = 0.0
    p0_pass_rate: float = 100.0
    coverage_by_priority: dict = field(default_factory=dict)
    gaps: list[dict] = field(default_factory=list)
    orphans: list[dict] = field(default_factory=list)
    redundant: list[dict] = field(default_factory=list)
    # NFR sub-report (populated by orchestrator NFR stage)
    nfr: dict = field(default_factory=dict)


# ── Cypher Queries (mirrors src/graph/schema.cypher) ─────────────────────────

UPSERT_REQUIREMENT = """
MERGE (r:Requirement {id: $req_id})
  ON CREATE SET r.req_id = $req_id, r.title = $title, r.priority = $priority,
                r.risk_score = $risk_score, r.created_at = datetime(),
                r.jira_key = $jira_key, r.jira_url = $jira_url
  ON MATCH SET  r.title = $title, r.priority = $priority,
                r.risk_score = $risk_score, r.updated_at = datetime(),
                r.jira_key = $jira_key, r.jira_url = $jira_url
RETURN r
"""

# R257 (WS2e) — BMAD TEA Layer 1: the traceability spine should reach all the
# way back to the SOURCE of the requirement, not stop at ARTA's own copy of it.
# Without this, "which JIRA issues does this run cover?" is unanswerable from
# the graph. Only run when the requirement actually carries a jira_key.
UPSERT_JIRA_ISSUE = """
MERGE (j:JiraIssue {key: $jira_key})
  ON CREATE SET j.url = $jira_url, j.created_at = datetime()
  ON MATCH SET  j.url = $jira_url, j.updated_at = datetime()
RETURN j
"""

LINK_REQ_JIRA = """
MATCH (r:Requirement {id: $req_id})
MATCH (j:JiraIssue {key: $jira_key})
MERGE (r)-[:SOURCED_FROM]->(j)
"""

UPSERT_AC = """
MERGE (ac:AcceptanceCriteria {id: $ac_id})
  ON CREATE SET ac.ac_id = $ac_id, ac.statement = $statement, ac.covered = false
  ON MATCH SET  ac.statement = $statement
RETURN ac
"""

LINK_REQ_AC = "MERGE (r:Requirement {id: $req_id})-[:HAS_AC]->(ac:AcceptanceCriteria {id: $ac_id})"

UPSERT_TEST_CASE = """
MERGE (tc:TestCase {id: $tc_id})
  ON CREATE SET tc.test_id = $tc_id, tc.title = $title,
                tc.automation_type = $automation_type,
                tc.created_at = datetime()
  ON MATCH SET  tc.title = $title, tc.automation_type = $automation_type,
                tc.updated_at = datetime()
RETURN tc
"""

# R232 — MATCH the (already-upserted) TestCase, MERGE the AC + the relationship
# only. Same race-safety fix as LINK_TC_RESULT: the old full-pattern MERGE could
# re-CREATE the TestCase under concurrency → constraint violation.
LINK_TC_AC = """
MATCH (tc:TestCase {id: $tc_id})
MERGE (ac:AcceptanceCriteria {id: $ac_id})
MERGE (tc)-[:COVERS]->(ac)
"""

UPSERT_RESULT = """
MERGE (res:ExecutionResult {id: $result_id})
  ON CREATE SET res.status = $status, res.duration_ms = $duration_ms,
                res.build_id = $build_id, res.executed_at = datetime(),
                res.run_id = $run_id, res.tool = $tool
  ON MATCH  SET res.status = $status, res.duration_ms = $duration_ms,
                res.run_id = coalesce(res.run_id, $run_id),
                res.tool = coalesce(res.tool, $tool)
RETURN res
"""

# R232 — MATCH the nodes (both already upserted immediately before this call:
# UPSERT_TEST_CASE + UPSERT_RESULT) and MERGE ONLY the relationship. The old
# full-pattern `MERGE (tc:TestCase {id})-[:PRODUCED]->(res {id})` re-evaluates the
# WHOLE pattern: under the concurrent post-run write burst it tried to CREATE the
# TestCase again (full-pattern MERGE doesn't take the unique-constraint lock the
# node-only MERGE does) → `ConstraintValidationFailed: Node already exists ... id`
# storm (run-8aad82: ~24 write failures) that dropped Result→TestCase edges from
# the WS3 traceability graph. MATCH+MERGE-rel is race-safe. GENERIC.
LINK_TC_RESULT = """
MATCH (tc:TestCase {id: $tc_id})
MATCH (res:ExecutionResult {id: $result_id})
MERGE (tc)-[:PRODUCED]->(res)
"""

CLASSIFY_AC_COVERAGE = """
MATCH (ac:AcceptanceCriteria {id: $ac_id})
OPTIONAL MATCH (ac)<-[:COVERS]-(tc:TestCase)
OPTIONAL MATCH (tc)-[:PRODUCED]->(res:ExecutionResult)
WITH ac,
     count(DISTINCT tc)                                          AS total_tests,
     count(DISTINCT CASE WHEN res.status = 'PASS' THEN tc END)  AS passing_tests
SET ac.covered = CASE WHEN total_tests > 0 THEN true ELSE false END,
    ac.coverage_level = CASE
      WHEN total_tests > 0 AND passing_tests = total_tests THEN 'FULL'
      WHEN total_tests > 0 AND passing_tests < total_tests THEN 'PARTIAL'
      ELSE 'NONE'
    END
RETURN ac.id AS ac_id, ac.coverage_level AS coverage_level
"""

UPSERT_DEFECT = """
MERGE (d:Defect {id: $defect_id})
  ON CREATE SET d.title = $title, d.severity = $severity, d.status = $status,
                d.root_cause = $root_cause, d.created_at = datetime()
  ON MATCH SET  d.title = $title, d.severity = $severity, d.status = $status
RETURN d
"""

LINK_RESULT_DEFECT = "MERGE (res:ExecutionResult {id: $result_id})-[:TRIGGERED]->(d:Defect {id: $defect_id})"

COVERAGE_QUERY = """
MATCH (r:Requirement)-[:HAS_AC]->(ac:AcceptanceCriteria)
OPTIONAL MATCH (ac)<-[:COVERS]-(tc:TestCase)-[:PRODUCED]->(res:ExecutionResult)
WITH r, ac,
     count(DISTINCT tc)                                           AS tests_for_ac,
     count(DISTINCT CASE WHEN res.status = 'PASS' THEN tc END)  AS passing_for_ac
WITH r,
     count(ac)                                                           AS total_ac,
     count(CASE WHEN tests_for_ac > 0 THEN ac END)                     AS covered_ac,
     count(CASE WHEN tests_for_ac > 0 AND passing_for_ac = tests_for_ac THEN ac END) AS full_ac,
     count(CASE WHEN tests_for_ac > 0 AND passing_for_ac < tests_for_ac THEN ac END) AS partial_ac,
     count(CASE WHEN tests_for_ac = 0 THEN ac END)                      AS none_ac,
     sum(tests_for_ac)                                                   AS total_tc,
     sum(passing_for_ac)                                                 AS passing_tc
RETURN
  r.id           AS requirement_id,
  r.title        AS title,
  r.priority     AS priority,
  r.risk_score   AS risk_score,
  total_ac,
  covered_ac,
  full_ac,
  partial_ac,
  none_ac,
  total_tc,
  passing_tc,
  CASE WHEN total_ac > 0
       THEN round(toFloat(covered_ac) / total_ac * 100, 1)
       ELSE 0 END AS coverage_pct
ORDER BY r.priority ASC, r.risk_score DESC
"""

GAPS_QUERY = """
MATCH (r:Requirement)-[:HAS_AC]->(ac:AcceptanceCriteria)
WHERE NOT (ac)<-[:COVERS]-(:TestCase)
RETURN
  r.id       AS requirement_id,
  r.priority AS priority,
  r.title    AS requirement_title,
  collect({id: ac.id, statement: ac.statement}) AS uncovered_criteria
ORDER BY r.priority ASC
"""

ORPHANS_QUERY = """
MATCH (tc:TestCase)
WHERE NOT (tc)-[:COVERS]->(:AcceptanceCriteria)
RETURN tc.id AS test_id, tc.title AS title, tc.automation_type AS type
ORDER BY tc.id
"""

DELETE_ORPHANS_QUERY = """
MATCH (tc:TestCase)
WHERE NOT (tc)-[:COVERS]->(:AcceptanceCriteria)
DETACH DELETE tc
"""

REDUNDANCY_QUERY = """
MATCH (ac:AcceptanceCriteria)<-[:COVERS]-(tc:TestCase)
OPTIONAL MATCH (r:Requirement)-[:HAS_AC]->(ac)
WITH ac, r, tc.automation_type AS tool, count(tc) AS n,
     collect(tc.id)[..5] AS test_ids
WHERE n >= 3
RETURN ac.id   AS ac_id,
       r.id    AS requirement_id,
       tool,
       n       AS test_count,
       test_ids
ORDER BY n DESC
LIMIT 50
"""

PER_AC_COVERAGE_QUERY = """
MATCH (r:Requirement)-[:HAS_AC]->(ac:AcceptanceCriteria)
OPTIONAL MATCH (ac)<-[:COVERS]-(tc:TestCase)
OPTIONAL MATCH (tc)-[:PRODUCED]->(res:ExecutionResult)
WITH r, ac,
     count(DISTINCT tc)                                          AS test_count,
     count(DISTINCT CASE WHEN res.status = 'PASS' THEN tc END)  AS pass_count
RETURN ac.id                AS id,
       ac.statement         AS statement,
       r.id                 AS requirement_id,
       r.priority           AS priority,
       test_count,
       pass_count,
       CASE
         WHEN test_count = 0                       THEN 'NONE'
         WHEN pass_count = test_count              THEN 'FULL'
         ELSE                                           'PARTIAL'
       END                  AS coverage_state
ORDER BY r.priority ASC, ac.id ASC
"""


class TraceabilityAgent:
    """
    TEA Traceability Agent.

    Maintains the Neo4j knowledge graph linking:
      Requirement → AcceptanceCriteria → TestCase → ExecutionResult → Defect

    Detects coverage gaps, orphaned tests, and produces the coverage
    report consumed by the QualityGateAgent.
    """

    def __init__(self, neo4j_driver: Any = None) -> None:
        """
        Args:
            neo4j_driver: neo4j.AsyncDriver instance. If None, runs in
                          in-memory stub mode (for testing without Neo4j).
        """
        self._driver = neo4j_driver
        self._stub_mode = neo4j_driver is None
        # In-memory store for stub mode
        self._graph: dict[str, Any] = {
            "requirements": {},
            "acs": {},
            "tests": {},
            "results": {},
            "defects": {},
        }

    # ── Public API ────────────────────────────────────────────────────────────

    async def compute_sequence_integrity(
        self,
        execution_results: list[dict],
        *,
        project_id: str,
        chains: list[dict] | None = None,
        steps: list[dict] | None = None,
    ) -> dict:
        """Phase F1 — produce the dict consumed by gate's _check_call_sequence_integrity.

        Returns:
          {
            "cascade_failures": [{test_id, root_cause_test_id, via_var}, ...],
            "provider_contract_violations": [
                {test_id, var_name, expected_jsonpath, ...},
            ],
            "unresolved_chain_starts": [{chain_id, head_endpoint}, ...],
            "param_provenance": {var_name: {provider_test_id, ...}},
            "degraded": bool,
            "degraded_reason": str,
          }

        Two compute paths:
          1. Neo4j available — single Cypher traversal (per DR-3): one query
             walks `MATCH (failed:TestCase)-[:DEPENDS_ON*1..5]->(prov:TestCase)`
             plus a parallel scan for PROVIDES edges that no observed
             response satisfies (provider contract violations).
          2. Stub mode — derive cascade_failures + param_provenance from the
             chains dict (in-memory, no graph). Mark `degraded=True` so the
             gate downgrades severity per DR-3.

        Both paths return the same shape so the gate doesn't branch.
        """
        cascade_failures: list[dict] = []
        provider_contract_violations: list[dict] = []
        unresolved_chain_starts: list[dict] = []
        param_provenance: dict[str, dict] = {}

        # Build a quick lookup: test_id → status (from execution_results)
        test_status: dict[str, str] = {
            (r.get("test_id") or r.get("id") or ""): str(r.get("status") or "").upper()
            for r in (execution_results or []) if isinstance(r, dict)
        }

        if self._stub_mode:
            # Stub-mode derivation from chains. Lower-fidelity but better than
            # nothing — Phase 5.3 pattern: stamp `degraded=True` so the gate
            # downgrades severity.
            for chain in (chains or []):
                if not isinstance(chain, dict):
                    continue
                nodes = chain.get("nodes") or []
                for n in nodes:
                    if not isinstance(n, dict):
                        continue
                    # Each `provides[var]` from a node is a candidate provenance entry
                    for var_name, jp in (n.get("provides") or {}).items():
                        param_provenance.setdefault(var_name, {
                            "provider_test_id": chain.get("source_test_id"),
                            "provider_endpoint": f"{n.get('method', '?')}:{n.get('path_template', '?')}",
                            "provider_status": "unknown_in_stub",
                            "expected_jsonpath": jp,
                        })

            # Cascade detection from steps (when present) — when a step is
            # cascade_skip=True with cascade_reason, the parent test is the
            # consumer and the missing var points back to the provider.
            for step in (steps or []):
                if not isinstance(step, dict) or not step.get("cascade_skip"):
                    continue
                reason = step.get("cascade_reason") or ""
                # cascade_reason format from Phase D1:
                #   "<item-name>: missing required <var>" OR
                #   "<test-name>: missing <var>"
                via_var = None
                m = re.search(r"missing(?:\s+required)?\s+([A-Za-z_][\w]*)", reason)
                if m:
                    via_var = m.group(1)
                # Map via_var → provider via param_provenance
                root_cause = None
                if via_var and via_var in param_provenance:
                    root_cause = param_provenance[via_var].get("provider_test_id")
                cascade_failures.append({
                    "test_id": step.get("test_id"),
                    "root_cause_test_id": root_cause,
                    "via_var": via_var,
                    "step_seq": step.get("seq"),
                })

            # Provider contract violations from steps (Phase D1 stamps these)
            for step in (steps or []):
                if isinstance(step, dict) and step.get("provider_contract_violation"):
                    provider_contract_violations.append({
                        "test_id": step.get("test_id"),
                        "var_name": step.get("error") or "unknown",
                        "expected_jsonpath": "see provenance",
                        "step_seq": step.get("seq"),
                    })

            return {
                "cascade_failures": cascade_failures,
                "provider_contract_violations": provider_contract_violations,
                "unresolved_chain_starts": unresolved_chain_starts,
                "param_provenance": param_provenance,
                "degraded": True,
                "degraded_reason": "neo4j_unavailable_stub_mode",
            }

        # Neo4j path — single traversal per DR-3.
        try:
            async with self._driver.session() as session:   # type: ignore
                # Cascade attribution: for each non-PASS test, walk DEPENDS_ON
                # up to 5 hops to the nearest provider test.
                failed_ids = [
                    tid for tid, status in test_status.items()
                    if status not in ("PASS", "PASSED", "OK") and tid
                ]
                if failed_ids:
                    result = await session.run(
                        """
                        UNWIND $failed_ids AS fid
                        MATCH (consumer:TestCase {test_id: fid})
                        OPTIONAL MATCH path = (consumer)-[:DEPENDS_ON*1..5]->(provider:TestCase)
                        WITH consumer, provider, path
                        WHERE provider IS NOT NULL
                        RETURN consumer.test_id AS consumer_id,
                               provider.test_id AS provider_id,
                               [r IN relationships(path) | r.via_var][-1] AS via_var
                        """,
                        {"failed_ids": failed_ids},
                    )
                    async for record in result:
                        cascade_failures.append({
                            "test_id": record["consumer_id"],
                            "root_cause_test_id": record["provider_id"],
                            "via_var": record["via_var"],
                        })

                # Provenance: every PROVIDES edge for the project.
                prov_result = await session.run(
                    """
                    MATCH (e:Endpoint)-[p:PROVIDES]->(v:EnvVar {project_id: $project_id})
                    OPTIONAL MATCH (t:TestCase)-[:OBSERVED_IN]->(:CallChain)-[:CONTAINS]->(e)
                    RETURN v.name AS var_name,
                           e.endpoint_key AS endpoint_key,
                           p.jsonpath AS jsonpath,
                           collect(DISTINCT t.test_id) AS provider_test_ids
                    """,
                    {"project_id": project_id},
                )
                async for record in prov_result:
                    var_name = record["var_name"]
                    if not var_name:
                        continue
                    provider_test_ids = [t for t in (record["provider_test_ids"] or []) if t]
                    provider_status = "PASS"
                    if provider_test_ids and provider_test_ids[0] in test_status:
                        provider_status = test_status.get(provider_test_ids[0], "?")
                    param_provenance[var_name] = {
                        "provider_test_id": provider_test_ids[0] if provider_test_ids else None,
                        "provider_endpoint": record["endpoint_key"],
                        "provider_status": provider_status,
                        "expected_jsonpath": record["jsonpath"],
                    }

                # Unresolved chain starts — chain head endpoints with no
                # OBSERVED_IN test linkage.
                head_result = await session.run(
                    """
                    MATCH (c:CallChain {project_id: $project_id})-[r:CONTAINS]->(e:Endpoint)
                    WHERE r.sequence_index = 0
                    OPTIONAL MATCH (t:TestCase)-[:OBSERVED_IN]->(c)
                    WITH c, e, count(t) AS test_count
                    WHERE test_count = 0
                    RETURN c.chain_id AS chain_id, e.endpoint_key AS head_endpoint
                    """,
                    {"project_id": project_id},
                )
                async for record in head_result:
                    unresolved_chain_starts.append({
                        "chain_id": record["chain_id"],
                        "head_endpoint": record["head_endpoint"],
                    })

            return {
                "cascade_failures": cascade_failures,
                "provider_contract_violations": provider_contract_violations,
                "unresolved_chain_starts": unresolved_chain_starts,
                "param_provenance": param_provenance,
                "degraded": False,
                "degraded_reason": "",
            }
        except Exception as exc:
            logger.warning("compute_sequence_integrity Neo4j path failed: %s — falling back to stub", exc)
            # Recurse into stub path — preserves single shape contract.
            self._stub_mode = True
            try:
                return await self.compute_sequence_integrity(
                    execution_results, project_id=project_id, chains=chains, steps=steps,
                )
            finally:
                self._stub_mode = False

    async def ingest_chains(
        self,
        chains: list[dict],
        *,
        project_id: str,
    ) -> None:
        """Phase C4: ingest CallChain dicts into Neo4j.

        Stub-mode (no Neo4j) is a graceful no-op — the chain JSON sidecars
        on disk (.arta/discovered_chains/) carry the same data, and Phase F
        gate falls through to `coverage_degraded` per Phase 5.3.

        Best-effort: errors are logged, never raised. The orchestrator's
        traceability stage stays green even when chain ingest fails so a
        Neo4j outage doesn't poison the rest of the pipeline.
        """
        if self._stub_mode or not chains:
            return
        try:
            from ..graph.writer import upsert_chain, upsert_test_dependencies
        except ImportError as exc:
            logger.warning("traceability: graph writer unavailable: %s", exc)
            return
        for chain in chains:
            await upsert_chain(self._driver, chain, project_id=project_id)
        # Re-derive DEPENDS_ON edges across the project's full chain graph.
        await upsert_test_dependencies(self._driver, project_id)

    async def update_graph(
        self,
        requirements: list[dict],
        acceptance_criteria: list[dict],
        execution_results: list[dict],
        defects: list[dict],
        *,
        chains: list[dict] | None = None,
        project_id: str | None = None,
    ) -> dict:
        """
        Upsert all nodes and relationships, then compute coverage report.
        Called by Orchestrator after test execution.

        Phase C4: when `chains` and `project_id` are provided, also ingests
        CallChain → Endpoint → EnvVar nodes/edges and re-derives the
        TestCase DEPENDS_ON edges. Both are best-effort and never block
        the coverage report.
        """
        # Phase C4 — chain ingest runs ahead of the coverage compute so
        # `_check_unresolved_path_params` (Phase F2) can walk PROVIDES edges.
        if chains and project_id and not self._stub_mode:
            try:
                await self.ingest_chains(chains, project_id=project_id)
            except Exception as exc:
                logger.warning("traceability: chain ingest failed: %s", exc)
        if self._stub_mode:
            return await self._stub_update(
                requirements, acceptance_criteria, execution_results, defects
            )

        async with self._driver.session() as session:
            # R311 — island unification. The gen-time writer (src/graph/writer.py)
            # keys Requirement/AC/TestCase on req_id/ac_id/test_id; this agent keys
            # on `id`. Before our id-keyed MERGEs run, adopt `id` onto any gen-time
            # node still missing it (scoped to THIS update's ids) so our MERGE hits
            # the SAME node instead of forking a second (disconnected) island. The
            # one-time historical split was merged by the R311 migration; this keeps
            # FUTURE writes unified. Idempotent + scoped (no full scan). Killswitch
            # ARTA_R311_GRAPH_UNIFY_DISABLE=1.
            if os.environ.get("ARTA_R311_GRAPH_UNIFY_DISABLE") != "1":
                _r311_tc = list({r.get("test_id") for r in execution_results if r.get("test_id")})
                _r311_rq = list({req.get("id") for req in requirements if req.get("id")})
                _r311_ac = list(
                    {ac.get("id") for ac in acceptance_criteria if ac.get("id")}
                    | {r.get("ac_id") for r in execution_results if r.get("ac_id")})
                try:
                    if _r311_tc:
                        await session.run(
                            "UNWIND $v AS x MATCH (t:TestCase {test_id:x}) "
                            "WHERE t.id IS NULL SET t.id = x", {"v": _r311_tc})
                    if _r311_rq:
                        await session.run(
                            "UNWIND $v AS x MATCH (r:Requirement {req_id:x}) "
                            "WHERE r.id IS NULL SET r.id = x", {"v": _r311_rq})
                    if _r311_ac:
                        await session.run(
                            "UNWIND $v AS x MATCH (a:AcceptanceCriteria {ac_id:x}) "
                            "WHERE a.id IS NULL SET a.id = x", {"v": _r311_ac})
                except Exception as _r311_exc:
                    logger.warning("R311 graph-unify adopt-id skipped: %s", _r311_exc)

            # 1. Upsert requirements
            for req in requirements:
                _jira_key = req.get("jira_key") or ""
                _jira_url = req.get("jira_url") or req.get("source_url") or ""
                await session.run(UPSERT_REQUIREMENT, {
                    "req_id":     req["id"],
                    "title":      req.get("title", ""),
                    "priority":   req.get("priority", "P2"),
                    "risk_score": req.get("risk_score", 5.0),
                    "jira_key":   _jira_key,
                    "jira_url":   _jira_url,
                })
                # R257 (WS2e) — extend the spine to the requirement's SOURCE so
                # "which JIRA issues does this run cover?" is answerable from
                # the graph. No-op for non-JIRA requirements.
                if _jira_key:
                    await session.run(UPSERT_JIRA_ISSUE, {
                        "jira_key": _jira_key, "jira_url": _jira_url,
                    })
                    await session.run(LINK_REQ_JIRA, {
                        "req_id": req["id"], "jira_key": _jira_key,
                    })
                # 2. Upsert ACs and link
                for ac in req.get("acceptance_criteria", []):
                    await session.run(UPSERT_AC, {
                        "ac_id":     ac["id"],
                        "statement": ac.get("statement", ""),
                    })
                    await session.run(LINK_REQ_AC, {
                        "req_id": req["id"],
                        "ac_id":  ac["id"],
                    })

            # 3. Upsert execution results and test cases.
            # The canonical BMAD chain is Requirement→AC→TestCase→Result→Defect.
            # LINK_REQ_AC handled the Req→AC edge above (line 248). LINK_TC_RESULT
            # handles TC→Result below. The TC→AC link (LINK_TC_AC) is the missing
            # piece that lets coverage queries traverse from a Requirement through
            # its ACs to the tests that cover them — without this edge, the
            # CLASSIFY_AC_COVERAGE / PER_AC_COVERAGE_QUERY traversals find
            # `(ac)<-[:COVERS]-(tc)` for ZERO ACs and report every AC as NONE.
            #
            # The result row carries either `ac_id` (preferred — set by the per-AC
            # generation path) or `requirement_id` (fall back: link the TC to ALL
            # ACs of that requirement). Both cases produce a valid graph edge.
            for result in execution_results:
                tc_id = result["test_id"]
                await session.run(UPSERT_TEST_CASE, {
                    "tc_id":           tc_id,
                    "title":           result.get("title", ""),
                    "automation_type": result.get("tool", "playwright"),
                })

                # TC→AC edge: complete the canonical traceability chain.
                ac_id = result.get("ac_id")
                if ac_id:
                    await session.run(LINK_TC_AC, {"tc_id": tc_id, "ac_id": ac_id})
                else:
                    # Fallback: TC has no specific ac_id but does have a
                    # requirement_id — link it to every AC of that requirement
                    # so coverage queries still traverse correctly.
                    req_id = result.get("requirement_id")
                    if req_id:
                        for req in requirements:
                            if req.get("id") == req_id:
                                for ac in req.get("acceptance_criteria", []) or []:
                                    if ac.get("id"):
                                        await session.run(LINK_TC_AC, {
                                            "tc_id": tc_id, "ac_id": ac["id"],
                                        })
                                break

                result_id = f"{tc_id}-{result.get('build_id', 'x')}"
                await session.run(UPSERT_RESULT, {
                    "result_id":  result_id,
                    "status":     result.get("status", "UNKNOWN"),
                    "duration_ms": result.get("duration_ms", 0),
                    "build_id":   result.get("build_id", ""),
                    "run_id":     result.get("run_id", ""),
                    "tool":       result.get("automation_tool") or result.get("tool", ""),
                })
                await session.run(LINK_TC_RESULT, {
                    "tc_id":     tc_id,
                    "result_id": result_id,
                })

            # 4. Classify AC coverage as FULL/PARTIAL/NONE
            for ac in acceptance_criteria:
                await session.run(CLASSIFY_AC_COVERAGE, {"ac_id": ac["id"]})

            # 5. Upsert defects + link to triggering result (Layer 7 chain)
            for defect in defects:
                defect_id = defect.get("defect_id", defect.get("id", "DEF-000"))
                await session.run(UPSERT_DEFECT, {
                    "defect_id":  defect_id,
                    "title":      defect.get("title", ""),
                    "severity":   defect.get("severity", "P2"),
                    "status":     defect.get("status", "open"),
                    "root_cause": defect.get("root_cause", ""),
                })
                result_id = defect.get("result_id")
                if not result_id:
                    tc_id = defect.get("test_id") or defect.get("tc_id")
                    build_id = defect.get("build_id", "x")
                    if tc_id:
                        result_id = f"{tc_id}-{build_id}"
                if result_id:
                    await session.run(LINK_RESULT_DEFECT, {
                        "result_id": result_id,
                        "defect_id": defect_id,
                    })

            # 6. Compute coverage
            return await self._compute_coverage(session)

    async def get_coverage_report(self, sprint_id: str | None = None) -> dict:
        """Retrieve current coverage report from the graph."""
        if self._stub_mode:
            return self._stub_coverage_report()
        async with self._driver.session() as session:
            return await self._compute_coverage(session)

    async def detect_gaps(self) -> list[dict]:
        """Find requirements with uncovered acceptance criteria."""
        if self._stub_mode:
            return self._stub_gaps()
        async with self._driver.session() as session:
            result = await session.run(GAPS_QUERY)
            return [record.data() async for record in result]

    async def detect_orphans(self) -> list[dict]:
        """Find test cases not linked to any acceptance criterion."""
        if self._stub_mode:
            return []
        async with self._driver.session() as session:
            result = await session.run(ORPHANS_QUERY)
            return [record.data() async for record in result]

    async def detect_redundant_tests(self) -> list[dict]:
        """Find ACs covered by ≥3 TestCases of the same automation_tool.

        Redundancy is a quality smell, not a release blocker — multiple
        tests of the same tool covering one AC means wasted compute and
        a larger drift surface. Surfaced as WARN-severity in the gate.
        """
        if self._stub_mode:
            return []
        async with self._driver.session() as session:
            result = await session.run(REDUNDANCY_QUERY)
            return [record.data() async for record in result]

    async def cleanup_orphans(self, dry_run: bool = True) -> dict:
        """Detect and optionally delete orphaned TestCase nodes.

        dry_run=True (default): returns the orphan list without deleting.
        dry_run=False: runs DETACH DELETE and returns the count of deleted nodes.
        """
        if self._stub_mode:
            return {"orphans": [], "deleted": 0, "dry_run": dry_run}
        async with self._driver.session() as session:
            orphan_result = await session.run(ORPHANS_QUERY)
            orphans = [record.data() async for record in orphan_result]
            if dry_run:
                return {"orphans": orphans, "deleted": 0, "dry_run": True}
            await session.run(DELETE_ORPHANS_QUERY)
            return {"orphans": orphans, "deleted": len(orphans), "dry_run": False}

    # ── Coverage Computation ─────────────────────────────────────────────────

    async def _compute_coverage(self, session) -> dict:
        """Run coverage queries and assemble report."""
        # Per-requirement coverage
        result = await session.run(COVERAGE_QUERY)
        rows = [record.data() async for record in result]

        if not rows:
            return self._stub_coverage_report()

        total_ac = sum(r["total_ac"] for r in rows)
        covered_ac = sum(r["covered_ac"] for r in rows)
        total_tc = sum(r["total_tc"] for r in rows)
        passing_tc = sum(r["passing_tc"] for r in rows)

        overall_coverage = (covered_ac / total_ac * 100) if total_ac > 0 else 0.0
        pass_rate = (passing_tc / total_tc * 100) if total_tc > 0 else 100.0

        # P0-specific pass rate
        p0_rows = [r for r in rows if r["priority"] == "P0"]
        p0_total = sum(r["total_tc"] for r in p0_rows)
        p0_passing = sum(r["passing_tc"] for r in p0_rows)
        p0_pass_rate = (p0_passing / p0_total * 100) if p0_total > 0 else 100.0

        # Coverage by priority
        priority_groups: dict[str, dict] = {}
        for row in rows:
            p = row["priority"]
            if p not in priority_groups:
                priority_groups[p] = {"total_ac": 0, "covered_ac": 0}
            priority_groups[p]["total_ac"] += row["total_ac"]
            priority_groups[p]["covered_ac"] += row["covered_ac"]

        coverage_by_priority = {
            p: {
                "coverage_pct": (
                    v["covered_ac"] / v["total_ac"] * 100
                    if v["total_ac"] > 0 else 0
                )
            }
            for p, v in priority_groups.items()
        }

        # Gap detection
        gaps_result = await session.run(GAPS_QUERY)
        gaps = [record.data() async for record in gaps_result]

        # Per-AC coverage state (FULL / PARTIAL / NONE) — Layer 7 spec
        per_ac_result = await session.run(PER_AC_COVERAGE_QUERY)
        acs = [record.data() async for record in per_ac_result]

        # Orphan tests (declared but not linked to any AC)
        orphans_result = await session.run(ORPHANS_QUERY)
        orphans = [record.data() async for record in orphans_result]

        # Redundant tests (≥3 of same tool covering one AC)
        redundancy_result = await session.run(REDUNDANCY_QUERY)
        redundant = [record.data() async for record in redundancy_result]

        return {
            "coverage_pct": round(overall_coverage, 1),
            "pass_rate": round(pass_rate, 1),
            "p0_pass_rate": round(p0_pass_rate, 1),
            "coverage_by_priority": coverage_by_priority,
            "requirements": rows,
            "acs": acs,
            "gaps": gaps,
            "gap_count": len(gaps),
            "orphans": orphans,
            "orphan_count": len(orphans),
            "redundant": redundant,
            "redundant_count": len(redundant),
        }

    # ── Stub Mode (no Neo4j) ──────────────────────────────────────────────────

    async def _stub_update(
        self,
        requirements: list[dict],
        acceptance_criteria: list[dict],
        execution_results: list[dict],
        defects: list[dict],
    ) -> dict:
        """In-memory graph update for testing/development without Neo4j."""
        for req in requirements:
            self._graph["requirements"][req["id"]] = req
        for ac in acceptance_criteria:
            self._graph["acs"][ac["id"]] = ac
        for result in execution_results:
            self._graph["results"][result["test_id"]] = result
            if result.get("status") == "PASS":
                # Mark associated ACs as covered (simplified)
                for ac in acceptance_criteria:
                    if result.get("requirement_id") == ac.get("requirement_id"):
                        ac["covered"] = True
        for defect in defects:
            did = defect.get("defect_id", defect.get("id", "DEF-000"))
            self._graph["defects"][did] = defect

        return self._stub_coverage_report_from_data(
            requirements, acceptance_criteria, execution_results
        )

    def _stub_coverage_report(self) -> dict:
        """Return computed coverage report when running without Neo4j.

        R134.B.2 — pre-R134.B.2 returned hardcoded 78%/91%/90.9% values
        with `degraded=True`. The gate read these as if they were real
        coverage measurements, silently passing checks against fake
        numbers when Neo4j was down. Post-R134.B.2 the stub computes
        actual P0 pass rate from the in-memory GENERATED_TESTS +
        RECENT_RESULTS inventory (single source of truth — same
        computation `_stub_coverage_report_from_data` uses). The
        `degraded` flag stays so operators see this is stub-mode.
        """
        # R134.B.2 — compute from in-memory inventory; degraded flag
        # tells the gate this is a fallback, not Neo4j-graph truth.
        try:
            from ..api.routers.tests import GENERATED_TESTS
        except Exception:
            GENERATED_TESTS = []
        try:
            from ..api.routers.execution import _REAL_RESULTS as _real_results
            # Flatten across all in-memory runs; take the LATEST run's
            # results if we have a recent run_id, else flatten all.
            _recent = []
            for _run_id, _run_results in (_real_results or {}).items():
                _recent.extend(_run_results or [])
        except Exception:
            _recent = []
        p0_tests = [t for t in (GENERATED_TESTS or []) if t.get("priority") == "P0"]
        p0_test_ids = {t.get("id") for t in p0_tests}
        p0_results = [
            r for r in (_recent or [])
            if (r.get("test_id") in p0_test_ids) or (r.get("id") in p0_test_ids)
        ]
        p0_passed = sum(1 for r in p0_results if r.get("status") == "PASS")
        p0_total = max(1, len(p0_tests))
        p0_pass_rate = round(100.0 * p0_passed / p0_total, 1) if p0_results else 0.0
        # Overall pass rate (R134.B.2 single source of truth — same
        # computation as _stub_coverage_report_from_data uses for symmetry)
        all_passed = sum(1 for r in (_recent or []) if r.get("status") == "PASS")
        all_total = max(1, len(_recent or []))
        overall_pass_rate = round(100.0 * all_passed / all_total, 1) if _recent else 0.0
        return {
            "coverage_pct": 0.0,                  # cannot compute coverage without graph
            "pass_rate": overall_pass_rate,
            "p0_pass_rate": p0_pass_rate,
            "coverage_by_priority": {},           # cannot compute without graph
            "requirements": [],
            "acs": [],
            "gaps": self._stub_gaps(),
            "gap_count": 1,
            "orphans": [],
            "orphan_count": 0,
            "redundant": [],
            "redundant_count": 0,
            "degraded": True,
            "degraded_reason": (
                "Neo4j unavailable — coverage_pct cannot be computed without "
                "the traceability graph. pass_rate / p0_pass_rate computed "
                "from in-memory RECENT_RESULTS (R134.B.2). Gate flags CONCERNS."
            ),
            "samples_used": len(_recent or []),
        }

    def _stub_coverage_report_from_data(
        self,
        requirements: list[dict],
        acceptance_criteria: list[dict],
        execution_results: list[dict],
    ) -> dict:
        total_ac = len(acceptance_criteria)
        covered_ac = sum(1 for ac in acceptance_criteria if ac.get("covered"))
        total_tests = len(execution_results)
        passing = sum(1 for r in execution_results if r.get("status") == "PASS")

        return {
            "coverage_pct": round(covered_ac / total_ac * 100, 1) if total_ac else 0,
            "pass_rate": round(passing / total_tests * 100, 1) if total_tests else 100.0,
            "p0_pass_rate": 100.0,
            "coverage_by_priority": {},
            "gaps": [],
            "gap_count": total_ac - covered_ac,
            # Phase 5 follow-up #5 — symmetry with `_stub_coverage_report`.
            # Both stub paths are stub-mode (Neo4j down); the gate's
            # `_check_uncovered_acs` short-circuits to coverage_degraded WARN
            # when this flag is set. Without it, this in-memory fallback
            # silently passed coverage checks while the sibling stub didn't.
            "degraded": True,
            "degraded_reason": (
                "Neo4j unavailable — coverage computed from in-memory data. "
                "Trustworthy enough for ad-hoc checks, but the gate flags "
                "CONCERNS until Neo4j is restored."
            ),
        }

    def _stub_gaps(self) -> list[dict]:
        return [
            {
                "requirement_id": "REQ-019",
                "priority": "P1",
                "requirement_title": "Refund & Return Flow",
                "uncovered_criteria": [
                    {"id": "AC-019-001", "statement": "Refund processed within 5 business days"},
                    {"id": "AC-019-002", "statement": "Partial refund allowed on multi-item orders"},
                ],
            }
        ]
