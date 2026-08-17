"""G2.5 — Populate Neo4j graph when tests are generated.

The traceability router reads from Neo4j (with PostgreSQL + in-memory fallback);
historically nothing wrote to Neo4j so the graph was always empty and callers
fell through to PG. This module provides idempotent MERGE-based upserts that
run non-blocking (swallow errors) after successful test generation.

Node labels: Requirement, AcceptanceCriteria, TestCase, Run, Defect, Commit,
             Endpoint (Phase C4), EnvVar (Phase C4), CallChain (Phase C4)

Edges: HAS_AC (Requirement → AC), TESTED_BY (Requirement → TestCase),
       VERIFIED_BY (TestCase → Run), DETECTED_BY (Defect → TestCase),
       CAUSED_BY (Defect → Commit),

       Phase C4:
         CONTAINS         (CallChain → Endpoint, with sequence_index)
         OBSERVED_IN      (TestCase → CallChain)
         PROVIDES         (Endpoint → EnvVar, with jsonpath)
         CONSUMES         (Endpoint → EnvVar, with jsonpath)
         DEPENDS_ON       (TestCase → TestCase, with via_var) — derived from
                          chain provider/consumer linkages
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("arta.graph")


# ── Module-level Neo4j driver registry (R215) ────────────────────────────────
# The driver is created once at app startup (main.py) and lives on
# app.state.neo4j. But BACKGROUND tasks and agent modules — discovery_executor,
# architecture_discovery — run WITHOUT a FastAPI request/app reference, so they
# could not reach it and silently skipped every graph write (each project's
# discovery_summary showed `neo4j_written: False`, for ALL SUTs, not just one).
# set_driver() is called at startup; get_driver() lets any module/background task
# reach the SAME driver. SUT-AGNOSTIC — this is the Neo4j connection, never
# project-specific — so it works for any current or future SUT.
_GRAPH_DRIVER: Any = None


def set_driver(driver: Any) -> None:
    """Register the process-wide Neo4j driver (called once at app startup)."""
    global _GRAPH_DRIVER
    _GRAPH_DRIVER = driver


def get_driver() -> Any:
    """Return the process-wide Neo4j driver (or None if Neo4j is unavailable).
    Lets agents / background tasks reach the driver without an app reference."""
    return _GRAPH_DRIVER


# ── B1 (charter conformance) — canonical Endpoint-key helpers ────────────────────
# THE single source of truth for the Neo4j Endpoint node key. Every write-side site
# keys endpoints COLON-joined ("METHOD:/template", e.g. writer.py:295/504); the
# Architecture-Discovery disk graphs key them SPACE-joined ("METHOD /template",
# architecture_discovery._ep_id). That divergence meant arch-discovery graphs could not
# MERGE-match the Endpoint nodes the traceability spine uses. These helpers normalize at
# the write boundary so both subsystems land on the SAME (:Endpoint {endpoint_key}) node.

def endpoint_key(method: str, path: str) -> str:
    """Canonical Endpoint key — COLON-joined, matches every write-side site."""
    return f"{(method or 'GET').upper()}:{path or '/'}"


def normalize_endpoint_key(raw: str) -> str:
    """Accept a display key ('METHOD /path', SPACE) OR a canonical key ('METHOD:/path',
    COLON) and return the canonical COLON form. Idempotent; passes through anything that
    is already colon-formed or has no space."""
    if not raw:
        return raw
    head = raw.split(" ", 1)[0]
    if ":" in head:            # already 'METHOD:...'
        return raw
    method, _, path = raw.partition(" ")
    return f"{method.upper()}:{path or '/'}" if path else raw


async def upsert_test_entries(neo4j_driver: Any, test_entries: list[dict],
                              _retry: bool = True) -> None:
    """G2.5: MERGE each test + its requirement into Neo4j.

    Non-blocking: any Neo4j error is logged and swallowed — test generation must
    not fail because the graph DB is unavailable.
    """
    if neo4j_driver is None or not test_entries:
        return
    try:
        async with neo4j_driver.session() as session:
            for t in test_entries:
                req_id = t.get("requirement_id") or ""
                test_id = t.get("id") or ""
                if not req_id or not test_id:
                    continue
                await session.run("""
                    MERGE (r:Requirement {req_id: $req_id})
                    MERGE (t:TestCase {test_id: $test_id})
                    ON CREATE SET
                        t.title = $title,
                        t.tool = $tool,
                        t.test_type = $test_type,
                        t.tier = $tier,
                        t.analytics_layer = $analytics_layer,
                        t.trace_id = $trace_id,
                        t.model_version = $model_version,
                        t.red_phase_status = $red_phase_status,
                        t.created_at = datetime()
                    ON MATCH SET
                        t.title = $title,
                        t.tool = $tool,
                        t.trace_id = $trace_id,
                        t.model_version = $model_version,
                        t.updated_at = datetime()
                    MERGE (r)-[:TESTED_BY]->(t)
                """, {
                    "req_id": req_id,
                    "test_id": test_id,
                    "title": t.get("title", ""),
                    "tool": t.get("tool", "playwright"),
                    "test_type": t.get("test_type", "UI"),
                    "tier": t.get("tier"),
                    "analytics_layer": t.get("analytics_layer"),
                    "trace_id": t.get("trace_id"),
                    "model_version": t.get("model_version"),
                    "red_phase_status": t.get("red_phase_status", "PENDING_VERIFICATION"),
                })

                # Also link to AC if present
                ac_id = t.get("ac_id")
                if ac_id:
                    try:
                        # R211 B3.2 — write BOTH directions: the canonical
                        # `TestCase-[:COVERS]->AcceptanceCriteria` edge (defined
                        # in schema.cypher but previously NEVER written) plus the
                        # existing AC-[:TESTED_BY]->TestCase.
                        await session.run("""
                            MERGE (a:AcceptanceCriteria {ac_id: $ac_id})
                            MERGE (r:Requirement {req_id: $req_id})
                            MERGE (r)-[:HAS_AC]->(a)
                            MERGE (t:TestCase {test_id: $test_id})
                            MERGE (a)-[:TESTED_BY]->(t)
                            MERGE (t)-[:COVERS]->(a)
                        """, {"ac_id": ac_id, "req_id": req_id, "test_id": test_id})
                    except Exception as exc:
                        log.debug("graph: AC edge upsert failed for %s: %s", ac_id, exc)
        log.info("graph: upserted %d test nodes + edges", len(test_entries))
    except Exception as exc:
        # Neo4j TransientErrors (lock deadlocks when gate-time traceability
        # edges and architecture upserts land concurrently — observed live on
        # all idempotent MERGEs: one backoff retry before giving up.
        if _retry and ("TransientError" in f"{exc}" or "Deadlock" in f"{exc}"):
            import asyncio as _aio_retry
            log.info("graph: transient Neo4j error — retrying upsert_test_entries once")
            await _aio_retry.sleep(0.5)
            return await upsert_test_entries(neo4j_driver, test_entries, _retry=False)
        log.warning("graph: upsert skipped (Neo4j unavailable?): %s", exc)


async def upsert_correction(neo4j_driver: Any, correction: dict) -> None:
    """R320 — link a tester correction into the traceability spine so provenance
    is traceable: (:Correction)-[:CORRECTS]->(:TestCase), -[:FOR_REQUIREMENT]->
    (:Requirement), -[:FOR_AC]->(:AcceptanceCriteria), tagged with the verdict +
    grounding_fact_ref. Idempotent MERGE; non-blocking (no-op without Neo4j)."""
    if neo4j_driver is None or not correction.get("correction_id"):
        return
    try:
        async with neo4j_driver.session() as session:
            await session.run("""
                MERGE (c:Correction {correction_id: $cid})
                SET c.kind = $kind, c.verdict = $verdict, c.corrected_by = $by,
                    c.grounding_fact_ref = $ref, c.correction_text = $text,
                    c.updated_at = datetime()
                FOREACH (_ IN CASE WHEN $test_id IS NULL OR $test_id = '' THEN [] ELSE [1] END |
                    MERGE (t:TestCase {test_id: $test_id})
                    MERGE (c)-[:CORRECTS]->(t))
                FOREACH (_ IN CASE WHEN $req_id IS NULL OR $req_id = '' THEN [] ELSE [1] END |
                    MERGE (r:Requirement {req_id: $req_id})
                    MERGE (c)-[:FOR_REQUIREMENT]->(r))
                FOREACH (_ IN CASE WHEN $ac_id IS NULL OR $ac_id = '' THEN [] ELSE [1] END |
                    MERGE (a:AcceptanceCriteria {ac_id: $ac_id})
                    MERGE (c)-[:FOR_AC]->(a))
            """, {
                "cid": correction.get("correction_id"),
                "kind": correction.get("kind"),
                "verdict": correction.get("verdict"),
                "by": correction.get("corrected_by"),
                "ref": correction.get("grounding_fact_ref"),
                "text": (correction.get("correction_text") or "")[:500],
                "test_id": correction.get("test_id") or "",
                "req_id": correction.get("requirement_id") or "",
                "ac_id": correction.get("ac_id") or "",
            })
        log.info("graph: upserted correction %s", correction.get("correction_id"))
    except Exception as exc:
        log.debug("graph: correction upsert skipped (Neo4j unavailable?): %s", exc)


async def upsert_run_results(
    neo4j_driver: Any,
    run_id: str,
    gate_decision: str,
    started_at: str,
    finished_at: str,
    test_results: list[dict],
    defects: list[dict] | None = None,
) -> None:
    """Fix QQQ (Phase G): write Run + Defect nodes + edges to Neo4j.

    After each completed run, this populates:
        (:TestCase)-[:VERIFIED_BY]->(:Run {run_id, status, gate, started_at, finished_at})
        (:Defect {defect_id, severity})-[:DETECTED_BY]->(:TestCase)
        (:Defect)-[:CAUSED_BY]->(:Commit {sha})  -- when commit is known

    Non-blocking. Idempotent via MERGE.
    """
    if neo4j_driver is None or not run_id:
        return
    try:
        async with neo4j_driver.session() as session:
            # Upsert the Run node
            await session.run("""
                MERGE (run:Run {run_id: $run_id})
                ON CREATE SET
                    run.gate_decision = $gate_decision,
                    run.started_at = $started_at,
                    run.finished_at = $finished_at,
                    run.created_at = datetime()
                ON MATCH SET
                    run.gate_decision = $gate_decision,
                    run.finished_at = $finished_at,
                    run.updated_at = datetime()
            """, {
                "run_id": run_id,
                "gate_decision": gate_decision or "UNKNOWN",
                "started_at": started_at or "",
                "finished_at": finished_at or "",
            })
            # Link each TestCase to the Run via :VERIFIED_BY
            tc_count = 0
            for tr in (test_results or [])[:5000]:
                test_id = tr.get("test_id") or ""
                if not test_id:
                    continue
                try:
                    await session.run("""
                        MATCH (t:TestCase {test_id: $test_id})
                        MATCH (run:Run {run_id: $run_id})
                        MERGE (t)-[r:VERIFIED_BY]->(run)
                        ON CREATE SET r.status = $status, r.duration_ms = $duration_ms
                        ON MATCH SET r.status = $status, r.duration_ms = $duration_ms
                    """, {
                        "test_id": test_id,
                        "run_id": run_id,
                        "status": tr.get("status", "FAIL"),
                        "duration_ms": tr.get("duration_ms", 0),
                    })
                    tc_count += 1
                except Exception:
                    continue
            # Defect nodes + edges
            for d in (defects or []):
                defect_id = d.get("defect_id") or d.get("id") or ""
                test_id = d.get("test_id") or ""
                if not defect_id:
                    continue
                try:
                    await session.run("""
                        MERGE (def:Defect {defect_id: $defect_id})
                        ON CREATE SET
                            def.severity = $severity,
                            def.failure_class = $failure_class,
                            def.title = $title,
                            def.created_at = datetime()
                        ON MATCH SET
                            def.severity = $severity,
                            def.failure_class = $failure_class,
                            def.updated_at = datetime()
                    """, {
                        "defect_id": defect_id,
                        "severity": d.get("severity", "P3"),
                        "failure_class": d.get("failure_class", "unknown"),
                        "title": d.get("title", ""),
                    })
                    if test_id:
                        await session.run("""
                            MATCH (def:Defect {defect_id: $defect_id})
                            MATCH (t:TestCase {test_id: $test_id})
                            MERGE (def)-[:DETECTED_BY]->(t)
                        """, {"defect_id": defect_id, "test_id": test_id})
                    commit_sha = d.get("commit_sha") or d.get("breaker_commit")
                    if commit_sha:
                        await session.run("""
                            MERGE (c:Commit {sha: $sha})
                            MERGE (def:Defect {defect_id: $defect_id})
                            MERGE (def)-[:CAUSED_BY]->(c)
                        """, {"sha": commit_sha, "defect_id": defect_id})
                except Exception:
                    continue
        log.info(
            "graph (QQQ): run=%s gate=%s — linked %d TestCase→Run + %d Defect node(s)",
            run_id, gate_decision, tc_count, len(defects or []),
        )
    except Exception as exc:
        log.warning("graph QQQ: upsert_run_results skipped: %s", exc)


# ── Phase C4 — CallChain ingest ────────────────────────────────────────────


async def upsert_chain(
    neo4j_driver: Any,
    chain: dict,
    *,
    project_id: str,
) -> None:
    """Phase C4: ingest one CallChain into Neo4j.

    The chain dict is the JSON form (from `CallChain.to_dict()`). We MERGE:
      - one Endpoint node per (method, path_template) pair
      - one EnvVar node per (project_id, name) pair
      - one CallChain node per chain_id
      - CONTAINS edges from the chain to each endpoint (with sequence_index)
      - PROVIDES edges from endpoints to env-vars (with jsonpath)
      - CONSUMES edges from endpoints to env-vars
      - OBSERVED_IN edges from any linked TestCase to the chain

    Non-blocking: silently no-op when the driver is unavailable or queries fail
    (Phase 5.3 stub-mode pattern). The orchestrator's `coverage_report["degraded"]`
    flag carries the failure forward to the gate.
    """
    if neo4j_driver is None or not isinstance(chain, dict) or not chain.get("chain_id"):
        return
    try:
        async with neo4j_driver.session() as session:
            # 1. CallChain node
            await session.run(
                """
                MERGE (c:CallChain {chain_id: $chain_id})
                ON CREATE SET
                    c.semantic_hash = $semantic_hash,
                    c.project_id = $project_id,
                    c.captured_at = $captured_at,
                    c.occurrence_count = coalesce($occurrence_count, 1),
                    c.source_har = $source_har,
                    c.created_at = datetime()
                ON MATCH SET
                    c.semantic_hash = $semantic_hash,
                    c.occurrence_count = coalesce(c.occurrence_count, 0) + 1,
                    c.last_observed_at = $captured_at,
                    c.updated_at = datetime()
                """,
                {
                    "chain_id": chain["chain_id"],
                    "semantic_hash": chain.get("semantic_hash", ""),
                    "project_id": project_id,
                    "captured_at": chain.get("captured_at", ""),
                    "occurrence_count": chain.get("occurrence_count", 1),
                    "source_har": chain.get("source_har", ""),
                },
            )

            # 2. Endpoint nodes + CONTAINS edges + PROVIDES/CONSUMES edges
            nodes = chain.get("nodes") or []
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                endpoint_key = f"{n.get('method', 'GET')}:{n.get('path_template', '/')}"
                seq_idx = int(n.get("sequence_index") or 0)
                await session.run(
                    """
                    MERGE (e:Endpoint {endpoint_key: $endpoint_key})
                    ON CREATE SET
                        e.method = $method,
                        e.path_template = $path_template,
                        e.created_at = datetime()
                    ON MATCH SET
                        e.last_seen = datetime()
                    WITH e
                    MATCH (c:CallChain {chain_id: $chain_id})
                    MERGE (c)-[r:CONTAINS]->(e)
                    ON CREATE SET r.sequence_index = $seq_idx
                    ON MATCH SET r.sequence_index = $seq_idx
                    """,
                    {
                        "endpoint_key": endpoint_key,
                        "method": n.get("method", "GET"),
                        "path_template": n.get("path_template", "/"),
                        "chain_id": chain["chain_id"],
                        "seq_idx": seq_idx,
                    },
                )

                # PROVIDES edges
                for var_name, jsonpath in (n.get("provides") or {}).items():
                    await session.run(
                        """
                        MERGE (v:EnvVar {project_id: $project_id, name: $name})
                        WITH v
                        MATCH (e:Endpoint {endpoint_key: $endpoint_key})
                        MERGE (e)-[r:PROVIDES]->(v)
                        ON CREATE SET r.jsonpath = $jsonpath
                        ON MATCH SET r.jsonpath = $jsonpath
                        """,
                        {
                            "project_id": project_id,
                            "name": var_name,
                            "endpoint_key": endpoint_key,
                            "jsonpath": str(jsonpath),
                        },
                    )

                # CONSUMES edges (jsonpath here is just the "_consumed_at" marker;
                # the actual provider linkage is captured by DEPENDS_ON below)
                for var_name in (n.get("consumes") or {}).keys():
                    await session.run(
                        """
                        MERGE (v:EnvVar {project_id: $project_id, name: $name})
                        WITH v
                        MATCH (e:Endpoint {endpoint_key: $endpoint_key})
                        MERGE (e)-[r:CONSUMES]->(v)
                        ON CREATE SET r.first_seen = datetime()
                        """,
                        {
                            "project_id": project_id,
                            "name": var_name,
                            "endpoint_key": endpoint_key,
                        },
                    )

            # 3. OBSERVED_IN edge from the source test (if any)
            source_test_id = chain.get("source_test_id")
            if source_test_id:
                await session.run(
                    """
                    MATCH (t:TestCase {test_id: $test_id})
                    MATCH (c:CallChain {chain_id: $chain_id})
                    MERGE (t)-[:OBSERVED_IN]->(c)
                    """,
                    {"test_id": source_test_id, "chain_id": chain["chain_id"]},
                )

        log.info(
            "graph C4: upserted chain=%s nodes=%d project=%s",
            chain["chain_id"], len(nodes), project_id,
        )
    except Exception as exc:
        log.warning("graph C4: chain upsert skipped: %s", exc)


async def upsert_test_dependencies(
    neo4j_driver: Any,
    project_id: str,
    *,
    chains: list[dict] | None = None,
) -> None:
    """Phase C4: derive (TestCase)-[:DEPENDS_ON]->(TestCase) edges from chain
    provider/consumer linkages.

    Two TestCases are linked when one's chain provides an env var that the
    other's chain consumes. The derivation runs in Cypher so it works against
    the graph state (vs. needing all chains in memory at the same time).

    Idempotent: re-running won't duplicate edges; we MERGE on `via_var`.
    """
    if neo4j_driver is None:
        return
    try:
        async with neo4j_driver.session() as session:
            await session.run(
                """
                MATCH (t1:TestCase)-[:OBSERVED_IN]->(c1:CallChain)-[:CONTAINS]->(e1:Endpoint)-[p:PROVIDES]->(v:EnvVar {project_id: $project_id})
                MATCH (t2:TestCase)-[:OBSERVED_IN]->(c2:CallChain)-[:CONTAINS]->(e2:Endpoint)-[:CONSUMES]->(v)
                WHERE t1 <> t2
                MERGE (t2)-[r:DEPENDS_ON {via_var: v.name}]->(t1)
                ON CREATE SET r.created_at = datetime()
                """,
                {"project_id": project_id},
            )
        log.info("graph C4: refreshed DEPENDS_ON edges for project %s", project_id)
    except Exception as exc:
        log.warning("graph C4: DEPENDS_ON derivation skipped: %s", exc)


async def upsert_architecture_graphs(
    neo4j_driver: Any,
    *,
    project_id: str,
    graphs: dict,
) -> None:
    """Phase AD: mirror the Architecture Discovery graphs into Neo4j.

    MERGEs Service / Token / Claim / Scenario / Step nodes and their edges,
    and reuses the existing Endpoint nodes (by endpoint_key) for api/dependency
    linkage. Non-blocking: no-ops when the driver is unavailable or a query
    fails (same stub-mode contract as `upsert_chain`). Disk artifacts under
    `.arta/architecture_discovery/<pid>/` remain the source of truth.
    """
    if neo4j_driver is None or not isinstance(graphs, dict):
        return
    try:
        async with neo4j_driver.session() as session:
            # Services (architecture map)
            for n in (graphs.get("architecture_map", {}) or {}).get("nodes", []):
                if n.get("kind") != "service":
                    continue
                await session.run(
                    """
                    MERGE (s:Service {project_id: $project_id, repo: $repo})
                    ON CREATE SET s.name = $name, s.created_at = datetime()
                    ON MATCH SET s.updated_at = datetime()
                    """,
                    {"project_id": project_id, "repo": n.get("name"), "name": n.get("name")},
                )
            # Tokens + derives_from (auth graph)
            for n in (graphs.get("auth_graph", {}) or {}).get("nodes", []):
                if n.get("kind") == "token":
                    await session.run(
                        """
                        MERGE (t:Token {project_id: $project_id, kind: $kind})
                        ON CREATE SET t.source = $source, t.ttl_min = $ttl, t.created_at = datetime()
                        ON MATCH SET t.source = $source, t.ttl_min = $ttl, t.updated_at = datetime()
                        """,
                        {"project_id": project_id, "kind": n.get("name"),
                         "source": n.get("source"), "ttl": n.get("ttl_min")},
                    )
            for e in (graphs.get("auth_graph", {}) or {}).get("edges", []):
                if e.get("kind") == "derives_from":
                    await session.run(
                        """
                        MATCH (a:Token {project_id: $project_id, kind: $from_kind})
                        MATCH (b:Token {project_id: $project_id, kind: $to_kind})
                        MERGE (a)-[r:DERIVES_FROM]->(b)
                        ON CREATE SET r.via = $via, r.ttl_min = $ttl, r.created_at = datetime()
                        """,
                        {"project_id": project_id,
                         "from_kind": str(e.get("from", "")).replace("token:", ""),
                         "to_kind": str(e.get("to", "")).replace("token:", ""),
                         "via": e.get("via"), "ttl": e.get("ttl_min")},
                    )
            # Scenarios + steps (workflow graph)
            for n in (graphs.get("workflow_graph", {}) or {}).get("nodes", []):
                if n.get("kind") == "scenario":
                    await session.run(
                        """
                        MERGE (w:Scenario {scenario_id: $sid})
                        ON CREATE SET w.title = $title, w.feature = $feature,
                                      w.project_id = $project_id, w.created_at = datetime()
                        """,
                        {"sid": f"{project_id}:{n.get('id')}", "title": n.get("title"),
                         "feature": n.get("feature"), "project_id": project_id},
                    )
            # B3 (charter conformance) — persist api_graph Endpoint nodes MERGEd on the
            # CANONICAL (colon) endpoint_key so arch-discovery joins the SAME Endpoint
            # nodes the traceability spine walks (the space-vs-colon fix's payoff), and
            # enrich them with protocol / auth metadata. The disk api_graph keys nodes
            # SPACE-joined ("METHOD /path"); normalize_endpoint_key converts to the colon
            # form. dependency_graph + data_flow_graph stay disk-only (already
            # reconstructable from Neo4j DEPENDS_ON / PROVIDES / CONSUMES).
            for n in (graphs.get("api_graph", {}) or {}).get("nodes", []):
                if n.get("kind") != "endpoint":
                    continue
                _ek = normalize_endpoint_key(n.get("id") or "")
                if not _ek:
                    continue
                await session.run(
                    """
                    MERGE (e:Endpoint {endpoint_key: $ek})
                    ON CREATE SET e.method = $method, e.path_template = $path,
                                  e.protocol = $protocol, e.project_id = $project_id,
                                  e.created_at = datetime()
                    ON MATCH SET e.protocol = coalesce($protocol, e.protocol),
                                 e.method = coalesce($method, e.method),
                                 e.path_template = coalesce(e.path_template, $path),
                                 e.updated_at = datetime()
                    """,
                    {"ek": _ek, "method": n.get("method"), "path": n.get("path"),
                     "protocol": n.get("protocol"), "project_id": project_id},
                )
        log.info("graph AD: upserted architecture graphs for project %s", project_id)
    except Exception as exc:
        log.warning("graph AD: architecture-graph upsert skipped: %s", exc)


async def upsert_requirement_implements(
    neo4j_driver: Any,
    *,
    project_id: str,
    req_id: str,
    endpoint_keys: list[str],
) -> None:
    """Phase 3: persist Requirement-[:IMPLEMENTED_BY]->Endpoint edges so a
    test's exercised endpoints can be traced back to the requirement's
    implementing code. Reuses the Endpoint nodes from schema_phase_c. Non-
    blocking: no-ops when the driver is None or a query fails."""
    if neo4j_driver is None or not req_id or not endpoint_keys:
        return
    try:
        async with neo4j_driver.session() as session:
            for ek in endpoint_keys[:200]:
                await session.run(
                    """
                    MERGE (r:Requirement {req_id: $req_id})
                    MERGE (e:Endpoint {endpoint_key: $ek})
                    MERGE (r)-[rel:IMPLEMENTED_BY]->(e)
                    ON CREATE SET rel.project_id = $project_id, rel.created_at = datetime()
                    """,
                    {"req_id": req_id, "ek": ek, "project_id": project_id},
                )
        log.info("graph P3: IMPLEMENTED_BY edges for %s (%d endpoints)", req_id, len(endpoint_keys))
    except Exception as exc:
        log.warning("graph P3: IMPLEMENTED_BY upsert skipped: %s", exc)


async def upsert_traceability_edges(
    neo4j_driver: Any,
    *,
    project_id: str,
    req_id: str,
    mapped_endpoint_keys: list[str] | None = None,
    test_exercises: dict[str, list[str]] | None = None,
    fe_be_links: list[dict] | None = None,
) -> None:
    """R211 B3.2 — persist the grounded traceability spine edges at GEN time
    (the writers existed; the spine was just never wired):
      • Requirement-[:IMPLEMENTED_BY]->Endpoint   (mapped real endpoints)
      • TestCase-[:EXERCISES]->Endpoint           (endpoints each test touches)
      • FrontendRoute-[:INVOKES]->Endpoint        (the FE→BE Code→API link that
        `_r156_a_3_link_fe_to_be_routes` computes then DISCARDS)
    `test_exercises` = {test_id: [endpoint_key, ...]}; endpoint_key = "METHOD:/template".
    Non-blocking: no-ops when the driver is unavailable or a query fails.
    """
    if neo4j_driver is None or not req_id:
        return
    try:
        if mapped_endpoint_keys:
            await upsert_requirement_implements(
                neo4j_driver, project_id=project_id, req_id=req_id,
                endpoint_keys=list(mapped_endpoint_keys))
        async with neo4j_driver.session() as session:
            for test_id, eks in (test_exercises or {}).items():
                for ek in (eks or [])[:100]:
                    await session.run(
                        """
                        MERGE (t:TestCase {test_id: $test_id})
                        MERGE (e:Endpoint {endpoint_key: $ek})
                        MERGE (t)-[rel:EXERCISES]->(e)
                        ON CREATE SET rel.created_at = datetime()
                        """,
                        {"test_id": test_id, "ek": ek},
                    )
            for link in (fe_be_links or [])[:200]:
                fe = link.get("fe_route") or link.get("route") or link.get("url")
                ek = link.get("endpoint_key")
                if not ek and link.get("method") and (link.get("path") or link.get("url")):
                    ek = f"{link.get('method')}:{link.get('path') or link.get('url')}"
                if not fe or not ek:
                    continue
                # Neo4j relationship properties MUST be primitives — stringify a
                # whole INVOKES write throws and is silently swallowed.
                _auth = link.get("auth_requirement")
                if isinstance(_auth, (dict, list)):
                    import json as _json
                    _auth = _json.dumps(_auth, sort_keys=True)
                else:
                    _auth = str(_auth or "")
                await session.run(
                    """
                    MERGE (f:FrontendRoute {route: $fe, project_id: $project_id})
                    MERGE (e:Endpoint {endpoint_key: $ek})
                    MERGE (f)-[rel:INVOKES]->(e)
                    ON CREATE SET rel.created_at = datetime(),
                                  rel.auth_requirement = $auth,
                                  rel.body_schema_source = $body
                    """,
                    {"fe": fe, "ek": ek, "project_id": project_id,
                     "auth": _auth,
                     "body": (link.get("body_schema_source") or "")[:2000]},
                )
        log.info("graph B3.2: traceability edges for %s (impl=%d, exercises=%d tests, invokes=%d)",
                 req_id, len(mapped_endpoint_keys or []), len(test_exercises or {}),
                 len(fe_be_links or []))
    except Exception as exc:
        log.warning("graph B3.2: traceability edge upsert skipped: %s", exc)


# ── WS1 — full-chain traceability writers ────────────────────────────────────
# Populate the deck's COMPLETE chain: Requirement → RiskProfile → DatasetRecipe
# → Fixture → RecipeVerifier → Scenario → Spec → Execution(Run). ALL key on the
# `*_id` convention used above so they JOIN the existing Requirement{req_id} /
# TestCase{test_id} / AcceptanceCriteria{ac_id} nodes (NOT the {id} convention
# used by the post-run/read path — those would create disconnected nodes).
# Async + MERGE (idempotent) + error-swallowing; callers gate on
# ARTA_TRACE_FULL_CHAIN_DISABLE.

async def upsert_requirement_profile(neo4j_driver: Any, req_id: str, profile: dict) -> None:
    """(:Requirement {req_id})-[:HAS_PROFILE]->(:RiskProfile {profile_id})."""
    import json as _json
    if neo4j_driver is None or not req_id or not isinstance(profile, dict):
        return
    try:
        async with neo4j_driver.session() as session:
            await session.run("""
                MERGE (r:Requirement {req_id: $req_id})
                MERGE (p:RiskProfile {profile_id: $profile_id})
                ON CREATE SET p.risk_score=$risk_score, p.priority=$priority,
                    p.risk_action=$risk_action, p.test_types=$test_types,
                    p.recommended_tools=$recommended_tools, p.created_at=datetime()
                ON MATCH SET p.risk_score=$risk_score, p.priority=$priority,
                    p.test_types=$test_types, p.recommended_tools=$recommended_tools,
                    p.updated_at=datetime()
                MERGE (r)-[:HAS_PROFILE]->(p)
            """, {
                "req_id": req_id,
                "profile_id": f"{req_id}:profile",  # distinct from the Requirement node id
                "risk_score": profile.get("risk_score"),
                "priority": profile.get("priority", ""),
                "risk_action": profile.get("risk_action", ""),
                "test_types": _json.dumps(profile.get("test_types") or []),
                "recommended_tools": _json.dumps(profile.get("recommended_tools") or []),
            })
        log.info("graph WS1: profile for %s", req_id)
    except Exception as exc:
        log.warning("graph WS1: profile upsert skipped: %s", exc)


async def upsert_recipe_chain(neo4j_driver: Any, req_id: str, recipe: dict) -> None:
    """RiskProfile-[:HAS_RECIPE]->DatasetRecipe-[:MATERIALISES_TO]->Fixture +
    DatasetRecipe-[:VERIFIED_BY_RECIPE]->RecipeVerifier. From a recipe dict
    (.arta/recipes/*.json or DatasetRecipe.model_dump())."""
    if neo4j_driver is None or not req_id or not isinstance(recipe, dict):
        return
    version = str(recipe.get("version") or "1.0.0")
    recipe_id = f"{req_id}:v{version}"
    canonical = recipe.get("canonical_path") or ""
    row_count = recipe.get("row_count")
    try:
        async with neo4j_driver.session() as session:
            await session.run("""
                MERGE (r:Requirement {req_id: $req_id})
                MERGE (rec:DatasetRecipe {recipe_id: $recipe_id})
                ON CREATE SET rec.version=$version, rec.canonical_path=$canonical,
                    rec.row_count=$row_count, rec.expected_output_count=$eoc,
                    rec.measurable_count=$mc, rec.created_at=datetime()
                ON MATCH SET rec.version=$version, rec.canonical_path=$canonical,
                    rec.row_count=$row_count, rec.expected_output_count=$eoc,
                    rec.measurable_count=$mc, rec.updated_at=datetime()
                WITH r, rec
                OPTIONAL MATCH (r)-[:HAS_PROFILE]->(p:RiskProfile)
                FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | MERGE (p)-[:HAS_RECIPE]->(rec))
                FOREACH (_ IN CASE WHEN p IS NULL THEN [1] ELSE [] END | MERGE (r)-[:HAS_RECIPE]->(rec))
            """, {"req_id": req_id, "recipe_id": recipe_id, "version": version,
                  "canonical": canonical, "row_count": row_count,
                  "eoc": len(recipe.get("expected_outputs") or {}),
                  "mc": len(recipe.get("measurable_inputs") or [])})
            if canonical:
                await session.run("""
                    MERGE (rec:DatasetRecipe {recipe_id: $recipe_id})
                    MERGE (fx:Fixture {fixture_path: $canonical})
                    ON CREATE SET fx.row_count=$row_count, fx.version=$version, fx.created_at=datetime()
                    ON MATCH SET fx.row_count=$row_count, fx.version=$version
                    MERGE (rec)-[:MATERIALISES_TO]->(fx)
                """, {"recipe_id": recipe_id, "canonical": canonical, "row_count": row_count, "version": version})
            await session.run("""
                MERGE (rec:DatasetRecipe {recipe_id: $recipe_id})
                MERGE (v:RecipeVerifier {verifier_id: $verifier_id})
                ON CREATE SET v.attempts=$attempts, v.strategy=$strategy, v.failed=$failed, v.created_at=datetime()
                ON MATCH SET v.attempts=$attempts, v.strategy=$strategy, v.failed=$failed, v.updated_at=datetime()
                MERGE (rec)-[:VERIFIED_BY_RECIPE]->(v)
            """, {"recipe_id": recipe_id, "verifier_id": f"{recipe_id}:verify",
                  "attempts": recipe.get("verification_attempts"),
                  "strategy": recipe.get("verification_strategy", ""),
                  "failed": bool(recipe.get("verification_failed"))})

            # ── T5 — AC-traceability spine ──────────────────────────────────────
            # Persist the recipe's Columns + ExpectedOutputs + Measurables as nodes and
            # wire AcceptanceCriteria -[:MEASURED_BY]-> Measurable -[:GROUNDS]->
            # ExpectedOutput so every {dataset, question, ground-truth} traces to a
            # SPECIFIC AC measurable — the Requirement→AC→measurable→column→expected chain
            # GROUNDED, not just structural. Best-effort; skips missing pieces.
            cols = [c for c in (recipe.get("columns") or [])
                    if isinstance(c, dict) and c.get("name")]
            if cols:
                await session.run("""
                    MERGE (rec:DatasetRecipe {recipe_id: $recipe_id})
                    WITH rec UNWIND $cols AS col
                    MERGE (c:Column {column_id: $recipe_id + ':' + col.name})
                    ON CREATE SET c.name=col.name, c.dtype=col.dtype, c.shape=col.shape, c.created_at=datetime()
                    ON MATCH SET c.dtype=col.dtype, c.shape=col.shape
                    MERGE (rec)-[:HAS_COLUMN]->(c)
                """, {"recipe_id": recipe_id, "cols": [
                    {"name": c.get("name"), "dtype": c.get("dtype") or "",
                     "shape": (c.get("distribution") or {}).get("shape") or ""} for c in cols]})

            expected = recipe.get("expected_outputs") or {}
            oracle_keys = set(recipe.get("t3_computed_expected_keys") or [])
            if expected:
                await session.run("""
                    MERGE (rec:DatasetRecipe {recipe_id: $recipe_id})
                    WITH rec UNWIND $outs AS o
                    MERGE (e:ExpectedOutput {expected_id: $recipe_id + ':' + o.key})
                    ON CREATE SET e.key=o.key, e.value=o.value, e.oracle_backed=o.oracle, e.created_at=datetime()
                    ON MATCH SET e.value=o.value, e.oracle_backed=o.oracle
                    MERGE (rec)-[:HAS_EXPECTED]->(e)
                """, {"recipe_id": recipe_id, "outs": [
                    {"key": str(k), "value": str(v), "oracle": (str(k) in oracle_keys)}
                    for k, v in expected.items()]})

            measurables = [m for m in (recipe.get("measurable_inputs") or []) if isinstance(m, dict)]
            if measurables:
                ms_param = [{
                    "mid": f"{recipe_id}:m{i}", "value": str(m.get("value")),
                    "unit": m.get("unit") or "", "comparator": m.get("comparator") or "",
                    "property": (m.get("property") or "")} for i, m in enumerate(measurables)]
                await session.run("""
                    MERGE (r:Requirement {req_id: $req_id})
                    MERGE (rec:DatasetRecipe {recipe_id: $recipe_id})
                    WITH r, rec UNWIND $ms AS m
                    MERGE (meas:Measurable {measurable_id: m.mid})
                    ON CREATE SET meas.value=m.value, meas.unit=m.unit, meas.comparator=m.comparator,
                        meas.property=m.property, meas.created_at=datetime()
                    ON MATCH SET meas.value=m.value, meas.unit=m.unit, meas.comparator=m.comparator, meas.property=m.property
                    MERGE (rec)-[:HAS_MEASURABLE]->(meas)
                    WITH r, rec, meas, m
                    OPTIONAL MATCH (r)-[:HAS_AC]->(a:AcceptanceCriteria)
                    FOREACH (_ IN CASE WHEN a IS NULL THEN [] ELSE [1] END | MERGE (a)-[:MEASURED_BY]->(meas))
                    WITH rec, meas, m
                    OPTIONAL MATCH (rec)-[:HAS_EXPECTED]->(e:ExpectedOutput)
                        WHERE m.property <> '' AND e.key CONTAINS m.property
                    FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END | MERGE (meas)-[:GROUNDS]->(e))
                """, {"req_id": req_id, "recipe_id": recipe_id, "ms": ms_param})
        log.info("graph WS1: recipe chain for %s", req_id)
    except Exception as exc:
        log.warning("graph WS1: recipe chain upsert skipped: %s", exc)


async def upsert_spec_files(neo4j_driver: Any, req_id: str, specs: list[dict]) -> None:
    """Spec{spec_path,framework} + (Requirement)-[:HAS_SPEC]->(Spec) +
    Fixture-[:USED_BY]->Spec (when a fixture exists) + Spec-[:GENERATES]->TestCase.
    specs: [{spec_path, framework, test_id?}]."""
    if neo4j_driver is None or not req_id or not specs:
        return
    try:
        async with neo4j_driver.session() as session:
            for s in specs:
                spec_path = s.get("spec_path") or ""
                if not spec_path:
                    continue
                await session.run("""
                    MERGE (r:Requirement {req_id: $req_id})
                    MERGE (spec:Spec {spec_path: $spec_path})
                    ON CREATE SET spec.framework=$framework, spec.created_at=datetime()
                    ON MATCH SET spec.framework=$framework
                    MERGE (r)-[:HAS_SPEC]->(spec)
                    WITH r, spec
                    OPTIONAL MATCH (r)-[:HAS_PROFILE]->(:RiskProfile)-[:HAS_RECIPE]->(:DatasetRecipe)-[:MATERIALISES_TO]->(fx:Fixture)
                    FOREACH (_ IN CASE WHEN fx IS NULL THEN [] ELSE [1] END | MERGE (fx)-[:USED_BY]->(spec))
                """, {"req_id": req_id, "spec_path": spec_path, "framework": s.get("framework", "")})
                test_id = s.get("test_id")
                if test_id:
                    await session.run("""
                        MATCH (spec:Spec {spec_path: $spec_path})
                        MATCH (t:TestCase {test_id: $test_id})
                        MERGE (spec)-[:GENERATES]->(t)
                    """, {"spec_path": spec_path, "test_id": test_id})
        log.info("graph WS1: %d spec node(s) for %s", len(specs), req_id)
    except Exception as exc:
        log.warning("graph WS1: spec upsert skipped: %s", exc)


async def upsert_scenarios(neo4j_driver: Any, req_id: str, test_entries: list[dict]) -> None:
    """Scenario{scenario_id,kind} + (Requirement)-[:HAS_SCENARIO]->(Scenario)-
    [:REALISED_BY]->(TestCase). kind ∈ {happy_path, analytics, adversarial}."""
    if neo4j_driver is None or not req_id or not test_entries:
        return

    def _kind(t: dict) -> str:
        if t.get("adversarial_category"):
            return "adversarial"
        if t.get("analytics_layer") or t.get("tier"):
            return "analytics"
        return "happy_path"

    try:
        async with neo4j_driver.session() as session:
            for t in test_entries:
                test_id = t.get("id") or t.get("test_id") or ""
                if not test_id:
                    continue
                kind = _kind(t)
                ac = t.get("ac_id") or ""
                sid = f"{req_id}:{ac or test_id}:{kind}"
                await session.run("""
                    MERGE (r:Requirement {req_id: $req_id})
                    MERGE (s:Scenario {scenario_id: $sid})
                    ON CREATE SET s.kind=$kind, s.ac_id=$ac, s.created_at=datetime()
                    MERGE (r)-[:HAS_SCENARIO]->(s)
                    MERGE (t:TestCase {test_id: $test_id})
                    MERGE (s)-[:REALISED_BY]->(t)
                """, {"req_id": req_id, "sid": sid, "kind": kind, "ac": ac, "test_id": test_id})
        log.info("graph WS1: scenarios for %s", req_id)
    except Exception as exc:
        log.warning("graph WS1: scenario upsert skipped: %s", exc)


async def upsert_spec_execution_edges(neo4j_driver: Any, run_id: str,
                                      req_slugs: "list[str] | None" = None) -> None:
    """(Spec)-[:EXECUTED_AS]->(Run). Two strategies:
    1. precise — Spec-GENERATES->TestCase-VERIFIED_BY->Run (when result test_ids
       align with TestCase ids);
    2. slug fallback — execution-result test_ids are collection-item-derived
       (e.g. `API-req_am_001_chain_2...`), NOT TestCase ids, so the precise join
       usually misses. Link Spec→Run by the `req_am_NNN` slug shared by the
       result id AND the Spec.spec_path. Pass the run's distinct req-slugs."""
    if neo4j_driver is None or not run_id:
        return
    try:
        async with neo4j_driver.session() as session:
            await session.run("""
                MATCH (run:Run {run_id: $run_id})
                MATCH (spec:Spec)-[:GENERATES]->(:TestCase)-[:VERIFIED_BY]->(run)
                MERGE (spec)-[:EXECUTED_AS]->(run)
            """, {"run_id": run_id})
            for slug in (req_slugs or []):
                if not slug:
                    continue
                await session.run("""
                    MATCH (run:Run {run_id: $run_id})
                    MATCH (spec:Spec) WHERE spec.spec_path CONTAINS $slug
                    MERGE (spec)-[:EXECUTED_AS]->(run)
                """, {"run_id": run_id, "slug": slug})
        log.info("graph WS1: spec→execution edges for run %s (slugs=%d)", run_id, len(req_slugs or []))
    except Exception as exc:
        log.warning("graph WS1: spec-execution edge upsert skipped: %s", exc)
