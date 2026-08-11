"""ARTA Exploratory Testing Router — Charter-based session management."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
# F4-1: API-key check is centralised in src/api/dependencies.py.
from ..dependencies import require_api_key as _require_api_key  # noqa: E402


# ── Request models ──────────────────────────────────────────────────────

class SessionCreate(BaseModel):
    charter: str
    tagged_requirements: list[str] = []
    time_limit_minutes: int = 60


class FindingCreate(BaseModel):
    text: str
    severity: str = "medium"  # critical|high|medium|low
    screenshot_url: str | None = None


class ConvertRequest(BaseModel):
    target_type: str  # "defect" | "test_case"


# ── In-memory store (prototype) ────────────────────────────────────────

SESSIONS: dict[str, dict] = {}
FINDINGS: dict[str, list[dict]] = {}


# ── Endpoints ───────────────────────────────────────────────────────────

@router.post("/sessions", status_code=201, dependencies=[Depends(_require_api_key)])
async def create_session(body: SessionCreate):
    """Start a new exploratory testing session."""
    from ..db_adapter import try_db

    session_id = f"exp-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    async with try_db() as db:
        if db:
            from ...db.repository import ExploratorySessionRepo, _to_dict
            repo = ExploratorySessionRepo(db)
            row = await repo.create({
                "session_id": session_id,
                "charter": body.charter,
                "tagged_requirements": body.tagged_requirements,
                "time_limit_minutes": body.time_limit_minutes,
                "started_at": now,
            })
            return _to_dict(row)

    session = {
        "id": session_id,
        "charter": body.charter,
        "tagged_requirements": body.tagged_requirements,
        "time_limit_minutes": body.time_limit_minutes,
        "started_at": now,
        "completed_at": None,
        "status": "active",
        "findings_count": 0,
    }
    SESSIONS[session_id] = session
    FINDINGS[session_id] = []
    return session


@router.get("/sessions", dependencies=[Depends(_require_api_key)])
async def list_sessions(status: str | None = None, project_id: str | None = None):
    """List exploratory testing sessions."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import ExploratorySessionRepo, _to_dict
            repo = ExploratorySessionRepo(db)
            rows = await repo.list(status=status)
            return {"sessions": [_to_dict(r) for r in rows], "total": len(rows)}

    sessions = list(SESSIONS.values())
    if status:
        sessions = [s for s in sessions if s.get("status") == status]
    return {"sessions": sessions, "total": len(sessions)}


@router.post("/sessions/{session_id}/findings", status_code=201, dependencies=[Depends(_require_api_key)])
async def add_finding(session_id: str, body: FindingCreate):
    """Record a finding during an exploratory session."""
    from ..db_adapter import try_db

    finding_id = f"find-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    async with try_db() as db:
        if db:
            from ...db.repository import ExploratoryFindingRepo, _to_dict
            repo = ExploratoryFindingRepo(db)
            row = await repo.create({
                "finding_id": finding_id,
                "session_id": session_id,
                "text": body.text,
                "severity": body.severity,
                "screenshot_url": body.screenshot_url,
                "created_at": now,
            })
            return _to_dict(row)

    if session_id not in SESSIONS:
        raise HTTPException(404, f"Session {session_id} not found")

    finding = {
        "id": finding_id,
        "session_id": session_id,
        "text": body.text,
        "severity": body.severity,
        "screenshot_url": body.screenshot_url,
        "created_at": now,
        "converted_to_type": None,
        "converted_to_id": None,
    }
    FINDINGS.setdefault(session_id, []).append(finding)
    SESSIONS[session_id]["findings_count"] = len(FINDINGS[session_id])
    return finding


@router.post("/sessions/{session_id}/complete", dependencies=[Depends(_require_api_key)])
async def complete_session(session_id: str):
    """End an exploratory testing session and summarize findings."""
    from ..db_adapter import try_db

    now = datetime.now(timezone.utc).isoformat()

    async with try_db() as db:
        if db:
            from ...db.repository import ExploratorySessionRepo, ExploratoryFindingRepo, _to_dict
            session_repo = ExploratorySessionRepo(db)
            finding_repo = ExploratoryFindingRepo(db)
            session = await session_repo.complete(session_id, now)
            if not session:
                raise HTTPException(404, f"Session {session_id} not found")
            findings = await finding_repo.list_for_session(session_id)
            return {
                "session": _to_dict(session),
                "findings": [_to_dict(f) for f in findings],
                "summary": {
                    "total_findings": len(findings),
                    "by_severity": _count_severity(findings),
                },
            }

    if session_id not in SESSIONS:
        raise HTTPException(404, f"Session {session_id} not found")

    SESSIONS[session_id]["completed_at"] = now
    SESSIONS[session_id]["status"] = "completed"
    findings = FINDINGS.get(session_id, [])
    return {
        "session": SESSIONS[session_id],
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_severity": {s: sum(1 for f in findings if f.get("severity") == s) for s in ("critical", "high", "medium", "low")},
        },
    }


@router.post("/sessions/{session_id}/findings/{finding_id}/convert", dependencies=[Depends(_require_api_key)])
async def convert_finding(session_id: str, finding_id: str, body: ConvertRequest):
    """Convert a finding into a defect or test case."""
    from ..db_adapter import try_db

    # Find the finding
    finding = None
    findings = FINDINGS.get(session_id, [])
    for f in findings:
        if f["id"] == finding_id:
            finding = f
            break

    if not finding:
        raise HTTPException(404, f"Finding {finding_id} not found in session {session_id}")

    converted_id = f"{'DEF' if body.target_type == 'defect' else 'TC'}-{uuid.uuid4().hex[:6].upper()}"

    if body.target_type == "defect":
        async with try_db() as db:
            if db:
                from ...db.repository import DefectRepo
                repo = DefectRepo(db)
                await repo.create({
                    "defect_id": converted_id,
                    "title": finding["text"][:200],
                    "severity": finding.get("severity", "medium").upper(),
                    "status": "open",
                    "description": f"Converted from exploratory session {session_id}",
                })

    finding["converted_to_type"] = body.target_type
    finding["converted_to_id"] = converted_id

    return {
        "finding_id": finding_id,
        "converted_to_type": body.target_type,
        "converted_to_id": converted_id,
    }


def _count_severity(findings) -> dict:
    counts: dict[str, int] = {}
    for f in findings:
        s = getattr(f, "severity", "medium") if hasattr(f, "severity") else f.get("severity", "medium")
        counts[s] = counts.get(s, 0) + 1
    return counts
