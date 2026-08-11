"""R30.2 — Operator triage queue.

Surfaces defects whose triage classifier landed in `operator_review`
(LLM-confidence < 0.7 OR pattern not matched by deterministic rules).
Each row carries 3 quick-action buttons mapped to the triage_category
the operator picks: test_gen_bug → self-heal; sut_regression → keep
defect open; sut_contract_change → heal+notify.

Endpoints:
  GET  /api/triage                  — list operator_review defects
  POST /api/triage/{defect_id}      — record operator decision

Storage: writes to `Defect.metadata.operator_triage = {category, at,
operator_id}`. Doesn't reclassify the underlying defect_class — the
gate + heal proposers re-read from `triage_category` so the next run
uses the operator's verdict to gate proposals.
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..dependencies import require_api_key as _require_api_key  # noqa: E402

log = logging.getLogger("arta.triage")
from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
_VALID_CATEGORIES = {
    "test_gen_bug",
    "sut_regression",
    "sut_contract_change",
}


def _normalize(d: dict) -> dict:
    """Project the small triage-shaped payload from a defect dict."""
    md = d.get("metadata") or {}
    if not isinstance(md, dict):
        md = {}
    return {
        "defect_id": d.get("defect_id") or d.get("id"),
        "title": d.get("title"),
        "test_id": md.get("test_id") or d.get("test_id"),
        "severity": d.get("severity"),
        "priority": d.get("priority"),
        "status": d.get("status"),
        "triage_category": d.get("triage_category") or md.get("triage_category"),
        "triage_confidence": d.get("triage_confidence") or md.get("triage_confidence"),
        "triage_signals": d.get("triage_signals") or md.get("triage_signals") or [],
        # R38.5 — surface the operator_review subclass (e.g.
        # "auth_scope_mismatch") so the triage UI can render a distinct
        # chip and offer a "Fix auth profile" CTA instead of treating
        # 401/403 failures as generic unclassified rows.
        "operator_review_subclass": (
            d.get("operator_review_subclass")
            or md.get("operator_review_subclass")
        ),
        "operator_triage": md.get("operator_triage"),
        "error_message": d.get("description") or d.get("root_cause"),
        "project_id": str(d.get("project_id") or "") or None,
        "run_id": md.get("last_seen_run_id"),
        "created_at": d.get("created_at"),
    }


@router.get("", dependencies=[Depends(_require_api_key)])
async def list_triage_queue(
    project_id: str | None = None,
    run_id: str | None = None,
    limit: int = 200,
) -> dict:
    """List defects awaiting operator triage.

    Pulls every defect with `triage_category=operator_review` (or
    matching `defect_class=operator_review` from R30.1's persistence
    path). Operators classify each via POST /api/triage/{defect_id}.
    """
    from ..db_adapter import try_db
    items: list[dict] = []
    async with try_db() as db:
        if db is None:
            return {"items": [], "total": 0}
        from ...db.repository import DefectRepo, _to_dict
        repo = DefectRepo(db)
        # R30.8 — query via the indexed `triage_category` column. Pre-R30.8
        # we pulled a wider set and post-filtered on JSONB metadata —
        # O(N) per request. The dedicated column + idx_def_triage_cat
        # makes this an index scan.
        rows, _ = await repo.list(
            project_id=project_id,
            run_id=run_id,
            triage_category="operator_review",
            status=None,
            limit=limit,
        )
        # Backward-compat fallback: pre-R30.8 defects only have
        # `operator_review` in their metadata JSONB or `tags[]`.
        # Also check the legacy paths so older runs' triage queue still
        # populates while the column backfill is in progress.
        if not rows:
            rows_legacy, _ = await repo.list(
                project_id=project_id,
                run_id=run_id,
                status=None,
                limit=limit,
            )
            rows = list(rows_legacy)
        for r in rows:
            d = _to_dict(r)
            md = d.get("metadata") or d.get("metadata_") or {}
            if isinstance(md, str):
                try:
                    import json as _json
                    md = _json.loads(md)
                except Exception:
                    md = {}
            # R30.8 — prefer the dedicated column; fall back to JSONB
            # for legacy rows.
            cat = (
                d.get("triage_category")
                or md.get("triage_category")
                or md.get("defect_class")
            )
            tags = d.get("tags") or []
            # Match operator_review by category OR by tag (R30.1 stamps
            # both via the persistence path).
            if cat == "operator_review" or "operator_review" in tags:
                d["metadata"] = md
                d["triage_category"] = cat
                items.append(_normalize(d))
    return {"items": items, "total": len(items)}


class TriageDecision(BaseModel):
    triage_category: str
    operator_id: str | None = None
    notes: str | None = None


@router.post("/{defect_id}", dependencies=[Depends(_require_api_key)])
async def record_triage_decision(defect_id: str, body: TriageDecision) -> dict:
    """Record the operator's verdict on an `operator_review` defect.

    Updates the Defect's `metadata.operator_triage` and (when the verdict
    is `sut_regression`) flips the visible status from `needs_triage` →
    `open` so it surfaces in the main Defect Intelligence view. Other
    verdicts (`test_gen_bug`, `sut_contract_change`) flip status to
    `auto_healing`/`auto_healing_with_review` matching R30.1's downstream
    gating.
    """
    if body.triage_category not in _VALID_CATEGORIES:
        raise HTTPException(
            400, f"triage_category must be one of {sorted(_VALID_CATEGORIES)}"
        )
    from ..db_adapter import try_db
    async with try_db() as db:
        if db is None:
            raise HTTPException(503, "DB unavailable — cannot record decision")
        from sqlalchemy import select as _sel, update as _upd
        from ...db.models import Defect as _DefectModel
        # Find the defect by external `defect_id` (DEF-...) — primary key
        # is a UUID. Pre-fix this would 404 even when the row existed.
        try:
            uuid_val = _uuid.UUID(defect_id)
        except ValueError:
            uuid_val = None
        stmt = _sel(_DefectModel).where(
            (_DefectModel.defect_id == defect_id)
            | (_DefectModel.id == uuid_val if uuid_val else False)
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, f"defect {defect_id} not found")
        md = dict(row.metadata_ or {})
        md["operator_triage"] = {
            "category": body.triage_category,
            "operator_id": body.operator_id or "anonymous",
            "notes": body.notes,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        md["triage_category"] = body.triage_category
        # R30.7-D — when operator picks `sut_regression`, stamp
        # `skip_healing=true` so the heal proposer (R30.3) blocks
        # auto-heal proposals on this defect for ALL future runs.
        # Pre-R30.7-D the operator's verdict only affected DB metadata
        # but the heal proposer never consulted it → "Mark SUT bug" did
        # nothing visible.
        md["skip_healing"] = body.triage_category == "sut_regression"
        # Map operator decision → visible status.
        from ...db.models import DefectStatusEnum
        if body.triage_category == "sut_regression":
            new_status = DefectStatusEnum.open
        else:
            # test_gen_bug → auto_healing; sut_contract_change → auto_healing_with_review
            # DefectStatusEnum may not have those exact values; fall back to in_progress.
            try:
                new_status = DefectStatusEnum.in_progress
            except AttributeError:
                new_status = DefectStatusEnum.open
        # R30.8 — also write the dedicated `triage_category` column
        # alongside the metadata JSONB. The column is indexed; the
        # JSONB stays for backward compatibility with older readers.
        await db.execute(
            _upd(_DefectModel)
            .where(_DefectModel.id == row.id)
            .values(
                metadata_=md,
                status=new_status,
                triage_category=body.triage_category,
            )
        )
        await db.commit()
    return {
        "defect_id": defect_id,
        "triage_category": body.triage_category,
        "decided_at": md["operator_triage"]["decided_at"],
    }
