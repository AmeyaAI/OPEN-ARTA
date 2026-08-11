"""R37.6 — SUT quality score + trend endpoint.

Surfaces metrics that measure SUT (System Under Test) quality, NOT test
quality. The distinction matters: ARTA's value is detecting real
backend regressions and tracking them to resolution. The per-tool
effective pass rate (R33.7) measures ARTA's tool quality; this endpoint
measures whether the SUT is getting better or worse over time.

Metrics:
  - sut_health_pct: passing tests / (passing + sut_regression-classified
    fails). What fraction of dispatched tests reflect a healthy SUT.
  - new_regressions_count: sut_regression defects in the latest run that
    didn't exist in the prior run (delta).
  - fixed_count: sut_regression defects from prior runs that now have
    status=resolved (or no longer reproduce).
  - mttr_hours_p50/p95: median / 95th percentile time from defect-detect
    → defect-resolved (closed-loop tracking).
  - open_p0_count: P0 sut_regression defects currently open.
  - open_p1_count: P1 sut_regression defects currently open.

Operators see this on the project home page as a 30-day sparkline.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, and_

from ..dependencies import require_api_key as _require_api_key  # noqa: E402

log = logging.getLogger("arta.sut_quality")
from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
@router.get("/quality-score", dependencies=[Depends(_require_api_key)])
async def get_sut_quality_score(
    project_id: str = Query(..., description="Project UUID"),
    days: int = Query(30, ge=1, le=180, description="Lookback window"),
) -> dict[str, Any]:
    """Return current SUT quality metrics + 30-day trend points.

    Best-effort: when the DB is offline or queries fail, returns a
    zeroed payload rather than 500ing — the dashboard widget renders
    "no data" rather than disappearing.
    """
    from ..db_adapter import try_db
    from ...db.models import Defect, DefectStatusEnum, RiskPriority, TestRun
    import uuid as _uuid

    try:
        pid_uuid = _uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid project_id (must be UUID)")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: dict[str, Any] = {
        "project_id": project_id,
        "window_days": days,
        "sut_health_pct": None,
        "open_p0_count": 0,
        "open_p1_count": 0,
        "new_regressions_count": 0,
        "fixed_count": 0,
        "mttr_hours_p50": None,
        "mttr_hours_p95": None,
        "trend": [],
    }

    async with try_db() as db:
        if db is None:
            return out

        try:
            # Open P0/P1 sut_regression count (current state).
            for pri, key in [(RiskPriority.P0, "open_p0_count"),
                             (RiskPriority.P1, "open_p1_count")]:
                row = (await db.execute(
                    select(func.count(Defect.id)).where(and_(
                        Defect.project_id == pid_uuid,
                        Defect.status == DefectStatusEnum.open,
                        Defect.priority == pri,
                        Defect.triage_category == "sut_regression",
                    ))
                )).scalar()
                out[key] = int(row or 0)

            # Total sut_regression in window (resolved + open) for trend.
            # Defect doesn't have a dedicated resolved_at column — use
            # updated_at when status==resolved as a best-effort proxy.
            window_defects = (await db.execute(
                select(
                    Defect.created_at,
                    Defect.updated_at,
                    Defect.status,
                    Defect.priority,
                ).where(and_(
                    Defect.project_id == pid_uuid,
                    Defect.triage_category == "sut_regression",
                    Defect.created_at >= cutoff,
                ))
            )).all()

            # MTTR: defects whose status is resolved (using updated_at
            # as the resolution timestamp proxy).
            mttr_hours: list[float] = []
            fixed_count = 0
            for row in window_defects:
                created_at = row[0]
                resolved_at = row[1]
                status = row[2]
                if status == DefectStatusEnum.resolved and resolved_at and created_at:
                    delta_h = (resolved_at - created_at).total_seconds() / 3600.0
                    if delta_h >= 0:
                        mttr_hours.append(delta_h)
                        fixed_count += 1

            if mttr_hours:
                mttr_hours.sort()
                n = len(mttr_hours)
                out["mttr_hours_p50"] = round(mttr_hours[n // 2], 2)
                p95_idx = max(0, int(n * 0.95) - 1)
                out["mttr_hours_p95"] = round(mttr_hours[p95_idx], 2)
            out["fixed_count"] = fixed_count

            # New regressions: defects in last 24h that are still open.
            recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            recent_open = (await db.execute(
                select(func.count(Defect.id)).where(and_(
                    Defect.project_id == pid_uuid,
                    Defect.triage_category == "sut_regression",
                    Defect.created_at >= recent_cutoff,
                    Defect.status == DefectStatusEnum.open,
                ))
            )).scalar()
            out["new_regressions_count"] = int(recent_open or 0)

            # SUT health pct from the latest run's pass + sut_regression counts.
            latest_run = (await db.execute(
                select(TestRun).where(TestRun.project_id == pid_uuid)
                .order_by(TestRun.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            if latest_run is not None:
                passed = int(getattr(latest_run, "passed", 0) or 0)
                # Approximate sut_regression failures as the count of
                # sut_regression defects associated with this run. Each
                # defect can represent multiple affected tests; we use
                # sum(affected_tests) when present.
                run_defects = (await db.execute(
                    select(Defect.metadata_).where(and_(
                        Defect.run_id == latest_run.id,
                        Defect.triage_category == "sut_regression",
                    ))
                )).all()
                sut_fail_count = 0
                for (md,) in run_defects:
                    if isinstance(md, dict):
                        affected = md.get("affected_tests") or []
                        if isinstance(affected, list) and affected:
                            sut_fail_count += len(affected)
                        else:
                            sut_fail_count += 1
                denom = passed + sut_fail_count
                if denom > 0:
                    out["sut_health_pct"] = round(100.0 * passed / denom, 1)

            # 30-day trend: bucket defects by day of created_at.
            buckets: dict[str, int] = {}
            for row in window_defects:
                created_at = row[0]
                if not created_at:
                    continue
                key = created_at.date().isoformat()
                buckets[key] = buckets.get(key, 0) + 1
            out["trend"] = [
                {"date": k, "new_regressions": v}
                for k, v in sorted(buckets.items())
            ]
        except Exception as exc:
            log.warning("R37.6: SUT quality query failed: %s", exc)

    return out


# R72.4 — per-endpoint SUT health rollup
# ─────────────────────────────────────────────────────────────────────
import re as _re_72_4

_UUID_RE = _re_72_4.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_NUMERIC_PATH_SEG_RE = _re_72_4.compile(r"/(\d{4,})(?=/|$)")


def _normalize_endpoint_key(key: str) -> str:
    """R72.4 — collapse UUIDs + long numeric IDs in endpoint paths so
    per-endpoint health rolls up to the template, not per-instance.

    Examples:
      "GET:/api/users/abc-...-def"      → "GET:/api/users/{id}"
      "POST /api/orders/12345/items"    → "POST /api/orders/{id}/items"
      "GET:/api/baseline/abc:def"       → "GET:/api/baseline/{id}:def"

    Also normalises the method-separator: pre-R69.3 emitter used
    space; R69.3 standardised on colon. Output uses colon.
    """
    if not isinstance(key, str) or not key:
        return key
    # Split method from path: handle both "METHOD path" and "METHOD:path"
    method = "UNKNOWN"
    path = key
    if " " in key and ":" not in key.split(" ", 1)[0]:
        method, path = key.split(" ", 1)
    elif ":" in key:
        method, path = key.split(":", 1)
    method = method.strip().upper()
    path = path.strip()
    # Normalise UUIDs + long numeric IDs to {id}
    path = _UUID_RE.sub("{id}", path)
    path = _NUMERIC_PATH_SEG_RE.sub("/{id}", path)
    return f"{method}:{path}"


@router.get("/endpoint-health", dependencies=[Depends(_require_api_key)])
async def get_endpoint_health(
    project_id: str = Query(..., description="Project UUID"),
    days: int = Query(7, ge=1, le=90, description="Lookback window in days"),
    min_runs: int = Query(1, ge=1, description="Min test runs to include an endpoint"),
) -> dict[str, Any]:
    """R72.4 — per-endpoint SUT health rollup.

    For each SUT endpoint (identified by `metadata.endpoint_keys` on
    execution_results rows, R55.7+R67.D+R69.3 wired this for Newman):
      - pass_count / fail_count / blocked_count across the window
      - pass_pct = pass / (pass + fail), excludes BLOCKED
      - last_seen timestamp
      - coverage_test_count (unique test_ids that hit it)

    Operators see this as "Worst 10 endpoints" + per-component rollup
    so they get a prioritized hit list. Closes the goal's "report the
    quality of the SUT" deliverable: not just defect counts but a
    structured health view per endpoint.

    UUID + long numeric path segments are collapsed to `{id}` so
    /api/users/abc-...-def and /api/users/123-...-456 roll up to
    "GET:/api/users/{id}". Templated paths (like "{user_id}") pass
    through unchanged.

    Best-effort: when no Result→Endpoint edges exist yet (cold-start
    project), returns an empty list rather than 500ing.
    """
    from ..db_adapter import try_db
    from sqlalchemy import text
    import uuid as _uuid

    try:
        _uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid project_id (must be UUID)")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: dict[str, Any] = {
        "project_id": project_id,
        "window_days": days,
        "endpoints": [],
        "worst_10": [],
        "components": {},
        "totals": {
            "endpoints_seen": 0,
            "endpoints_above_95": 0,
            "endpoints_below_50": 0,
        },
    }

    async with try_db() as db:
        if db is None:
            return out
        try:
            rows = (await db.execute(
                text("""
                    SELECT
                      jsonb_array_elements_text(er.metadata->'endpoint_keys') AS ep,
                      er.status,
                      er.test_id,
                      tr.started_at
                    FROM execution_results er
                    JOIN test_runs tr ON tr.id = er.run_id
                    WHERE er.metadata->'endpoint_keys' IS NOT NULL
                      AND jsonb_typeof(er.metadata->'endpoint_keys') = 'array'
                      AND tr.project_id = :pid
                      AND tr.started_at >= :cutoff
                """),
                {"pid": project_id, "cutoff": cutoff},
            )).all()

            # Aggregate per normalised endpoint_key
            agg: dict[str, dict] = {}
            for ep_raw, status, test_id, started_at in rows:
                ep = _normalize_endpoint_key(ep_raw)
                bucket = agg.setdefault(ep, {
                    "endpoint_key": ep,
                    "pass_count": 0,
                    "fail_count": 0,
                    "blocked_count": 0,
                    "test_ids": set(),
                    "last_seen": None,
                })
                s = (status or "").upper()
                if s == "PASS":
                    bucket["pass_count"] += 1
                elif s == "FAIL":
                    bucket["fail_count"] += 1
                elif s in ("BLOCKED", "SKIP", "SKIPPED"):
                    bucket["blocked_count"] += 1
                if isinstance(test_id, str):
                    bucket["test_ids"].add(test_id)
                if started_at and (
                    bucket["last_seen"] is None or started_at > bucket["last_seen"]
                ):
                    bucket["last_seen"] = started_at

            endpoints = []
            for ep, b in agg.items():
                judge_denom = b["pass_count"] + b["fail_count"]
                if judge_denom < min_runs:
                    continue
                pass_pct = round(100.0 * b["pass_count"] / judge_denom, 1) if judge_denom else 0.0
                endpoints.append({
                    "endpoint_key": ep,
                    "pass_count": b["pass_count"],
                    "fail_count": b["fail_count"],
                    "blocked_count": b["blocked_count"],
                    "judge_total": judge_denom,
                    "pass_pct": pass_pct,
                    "coverage_test_count": len(b["test_ids"]),
                    "last_seen": b["last_seen"].isoformat() if b["last_seen"] else None,
                })

            endpoints.sort(key=lambda e: (e["pass_pct"], -e["fail_count"]))
            out["endpoints"] = endpoints
            out["worst_10"] = endpoints[:10]
            out["totals"]["endpoints_seen"] = len(endpoints)
            out["totals"]["endpoints_above_95"] = sum(1 for e in endpoints if e["pass_pct"] >= 95.0)
            out["totals"]["endpoints_below_50"] = sum(1 for e in endpoints if e["pass_pct"] < 50.0)

            # Component rollup: regex-bucket by URL prefix.
            # Pattern: take the first 2 path segments after the method as
            # the component key. "/api/users/{id}" → "/api/users".
            components: dict[str, dict] = {}
            for e in endpoints:
                ek = e["endpoint_key"]
                # Split METHOD:path
                if ":" in ek:
                    _, p = ek.split(":", 1)
                else:
                    p = ek
                segs = [s for s in p.split("/") if s]
                comp = "/" + "/".join(segs[:2]) if len(segs) >= 2 else (p or "unknown")
                c = components.setdefault(comp, {
                    "component": comp,
                    "pass_count": 0,
                    "fail_count": 0,
                    "endpoint_count": 0,
                })
                c["pass_count"] += e["pass_count"]
                c["fail_count"] += e["fail_count"]
                c["endpoint_count"] += 1
            for c in components.values():
                denom = c["pass_count"] + c["fail_count"]
                c["pass_pct"] = round(100.0 * c["pass_count"] / denom, 1) if denom else 0.0
            out["components"] = sorted(components.values(), key=lambda c: c["pass_pct"])
        except Exception as exc:
            log.warning("R72.4: endpoint-health query failed: %s", exc)

    return out
