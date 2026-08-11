"""ARTA Defects Router — Defect management + flakiness detection."""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

# F4-1: API-key check is centralised in src/api/dependencies.py.
# Aliased to keep all existing `Depends(_require_api_key)` call sites unchanged.
from ..dependencies import require_api_key as _require_api_key

log = logging.getLogger("arta.defects")

# F6-17: In production, demo defect / flaky-test seed data MUST NOT pollute
# real responses. We define the dev-mode lists below; the public MOCK_DEFECTS
# / MOCK_FLAKY constants are emptied in production so endpoints fall through
# to the DB without leaking demo records into customer dashboards.
_IS_PROD = os.environ.get("ENVIRONMENT", "development").lower() == "production"

from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
class DefectCreateRequest(BaseModel):
    title: str
    description: str | None = None
    severity: str = "P1"
    requirement_id: str | None = None
    test_case_id: str | None = None


class FileJiraRequest(BaseModel):
    project_key: str = "PROJ"
    issue_type: str = "Bug"
    assignee: str | None = None


_DEV_DEFECTS = [
    {
        "id": "DEF-001",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "title": "Timeout error under concurrent load (>200 requests)",
        "severity": "P0",
        "status": "open",
        "test_id": "TC-003",
        "requirement_id": "REQ-001",
        "root_cause": (
            "Race condition in request handler when >200 concurrent requests. "
            "The mutex lock is not applied to the ID generator, causing duplicate "
            "IDs at high concurrency, which triggers a database unique constraint "
            "violation returned as HTTP 500."
        ),
        "failure_type": "BUG",
        "confidence": 0.92,
        "impacted_files": [
            "core-service/src/handler.ts:247",
            "core-service/src/id-generator.ts:89",
        ],
        "impacted_features": ["Core Processing", "Data Integrity"],
        "suggested_fix": (
            "Add distributed Redis lock around ID generation:\n"
            "  await this.redis.lock('id-gen', 100);\n"
            "  const id = await generateUUID();\n"
            "  await this.redis.unlock('id-gen');"
        ),
        "fix_effort": "hours",
        "auto_detected": True,
        "created_at": "2026-03-11T12:14:00Z",
        "jira_id": None,
    },
    {
        "id": "DEF-002",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "title": "UI element not updating after data change",
        "severity": "P1",
        "status": "open",
        "test_id": "TC-004",
        "requirement_id": "REQ-002",
        "root_cause": (
            "React state not re-rendered after API response. "
            "The context dispatches UPDATE action but the component "
            "reads from a stale prop reference instead of context."
        ),
        "failure_type": "BUG",
        "confidence": 0.88,
        "impacted_files": [
            "frontend/components/StatusBadge.tsx:34",
            "frontend/context/DataContext.tsx:112",
        ],
        "impacted_features": ["Data Display", "Navigation"],
        "suggested_fix": "Replace `props.count` with `useContext(DataContext).count` in StatusBadge.tsx",
        "fix_effort": "minutes",
        "auto_detected": True,
        "created_at": "2026-03-10T09:22:00Z",
        "jira_id": None,
    },
    {
        "id": "DEF-003",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "title": "Filter returns 0 results for valid query variations",
        "severity": "P1",
        "status": "open",
        "test_id": "TC-005",
        "requirement_id": "REQ-004",
        "root_cause": (
            "Text search not handling plural/singular variants. "
            "Default analyzer treats different word forms as separate tokens. "
            "Need to enable stemming in the search configuration."
        ),
        "failure_type": "BUG",
        "confidence": 0.95,
        "impacted_files": [
            "data-layer/src/search-config.json:15",
        ],
        "impacted_features": ["Search & Filter", "Reporting"],
        "suggested_fix": (
            "Add stemming analyzer to search config:\n"
            '  "analyzer": { "stemmer": { "type": "standard", '
            '"filter": ["lowercase", "snowball"] } }'
        ),
        "fix_effort": "hours",
        "auto_detected": True,
        "created_at": "2026-03-09T16:40:00Z",
        "jira_id": None,
    },
    # ── BugTrackr project defects ────────────────────────────────────────────
    {
        "id": "DEF-BT-001",
        "project_id": "18347cc8-96e8-44f2-9c26-2be7c2953ca3",
        "title": "Invalid status transition allowed: Closed -> In Progress",
        "severity": "P1",
        "status": "open",
        "test_id": "TC-BT-002",
        "requirement_id": "REQ-BT-002",
        "root_cause": (
            "Status workflow validation only checks forward transitions "
            "(Open->InProgress->Resolved->Closed) but does not block reverse "
            "transitions. The validateTransition() function uses an allow-list "
            "for the next state but falls through to allow any transition when "
            "the current state is 'Closed'."
        ),
        "failure_type": "BUG",
        "confidence": 0.91,
        "impacted_files": [
            "src/models/bug.ts:112",
            "src/services/status-workflow.ts:45",
        ],
        "impacted_features": ["Bug Status Workflow", "Data Integrity"],
        "suggested_fix": (
            "Add explicit deny-list for Closed state transitions:\n"
            "  if (currentStatus === 'Closed') {\n"
            "    throw new InvalidTransitionError('Closed', targetStatus);\n"
            "  }"
        ),
        "fix_effort": "minutes",
        "auto_detected": True,
        "created_at": "2026-03-12T10:30:00Z",
        "jira_id": None,
    },
    {
        "id": "DEF-BT-002",
        "project_id": "18347cc8-96e8-44f2-9c26-2be7c2953ca3",
        "title": "RBAC bypass: Tester role can delete bugs owned by other users",
        "severity": "P0",
        "status": "open",
        "test_id": "TC-BT-003",
        "requirement_id": "REQ-BT-003",
        "root_cause": (
            "The DELETE /api/bugs/:id endpoint checks authentication but not "
            "role authorization. The middleware verifies the JWT token is valid "
            "but does not enforce that only Admins can delete bugs. Testers and "
            "Developers can call the endpoint directly."
        ),
        "failure_type": "BUG",
        "confidence": 0.96,
        "impacted_files": [
            "src/routes/bugs.ts:78",
            "src/middleware/auth.ts:34",
        ],
        "impacted_features": ["Role-Based Access Control", "Security"],
        "suggested_fix": (
            "Add role check middleware to DELETE route:\n"
            "  router.delete('/:id', requireRole('admin'), deleteBug);"
        ),
        "fix_effort": "minutes",
        "auto_detected": True,
        "created_at": "2026-03-11T14:15:00Z",
        "jira_id": None,
    },
    {
        "id": "DEF-BT-003",
        "project_id": "18347cc8-96e8-44f2-9c26-2be7c2953ca3",
        "title": "Audit log entries missing for bulk status updates",
        "severity": "P2",
        "status": "open",
        "test_id": "TC-BT-005",
        "requirement_id": "REQ-BT-005",
        "root_cause": (
            "The bulkUpdateStatus() function bypasses the normal updateBug() "
            "path which emits audit events. It directly writes to the database "
            "without going through the event emitter, so activity log entries "
            "are not created for bulk operations."
        ),
        "failure_type": "BUG",
        "confidence": 0.88,
        "impacted_files": [
            "src/services/bug-service.ts:189",
            "src/services/audit-logger.ts:22",
        ],
        "impacted_features": ["Activity Tracking", "Audit Logs"],
        "suggested_fix": (
            "Route bulk updates through the same audit pipeline:\n"
            "  for (const bugId of bugIds) {\n"
            "    await this.updateBug(bugId, { status: newStatus }, actor);\n"
            "  }"
        ),
        "fix_effort": "hours",
        "auto_detected": True,
        "created_at": "2026-03-10T08:45:00Z",
        "jira_id": None,
    },
]

_DEV_FLAKY = [
    {
        "test_id": "TC-006",
        "title": "External API integration",
        "fail_rate": 0.35,
        "run_count": 20,
        "flakiness_cause": "external_dependency",
        "recommendation": "add_retry",
        "fix": "Add test.describe.configure({ retries: 2 }) — external sandbox is intermittently unavailable",
        "quarantine": False,
    },
    {
        "test_id": "TC-007",
        "title": "Async notification delivery",
        "fail_rate": 0.20,
        "run_count": 15,
        "flakiness_cause": "timing",
        "recommendation": "add_wait",
        "fix": "Replace fixed 2s wait with polling: await expect(notificationLocator).toBeVisible({ timeout: 10000 })",
        "quarantine": False,
    },
]

# F6-17: prod-mode swap. Existing call sites read MOCK_DEFECTS / MOCK_FLAKY
# without conditionals; we make them empty in production so the demo records
# never reach customer dashboards.
MOCK_DEFECTS: list[dict] = [] if _IS_PROD else list(_DEV_DEFECTS)
MOCK_FLAKY: list[dict] = [] if _IS_PROD else list(_DEV_FLAKY)
if _IS_PROD:
    log.info("F6-17: production mode — MOCK_DEFECTS / MOCK_FLAKY emptied (DB will populate)")


def _normalize_defect(d: dict) -> dict:
    """Ensure defect dict has defect_id field (frontend expects it)."""
    item = dict(d)
    item.setdefault("defect_id", item.get("id", ""))
    # FE-sync P1 follow-through — hoist the deep_dive/preventive_action that
    # analyze_defect persisted into the `metadata` JSONB up to top-level, so a
    # reloaded GET /api/defects re-shows them without re-running analysis.
    meta = item.get("metadata_") or item.get("metadata") or {}
    if isinstance(meta, dict):
        for k in ("deep_dive", "preventive_action"):
            if k not in item and meta.get(k):
                item[k] = meta[k]
    return item


@router.get("", dependencies=[Depends(_require_api_key)])
async def list_defects(
    severity: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    limit: int = 200,
):
    """List defects with optional filters.

    R28.4 — added `priority` and `run_id` query parameters. With
    `run_id`, the response is scoped to defects produced during that
    specific run (operator's "navigate back to prior run" flow). The
    repo resolves the external run_id string → test_runs.id internally.
    """
    from ..db_adapter import try_db

    db_defects: list[dict] = []
    db_total = 0
    async with try_db() as db:
        if db:
            from ...db.repository import DefectRepo, _to_dict
            repo = DefectRepo(db)
            rows, db_total = await repo.list(
                severity=severity,
                status=status or "open",
                priority=priority,
                project_id=project_id,
                run_id=run_id,
                limit=limit,
            )
            db_defects = [_to_dict(r) for r in rows]

    # Merge in-memory mock defects not already in DB.
    # R28.4 — when run_id is supplied, skip the mock pool entirely
    # (mocks aren't run-scoped, so mixing them in would falsely show
    # them on every prior run).
    db_ids = {d.get("defect_id") or d.get("id") for d in db_defects}
    if not run_id:
        mock_pool = list(MOCK_DEFECTS)
        if project_id:
            mock_pool = [d for d in mock_pool if d.get("project_id") == project_id]
        if status is not None:
            mock_pool = [d for d in mock_pool if d["status"] == status]
        if severity:
            mock_pool = [d for d in mock_pool if d["severity"] == severity.upper()]
        if priority:
            mock_pool = [d for d in mock_pool if d.get("priority") == priority]
        for d in mock_pool:
            did = d.get("defect_id") or d.get("id")
            if did and did not in db_ids:
                db_defects.append(d)

    defects = [_normalize_defect(d) for d in db_defects]
    # R28.4 — `items` key for parity with other list endpoints; keep
    # `defects` key for backwards-compat with existing UI consumers.
    return {
        "defects": defects,
        "items": defects,
        "total": len(defects),
        "run_id": run_id,
    }


@router.post("", status_code=201, dependencies=[Depends(_require_api_key)])
async def create_defect(body: DefectCreateRequest):
    """Create a new defect from UI."""
    import uuid as _uuid
    from ..db_adapter import try_db

    defect_id = f"DEF-{_uuid.uuid4().hex[:6].upper()}"

    async with try_db() as db:
        if db:
            from ...db.repository import DefectRepo, _to_dict
            repo = DefectRepo(db)
            row = await repo.create({
                "defect_id": defect_id,
                "title": body.title,
                "description": body.description or "",
                "severity": body.severity.upper(),
                "status": "open",
                "requirement_id": body.requirement_id,
                "test_id": body.test_case_id,
            })
            return _to_dict(row)

    # Mock fallback
    defect = {
        "id": defect_id,
        "title": body.title,
        "severity": body.severity.upper(),
        "status": "open",
        "description": body.description or "",
        "requirement_id": body.requirement_id,
        "test_id": body.test_case_id,
    }
    MOCK_DEFECTS.append(defect)
    return defect


@router.get("/flaky", dependencies=[Depends(_require_api_key)])
async def get_flaky_tests():
    """List flaky tests detected by DefectIntelAgent."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import TestCaseRepo, _to_dict
            repo = TestCaseRepo(db)
            rows, _ = await repo.list(flaky_only=True)
            flaky = [_to_dict(r) for r in rows]
            return {"flaky_tests": flaky, "total": len(flaky)}

    return {"flaky_tests": MOCK_FLAKY, "total": len(MOCK_FLAKY)}


@router.get("/{defect_id}", dependencies=[Depends(_require_api_key)])
async def get_defect(defect_id: str):
    """Get defect detail with root cause and suggested fix."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import DefectRepo, _to_dict
            repo = DefectRepo(db)
            row = await repo.get(defect_id.upper())
            if row:
                return _to_dict(row)

    defect = next((d for d in MOCK_DEFECTS if d["id"] == defect_id.upper()), None)
    if not defect:
        raise HTTPException(status_code=404, detail=f"Defect {defect_id} not found")
    return defect


@router.post("/{defect_id}/file-jira", dependencies=[Depends(_require_api_key)])
async def file_to_jira(defect_id: str, body: FileJiraRequest, request: Request):
    """Auto-file defect to Jira with full AI-generated context."""
    from ..db_adapter import try_db

    defect = None
    async with try_db() as db:
        if db:
            from ...db.repository import DefectRepo, _to_dict
            repo = DefectRepo(db)
            row = await repo.get(defect_id.upper())
            if row:
                defect = _to_dict(row)

    if not defect:
        defect = next((d for d in MOCK_DEFECTS if d["id"] == defect_id.upper()), None)
    if not defect:
        raise HTTPException(status_code=404, detail=f"Defect {defect_id} not found")

    summary = defect.get("title", "")
    severity = defect.get("severity", "P1")
    description = f"Root cause: {defect.get('root_cause', '')}\n\nSuggested fix: {defect.get('suggested_fix', '')}"
    labels = ["arta-auto-detected", "test-failure"]

    # Try real Jira if available
    jira_client = getattr(request.app.state, "jira", None)
    if jira_client and jira_client.available:
        from ...integrations.jira_client import JiraClient
        result = await jira_client.create_issue(
            summary=summary,
            description=description,
            issue_type=body.issue_type,
            priority=JiraClient.severity_to_jira_priority(severity),
            labels=labels,
            project_key=body.project_key,
        )
        jira_key = result.get("key", "")
        return {
            "jira_id": jira_key,
            "jira_url": f"{jira_client.url}/browse/{jira_key}",
            "defect_id": defect_id,
            "message": f"Defect filed to Jira as {jira_key}",
            "issue": {
                "summary": summary,
                "priority": severity,
                "description": description,
                "labels": labels,
            },
        }

    # Mock fallback
    mock_jira_id = f"{body.project_key}-{9000 + abs(hash(defect_id)) % 1000}"
    return {
        "jira_id": mock_jira_id,
        "jira_url": f"https://jira.example.com/browse/{mock_jira_id}",
        "defect_id": defect_id,
        "message": f"Defect filed to Jira as {mock_jira_id}",
        "issue": {
            "summary": summary,
            "priority": severity,
            "description": description,
            "labels": labels,
        },
    }


@router.post("/{defect_id}/analyze", dependencies=[Depends(_require_api_key)])
async def analyze_defect(defect_id: str, request: Request):
    """G2: Re-run AI root cause analysis for a defect (synchronous; updates in place).

    Calls DefectIntelAgent.analyze_failures() and merges root_cause + suggested_fix
    into the in-memory defect record.
    """
    from fastapi import HTTPException
    defect = next((d for d in MOCK_DEFECTS if d["id"] == defect_id.upper()), None)
    _from_db = False
    if not defect:
        # FE-sync P1 follow-through — fall back to the DB so the 5-level deep-dive
        # works on REAL (operator-visible) defects, not just the MOCK_DEFECTS demo
        # pool. Without this the /defects "Run deep-dive" button 404s on every
        # DB-backed defect — which is every defect a real run produces.
        from ..db_adapter import try_db
        async with try_db() as db:
            if db:
                from ...db.repository import DefectRepo, _to_dict
                obj = await DefectRepo(db).get(defect_id)
                if obj is not None:
                    defect = _to_dict(obj)
                    _from_db = True
    if not defect:
        raise HTTPException(status_code=404, detail=f"Defect {defect_id} not found")

    client = getattr(request.app.state, "anthropic", None)
    if client is None:
        raise HTTPException(status_code=503, detail="LLM client not configured — cannot run analysis")

    from ...agents.defect_intel import DefectIntelAgent
    agent = DefectIntelAgent(client)
    failure = {
        "test_id": defect.get("test_id", ""),
        "title": defect.get("title", ""),
        "test_type": defect.get("test_type", "UI"),
        "requirement_id": defect.get("requirement_id", ""),
        "priority": defect.get("severity", "P2"),
        "error_message": defect.get("error_message", defect.get("description", "")),
        "stack_trace": defect.get("stack_trace", "N/A"),
        "logs": defect.get("logs", "N/A"),
    }
    # Phase J11 — pass sequence_integrity from the most recent run that
    # observed this test_id so cascade-aware classification collapses
    # cascades into root causes. Empty dict when the test isn't in any
    # recent run; defect intel degrades gracefully in that case.
    seq_integrity: dict = {}
    try:
        from .execution import _REAL_RUNS, _REAL_RESULTS
        tid = defect.get("test_id", "")
        for rid, results in (_REAL_RESULTS or {}).items():
            if isinstance(results, list) and any(
                isinstance(r, dict) and r.get("test_id") == tid for r in results
            ):
                run = (_REAL_RUNS or {}).get(rid) or {}
                if isinstance(run.get("sequence_integrity"), dict):
                    seq_integrity = run["sequence_integrity"]
                break
    except (ImportError, AttributeError):
        pass

    # FE-sync P1 — the explicit "Run 5-level deep-dive" request must produce the
    # FULL LLM RCA (5-level deep_dive + preventive_action). The bulk
    # analyze_failures() path TRIAGES first and short-circuits every
    # non-sut_regression defect (test_gen_bug / operator_review) into a
    # deterministic dict with NO deep_dive — so for the operator's explicit descent
    # we call the single-defect RCA (`_analyze_single`, which runs RCA_PROMPT's
    # 5-level schema + normalizes it) directly. Fall back to analyze_failures only
    # if that path is unavailable/errors, so we still return a root_cause.
    analysis: dict | None = None
    single = getattr(agent, "_analyze_single", None)
    if single is not None:
        try:
            analysis = await single(failure)
        except Exception as exc:
            log.warning("DefectIntelAgent._analyze_single failed for %s: %s: %s — "
                        "falling back to analyze_failures", defect_id, type(exc).__name__, exc)
            analysis = None
    if not analysis:
        try:
            try:
                results = await agent.analyze_failures([failure], sequence_integrity=seq_integrity)
            except TypeError:
                results = await agent.analyze_failures([failure])
        except Exception as exc:
            log.warning("DefectIntelAgent failed for %s: %s: %s",
                        defect_id, type(exc).__name__, exc)
            raise HTTPException(status_code=502, detail=f"Analysis failed: {exc}")

        if not results:
            raise HTTPException(status_code=502, detail="Analysis returned empty result")

        analysis = results[0]
    # Merge analysis into the live defect record so subsequent GETs see the updates.
    # FE-sync P1 — also carry the 5-level `deep_dive` + `preventive_action` that
    # DefectIntelAgent now produces (they were discarded here, so the dashboard only
    # ever saw a flat root_cause).
    for key in ("root_cause", "suggested_fix", "confidence", "category",
                "deep_dive", "preventive_action"):
        if analysis.get(key):
            defect[key] = analysis[key]
    defect["last_analyzed_at"] = analysis.get("analyzed_at") or _datetime_now_iso()

    # FE-sync P1 follow-through — when the defect is DB-backed, persist the analysis
    # so a later GET /api/defects re-shows it. root_cause/suggested_fix/
    # root_cause_category are real columns; deep_dive + preventive_action ride the
    # `metadata` JSONB (no migration). Best-effort: a persist failure must not fail
    # the analyze call, whose primary payload is the response below.
    if _from_db:
        try:
            from ..db_adapter import try_db
            async with try_db() as db:
                if db:
                    from sqlalchemy import select
                    from ...db.models import Defect as _DefectModel
                    # A defect_id is a cluster SIGNATURE — the same signature has one
                    # row PER run (composite key (defect_id, run_id)). Persist the RCA
                    # onto EVERY row of the cluster so any run's view re-shows it (a
                    # single .get()/.first() would leave the other rows stale).
                    rows = (await db.execute(
                        select(_DefectModel).where(_DefectModel.defect_id == defect_id)
                    )).scalars().all()
                    for row in rows:
                        meta = dict(getattr(row, "metadata_", None) or {})
                        if defect.get("deep_dive"):
                            meta["deep_dive"] = defect["deep_dive"]
                        if defect.get("preventive_action"):
                            meta["preventive_action"] = defect["preventive_action"]
                        row.metadata_ = meta  # reassign so SQLAlchemy tracks the JSONB change
                        if defect.get("root_cause"):
                            row.root_cause = defect["root_cause"]
                        if defect.get("suggested_fix"):
                            row.suggested_fix = defect["suggested_fix"]
                        if defect.get("category") and hasattr(row, "root_cause_category"):
                            row.root_cause_category = defect["category"]
                    await db.flush()
        except Exception as exc:
            log.warning("analyze_defect DB persist failed for %s: %s: %s",
                        defect_id, type(exc).__name__, exc)

    return {
        "defect_id": defect_id,
        "status": "completed",
        "root_cause": defect.get("root_cause"),
        "suggested_fix": defect.get("suggested_fix"),
        "confidence": defect.get("confidence"),
        # FE-sync P1 — the operator-facing 5-level RCA + preventive action.
        "deep_dive": defect.get("deep_dive"),
        "preventive_action": defect.get("preventive_action"),
    }


def _datetime_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
