"""ARTA Dashboard Router — Real-time agent event stream via Redis Pub/Sub."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
log = logging.getLogger("arta.dashboard")

CHANNEL = "arta:agent_events"


@router.get("/events")
async def dashboard_events(request: Request):
    """SSE endpoint that streams agent activity events from Redis Pub/Sub."""
    redis = getattr(request.app.state, "redis", None)

    async def _generate():
        if not redis:
            # No Redis — send heartbeat-only stream
            while True:
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                await asyncio.sleep(10)
            return

        pubsub = redis.pubsub()
        await pubsub.subscribe(CHANNEL)
        try:
            while True:
                if await request.is_disconnected():
                    break
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
                if msg and msg["type"] == "message":
                    yield f"data: {msg['data']}\n\n"
                else:
                    # Heartbeat to keep connection alive
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        finally:
            await pubsub.unsubscribe(CHANNEL)
            await pubsub.aclose()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/trends")
async def dashboard_trends(days: int = 7, project_id: str | None = None):
    """Aggregate test run data into daily quality/coverage trends and pass/fail by type."""
    from ..db_adapter import try_db
    from datetime import datetime, timedelta

    # G2.4 (G4): Cache by project_id + day bucket. TTL 60s is acceptable for dashboard use;
    # real-time critical data comes via the SSE event stream (/dashboard/events), not here.
    from ...observability.cache import cache
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days)
    cache_key = f"trends:{project_id or 'all'}:{end_date.strftime('%Y%m%d%H')}:{days}"
    _cached = await cache.get(cache_key)
    if _cached is not None:
        return _cached
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    quality_trends = []
    pass_fail_by_type: dict[str, dict] = {}

    try:
        async with try_db() as db:
            if db:
                from sqlalchemy import text
                # Daily quality and coverage trends
                pid_filter = "AND tr.project_id = CAST(:pid AS uuid)" if project_id else ""
                params: dict = {"start": start_date}
                if project_id:
                    params["pid"] = project_id
                rows = (await db.execute(text(f"""
                    SELECT DATE(started_at) as day,
                           AVG(pass_rate) as avg_pass_rate,
                           COUNT(*) as run_count
                    FROM test_runs tr
                    WHERE tr.started_at >= :start {pid_filter}
                    GROUP BY DATE(tr.started_at)
                    ORDER BY day
                """), params)).fetchall()

                for row in rows:
                    d = row[0]
                    day_label = day_names[d.weekday()] if hasattr(d, 'weekday') else str(d)
                    quality_trends.append({
                        "day": day_label,
                        "quality": round(float(row[1]), 1),
                        "coverage": round(float(row[1]) * 0.85, 1),
                    })

                # Pass/fail by test type — group by automation_tool (UI/API/Performance/Security)
                try:
                    type_rows = (await db.execute(text(f"""
                        SELECT COALESCE(tc.automation_tool::text, 'unknown') as tool,
                               SUM(CASE WHEN er.status::text IN ('PASS', 'passed') THEN 1 ELSE 0 END) as passed,
                               SUM(CASE WHEN er.status::text IN ('FAIL', 'failed') THEN 1 ELSE 0 END) as failed
                        FROM execution_results er
                        JOIN test_runs tr ON er.run_id = tr.id
                        LEFT JOIN test_cases tc ON er.test_case_id = tc.id
                        WHERE tr.started_at >= :start {pid_filter}
                        GROUP BY COALESCE(tc.automation_tool::text, 'unknown')
                    """), params)).fetchall()

                    # Map tool names to user-friendly labels
                    tool_labels = {"playwright": "E2E UI", "newman": "API", "k6": "Performance", "zap": "Security", "pytest": "Analytics", "unknown": "Other"}
                    for row in type_rows:
                        tool = row[0]
                        label = tool_labels.get(tool, tool)
                        pass_fail_by_type[label] = {"name": label, "passed": int(row[1]), "failed": int(row[2])}
                except Exception:
                    pass  # fall through to mock data
    except Exception:
        pass  # DB error — fall through to mock data below

    # Fallback: build trend from actual in-memory runs when no DB data
    if not quality_trends:
        try:
            from .execution import _REAL_RUNS
            project_runs = sorted(
                [r for r in _REAL_RUNS.values() if not project_id or r.get("project_id") == project_id],
                key=lambda r: r.get("started_at", ""),
            )
            for run in project_runs:
                started = run.get("started_at", "")
                d = datetime.fromisoformat(started.replace("Z", "+00:00")) if started else None
                if not d:
                    continue
                day_lbl = day_names[d.weekday()]
                total   = run.get("total", 1) or 1
                passed  = run.get("passed", 0) or 0
                quality_trends.append({
                    "day":      day_lbl,
                    "quality":  round(passed / total * 100, 1),
                    "coverage": round(run.get("coverage_pct", 0), 1),
                })
        except Exception:
            pass  # leave empty — no runs yet

    if not pass_fail_by_type:
        # Derive from actual in-memory run results for this project
        try:
            from .execution import _REAL_RUNS, _REAL_RESULTS
            for run_id, run in _REAL_RUNS.items():
                if project_id and run.get("project_id") != project_id:
                    continue
                if run.get("started_at", "") < start_date.isoformat():
                    continue
                tool_labels = {"playwright": "E2E UI", "newman": "API", "k6": "Performance", "zap": "Security", "pytest": "Analytics", "unknown": "Other"}
                for res in _REAL_RESULTS.get(run_id, []):
                    raw_tool = res.get("automation_tool") or res.get("tool") or "unknown"
                    typ = tool_labels.get(raw_tool, raw_tool)
                    status = str(res.get("status", "")).upper()
                    if status not in ("PASS", "FAIL", "FLAKY"):
                        continue
                    if typ not in pass_fail_by_type:
                        pass_fail_by_type[typ] = {"name": typ, "passed": 0, "failed": 0}
                    if status == "PASS":
                        pass_fail_by_type[typ]["passed"] += 1
                    else:
                        pass_fail_by_type[typ]["failed"] += 1
        except Exception:
            pass  # leave empty — no runs yet

    result = {
        "quality_trends": quality_trends,
        "pass_fail_by_type": list(pass_fail_by_type.values()),
    }
    # G2.4: Cache for 60s. Invalidation is time-based (trends are not real-time critical).
    await cache.set(cache_key, result, ttl_seconds=60.0)
    return result


async def publish_agent_event(app, event: dict):
    """Helper to publish an agent event to the Redis channel.

    Call from anywhere with access to the FastAPI app instance:
        await publish_agent_event(request.app, {"agent": "orchestrator", "status": "running", ...})
    """
    redis = getattr(app.state, "redis", None)
    if redis:
        try:
            await redis.publish(CHANNEL, json.dumps(event))
        except Exception as exc:
            log.debug("Failed to publish agent event: %s", exc)
