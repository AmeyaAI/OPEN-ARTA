"""ARTA Quality Gates Router — Evidence-based release decisioning (BMAD TEA 4-outcome model)."""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel

from .auth import require_role


# F4-1: API-key check is centralised in src/api/dependencies.py.
from ..dependencies import require_api_key as _require_api_key  # noqa: E402

from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
class GateCheckRequest(BaseModel):
    build_id: str
    environment: str = "staging"
    override_reason: str | None = None   # Requires admin role in production
    commit_sha: str | None = None        # If set, creates a GitHub check run


class ThresholdUpdate(BaseModel):
    p0_coverage_pct: float = 100.0
    p1_coverage_pct: float = 90.0
    p2_coverage_pct: float = 75.0
    overall_coverage_pct: float = 80.0
    p0_pass_rate_pct: float = 100.0
    overall_pass_rate_pct: float = 90.0
    max_open_p0_defects: int = 0
    max_open_p1_defects: int = 3
    performance_p95_ms: int = 3000
    max_critical_security_findings: int = 0


class WaiverRequest(BaseModel):
    """Create a gate waiver — authorized exception with rationale + expiry."""
    gate_id: str
    waived_check: str
    rationale: str
    expires_at: datetime


# Mock data uses BMAD TEA 4-outcome model: PASS / CONCERNS / FAIL / WAIVED
MOCK_GATES = {
    "487": {
        "build_id": "487",
        "decision": "FAIL",
        "environment": "staging",
        "assessed_at": "2026-03-11T14:34:30Z",
        "summary": "RELEASE FAILED \u2014 1 critical check failed: p0_pass_rate (90.9% vs =100%)",
        "blocking_checks": [
            {
                "name": "p0_pass_rate",
                "actual": "90.9%",
                "expected": "=100%",
                "detail": "TC-126 (payment timeout) failing under load",
                "category": "pass_rate",
            },
        ],
        "warnings": [
            {
                "name": "coverage_REQ-019",
                "actual": "0%",
                "expected": "\u226590%",
                "detail": "Refund flow has zero test coverage",
                "category": "coverage",
            }
        ],
        "passed_checks": [
            {"name": "p0_coverage",         "actual": "87%",   "expected": "\u2265100%", "category": "coverage"},
            {"name": "overall_coverage",    "actual": "78%",   "expected": "\u226580%",  "category": "coverage"},
            {"name": "open_p0_defects",     "actual": "1",     "expected": "\u22640",    "category": "defect"},
            {"name": "performance_p95",     "actual": "2.8s",  "expected": "\u22643s",   "category": "nfr_performance"},
            {"name": "security_findings",   "actual": "0",     "expected": "\u22640",    "category": "nfr_security"},
        ],
        "evidence_package_id": "ep-487-staging",
    },
    "486": {
        "build_id": "486",
        "decision": "PASS",
        "environment": "staging",
        "assessed_at": "2026-03-11T10:19:45Z",
        "summary": "RELEASE APPROVED \u2014 11/11 checks passed",
        "blocking_checks": [],
        "warnings": [],
        "passed_checks": [
            {"name": "p0_pass_rate",        "actual": "100%",  "expected": "=100%",  "category": "pass_rate"},
            {"name": "overall_coverage",    "actual": "76.5%", "expected": "\u226580%", "category": "coverage"},
            {"name": "open_p0_defects",     "actual": "0",     "expected": "\u22640",   "category": "defect"},
        ],
        "evidence_package_id": "ep-486-staging",
    },
    "485": {
        "build_id": "485",
        "decision": "CONCERNS",
        "environment": "staging",
        "assessed_at": "2026-03-10T16:45:00Z",
        "summary": "RELEASE CONCERNS \u2014 9/11 checks passed, 2 concern(s) require mitigation: code_coverage, code_duplication",
        "blocking_checks": [],
        "warnings": [
            {"name": "code_coverage", "actual": "72.5%", "expected": "\u226580%", "category": "nfr_maintainability"},
            {"name": "code_duplication", "actual": "6.2%", "expected": "\u22645%", "category": "nfr_maintainability"},
        ],
        "passed_checks": [
            {"name": "p0_pass_rate",     "actual": "100%", "expected": "=100%",  "category": "pass_rate"},
            {"name": "overall_coverage", "actual": "81%",  "expected": "\u226580%", "category": "coverage"},
        ],
        "evidence_package_id": "ep-485-staging",
    },
}

CURRENT_THRESHOLDS = {
    "environment": "default",
    "p0_coverage_pct": 100.0,
    "p1_coverage_pct": 90.0,
    "p2_coverage_pct": 75.0,
    "p3_coverage_pct": 50.0,
    "overall_coverage_pct": 80.0,
    "p0_pass_rate_pct": 100.0,
    "p1_pass_rate_pct": 95.0,
    "overall_pass_rate_pct": 90.0,
    "max_open_p0_defects": 0,
    "max_open_p1_defects": 3,
    "performance_p95_ms": 3000,
    "performance_p99_ms": 5000,
    "performance_error_rate_pct": 1.0,
    "max_critical_security_findings": 0,
    "max_high_security_findings": 2,
    "min_code_coverage_pct": 80.0,
    "max_duplication_pct": 5.0,
}


@router.post("/check", dependencies=[Depends(_require_api_key)])
async def check_gate(body: GateCheckRequest, request: Request, response: Response):
    """
    Run quality gate assessment for a build.
    Returns PASS/CONCERNS/FAIL/WAIVED decision with full evidence.

    Phase 5.4: when the build_id matches a run still in the evidence-collection
    phase, return 202 + Retry-After so the client doesn't get a decision based
    on a partial manifest. The check waits for the manifest sidecar to exist
    on disk (written at the END of `_persist_run_to_db`).
    """
    from ..db_adapter import try_db

    # Phase 5.4 — Evidence-manifest readiness gate. The manifest sidecar
    # (written by Phase 3.3) only exists once the run has reached the very
    # end of `_persist_run_to_db`. Polling the gate before then would compute
    # a decision against an incomplete evidence set — refuse with 202.
    try:
        from pathlib import Path as _Path
        results_dir = _Path(os.environ.get("ARTA_RESULTS_DIR", "/tmp/arta-results"))
        # Phase 5 follow-up #4 — _REAL_RUNS is keyed by run_id, NOT build_id,
        # so the previous `_REAL_RUNS.get(body.build_id)` was effectively
        # dead code. Scan values for the matching build_id; map to the
        # run's manifest path. When build_id itself is a run_id (e.g. CI
        # passes the run_id as build identifier), the dict get path
        # below catches that case too.
        try:
            from .execution import _REAL_RUNS
            run_state = (
                _REAL_RUNS.get(body.build_id)
                or next(
                    (r for r in _REAL_RUNS.values() if r.get("build_id") == body.build_id),
                    None,
                )
            )
        except Exception:
            run_state = None
        run_id_for_manifest = (run_state or {}).get("run_id") or body.build_id
        manifest_path = results_dir / f"{run_id_for_manifest}-manifest.json"
        run_status = (run_state or {}).get("status", "").lower()
        # When the run exists but is not yet terminal AND the manifest
        # sidecar isn't on disk yet, defer the decision.
        if run_state and run_status not in {"completed", "failed", "cancelled", "passed"}:
            if not manifest_path.is_file():
                response.status_code = 202
                response.headers["Retry-After"] = "5"
                return {
                    "status": "pending",
                    "build_id": body.build_id,
                    "run_id": run_id_for_manifest,
                    "reason": (
                        f"Evidence manifest not yet written for in-flight run "
                        f"(status={run_status or 'unknown'}). Retry after 5 seconds."
                    ),
                    "manifest_expected_at": str(manifest_path),
                }
    except Exception as _ev_gate_exc:
        # Best-effort — never block a healthy gate on the readiness probe.
        import logging as _logging_local
        _logging_local.getLogger("arta.gates").debug(
            "gates/check: evidence-readiness probe skipped: %s", _ev_gate_exc
        )

    # Check DB first for stored decision
    async with try_db() as db:
        if db:
            from ...db.repository import QualityGateRepo, _to_dict
            repo = QualityGateRepo(db)
            existing = await repo.get_for_build(body.build_id)
            if existing:
                return _to_dict(existing)

    # Check mock store
    if body.build_id in MOCK_GATES:
        return MOCK_GATES[body.build_id]

    # For dynamic builds: run real quality gate agent
    from ...agents.quality_gate_agent import QualityGateAgent, GateThresholds
    from ...agents.traceability_agent import TraceabilityAgent

    traceability = TraceabilityAgent(getattr(request.app.state, "neo4j", None))
    coverage_report = await traceability.get_coverage_report()

    thresholds = GateThresholds(**{
        k: v for k, v in CURRENT_THRESHOLDS.items()
        if k not in ("environment",) and hasattr(GateThresholds, k)
    })
    gate = QualityGateAgent(thresholds=thresholds)

    # F3-7 + F5-1: Pull execution-derived signals from the most recent runs.
    # - execution_history feeds the flakiness gate
    # - run.nfr (a11y_violations_*, etc.) merges into coverage_report["nfr"]
    #   so checks like _check_a11y see real values instead of treating the
    #   keys as absent (silently skipped).
    execution_history: list[dict] = []
    merged_nfr: dict = dict(coverage_report.get("nfr") or {})
    # run-dea20e follow-up: aggregate the unresolved Newman path-params across
    # in-flight + recently-completed runs so the gate's
    # `_check_unresolved_path_params` has signal to fire on. Without this
    # merge, the new check at quality_gate_agent never sees the per-run dict.
    unresolved_pp: set[str] = set(coverage_report.get("unresolved_path_params") or [])
    # Phase J review-fix: sequence_integrity from the most recent run with
    # one populated. Without this merge, the gate's _check_call_sequence_integrity
    # always sees `{}` → cascade attribution silently no-ops, mirroring the
    # exact bug J11 fixed for the analyze_failures callers.
    most_recent_seq_integrity: dict | None = None
    most_recent_seq_started_at: str = ""
    try:
        from .execution import _REAL_RUNS
        for run in _REAL_RUNS.values():
            for tr in (run.get("results") or []):
                tid = tr.get("test_id") or tr.get("id")
                status = tr.get("status")
                if tid and status:
                    execution_history.append({"test_id": tid, "status": status})
            # Take the latest non-empty NFR block — multiple runs may contribute
            # different dimensions; later runs override earlier ones for the same key.
            run_nfr = run.get("nfr") or {}
            for k, v in run_nfr.items():
                if v is not None:
                    merged_nfr[k] = v
            for p in (run.get("unresolved_path_params") or []):
                if p:
                    unresolved_pp.add(p)
            # Pull sequence_integrity from the most recent run that has one.
            # Strict `>` so runs with empty started_at don't shadow later real
            # ones; first run with a real timestamp wins on tie.
            run_seq = run.get("sequence_integrity")
            if isinstance(run_seq, dict) and run_seq:
                started_at = run.get("finished_at") or run.get("started_at") or ""
                if started_at and started_at > most_recent_seq_started_at:
                    most_recent_seq_started_at = started_at
                    most_recent_seq_integrity = run_seq
                elif most_recent_seq_integrity is None:
                    # Fallback when no run has a real timestamp yet.
                    most_recent_seq_integrity = run_seq
    except Exception:
        execution_history = []
    if unresolved_pp:
        coverage_report["unresolved_path_params"] = sorted(unresolved_pp)
    if most_recent_seq_integrity:
        coverage_report["sequence_integrity"] = most_recent_seq_integrity

    # Supplement with PostgreSQL execution history — survives server restarts
    # so the flakiness gate can evaluate rolling-window data even after a restart.
    try:
        from ..db_adapter import try_db
        from sqlalchemy import select as _sa_select
        async with try_db() as _db:
            if _db:
                from ...db.models import ExecutionResult as _ER
                _rows = (await _db.execute(
                    _sa_select(_ER.test_id, _ER.status)
                    .order_by(_ER.executed_at.desc())
                    .limit(200)
                )).all()
                _seen_ids = {e["test_id"] for e in execution_history}
                for _row in _rows:
                    if _row.test_id not in _seen_ids:
                        execution_history.append({"test_id": _row.test_id, "status": str(_row.status)})
    except Exception:
        pass

    coverage_report["nfr"] = merged_nfr

    # BMAD RV: scan generated test files for canonical anti-patterns
    # (waitForTimeout, hard sleeps, conditional assertions). Surfaced as a
    # CONCERNS dimension in the gate via _check_test_quality. Cheap regex
    # scan over already-validated source files; safe to run unconditionally.
    try:
        from ...agents.automation_engineer import AutomationEngineerAgent
        from pathlib import Path as _Path
        _automation_root = _Path(os.environ.get("ARTA_AUTOMATION_DIR", "src/automation"))
        _candidates: list[str] = []
        if _automation_root.exists():
            for sub in ("playwright", "k6", "cypress", "selenium", "appium"):
                d = _automation_root / sub
                if d.is_dir():
                    _candidates.extend(str(p) for p in d.rglob("*")
                                       if p.is_file() and not p.name.endswith(".broken"))
        coverage_report["test_quality"] = AutomationEngineerAgent.scan_test_quality(_candidates)
    except Exception as _tq_exc:
        import logging as _logging
        _logging.getLogger("arta.gates").debug("test-quality scan skipped: %s", _tq_exc)

    # F6-12: Load the project's most recent strategy artifact (F5-2) and pass
    # the risk profiles into the gate so risk-coverage and score=9 auto-fail
    # rules fire against real risk data rather than an empty list. Without this
    # the strategy artifact is write-only — auditable on disk but never consulted.
    risk_profiles_for_gate: list[dict] = []
    strategy_summary: dict | None = None
    try:
        import json as _json
        from pathlib import Path as _Path
        _strat_dir = _Path(os.environ.get("ARTA_STRATEGIES_DIR", ".arta/strategies"))
        # Strategy filenames are `{project_id}_{ts}_{trace}.json`.
        # build_id may not equal project_id; pick the newest artifact regardless of project
        # since the gate is build-scoped and the strategy is run-scoped.
        if _strat_dir.exists():
            _candidates = sorted(_strat_dir.glob("*.json"),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
            if _candidates:
                _data = _json.loads(_candidates[0].read_text())
                risk_profiles_for_gate = _data.get("profiles") or []
                strategy_summary = {
                    "trace_id": _data.get("trace_id"),
                    "model": _data.get("model"),
                    "generated_at": _data.get("generated_at"),
                    "risk_distribution": _data.get("risk_distribution"),
                    "top_risks": (_data.get("top_risks_score_ge_6") or [])[:5],
                }
    except Exception as _strat_exc:
        import logging as _logging
        _logging.getLogger("arta.gates").debug("strategy artifact load skipped: %s", _strat_exc)

    # F7-1: Build the full strategy dict (not just the summary) so the gate's
    # _check_compliance can read `compliance_markers` from it.
    strategy_for_gate: dict | None = None
    if strategy_summary and risk_profiles_for_gate is not None:
        # Reload the same artifact we summarised; cheap, file is already cached.
        try:
            import json as _json2
            from pathlib import Path as _Path2
            _strat_dir = _Path2(os.environ.get("ARTA_STRATEGIES_DIR", ".arta/strategies"))
            _candidates = sorted(_strat_dir.glob("*.json"),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
            if _candidates:
                strategy_for_gate = _json2.loads(_candidates[0].read_text())
        except Exception:
            strategy_for_gate = None

    # R-GateDefectAware — load defects from DB before passing to the gate
    # decision. Pre-fix this passed an empty list, making the
    # "Open P0 defects ≤ 0" check tautologically pass even when 100+ P0
    # defects existed in the database. Verified live in run-c87ee5: 2
    # priority=P0 defects in DB, but dashboard badge showed
    # "Open P0 defects: 0 ✓".
    gate_defects: list[dict] = []
    try:
        from ..db_adapter import try_db
        from ...db.repository import DefectRepo, _to_dict
        async with try_db() as _db:
            if _db is not None:
                _repo = DefectRepo(_db)
                # Default: open defects across the project this run belongs to.
                _project_id_for_gate = (
                    coverage_report.get("project_id")
                    if isinstance(coverage_report, dict) else None
                )
                _rows, _ = await _repo.list(
                    project_id=_project_id_for_gate,
                    status="open",
                    limit=500,
                )
                gate_defects = [_to_dict(r) for r in _rows]
                # Coerce the enum-typed `severity`/`priority`/`status` fields
                # to plain strings so the gate's str-equality checks work.
                for d in gate_defects:
                    for k in ("severity", "priority", "status"):
                        v = d.get(k)
                        if hasattr(v, "value"):
                            d[k] = v.value
                        elif v is not None:
                            d[k] = str(v)
    except Exception as _defect_exc:
        import logging as _l
        _l.getLogger("arta.gates").warning(
            "R-GateDefectAware: defect load failed (defaulting to []): %s",
            _defect_exc,
        )

    decision = await gate.decide(
        coverage_report=coverage_report,
        risk_profiles=risk_profiles_for_gate,
        defects=gate_defects,
        execution_history=execution_history or None,
        strategy=strategy_for_gate,
    )
    if strategy_summary:
        decision["strategy_summary"] = strategy_summary  # F6-12: surface in response

    result = {
        "build_id": body.build_id,
        "environment": body.environment,
        **decision,
    }

    # Persist to DB if available — F7-5: persistence failures must not 500 the
    # whole gate response. The decision was already computed above; if the DB
    # rejects the row (e.g. NOT-NULL on run_id when called directly via /check
    # without a run context) we log + continue so the API still returns the
    # decision to the caller. Persistence is a separate concern from gating.
    try:
        async with try_db() as db:
            if db:
                from ...db.repository import QualityGateRepo
                repo = QualityGateRepo(db)
                await repo.create({
                    "build_id": body.build_id,
                    "environment": body.environment,
                    "decision": decision.get("decision", "FAIL"),
                    "summary": decision.get("summary", ""),
                    "blocking_checks": decision.get("blocking", []),
                    "warnings": decision.get("warnings", []),
                    "passed_checks": decision.get("passed", []),
                    "evidence_package_id": decision.get("evidence_package_id"),
                })
    except Exception as _persist_exc:
        import logging as _lg
        _lg.getLogger("arta.gates").warning(
            "gates/check: persist failed (decision returned anyway): %s", _persist_exc,
        )

    # Notify if failed (Gap-1.5: supervised — logs exceptions instead of silent fail)
    if result.get("decision") == "FAIL":
        notifier = getattr(request.app.state, "notifier", None)
        if notifier:
            import asyncio
            from ...observability.task_supervisor import supervise
            supervise(
                asyncio.create_task(notifier.notify_gate_blocked(result)),
                "gate_notify_blocked",
            )

    # Create GitHub check run if commit_sha provided (Gap-1.5: supervised)
    github = getattr(request.app.state, "github", None)
    if github and body.commit_sha:
        import asyncio
        from ...observability.task_supervisor import supervise
        supervise(
            asyncio.create_task(github.create_check_run(
                head_sha=body.commit_sha,
                name="ARTA Quality Gate",
                conclusion="success" if result["decision"] == "PASS" else "failure",
                summary=result.get("summary", ""),
            )),
            "github_check_run",
        )

    return result


@router.get("/thresholds", dependencies=[Depends(_require_api_key)])
async def get_thresholds():
    """Get current quality gate thresholds — loads from DB if available."""
    from ..db_adapter import try_db
    try:
        async with try_db() as db:
            if db:
                from sqlalchemy import text
                row = (await db.execute(text(
                    "SELECT value FROM platform_config WHERE key = 'gate_thresholds'"
                ))).first()
                if row and row[0]:
                    import json
                    stored = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                    CURRENT_THRESHOLDS.update(stored)
    except Exception:
        pass
    return CURRENT_THRESHOLDS


@router.put("/thresholds", dependencies=[Depends(_require_api_key), Depends(require_role("admin"))])
async def update_thresholds(body: ThresholdUpdate):
    """Update quality gate thresholds (admin only). Persists to DB."""
    CURRENT_THRESHOLDS.update(body.model_dump())

    # Persist to DB
    from ..db_adapter import try_db
    try:
        async with try_db() as db:
            if db:
                import json
                from sqlalchemy import text
                await db.execute(text("""
                    INSERT INTO platform_config (key, value, updated_at)
                    VALUES ('gate_thresholds', CAST(:val AS jsonb), NOW())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """), {"val": json.dumps(CURRENT_THRESHOLDS)})
                await db.commit()
    except Exception as exc:
        import logging
        logging.getLogger("arta.gates").warning("Failed to persist thresholds to DB: %s", exc)

    return {"message": "Thresholds updated", "thresholds": CURRENT_THRESHOLDS}


@router.post("/waivers", dependencies=[Depends(require_role("admin"))])
async def create_waiver(body: WaiverRequest, request: Request):
    """
    Create a gate waiver — authorized exception for a specific check.
    BMAD TEA: Waivers require rationale + expiry. Admin-only.
    """
    from ..db_adapter import try_db

    waiver_data = {
        "gate_id": body.gate_id,
        "waived_check": body.waived_check,
        "rationale": body.rationale,
        "expires_at": body.expires_at.isoformat(),
    }

    async with try_db() as db:
        if db:
            from ...db.repository import GateWaiverRepo
            repo = GateWaiverRepo(db)
            created = await repo.create(waiver_data)
            return {"message": "Waiver created", "waiver": created}

    # Fallback: return in-memory response
    return {"message": "Waiver created (in-memory)", "waiver": waiver_data}


@router.get("/nfr", dependencies=[Depends(_require_api_key)])
async def get_nfr_assessment(
    build_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
):
    """Return NFR assessment computed from real test execution results.

    R68.5 — `run_id` lets operator pin the assessment to a specific run
    rather than the auto-selected latest one. Pre-R68.5 the endpoint
    silently auto-selected latest; operator had no way to view NFR for
    a prior run from the dashboard. The frontend's RunScopeSelector
    threads the chosen run via this param.

    If no execution data exists, returns needs_execution=True so the frontend
    can prompt the user to run tests first.
    """
    from .execution import _REAL_RUNS, _REAL_RESULTS

    real_results: list[dict] = []
    latest_run: dict | None = None

    # R68.5 — when an explicit run_id is supplied, use THAT run; otherwise
    # auto-select the latest completed run (pre-R68.5 behaviour).
    if run_id:
        cand = _REAL_RUNS.get(run_id) or {}
        # Honor both explicit run_id and `id`-keyed dicts (mock paths)
        if not cand:
            for _rid, _meta in _REAL_RUNS.items():
                if _meta.get("run_id") == run_id or _meta.get("id") == run_id:
                    cand = _meta
                    break
        if cand:
            latest_run = cand

    if latest_run is None:
        # Find the latest COMPLETED run for this project (not aggregated across all runs)
        for _rid, run_meta in _REAL_RUNS.items():
            if project_id and run_meta.get("project_id") != project_id:
                continue
            if run_meta.get("status") != "completed":
                continue
            if latest_run is None or run_meta.get("started_at", "") > latest_run.get("started_at", ""):
                latest_run = run_meta

    # G2.4 (G5): Cache NFR result keyed by the latest run_id.
    # Runs are immutable after completion → 5 min TTL is just memory hygiene.
    from ...observability.cache import cache
    _nfr_cache_key = f"nfr:{(latest_run or {}).get('run_id', 'none')}:{build_id or ''}:{run_id or ''}"
    _cached_nfr = await cache.get(_nfr_cache_key)
    if _cached_nfr is not None:
        return _cached_nfr

    # Use ONLY the latest run's results — ensures NFR matches Run History
    if latest_run:
        lr_id = latest_run.get("run_id") or latest_run.get("id", "")
        real_results = list(_REAL_RESULTS.get(lr_id, []))

    # Also check DB for persisted runs
    # R28.7 — also fetch test_id and the test_case's script_path so
    # the NFR "All Test Results" rows can link to the test source.
    # R115.D — when `latest_run` is known, scope the query by run_id so
    # ALL tool rows for that run are visible (pre-R115.D the LIMIT 100
    # ordered by created_at DESC across the entire project; newman
    # window → Accessibility tile reported "Not Assessed" even when
    # axe specs ran successfully). Run-scoped query lifts the cap +
    # represents the full per-tool distribution.
    if not real_results:
        from ..db_adapter import try_db
        async with try_db() as db:
            if db:
                from sqlalchemy import text
                _lr_id = (latest_run or {}).get("run_id") or (latest_run or {}).get("id") or run_id
                try:
                    if _lr_id:
                        # Run-scoped: fetch ALL rows for the chosen run
                        rows = (await db.execute(text("""
                            SELECT
                                er.status::text,
                                er.duration_ms,
                                er.title,
                                er.test_id,
                                tc.script_path,
                                er.automation_tool::text
                            FROM execution_results er
                            JOIN test_runs tr ON er.run_id = tr.id
                            LEFT JOIN test_cases tc ON tc.test_id = er.test_id
                            WHERE tr.run_id = :rid
                            ORDER BY er.id DESC
                        """), {"rid": _lr_id})).fetchall()
                    else:
                        # No run pinned → fall back to project-wide top-100
                        rows = (await db.execute(text("""
                            SELECT
                                er.status::text,
                                er.duration_ms,
                                er.title,
                                er.test_id,
                                tc.script_path,
                                er.automation_tool::text
                            FROM execution_results er
                            JOIN test_runs tr ON er.run_id = tr.id
                            LEFT JOIN test_cases tc ON tc.test_id = er.test_id
                            WHERE (CAST(:pid AS text) IS NULL OR tr.project_id = CAST(:pid AS uuid))
                            ORDER BY tr.created_at DESC LIMIT 100
                        """), {"pid": project_id})).fetchall()
                    for row in rows:
                        real_results.append({
                            "status": row[0],
                            "duration_ms": row[1],
                            "title": row[2],
                            "test_id": row[3],
                            "script_path": row[4],
                            "automation_tool": row[5],
                        })
                except Exception:
                    # Fall back to the original projection on schema mismatch
                    try:
                        rows = (await db.execute(text("""
                            SELECT er.status::text, er.duration_ms, er.title
                            FROM execution_results er
                            JOIN test_runs tr ON er.run_id = tr.id
                            WHERE (CAST(:pid AS text) IS NULL OR tr.project_id = CAST(:pid AS uuid))
                            ORDER BY tr.created_at DESC LIMIT 100
                        """), {"pid": project_id})).fetchall()
                        for row in rows:
                            real_results.append({"status": row[0], "duration_ms": row[1], "title": row[2]})
                    except Exception:
                        pass

    if not real_results:
        # Check if a run is currently in progress or recently failed
        project_runs = [
            r for r in _REAL_RUNS.values()
            if not project_id or r.get("project_id") == project_id
        ]
        active_runs = [r for r in project_runs if r.get("status") == "running"]
        failed_runs = sorted(
            [r for r in project_runs if r.get("status") == "failed"],
            key=lambda r: r.get("finished_at", ""), reverse=True,
        )
        if active_runs:
            run = active_runs[0]
            return {
                "categories": None,
                "needs_execution": False,
                "running": True,
                "run_id": run.get("run_id"),
                "started_at": run.get("started_at"),
                "project_id": project_id,
            }
        if failed_runs:
            run = failed_runs[0]
            return {
                "categories": None,
                "needs_execution": False,
                "failed": True,
                "run_id": run.get("run_id"),
                "error": run.get("error", "Test execution failed — check if the target application is running"),
                "project_id": project_id,
            }
        return {"categories": None, "needs_execution": True, "project_id": project_id}

    # ── Compute NFR from real execution data ──────────────────────────────
    durations = [r.get("duration_ms", 0) for r in real_results if r.get("duration_ms")]
    total = len(real_results)
    passed = sum(1 for r in real_results if str(r.get("status", "")).upper() in ("PASS", "PASSED"))
    failed = total - passed
    error_rate = round(failed / total * 100, 2) if total else 0
    pass_rate = round(passed / total * 100, 1) if total else 0

    avg_ms = round(sum(durations) / len(durations)) if durations else 0

    # ── Percentile helper ──────────────────────────────────────────────────
    sorted_durations = sorted(durations) if durations else []
    def _percentile(pct: float) -> int:
        if not sorted_durations:
            return 0
        idx = min(int(len(sorted_durations) * pct), len(sorted_durations) - 1)
        return sorted_durations[idx]

    # P6 (truthful NFR) — Performance must derive from k6 (the perf tool), NOT
    # from ALL test-execution durations. Pre-P6 p95 was the 95th-percentile of
    # every PW/Newman spec's run time (run-3a810c: p95 36.4s = a slow UI spec) —
    # test-runner latency mislabeled as SUT-load performance. Scope to k6; if no
    # k6 ran, Performance is NOT measured (not FAIL).
    def _tool_of_perf(r):
        return (r.get("automation_tool") or r.get("tool") or "").lower()
    perf_results = [r for r in real_results if _tool_of_perf(r) in ("k6", "performance", "perf")]
    perf_measured = len(perf_results) > 0
    _perf_durs = sorted(r.get("duration_ms", 0) for r in perf_results if r.get("duration_ms"))
    def _perf_pct(pct: float) -> int:
        if not _perf_durs:
            return 0
        return _perf_durs[min(int(len(_perf_durs) * pct), len(_perf_durs) - 1)]
    p95_ms = _perf_pct(0.95)
    p99_ms = _perf_pct(0.99)
    p95_s = round(p95_ms / 1000, 2)
    p99_s = round(p99_ms / 1000, 2)
    perf_err_rate = (round(sum(1 for r in perf_results
                               if str(r.get("status", "")).upper() not in ("PASS", "PASSED"))
                           / len(perf_results) * 100, 2) if perf_results else 0)

    # Performance category
    perf_pass = perf_measured and p95_s <= 3.0 and perf_err_rate <= 1.0
    perf_findings = []
    if not perf_measured:
        perf_findings.append({"id": "perf-none", "description": "No k6 performance tests were executed — SUT load performance is NOT measured (functional test durations are not a load-performance signal).", "severity": "info"})
    if perf_measured and p95_s > 3.0:
        perf_findings.append({"id": "perf-p95", "description": f"k6 p95 {p95_s}s exceeds 3s threshold", "severity": "high" if p95_s > 5.0 else "medium"})
    if perf_measured and perf_err_rate > 1.0:
        perf_findings.append({"id": "perf-err", "description": f"k6 error rate {perf_err_rate}% exceeds 1% threshold", "severity": "high" if perf_err_rate > 5.0 else "medium"})

    # Reliability: derive from pass consistency
    flaky_count = sum(1 for r in real_results if str(r.get("status", "")).upper() == "FLAKY")
    flaky_rate = round(flaky_count / total * 100, 1) if total else 0
    reliability_pass = pass_rate >= 90 and flaky_rate < 2

    duration_distribution = {
        "p50": _percentile(0.50),
        "p75": _percentile(0.75),
        "p90": _percentile(0.90),
        "p95": _percentile(0.95),
        "p99": _percentile(0.99),
        "max": sorted_durations[-1] if sorted_durations else 0,
    }

    # ── Build per-test detail lists ─────────────────────────────────────────
    # R28.7 — surface `test_id` and `script_path` so the NFR Assessment
    # "All Test Results" rows can link to the test source via Test
    # Explorer (`/test-explorer?test_id=...`). Pre-R28.7 these fields
    # were dropped here, leaving operators with a non-clickable list of
    # test names.
    test_details = []
    for r in real_results:
        test_details.append({
            "test_id": r.get("test_id") or "",
            "script_path": r.get("script_path") or "",
            "title": r.get("title", "Unknown"),
            "status": str(r.get("status", "UNKNOWN")).upper(),
            "duration_ms": r.get("duration_ms", 0),
            "error_message": r.get("error_message") or r.get("error") or None,
            "tool": r.get("automation_tool") or r.get("tool") or "playwright",
        })

    # Slowest 5 tests
    slowest_tests = sorted(test_details, key=lambda t: t["duration_ms"] or 0, reverse=True)[:5]

    # Failed tests with error details
    failed_tests = [t for t in test_details if t["status"] in ("FAIL", "FAILED", "ERROR")]

    # ── Security: detect if dedicated security tests were executed ───────
    # P6 (truthful NFR) — detect by TOOL, not title keywords. The keyword match
    # false-positived on functional negative tests ("SQL injection prevented",
    # "Unauthorized returns 401") → the panel showed Security "Measured" with the
    # 700-test functional summary when NO ZAP ran (run-3a810c). A category is
    # "measured" only when its dedicated tool actually produced results.
    def _tool_of(r):
        return (r.get("automation_tool") or r.get("tool") or "").lower()
    security_results = [r for r in real_results if _tool_of(r) in ("zap", "owasp", "security")]
    has_security_tests = len(security_results) > 0
    sec_passed = sum(1 for r in security_results if str(r.get("status", "")).upper() in ("PASS", "PASSED"))
    sec_failed = len(security_results) - sec_passed

    sec_findings = []
    if not has_security_tests:
        sec_findings.append({
            "id": "sec-no-tests",
            "description": "No dedicated security tests were executed (no ZAP/security-tool results). Security is NOT measured — configure & run OWASP ZAP for a real assessment. (Functional pass/fail is reported under Reliability, not here.)",
            "severity": "info",
        })
    # P6 — security findings are security-scoped, not the functional failure count.
    if has_security_tests and sec_failed > 0:
        sec_findings.append({"id": "sec-fail", "description": f"{sec_failed} security test(s) failed", "severity": "high"})

    # R115.D — derive Accessibility coverage from actual axe test results.
    # Pre-R115.D the tile was hardcoded "Not Assessed" regardless of whether
    # axe specs ran successfully. Live evidence run-f45f85: 12 axe PASS for
    # Executed 0", misleading operators into believing a11y was unconfigured.
    # Fix: count axe-tool rows + compute WCAG coverage = passed / total.
    a11y_total = 0
    a11y_passed = 0
    a11y_failed = 0
    for r in real_results:
        _tool = (r.get("automation_tool") or r.get("tool") or "").lower()
        if _tool != "axe":
            continue
        a11y_total += 1
        _status = str(r.get("status", "")).upper()
        if _status in ("PASS", "PASSED"):
            a11y_passed += 1
        elif _status in ("FAIL", "FAILED", "ERROR"):
            a11y_failed += 1
    has_a11y_tests = a11y_total > 0
    a11y_pass_rate = round((a11y_passed / a11y_total) * 100, 1) if a11y_total else 0.0
    a11y_findings = []
    if not has_a11y_tests:
        a11y_findings.append({
            "id": "a11y-not-assessed",
            "description": "No accessibility tests configured. Integrate axe-core in Playwright tests or run a WCAG scan for compliance assessment.",
            "severity": "info",
        })
    elif a11y_failed > 0:
        a11y_findings.append({
            "id": "a11y-fail",
            "description": f"{a11y_failed} axe accessibility test(s) failed — WCAG violations detected; review the failing specs.",
            "severity": "high",
        })

    categories = [
        {
            "name": "Performance",
            "icon": "⚡",
            # P6 — NOT_ASSESSED when no k6 ran; else derive from k6-scoped metrics.
            "status": ("NOT_ASSESSED" if not perf_measured
                       else ("PASS" if perf_pass else ("CONCERNS" if p95_s <= 5.0 else "FAIL"))),
            "score": (0 if not perf_measured
                      else max(0, 100 - int(perf_err_rate * 10) - max(0, int((p95_s - 1.0) * 15)))),
            "data_source": "k6_performance" if perf_measured else "no_k6",
            "confidence": "high" if perf_measured else "none",
            "metrics": [
                {"label": "k6 Tests", "value": "None run" if not perf_measured else str(len(perf_results)), "threshold": "≥1", "pass": perf_measured},
                {"label": "p95 Response Time", "value": "—" if not perf_measured else f"{p95_s}s", "threshold": "≤3s", "pass": perf_measured and p95_s <= 3.0},
                {"label": "p99 Response Time", "value": "—" if not perf_measured else f"{p99_s}s", "threshold": "≤5s", "pass": perf_measured and p99_s <= 5.0},
                {"label": "Error Rate", "value": "—" if not perf_measured else f"{perf_err_rate}%", "threshold": "≤1%", "pass": perf_measured and perf_err_rate <= 1.0},
            ],
            "findings": perf_findings,
        },
        {
            "name": "Security",
            "icon": "🛡️",
            # P6 — scope to actual ZAP/security-tool results, not the functional total.
            "status": "NOT_ASSESSED" if not has_security_tests else ("PASS" if sec_failed == 0 else "CONCERNS"),
            "score": 0 if not has_security_tests else (100 if sec_failed == 0 else max(50, 100 - sec_failed * 10)),
            "data_source": "security_tests" if has_security_tests else "no_security_tests",
            "confidence": "high" if has_security_tests else "none",
            "metrics": [
                {"label": "Security Tests", "value": "None run (no ZAP)" if not has_security_tests else str(len(security_results)), "threshold": "≥1", "pass": has_security_tests},
                {"label": "Test Failures", "value": str(sec_failed) if has_security_tests else "—", "threshold": "0", "pass": sec_failed == 0},
            ],
            "findings": sec_findings,
        },
        {
            "name": "Accessibility",
            "icon": "♿",
            "status": (
                "NOT_ASSESSED" if not has_a11y_tests
                else ("PASS" if a11y_failed == 0 else "CONCERNS")
            ),
            "score": (
                0 if not has_a11y_tests
                else (100 if a11y_failed == 0 else max(50, 100 - a11y_failed * 10))
            ),
            "data_source": "axe_results" if has_a11y_tests else "no_a11y_tests",
            "confidence": "high" if has_a11y_tests else "none",
            "metrics": [
                {
                    "label": "WCAG Coverage",
                    "value": "Not Assessed" if not has_a11y_tests else f"{a11y_pass_rate}% ({a11y_passed}/{a11y_total} specs PASS)",
                    "threshold": "Required",
                    "pass": has_a11y_tests and a11y_failed == 0,
                },
                {
                    "label": "Tests Executed",
                    "value": str(a11y_total),
                    "threshold": "≥1",
                    "pass": a11y_total >= 1,
                },
                {
                    "label": "Failures",
                    "value": str(a11y_failed),
                    "threshold": "0",
                    "pass": a11y_failed == 0,
                },
            ],
            "findings": a11y_findings,
        },
        {
            "name": "Reliability",
            "icon": "🔄",
            "status": "PASS" if reliability_pass else "CONCERNS",
            "score": int(pass_rate),
            "data_source": "execution",
            "confidence": "high",
            "metrics": [
                {"label": "Pass Rate", "value": f"{pass_rate}%", "threshold": "≥90%", "pass": pass_rate >= 90},
                {"label": "Flaky Rate", "value": f"{flaky_rate}%", "threshold": "<2%", "pass": flaky_rate < 2},
                {"label": "Total Executed", "value": str(total), "threshold": "≥1", "pass": total >= 1},
            ],
            "findings": [{"id": "rel-passrate", "description": f"Pass rate {pass_rate}% below 90% threshold", "severity": "high" if pass_rate < 80 else "medium"}] if pass_rate < 90 else [],
        },
        {
            "name": "Maintainability",
            "icon": "🔧",
            "status": "PASS" if total >= 5 else "CONCERNS",
            "score": min(100, total * 10),
            "data_source": "execution",
            "confidence": "medium",
            "metrics": [
                {"label": "Test Count", "value": str(total), "threshold": "≥5", "pass": total >= 5},
                {"label": "Coverage", "value": "Pending", "threshold": "≥80%", "pass": False},
            ],
            "findings": [{"id": "maint-cov", "description": "Code coverage tool not integrated — run Istanbul or Cobertura for accurate maintainability score.", "severity": "info"}] if total < 10 else [],
        },
    ]

    # ── Per-tool breakdown ────────────────────────────────────────────────
    from collections import defaultdict
    tool_breakdown: dict[str, dict] = {}
    for td in test_details:
        tool = td.get("tool", "playwright")
        if tool not in tool_breakdown:
            tool_breakdown[tool] = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "details": []}
        tool_breakdown[tool]["total"] += 1
        status = td.get("status", "")
        if status in ("PASS", "PASSED"):
            tool_breakdown[tool]["passed"] += 1
        elif status in ("FAIL", "FAILED", "ERROR"):
            tool_breakdown[tool]["failed"] += 1
        elif status in ("SKIP", "SKIPPED"):
            tool_breakdown[tool]["skipped"] += 1
        tool_breakdown[tool]["details"].append(td)

    # Add judge evaluations for analytics tests
    # Enrich analytics test details with judge evaluation (O(n) via dict lookup)
    results_by_title = {r.get("title"): r for r in real_results if r.get("title")}
    for td in test_details:
        if td.get("tool") == "pytest" and td.get("title") in results_by_title:
            r = results_by_title[td["title"]]
            td["judge_evaluation"] = r.get("judge_evaluation")
            td["analytics_layer"] = r.get("analytics_layer")
            td["tier"] = r.get("tier")

    # R68.5 — surface which run the assessment is for. Frontend banner
    # shows operator "Run: X" so they know what they're looking at vs
    # silently auto-latest.
    _selected_run_id = (
        (latest_run.get("run_id") if latest_run else None)
        or run_id
        or build_id
    )
    _nfr_result = {
        "build_id": build_id or (latest_run.get("build_id") if latest_run else "latest"),
        "project_id": project_id,
        "selected_run_id": _selected_run_id,
        "selected_run_started_at": (latest_run.get("started_at") if latest_run else None),
        "categories": categories,
        "source": "execution",
        "total_tests": total,
        "passed": passed,
        "failed": failed,
        "test_details": test_details,
        "slowest_tests": slowest_tests,
        "failed_tests": failed_tests,
        "duration_distribution": duration_distribution,
        "tool_breakdown": tool_breakdown,
    }
    # G2.4 (G5): Cache completed-run NFR for 5 min. Results are immutable by run_id.
    if latest_run and latest_run.get("status") == "completed":
        await cache.set(_nfr_cache_key, _nfr_result, ttl_seconds=300.0)
    return _nfr_result


def _grade_from_score(score: int) -> str:
    """Convert numeric quality score to letter grade."""
    if score >= 95: return "A+"
    if score >= 90: return "A"
    if score >= 85: return "B+"
    if score >= 80: return "B"
    if score >= 75: return "C+"
    if score >= 70: return "C"
    if score >= 60: return "D"
    return "F"


def _compute_violations(
    pass_rate: float,
    gate_decision: str,
    *,
    coverage_pct: float | None = None,
    open_defects: int | None = None,
    p0_defects: int | None = None,
) -> list[dict]:
    """Compute quality violations from gate check data."""
    violations = []
    if coverage_pct is not None and coverage_pct < 80:
        sev = "P0" if coverage_pct < 60 else "P1"
        violations.append({
            "severity": sev,
            "description": f"Requirement coverage at {coverage_pct:.0f}% — below 80% threshold",
            "recommendation": "Generate and link ATDD test cases for uncovered requirements",
        })
    if pass_rate < 100:
        sev = "P0" if pass_rate < 90 else "P1"
        violations.append({
            "severity": sev,
            "description": f"Pass rate at {pass_rate:.1f}%, below 100% target",
            "recommendation": "Investigate and fix failing tests before release",
        })
    if open_defects:
        sev = "P0" if (p0_defects or 0) > 0 else "P1"
        violations.append({
            "severity": sev,
            "description": (
                f"{open_defects} open defect{'s' if open_defects != 1 else ''} detected"
                + (f" — {p0_defects} P0 critical" if (p0_defects or 0) > 0 else "")
            ),
            "recommendation": "Resolve defects before quality gate evaluation",
        })
    if gate_decision in ("FAIL", "CONCERNS"):
        violations.append({
            "severity": "P1" if gate_decision == "FAIL" else "P2",
            "description": f"Quality gate decision: {gate_decision}",
            "recommendation": "Review gate check failures and address blockers",
        })
    return violations


@router.get("/latest", dependencies=[Depends(_require_api_key)])
async def get_latest_quality_score(project_id: str | None = None, limit: int = 30):
    """Return latest quality score for a project with real violations and progression."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            try:
                from sqlalchemy import text
                pid_filter = "AND project_id = CAST(:pid AS uuid)" if project_id else ""
                params: dict = {"pid": project_id} if project_id else {}

                row = (await db.execute(text(f"""
                    SELECT pass_rate, gate_decision, gate_summary
                    FROM test_runs WHERE completed_at IS NOT NULL {pid_filter}
                    ORDER BY completed_at DESC LIMIT 1
                """), params)).first()

                if row:
                    pass_rate = float(row[0] or 0)
                    gate_decision = row[1] or "PASS"

                    from .requirements import list_requirements
                    req_data = await list_requirements(project_id=project_id)
                    reqs = req_data.get("requirements", [])
                    cov = round(sum(r.get("coverage_pct", 0) for r in reqs) / max(len(reqs), 1), 1) if reqs else 0

                    nfr_data = await get_nfr_assessment(project_id=project_id)
                    nfr_cats = nfr_data.get("categories") or []
                    # 0 when NFR has not been assessed — consistent with NFR Assessment "No Data" state
                    nfr_score = round(sum(c.get("score", 0) for c in nfr_cats) / max(len(nfr_cats), 1)) if nfr_cats else 0

                    hi_risk = [r for r in reqs if r.get("priority") in ("P0", "P1")]
                    risk_cov = round(sum(r.get("coverage_pct", 0) for r in hi_risk) / max(len(hi_risk), 1)) if hi_risk else min(100, round(cov * 1.1))

                    score = int(cov * 0.25 + pass_rate * 0.35 + nfr_score * 0.25 + risk_cov * 0.15)

                    from .defects import list_defects
                    defect_data = await list_defects(project_id=project_id)
                    defects_list = defect_data.get("defects", [])
                    open_d = sum(1 for d in defects_list if d.get("status") == "open")
                    p0_d = sum(1 for d in defects_list if d.get("status") == "open" and d.get("severity") == "P0")

                    violations = _compute_violations(
                        pass_rate, gate_decision,
                        coverage_pct=cov, open_defects=open_d or None, p0_defects=p0_d or None,
                    )

                    prog_rows = (await db.execute(text(f"""
                        SELECT pass_rate FROM test_runs
                        WHERE completed_at IS NOT NULL {pid_filter}
                        ORDER BY completed_at DESC LIMIT :lim
                    """), {**params, "lim": limit})).all()

                    prog_vals = list(reversed([
                        int(float(pr[0] or 0) * 0.35 + cov * 0.25 + nfr_score * 0.25 + risk_cov * 0.15)
                        for pr in prog_rows
                    ]))
                    prog_labels = [f"Run {i + 1}" for i in range(len(prog_vals))]

                    # R44.4 — surface RAW per-tool pass rate + skip
                    # count from the latest run so the dashboard can
                    # render the gate decision without an extra
                    # /api/gates/check round-trip. Mirrors the logic
                    # in _check_per_tool_raw_pass_rates / _check_zero_skips.
                    per_tool_raw = _compute_per_tool_raw_for_latest_run(project_id)
                    return {
                        "score": score,
                        "grade": _grade_from_score(score),
                        "dimensions": [
                            {"name": "Coverage",       "score": int(cov),                    "weight": 25},
                            {"name": "Pass Rate",      "score": int(pass_rate),              "weight": 30},
                            {"name": "Defect Density", "score": max(0, 100 - open_d * 10),   "weight": 20},
                            {"name": "NFR Compliance", "score": nfr_score,                   "weight": 15},
                            {"name": "Risk Coverage",  "score": risk_cov,                    "weight": 10},
                        ],
                        "violations": violations,
                        "progression": prog_vals,
                        "progression_labels": prog_labels,
                        "per_tool_raw": per_tool_raw,
                    }
            except Exception:
                pass

    # No DB — only compute when real runs exist for this project
    from .execution import _REAL_RUNS
    project_runs = sorted(
        [r for r in _REAL_RUNS.values() if not project_id or r.get("project_id") == project_id],
        key=lambda r: r.get("started_at", ""),
        reverse=True,
    )
    if not project_runs:
        # No runs executed for this project yet — return no-data state
        return {
            "score": None,
            "grade": None,
            "dimensions": [],
            "violations": [],
            "progression": [],
            "progression_labels": [],
            # R44.4 — empty payload so the frontend hides the panel
            # cleanly instead of showing stale data from a sibling
            # project's runs.
            "per_tool_raw": {"tools": [], "skip_count": 0,
                             "all_tools_pass_target": True, "target_pct": 95.0},
        }

    # Compute from the actual in-memory runs
    from .requirements import list_requirements
    from .defects import list_defects

    latest = project_runs[0]
    pass_rate = round(latest.get("passed", 0) / max(latest.get("total", 1), 1) * 100, 1)

    req_data = await list_requirements(project_id=project_id)
    reqs = req_data.get("requirements", [])
    total_r = len(reqs)
    cov = round(sum(r.get("coverage_pct", 0) for r in reqs) / max(total_r, 1), 1) if reqs else round(latest.get("coverage_pct", 0), 1)

    defect_data = await list_defects(project_id=project_id)
    defects_list = defect_data.get("defects", [])
    open_d = sum(1 for d in defects_list if d.get("status") == "open")
    p0_d   = sum(1 for d in defects_list if d.get("status") == "open" and d.get("severity") == "P0")

    nfr_data = await get_nfr_assessment(project_id=project_id)
    nfr_cats = nfr_data.get("categories") or []
    nfr_score = round(sum(c.get("score", 0) for c in nfr_cats) / max(len(nfr_cats), 1)) if nfr_cats else 0

    hi_risk = [r for r in reqs if r.get("priority") in ("P0", "P1")]
    risk_cov = round(sum(r.get("coverage_pct", 0) for r in hi_risk) / max(len(hi_risk), 1)) if hi_risk else min(100, round(cov * 1.1))

    # K3: Insight Accuracy from LLM-as-Judge scores on analytics tests.
    # Pull judge_score from latest run results; aggregate per project.
    insight_accuracy = _compute_insight_accuracy(project_runs)

    # Compose composite score. Weights re-balanced to include Insight Accuracy
    # only when the project actually has analytics tests; otherwise its weight
    # redistributes to the other 4 dimensions.
    if insight_accuracy is not None:
        score = int(cov * 0.20 + pass_rate * 0.30 + nfr_score * 0.20 + risk_cov * 0.10 + insight_accuracy * 0.20) if total_r else 0
    else:
        score = int(cov * 0.25 + pass_rate * 0.35 + nfr_score * 0.25 + risk_cov * 0.15) if total_r else 0

    violations = _compute_violations(
        pass_rate, latest.get("gate_decision", ""),
        coverage_pct=cov if total_r > 0 else None,
        open_defects=open_d or None,
        p0_defects=p0_d or None,
    ) if total_r > 0 else []

    # Progression from all real runs (oldest → newest)
    prog_vals = [
        int(r.get("passed", 0) / max(r.get("total", 1), 1) * 100 * 0.35 + cov * 0.25 + nfr_score * 0.25 + risk_cov * 0.15)
        for r in reversed(project_runs)
    ]
    prog_labels = [f"Run {i + 1}" for i in range(len(prog_vals))]

    # K3: Conditionally include Insight Accuracy dimension when analytics tests exist
    base_dims = [
        {"name": "Coverage",       "score": int(cov),                  "weight": 25},
        {"name": "Pass Rate",      "score": int(pass_rate),            "weight": 30},
        {"name": "Defect Density", "score": max(0, 100 - open_d * 10), "weight": 20},
        {"name": "NFR Compliance", "score": nfr_score,                 "weight": 15},
        {"name": "Risk Coverage",  "score": risk_cov,                  "weight": 10},
    ]
    if insight_accuracy is not None:
        # Re-weight when Insight Accuracy is present
        base_dims = [
            {"name": "Coverage",         "score": int(cov),                  "weight": 20},
            {"name": "Pass Rate",        "score": int(pass_rate),            "weight": 30},
            {"name": "Defect Density",   "score": max(0, 100 - open_d * 10), "weight": 15},
            {"name": "NFR Compliance",   "score": nfr_score,                 "weight": 10},
            {"name": "Risk Coverage",    "score": risk_cov,                  "weight": 10},
            {"name": "Insight Accuracy", "score": int(insight_accuracy),     "weight": 15},
        ]

    return {
        "score": score or None,
        "grade": _grade_from_score(score) if score else None,
        "dimensions": base_dims if total_r > 0 else [],
        "violations": violations,
        "progression": prog_vals,
        "progression_labels": prog_labels,
        # R44.4 — same per-tool RAW payload as the DB-backed branch.
        "per_tool_raw": _compute_per_tool_raw_for_latest_run(project_id),
    }


def _compute_per_tool_raw_for_latest_run(project_id: str | None) -> dict:
    """R44.4 — compute RAW per-tool pass rate + skip count for the
    latest run. Mirrors `quality_gate_agent._check_per_tool_raw_pass_rates`
    + `_check_zero_skips` but surfaces the data directly to the
    dashboard so operators see which tool is below the 95% bar
    without having to read the gate-check evidence panel.

    Returns:
        {
          "tools": [{tool, passed, failed, blocked, skipped, total, raw_pct, meets_target}],
          "skip_count": int,
          "all_tools_pass_target": bool,
          "target_pct": 95.0,
        }
    """
    try:
        from .execution import _REAL_RUNS
    except Exception:
        return {"tools": [], "skip_count": 0, "all_tools_pass_target": True, "target_pct": 95.0}
    runs = [
        r for r in _REAL_RUNS.values()
        if isinstance(r, dict)
        and (not project_id or r.get("project_id") == project_id)
    ]
    if not runs:
        return {"tools": [], "skip_count": 0, "all_tools_pass_target": True, "target_pct": 95.0}
    latest = max(runs, key=lambda r: r.get("started_at") or "")
    results = latest.get("results") or []
    if not isinstance(results, list):
        return {"tools": [], "skip_count": 0, "all_tools_pass_target": True, "target_pct": 95.0}

    per_tool: dict[str, dict[str, int]] = {}
    skip_count = 0
    for row in results:
        if not isinstance(row, dict):
            continue
        tool = (row.get("automation_tool") or row.get("tool") or "unknown").lower()
        slot = per_tool.setdefault(tool, {
            "passed": 0, "failed": 0, "blocked": 0, "skipped": 0, "total": 0,
        })
        slot["total"] += 1
        status = (row.get("status") or "").upper()
        if status == "PASS":
            slot["passed"] += 1
        elif status == "BLOCKED":
            slot["blocked"] += 1
            skip_count += 1
        elif status in ("SKIP", "SKIPPED"):
            slot["skipped"] += 1
            skip_count += 1
        else:
            slot["failed"] += 1

    target = 95.0
    tools_out = []
    all_meet = bool(per_tool)
    for tool, c in sorted(per_tool.items()):
        total = c["total"]
        if total == 0:
            continue
        raw_pct = round(100.0 * c["passed"] / total, 1)
        meets = raw_pct >= target
        if not meets:
            all_meet = False
        tools_out.append({
            "tool": tool,
            "passed": c["passed"],
            "failed": c["failed"],
            "blocked": c["blocked"],
            "skipped": c["skipped"],
            "total": total,
            "raw_pct": raw_pct,
            "meets_target": meets,
        })

    return {
        "tools": tools_out,
        "skip_count": skip_count,
        "all_tools_pass_target": all_meet and skip_count == 0,
        "target_pct": target,
    }


def _compute_insight_accuracy(project_runs: list) -> float | None:
    """K3: Mean judge_score across all analytics test results in this project's runs.

    Returns None if no analytics test has been judged yet — Quality Score then omits the
    Insight Accuracy dimension and re-weights the other 4 to sum to 100%.
    """
    scores: list[float] = []
    for run in project_runs:
        for r in (run.get("results") or []):
            s = r.get("judge_score")
            if isinstance(s, (int, float)):
                scores.append(float(s))
    if not scores:
        return None
    return round((sum(scores) / len(scores)) * 100, 1)  # 0-100 scale


# ── Catch-all: retrieve gate by build_id (must be LAST to avoid stealing /nfr, /thresholds, etc.) ──

@router.get("/{build_id}", dependencies=[Depends(_require_api_key)])
async def get_gate(build_id: str):
    """Retrieve stored gate decision for a build."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import QualityGateRepo, _to_dict
            repo = QualityGateRepo(db)
            row = await repo.get_for_build(build_id)
            if row:
                return _to_dict(row)

    gate = MOCK_GATES.get(build_id)
    if not gate:
        raise HTTPException(
            status_code=404,
            detail=f"No gate decision found for build {build_id}",
        )
    return gate


@router.get("/{build_id}/verify", dependencies=[Depends(_require_api_key)])
async def verify_gate_audit_trail(build_id: str) -> dict:
    """Gap 8b — BMAD Layer 6: tamper-evident audit-trail verification.

    Recomputes HMAC-SHA256 over the stored audit_trail entries using the
    ARTA_AUDIT_HMAC_SECRET key, then compares to the stored hash. Returns
    `{verified: bool, signed_at: str, ...}`.

    A `verified: false` result means either (a) the stored audit_trail JSON
    was edited after signing, OR (b) the HMAC secret has changed (e.g. key
    rotation, ephemeral-key fallback after restart). Audit teams should
    treat (a) as a compliance incident and (b) as a config-management gap.
    """
    from ..db_adapter import try_db
    summary = None
    async with try_db() as db:
        if db:
            from ...db.repository import QualityGateRepo, _to_dict
            repo = QualityGateRepo(db)
            row = await repo.get_for_build(build_id)
            if row:
                summary = (_to_dict(row) or {}).get("summary")
    if summary is None:
        gate = MOCK_GATES.get(build_id)
        if not gate:
            raise HTTPException(404, f"No gate decision found for build {build_id}")
        summary = gate
    if isinstance(summary, str):
        try:
            import json as _json
            summary = _json.loads(summary)
        except Exception:
            summary = {}
    if not isinstance(summary, dict):
        raise HTTPException(500, "Stored gate summary is not a JSON object")
    audit_trail = summary.get("audit_trail")
    stored_hash = summary.get("audit_trail_hash")
    signed_at = summary.get("audit_trail_signed_at")
    if not stored_hash or audit_trail is None:
        return {
            "build_id": build_id,
            "verified": False,
            "reason": "audit_trail or hash missing — gate predates Gap 8b signing",
        }
    from ...agents.quality_gate_agent import QualityGateAgent
    recomputed, _ = QualityGateAgent._sign_audit_trail(audit_trail)
    return {
        "build_id": build_id,
        "verified": recomputed == stored_hash,
        "stored_hash": stored_hash,
        "recomputed_hash": recomputed,
        "signed_at": signed_at,
    }
