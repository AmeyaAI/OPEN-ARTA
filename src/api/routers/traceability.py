"""ARTA Traceability Router — Graph data for D3.js visualization."""
from __future__ import annotations

import logging
import os

log = logging.getLogger("arta.traceability")

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
# F4-1: API-key check is centralised in src/api/dependencies.py.
from ..dependencies import require_api_key as _require_api_key  # noqa: E402


# Node types and colors for the frontend
NODE_COLORS = {
    "requirement": "#6366f1",   # indigo
    "acceptance_criteria": "#8b5cf6",  # violet
    "test_case": "#06b6d4",     # cyan
    "result": "#10b981",        # green (pass) / #ef4444 (fail)
    "defect": "#ef4444",        # red
    # WS1 — full-chain stages (deck's Req→RiskProfile→Recipe→Fixture→Verifier→
    # Scenario→TestCase→Spec→Execution→Defect).
    "risk_profile": "#f59e0b",      # amber
    "dataset_recipe": "#14b8a6",    # teal
    "fixture": "#0ea5e9",           # sky
    "recipe_verifier": "#a78bfa",   # light violet
    "scenario": "#ec4899",          # pink
    "spec": "#22d3ee",              # bright cyan
    "execution": "#10b981",         # green (Run)
}


@router.get("/trace/{trace_id}", dependencies=[Depends(_require_api_key)])
async def get_trace_cohort(trace_id: str, request: Request):
    """F1-4: Return all tests created in a single generation request (by trace_id).

    Used by the Traceability UI to visualise the cohort of tests produced in one
    pipeline run — helps debug model/prompt regressions by comparing across
    traces from different model_version or prompt_version values.
    """
    # Prefer DB (survives restart). Fall back to in-memory GENERATED_TESTS.
    tests: list[dict] = []
    try:
        from ..db.session import async_session_factory
        from sqlalchemy import text as _text
        async with async_session_factory() as session:
            rows = (await session.execute(_text("""
                SELECT test_id, title, priority, test_type::text, automation_tool::text,
                       trace_id::text, model_version, prompt_version, red_phase_status,
                       judge_score, dataset_version, analytics_layer, tier,
                       adversarial_input, error_message, generation_source,
                       last_status::text, avg_duration_ms, created_at, requirement_id
                FROM test_cases
                WHERE trace_id = CAST(:tid AS uuid)
                ORDER BY analytics_layer, tier, test_id
            """), {"tid": trace_id})).fetchall()
            for r in rows:
                tests.append({
                    "id": r[0], "title": r[1], "priority": r[2],
                    "test_type": r[3], "automation_tool": r[4],
                    "trace_id": r[5], "model_version": r[6], "prompt_version": r[7],
                    "red_phase_status": r[8],
                    "judge_score": float(r[9]) if r[9] is not None else None,
                    "dataset_version": r[10], "analytics_layer": r[11], "tier": r[12],
                    "adversarial_input": r[13], "error_message": r[14],
                    "generation_source": r[15], "last_status": r[16],
                    "avg_duration_ms": r[17],
                    "created_at": r[18].isoformat() if r[18] else None,
                    "requirement_id": str(r[19]) if r[19] else None,
                })
    except Exception as exc:
        log.debug("trace cohort DB lookup failed, falling back to memory: %s", exc)

    if not tests:
        try:
            from .tests import GENERATED_TESTS
            for t in GENERATED_TESTS:
                if t.get("trace_id") == trace_id:
                    tests.append({
                        "id": t.get("id"),
                        "title": t.get("title"),
                        "priority": t.get("priority"),
                        "test_type": t.get("test_type"),
                        "automation_tool": t.get("automation_tool") or t.get("tool"),
                        "trace_id": t.get("trace_id"),
                        "model_version": t.get("model_version"),
                        "prompt_version": t.get("prompt_version"),
                        "red_phase_status": t.get("red_phase_status"),
                        "judge_score": t.get("judge_score"),
                        "dataset_version": (t.get("fixture") or {}).get("checksum") if isinstance(t.get("fixture"), dict) else None,
                        "analytics_layer": t.get("analytics_layer"),
                        "tier": t.get("tier"),
                        "adversarial_input": t.get("adversarial_input"),
                        "error_message": t.get("error_message"),
                        "generation_source": t.get("generation_source"),
                        "last_status": t.get("status"),
                        "avg_duration_ms": t.get("duration_ms"),
                        "requirement_id": t.get("requirement_id"),
                    })
        except Exception as exc:
            log.debug("trace cohort memory lookup failed: %s", exc)

    if not tests:
        raise HTTPException(status_code=404, detail=f"No tests found with trace_id={trace_id}")

    # Derive cohort-level metadata
    first = tests[0]
    by_tool: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    judge_scores: list[float] = []
    for t in tests:
        tool = t.get("automation_tool") or "unknown"
        by_tool[tool] = by_tool.get(tool, 0) + 1
        layer = t.get("analytics_layer")
        if layer:
            by_layer[layer] = by_layer.get(layer, 0) + 1
        js = t.get("judge_score")
        if isinstance(js, (int, float)):
            judge_scores.append(float(js))

    return {
        "trace_id": trace_id,
        "requirement_id": first.get("requirement_id"),
        "model_version": first.get("model_version"),
        "prompt_version": first.get("prompt_version"),
        "total_tests": len(tests),
        "by_tool": by_tool,
        "by_analytics_layer": by_layer,
        "judge_score_avg": round(sum(judge_scores) / len(judge_scores), 3) if judge_scores else None,
        "judge_score_count": len(judge_scores),
        "red_phase_summary": {
            status: sum(1 for t in tests if t.get("red_phase_status") == status)
            for status in ("RED", "GREEN_UNEXPECTED", "ERROR", "PENDING_VERIFICATION")
        },
        "tests": tests,
    }


@router.get("/graph", dependencies=[Depends(_require_api_key)])
async def get_traceability_graph(
    request: Request,
    project_id: str | None = None,
    run_id: str | None = None,
):
    """Return nodes and edges for the D3.js force-directed traceability graph.

    R28.6 — when `run_id` is supplied, scope ExecutionResult + Defect
    nodes to that specific run so the graph reflects THAT run's
    PASS/FAIL/Defect attribution. Without `run_id`, the graph shows
    project-aggregated state (latest known per AC). The Req → AC → TC
    backbone is config-time and never run-scoped.
    """
    # WS1 — full-chain graph from Neo4j (the deck's COMPLETE chain). The old
    # `get_coverage_report()` path returned no nodes/edges (always fell through);
    # `_full_chain_graph` traverses the `*_id` nodes the writers populate and
    # returns {nodes, edges, gaps}. Killswitch ARTA_TRACE_FULL_CHAIN_DISABLE=1
    # OR an empty graph → fall through to the legacy PG 5-node graph (zero-risk).
    neo4j_driver = getattr(request.app.state, "neo4j", None)
    if neo4j_driver and os.environ.get("ARTA_TRACE_FULL_CHAIN_DISABLE") != "1":
        try:
            fc = await _full_chain_graph(neo4j_driver, project_id, run_id)
            if fc and fc.get("nodes"):
                fc["node_colors"] = NODE_COLORS
                fc["run_id"] = run_id
                fc["_arta_source"] = "ws1_full_chain"
                return fc
        except Exception as _fc_exc:
            log.debug("WS1 full-chain graph read skipped: %s", _fc_exc)
            # Fall through to PostgreSQL

    # Fall back to PostgreSQL-based graph
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import (
                RequirementRepo, TestCaseRepo, DefectRepo, _to_dict,
            )
            req_repo = RequirementRepo(db)
            test_repo = TestCaseRepo(db)
            defect_repo = DefectRepo(db)

            nodes = []
            edges = []

            # Build a UUID→text_id lookup for requirements
            uuid_to_reqid: dict[str, str] = {}

            # Requirements — filtered by project
            reqs, _ = await req_repo.list(limit=100, project_id=project_id)
            for r in reqs:
                rd = _to_dict(r)
                # Prefer text req_id (REQ-BT-001) over UUID for node IDs
                req_id = rd.get("req_id") or rd.get("requirement_id") or str(rd.get("id", ""))
                uuid_to_reqid[str(rd.get("id", ""))] = req_id
                nodes.append({
                    "id": req_id,
                    "label": rd.get("title", req_id),
                    "type": "requirement",
                    "color": NODE_COLORS["requirement"],
                    "priority": rd.get("priority", "P2"),
                })
                # AC nodes
                for i, ac in enumerate(rd.get("acceptance_criteria", [])):
                    ac_id = f"{req_id}-AC-{i+1}"
                    ac_text = ac if isinstance(ac, str) else ac.get("description", ac.get("text", f"AC {i+1}"))
                    nodes.append({
                        "id": ac_id,
                        "label": ac_text[:60],
                        "type": "acceptance_criteria",
                        "color": NODE_COLORS["acceptance_criteria"],
                    })
                    edges.append({"source": req_id, "target": ac_id, "type": "has_ac"})

            # Test cases — filtered by project
            req_text_ids = {n["id"] for n in nodes if n["type"] == "requirement"}
            # R28.6 — build a UUID→AC-node-id lookup so we can wire
            # AC→TC edges. The AC nodes added above use ids of shape
            # "{req_id}-AC-{i+1}", but TestCase.ac_id FK points at
            # AcceptanceCriteria.id (UUID). Build the lookup from the
            # Requirement.acceptance_criteria relationship's actual
            # rows so each TC's ac_id maps to the right node id.
            ac_uuid_to_node: dict[str, str] = {}
            for r in reqs:
                rd = _to_dict(r)
                req_id = uuid_to_reqid.get(str(rd.get("id", "")), "")
                ac_relation = getattr(r, "acceptance_criteria", None) or []
                for i, ac_obj in enumerate(ac_relation):
                    ac_uuid = str(getattr(ac_obj, "id", ""))
                    if ac_uuid and req_id:
                        ac_uuid_to_node[ac_uuid] = f"{req_id}-AC-{i+1}"
            tests, _ = await test_repo.list(limit=200, project_id=project_id)
            for t in tests:
                td = _to_dict(t)
                test_id = td.get("test_id", td.get("id", ""))
                nodes.append({
                    "id": test_id,
                    "label": td.get("title", test_id),
                    "type": "test_case",
                    "color": NODE_COLORS["test_case"],
                    "tool": td.get("tool", "playwright"),
                    "status": td.get("last_status", ""),
                })
                # R28.6 — wire TC → AC edge when the TestCase has an
                # ac_id FK. Pre-R28.6 the graph only had Req → TC
                # direct edges, skipping the AC layer entirely. The
                # operator's target image (Req → AC → TC → PASS/FAIL
                # → Defect) needs this edge to render correctly.
                ac_fk = str(td.get("ac_id") or "")
                if ac_fk and ac_fk != "None":
                    ac_node_id = ac_uuid_to_node.get(ac_fk)
                    if ac_node_id:
                        edges.append({"source": ac_node_id, "target": test_id, "type": "verifies"})
                # Resolve requirement linkage: try FK UUID first, then metadata fallback
                raw_req_id = str(td.get("requirement_id", "") or "")
                linked_req_id = uuid_to_reqid.get(raw_req_id) if raw_req_id and raw_req_id != "None" else None
                # Fallback: check metadata JSONB for text requirement_id
                if not linked_req_id:
                    meta = td.get("metadata") or td.get("metadata_") or {}
                    if isinstance(meta, str):
                        import json as _json
                        try:
                            meta = _json.loads(meta)
                        except Exception:
                            meta = {}
                    meta_req = meta.get("requirement_id", "") if isinstance(meta, dict) else ""
                    if meta_req and meta_req in req_text_ids:
                        linked_req_id = meta_req
                    elif meta_req:
                        linked_req_id = uuid_to_reqid.get(meta_req)
                # Also accept raw_req_id if it directly matches a requirement text ID
                if not linked_req_id and raw_req_id in req_text_ids:
                    linked_req_id = raw_req_id
                if linked_req_id:
                    edges.append({"source": linked_req_id, "target": test_id, "type": "covers"})

            # R29.1 — emit ExecutionResult nodes between TC and Defect
            # when a run_id is supplied. Pre-R29.1 the PG fallback rendered
            # only Req → AC → TC → Defect, missing the Result layer that
            # carries PASS/FAIL/SKIP/BLOCKED status. With this layer the
            # 5-lane DAG renders correctly (matches the Neo4j path's
            # canonical chain Req → AC → TC → Result → Defect).
            #
            # `result_id_by_test_id` lets the Defect loop wire defect →
            # result edges instead of defect → tc directly.
            result_id_by_test_id: dict[str, str] = {}
            if run_id:
                try:
                    from sqlalchemy import select as _sel
                    from ...db.models import ExecutionResult as _ER, TestRun as _TR
                    # Resolve external run_id to test_runs.id UUID for FK.
                    tr_row = (await db.execute(
                        _sel(_TR.id).where(_TR.run_id == run_id)
                    )).first()
                    tr_uuid = tr_row[0] if tr_row else None
                    if tr_uuid is not None:
                        er_rows = (await db.execute(
                            _sel(_ER).where(_ER.run_id == tr_uuid).limit(500)
                        )).scalars().all()
                        for er in er_rows:
                            tc_id = getattr(er, "test_id", "") or ""
                            if not tc_id:
                                continue
                            res_node_id = f"{run_id}:{tc_id}"
                            status_raw = getattr(er, "status", None)
                            status_str = (
                                getattr(status_raw, "value", None)
                                or str(status_raw or "")
                            ).upper()
                            # Color: PASS=green, FAIL/BLOCKED=red, else gray.
                            color = (
                                "#10b981" if status_str == "PASS"
                                else "#ef4444" if status_str in ("FAIL", "BLOCKED")
                                else "#64748b"
                            )
                            tool_raw = getattr(er, "automation_tool", None)
                            tool_str = (
                                getattr(tool_raw, "value", None) or str(tool_raw or "")
                            )
                            nodes.append({
                                "id": res_node_id,
                                "label": status_str or "UNKNOWN",
                                "type": "result",
                                "color": color,
                                "status": status_str,
                                "tool": tool_str,
                                "duration_ms": getattr(er, "duration_ms", 0) or 0,
                                "run_id": run_id,
                            })
                            edges.append({
                                "source": tc_id,
                                "target": res_node_id,
                                "type": "produced",
                            })
                            result_id_by_test_id[tc_id] = res_node_id
                except Exception as _r29_exc:
                    import logging as _logging
                    _logging.getLogger("arta.traceability").debug(
                        "R29.1: PG result-node emission failed: %s", _r29_exc,
                    )

            # Defects — filtered by project AND optionally by run.
            # R28.6 — when run_id is supplied, scope to defects from
            # THAT run; the graph then reflects THAT run's failures
            # rather than the project-wide aggregate.
            defects, _ = await defect_repo.list(
                limit=200, project_id=project_id, run_id=run_id,
            )
            for d in defects:
                dd = _to_dict(d)
                defect_id = dd.get("defect_id", dd.get("id", ""))
                nodes.append({
                    "id": defect_id,
                    "label": dd.get("title", defect_id),
                    "type": "defect",
                    "color": NODE_COLORS["defect"],
                    "severity": dd.get("severity", "P2"),
                })
                test_id = dd.get("test_id", "")
                if test_id:
                    # R29.1 — when a Result node exists for this test in
                    # this run, anchor the defect on the Result (so the
                    # 5-lane DAG renders Result → Defect). Fall back to
                    # the TC → Defect edge when no Result is available
                    # (e.g., legacy runs pre-R29.1, or run_id not in URL).
                    src = result_id_by_test_id.get(test_id, test_id)
                    edges.append({"source": src, "target": defect_id, "type": "triggered" if src != test_id else "found_by"})
                raw_req = str(dd.get("requirement_id", ""))
                linked_req = uuid_to_reqid.get(raw_req, raw_req)
                if linked_req:
                    edges.append({"source": linked_req, "target": defect_id, "type": "impacts"})

            # Use DB path if it produced meaningful edges (not just isolated nodes)
            if edges:
                nodes, edges = _sanitize_graph(nodes, edges)
                ac_maps = _build_ac_test_map(nodes, edges)
                return {
                    "nodes": nodes, "edges": edges,
                    "node_colors": NODE_COLORS,
                    "run_id": run_id,
                    **ac_maps,
                }
            # If DB has nodes but no edges, fall through to in-memory which has richer test→req links

    # In-memory fallback — preferred for projects with in-memory requirements (richer edges)
    try:
        from .requirements import BUGTRACKR_REQUIREMENTS, BUGTRACKR_PROJECT_ID, ECOMMERCE_PROJECT_ID, PROJECT_REQUIREMENTS
        if project_id == BUGTRACKR_PROJECT_ID:
            return _build_graph_from_requirements(BUGTRACKR_REQUIREMENTS)
        if project_id and project_id in PROJECT_REQUIREMENTS:
            return _build_graph_from_requirements(PROJECT_REQUIREMENTS[project_id])
    except Exception as e:
        import logging
        logging.getLogger("arta.traceability").warning("In-memory graph fallback failed: %s", e)

    # If a specific project is selected but has no data, return empty graph (not E-Commerce mock)
    if project_id:
        return {"nodes": [], "edges": [], "node_colors": NODE_COLORS}

    # Default (no project selected): generic demo graph
    return _mock_graph()


def _sanitize_graph(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Remove nodes with null/empty IDs and edges referencing them."""
    valid_nodes = [n for n in nodes if n.get("id")]
    valid_ids = {n["id"] for n in valid_nodes}
    valid_edges = [
        e for e in edges
        if e.get("source") and e.get("target")
        and e["source"] in valid_ids and e["target"] in valid_ids
    ]
    return valid_nodes, valid_edges


def _build_ac_test_map(nodes: list[dict], edges: list[dict]) -> dict[str, list[dict]]:
    """Build a mapping from AC IDs to their linked test cases.

    Since tests link to requirements (not directly to ACs), we distribute
    tests across the ACs of the same parent requirement. Each AC gets all
    tests linked to its parent req.
    """
    # Build req → [ac_ids] map
    req_to_acs: dict[str, list[str]] = {}
    for e in edges:
        if e.get("type") == "has_ac":
            req_id = e["source"]
            if req_id not in req_to_acs:
                req_to_acs[req_id] = []
            req_to_acs[req_id].append(e["target"])

    # Build req → [test nodes] map
    req_to_tests: dict[str, list[dict]] = {}
    node_map = {n["id"]: n for n in nodes}
    for e in edges:
        if e.get("type") == "covers":
            req_id = e["source"]
            test_node = node_map.get(e["target"])
            if test_node:
                if req_id not in req_to_tests:
                    req_to_tests[req_id] = []
                req_to_tests[req_id].append({
                    "id": test_node["id"],
                    "title": test_node.get("label", test_node["id"]),
                    "status": test_node.get("status", ""),
                    "tool": test_node.get("tool", "playwright"),
                })

    # Build defect → req map
    ac_defects: dict[str, list[dict]] = {}
    for e in edges:
        if e.get("type") == "impacts":
            req_id = e["source"]
            defect_node = node_map.get(e["target"])
            if defect_node and req_id in req_to_acs:
                for ac_id in req_to_acs[req_id]:
                    if ac_id not in ac_defects:
                        ac_defects[ac_id] = []
                    ac_defects[ac_id].append({
                        "id": defect_node["id"],
                        "title": defect_node.get("label", defect_node["id"]),
                        "severity": defect_node.get("severity", "P2"),
                    })

    # Distribute tests to ACs: each AC gets its parent req's tests
    ac_test_map: dict[str, list[dict]] = {}
    for req_id, ac_ids in req_to_acs.items():
        tests = req_to_tests.get(req_id, [])
        for ac_id in ac_ids:
            ac_test_map[ac_id] = tests

    return {"ac_tests": ac_test_map, "ac_defects": ac_defects}


def _build_graph_from_requirements(reqs: list[dict]) -> dict:
    """Build traceability graph dynamically from in-memory requirements."""
    nodes = []
    edges = []

    for r in reqs:
        req_id = r.get("req_id", r.get("id", ""))
        nodes.append({
            "id": req_id,
            "label": r.get("title", req_id),
            "type": "requirement",
            "color": NODE_COLORS["requirement"],
            "priority": r.get("priority", "P2"),
        })
        for i, ac in enumerate(r.get("acceptance_criteria", [])):
            # Use the actual AC ID from the data (e.g., "AC-AM-001-01"), not a generated one
            ac_id = ac.get("id", f"{req_id}-AC-{i+1}") if isinstance(ac, dict) else f"{req_id}-AC-{i+1}"
            ac_text = ac if isinstance(ac, str) else ac.get("statement", ac.get("description", ac.get("text", f"AC {i+1}")))
            nodes.append({
                "id": ac_id,
                "label": str(ac_text)[:60],
                "type": "acceptance_criteria",
                "color": NODE_COLORS["acceptance_criteria"],
            })
            edges.append({"source": req_id, "target": ac_id, "type": "has_ac"})

    # Build lookup: AC ID → parent requirement ID (for connecting tests → ACs)
    ac_to_req = {}
    for r in reqs:
        req_id = r.get("req_id", r.get("id", ""))
        for i, ac in enumerate(r.get("acceptance_criteria", [])):
            ac_id = ac.get("id", f"{req_id}-AC-{i+1}") if isinstance(ac, dict) else f"{req_id}-AC-{i+1}"
            ac_to_req[ac_id] = req_id

    # Add test cases from in-memory test store
    all_ac_ids = set(ac_to_req.keys())
    req_ids = {r.get("req_id", r.get("id", "")) for r in reqs}
    try:
        from .tests import GENERATED_TESTS
        for t in GENERATED_TESTS:
            test_id = t.get("id", t.get("test_id", ""))
            t_req = t.get("requirement_id", "")
            t_ac = t.get("ac_id", "")
            if t_req and t_req in req_ids:
                nodes.append({
                    "id": test_id,
                    "label": t.get("title", test_id),
                    "type": "test_case",
                    "color": NODE_COLORS["test_case"],
                    "tool": t.get("tool", "playwright"),
                    "status": t.get("last_status", t.get("status", "")),
                })
                # Connect test → AC (if ac_id matches), otherwise test → requirement
                if t_ac and t_ac in all_ac_ids:
                    edges.append({"source": t_ac, "target": test_id, "type": "covers"})
                else:
                    edges.append({"source": t_req, "target": test_id, "type": "covers"})
    except Exception as e:
        log.warning("Failed to load in-memory tests for graph: %s", e)

    nodes, edges = _sanitize_graph(nodes, edges)
    ac_maps = _build_ac_test_map(nodes, edges)
    return {"nodes": nodes, "edges": edges, "node_colors": NODE_COLORS, **ac_maps}


def _mock_graph() -> dict:
    """Generate generic mock graph data when DB is unavailable."""
    nodes = [
        {"id": "REQ-001", "label": "Core Feature", "type": "requirement", "color": NODE_COLORS["requirement"], "priority": "P0"},
        {"id": "REQ-002", "label": "User Management", "type": "requirement", "color": NODE_COLORS["requirement"], "priority": "P1"},
        {"id": "REQ-003", "label": "Data Processing", "type": "requirement", "color": NODE_COLORS["requirement"], "priority": "P0"},
        {"id": "REQ-004", "label": "Reporting", "type": "requirement", "color": NODE_COLORS["requirement"], "priority": "P2"},
        {"id": "REQ-001-AC-1", "label": "CRUD operations functional", "type": "acceptance_criteria", "color": NODE_COLORS["acceptance_criteria"]},
        {"id": "REQ-001-AC-2", "label": "Validation rules enforced", "type": "acceptance_criteria", "color": NODE_COLORS["acceptance_criteria"]},
        {"id": "REQ-002-AC-1", "label": "Login/logout works correctly", "type": "acceptance_criteria", "color": NODE_COLORS["acceptance_criteria"]},
        {"id": "REQ-003-AC-1", "label": "Batch processing completes", "type": "acceptance_criteria", "color": NODE_COLORS["acceptance_criteria"]},
        {"id": "REQ-004-AC-1", "label": "Export generates valid output", "type": "acceptance_criteria", "color": NODE_COLORS["acceptance_criteria"]},
        {"id": "TC-001", "label": "Happy path CRUD", "type": "test_case", "color": NODE_COLORS["test_case"], "tool": "playwright", "status": "PASS"},
        {"id": "TC-002", "label": "Input validation", "type": "test_case", "color": NODE_COLORS["test_case"], "tool": "playwright", "status": "PASS"},
        {"id": "TC-003", "label": "Load test — concurrent users", "type": "test_case", "color": NODE_COLORS["test_case"], "tool": "k6", "status": "FAIL"},
        {"id": "TC-004", "label": "Auth flow verification", "type": "test_case", "color": NODE_COLORS["test_case"], "tool": "playwright", "status": "PASS"},
        {"id": "TC-005", "label": "Data export test", "type": "test_case", "color": NODE_COLORS["test_case"], "tool": "playwright", "status": "PASS"},
        {"id": "DEF-001", "label": "Timeout under concurrent load", "type": "defect", "color": NODE_COLORS["defect"], "severity": "P0"},
    ]
    edges = [
        {"source": "REQ-001", "target": "REQ-001-AC-1", "type": "has_ac"},
        {"source": "REQ-001", "target": "REQ-001-AC-2", "type": "has_ac"},
        {"source": "REQ-002", "target": "REQ-002-AC-1", "type": "has_ac"},
        {"source": "REQ-003", "target": "REQ-003-AC-1", "type": "has_ac"},
        {"source": "REQ-004", "target": "REQ-004-AC-1", "type": "has_ac"},
        {"source": "REQ-001", "target": "TC-001", "type": "covers"},
        {"source": "REQ-001", "target": "TC-002", "type": "covers"},
        {"source": "REQ-001", "target": "TC-003", "type": "covers"},
        {"source": "REQ-002", "target": "TC-004", "type": "covers"},
        {"source": "REQ-004", "target": "TC-005", "type": "covers"},
        {"source": "TC-003", "target": "DEF-001", "type": "found_by"},
        {"source": "REQ-001", "target": "DEF-001", "type": "impacts"},
    ]
    nodes, edges = _sanitize_graph(nodes, edges)
    ac_maps = _build_ac_test_map(nodes, edges)
    return {"nodes": nodes, "edges": edges, "node_colors": NODE_COLORS, **ac_maps}


@router.post("/orphans/cleanup", dependencies=[Depends(_require_api_key)])
async def cleanup_orphan_tests(request: Request, dry_run: bool = True) -> dict:
    """Detect and optionally remove orphaned TestCase nodes from Neo4j.

    dry_run=True (default): audit only — returns the list without deleting.
    dry_run=False: runs DETACH DELETE. Irreversible — confirm before calling.
    """
    from ...agents.traceability_agent import TraceabilityAgent
    agent = TraceabilityAgent(getattr(request.app.state, "neo4j", None))
    return await agent.cleanup_orphans(dry_run=dry_run)


@router.get("/coverage/matrix", dependencies=[Depends(_require_api_key)])
async def coverage_matrix(request: Request, project_id: str | None = None) -> dict:
    """BMAD Layer 3+7 deliverable: per-AC coverage state grouped by requirement.

    Each AC carries `coverage_state` ∈ {FULL, PARTIAL, NONE} and
    `test_count` so the UI can render a color-coded matrix and the gate's
    `_check_priority_coverage_targets` consumer has the granularity to enforce
    P0=100% / P1=90% / P2=50% / P3=20% canonical targets per requirement.
    """
    from ...agents.traceability_agent import TraceabilityAgent
    agent = TraceabilityAgent(getattr(request.app.state, "neo4j", None))
    report = await agent.get_coverage_report()
    matrix: dict[str, list[dict]] = {}
    for ac in report.get("acs") or []:
        req = ac.get("requirement_id") or "?"
        matrix.setdefault(req, []).append({
            "ac_id": ac.get("id"),
            "statement": ac.get("statement"),
            "priority": ac.get("priority", "P3"),
            "coverage_state": ac.get("coverage_state", "NONE"),
            "test_count": ac.get("test_count", 0),
            "pass_count": ac.get("pass_count", 0),
        })
    return {
        "project_id": project_id,
        "matrix": matrix,
        "summary": {
            "total_acs": sum(len(v) for v in matrix.values()),
            "full":    sum(1 for v in matrix.values() for ac in v if ac["coverage_state"] == "FULL"),
            "partial": sum(1 for v in matrix.values() for ac in v if ac["coverage_state"] == "PARTIAL"),
            "none":    sum(1 for v in matrix.values() for ac in v if ac["coverage_state"] == "NONE"),
        },
    }


# ── WS1 — full-chain traceability read + backfill ────────────────────────────
# (a:srcLabel)-[REL]->(b:dstLabel) edges of the deck's complete chain, over the
# `*_id`-keyed nodes the WS1 writers populate.
_CHAIN_EDGES_BASE = [
    ("Requirement", "req_id", "HAS_PROFILE", "RiskProfile", "profile_id", "requirement", "risk_profile"),
    ("RiskProfile", "profile_id", "HAS_RECIPE", "DatasetRecipe", "recipe_id", "risk_profile", "dataset_recipe"),
    ("Requirement", "req_id", "HAS_RECIPE", "DatasetRecipe", "recipe_id", "requirement", "dataset_recipe"),
    ("DatasetRecipe", "recipe_id", "MATERIALISES_TO", "Fixture", "fixture_path", "dataset_recipe", "fixture"),
    ("DatasetRecipe", "recipe_id", "VERIFIED_BY_RECIPE", "RecipeVerifier", "verifier_id", "dataset_recipe", "recipe_verifier"),
    ("Requirement", "req_id", "HAS_AC", "AcceptanceCriteria", "ac_id", "requirement", "acceptance_criteria"),
    ("Requirement", "req_id", "HAS_SCENARIO", "Scenario", "scenario_id", "requirement", "scenario"),
    ("Scenario", "scenario_id", "REALISED_BY", "TestCase", "test_id", "scenario", "test_case"),
    ("Requirement", "req_id", "HAS_SPEC", "Spec", "spec_path", "requirement", "spec"),
    ("Fixture", "fixture_path", "USED_BY", "Spec", "spec_path", "fixture", "spec"),
    ("Spec", "spec_path", "GENERATES", "TestCase", "test_id", "spec", "test_case"),
    ("TestCase", "test_id", "VERIFIED_BY", "Run", "run_id", "test_case", "execution"),
    ("Spec", "spec_path", "EXECUTED_AS", "Run", "run_id", "spec", "execution"),
    ("Defect", "defect_id", "DETECTED_BY", "TestCase", "test_id", "defect", "test_case"),
]

# B2 (charter conformance) — the code/API segment of the spine. These edges are ALREADY
# WRITTEN at gen time (writer.upsert_traceability_edges: IMPLEMENTED_BY / EXERCISES /
# INVOKES; AcceptanceCriteria-TESTED_BY->TestCase) but the read query never traversed
# them, so the charter's Requirement→AC→…→Endpoint half was not walkable. Adding these
# tuples makes the existing edges reachable — no writer change. Each tuple is an isolated
# per-edge MATCH (a malformed/absent edge silently contributes nothing; the label
# constraint on the AC edge disambiguates it from Requirement-TESTED_BY->TestCase).
_CHAIN_EDGES_SPINE_EXT = [
    ("Requirement", "req_id", "IMPLEMENTED_BY", "Endpoint", "endpoint_key", "requirement", "endpoint"),
    ("TestCase", "test_id", "EXERCISES", "Endpoint", "endpoint_key", "test_case", "endpoint"),
    ("FrontendRoute", "route", "INVOKES", "Endpoint", "endpoint_key", "frontend_route", "endpoint"),
    ("AcceptanceCriteria", "ac_id", "TESTED_BY", "TestCase", "test_id", "acceptance_criteria", "test_case"),
]

# Observe-first + reversible (the graph API/UI returns more nodes when extended).
_CHAIN_EDGES = _CHAIN_EDGES_BASE + (
    [] if os.environ.get("ARTA_SPINE_EXTEND_DISABLE") == "1" else _CHAIN_EDGES_SPINE_EXT)


def _chain_node_label(ntype: str, key, props: dict) -> str:
    props = props or {}
    if ntype == "risk_profile":   return f"risk {props.get('priority', '')}".strip()
    if ntype == "dataset_recipe": return f"recipe v{props.get('version', '')}"
    if ntype == "fixture":        return f"{props.get('row_count', '?')} rows"
    if ntype == "recipe_verifier":return "verifier " + ("✗" if props.get("failed") else "✓")
    if ntype == "scenario":       return str(props.get("kind", "scenario"))
    if ntype == "spec":           return str(props.get("framework") or "spec")
    if ntype == "execution":      return f"run {str(key)[:10]}"
    if ntype == "execution_result": return str(props.get("status", "result"))
    if ntype == "defect":         return str(props.get("severity", "defect"))
    if ntype == "test_case":      return str(key)[:20]
    if ntype == "endpoint":       return (f"{props.get('method', '')} "
                                          f"{props.get('path_template') or props.get('path') or ''}"
                                          ).strip() or str(key)
    if ntype == "frontend_route": return str(props.get("route") or key)
    return str(key)


def _compute_coverage_gaps(nodes: list[dict], edges: list[dict]) -> dict:
    """Pure helper (unit-testable): the MISSION OUTPUT — where the gen→execute
    chain is broken. Reqs with no RiskProfile (unplanned), no Spec (no script),
    Specs never executed, and open-defect count."""
    by_type: dict[str, set] = {}
    for n in nodes:
        by_type.setdefault(n["type"], set()).add(n["id"])
    src_by_rel: dict[str, set] = {}
    for e in edges:
        src_by_rel.setdefault(e["type"], set()).add(e["source"])
    reqs = by_type.get("requirement", set())
    specs = by_type.get("spec", set())
    return {
        "unplanned_requirements": sorted(reqs - src_by_rel.get("has_profile", set()))[:50],
        "requirements_without_spec": sorted(reqs - src_by_rel.get("has_spec", set()))[:50],
        "unrun_specs": sorted(specs - src_by_rel.get("executed_as", set()))[:50],
        "open_defect_count": len(by_type.get("defect", set())),
    }


async def _full_chain_graph(driver, project_id, run_id) -> dict:
    """Traverse the deck's complete chain over the `*_id` nodes. Returns
    {nodes, edges, gaps}. Empty nodes → caller falls back to the PG graph."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def _add(nid, ntype, props):
        if nid is None:
            return
        sid = str(nid)
        if sid not in nodes:
            n = {"id": sid, "label": _chain_node_label(ntype, nid, props or {}), "type": ntype}
            if ntype == "execution":
                n["status"] = (props or {}).get("gate_decision")
            nodes[sid] = n

    async with driver.session() as session:
        for sL, sK, rel, dL, dK, sT, dT in _CHAIN_EDGES:
            q = (f"MATCH (a:{sL})-[:{rel}]->(b:{dL}) "
                 f"RETURN a.{sK} AS sid, b.{dK} AS tid, a AS ap, b AS bp LIMIT 4000")
            try:
                res = await session.run(q)
                rows = await res.data()
            except Exception:
                continue
            for row in rows:
                sid, tid = row.get("sid"), row.get("tid")
                _add(sid, sT, dict(row.get("ap") or {}))
                _add(tid, dT, dict(row.get("bp") or {}))
                if sid is not None and tid is not None:
                    edges.append({"source": str(sid), "target": str(tid), "type": rel.lower()})

        # R311 — when a run is in focus, surface THAT run's ExecutionResults +
        # Defects connected to their TestCases. The island-merge (R311 migration +
        # traceability_agent adopt-id) makes the `TestCase(test_id)-[:PRODUCED]->
        # ExecutionResult-[:TRIGGERED]->Defect` chain reachable from the spine-side
        # (test_id-keyed) TestCase nodes the loop above already emitted. Scoped to
        # run_id so the global graph isn't flooded by ~184k results.
        # Killswitch ARTA_R311_GRAPH_RESULTS_DISABLE=1.
        if run_id and os.environ.get("ARTA_R311_GRAPH_RESULTS_DISABLE") != "1":
            rq = ("MATCH (t:TestCase)-[:PRODUCED]->(res:ExecutionResult {run_id: $rid}) "
                  "OPTIONAL MATCH (res)-[:TRIGGERED]->(d:Defect) "
                  "RETURN t.test_id AS tid, res.id AS rid_, res AS rp, "
                  "d.id AS did, d AS dp LIMIT 4000")
            try:
                rres = await session.run(rq, {"rid": run_id})
                for row in await rres.data():
                    tid, rid_ = row.get("tid"), row.get("rid_")
                    if not (tid and rid_):
                        continue
                    _add(tid, "test_case", {})
                    _add(rid_, "execution_result", dict(row.get("rp") or {}))
                    edges.append({"source": str(tid), "target": str(rid_), "type": "produced"})
                    did = row.get("did")
                    if did is not None:
                        _add(did, "defect", dict(row.get("dp") or {}))
                        edges.append({"source": str(rid_), "target": str(did), "type": "triggered"})
            except Exception:
                pass

    return {"nodes": list(nodes.values()), "edges": edges,
            "gaps": _compute_coverage_gaps(list(nodes.values()), edges)}


@router.post("/backfill", dependencies=[Depends(_require_api_key)])
async def backfill_full_chain(request: Request, project_id: str | None = Query(None)) -> dict:
    """WS1 — reconstruct the full chain for EXISTING data (the pipeline only
    fills NEW runs): read the recipes on disk + generated_tests → write
    RiskProfile/Recipe/Fixture/Verifier/Scenario/Spec/TestCase nodes. Idempotent
    (all MERGE). Run-side (Run/EXECUTED_AS) fills via the post-run hook.
    Killswitch ARTA_TRACE_FULL_CHAIN_DISABLE=1."""
    import json as _json_bf
    import glob as _glob_bf
    if os.environ.get("ARTA_TRACE_FULL_CHAIN_DISABLE") == "1":
        raise HTTPException(status_code=404, detail={"error": "full_chain_disabled"})
    driver = getattr(request.app.state, "neo4j", None)
    if driver is None:
        raise HTTPException(status_code=503, detail={"error": "neo4j_unavailable"})
    from ...graph import writer as _gw
    counts = {"recipes": 0, "requirements": 0, "tests": 0, "scenarios": 0, "specs": 0, "profiles": 0}

    # Self-heal: drop stale RiskProfile nodes from the earlier schema where
    # profile_id == req_id (collided with the Requirement node id → self-loop).
    # New profiles use `{req_id}:profile`.
    try:
        async with driver.session() as _s:
            await _s.run("MATCH (p:RiskProfile) WHERE NOT p.profile_id ENDS WITH ':profile' DETACH DELETE p")
            # Drop the redundant req→recipe fallback edge when the canonical
            # profile→recipe edge exists (deck path is r→profile→recipe).
            await _s.run("MATCH (:Requirement)-[h:HAS_RECIPE]->(rec:DatasetRecipe)<-[:HAS_RECIPE]-(:RiskProfile) DELETE h")
    except Exception:
        pass

    # Read generated_tests first → group by requirement.
    try:
        with open(".arta/generated_tests.json") as _f:
            gt = _json_bf.load(_f)
        items = gt if isinstance(gt, list) else (gt.get("tests") or [])
    except Exception:
        items = []
    by_req: dict[str, list] = {}
    for t in items:
        rid = t.get("requirement_id")
        if rid:
            by_req.setdefault(rid, []).append(t)

    # 1) PROFILES FIRST — so HAS_RECIPE attaches profile→recipe (the deck path),
    #    not the req→recipe fallback. (Recipe chain's OPTIONAL MATCH needs the
    #    RiskProfile to already exist.)
    for rid, ts in by_req.items():
        try:
            await _gw.upsert_requirement_profile(driver, rid, {"priority": ts[0].get("priority", "")})
            counts["profiles"] += 1
        except Exception:
            continue

    # 2) recipes → Recipe/Fixture/Verifier (keyed by the recipe's inner requirement_id)
    for rf in sorted(_glob_bf.glob(".arta/recipes/*_v*.json")):
        try:
            with open(rf) as _f:
                rec = _json_bf.load(_f)
            rid = rec.get("requirement_id")
            if not rid:
                continue
            await _gw.upsert_recipe_chain(driver, rid, rec)
            counts["recipes"] += 1
        except Exception:
            continue

    # 3) tests + scenarios + specs
    for rid, ts in by_req.items():
        try:
            await _gw.upsert_test_entries(driver, ts)
            await _gw.upsert_scenarios(driver, rid, ts)
            counts["tests"] += len(ts)
            counts["scenarios"] += 1
            specs = []
            for t in ts:
                sp = t.get("script_path") or t.get("file_path") or t.get("filename")
                if sp:
                    specs.append({"spec_path": sp, "framework": (t.get("tool") or ""), "test_id": t.get("id")})
            if specs:
                await _gw.upsert_spec_files(driver, rid, specs)
                counts["specs"] += len(specs)
            counts["requirements"] += 1
        except Exception:
            continue

    log.info("WS1 backfill: %s", counts)
    return {"status": "ok", "counts": counts}
