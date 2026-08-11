"""F6-16: Database retention.

Prevents unbounded growth of `test_runs` (and any future time-series tables).
Runs once at API startup and then every 24 hours via a supervised background
task. Default retention is 90 days; tune via env vars per table.

Tables managed today:
  - test_runs: keep N days (env: ARTA_RUN_RETENTION_DAYS, default 90)

Tables NOT managed:
  - defects: history matters for compliance / trend analysis — keep indefinitely
  - test_cases: kept indefinitely (drives traceability)
  - acceptance_criteria, requirements: kept indefinitely
  - users / project_roles: kept indefinitely

Add new tables here when they have a clear retention story; don't auto-prune
business data without an explicit decision.
"""
from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import text

log = logging.getLogger("arta.retention")


DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "test_runs": int(os.environ.get("ARTA_RUN_RETENTION_DAYS", "90")),
}


async def prune_once() -> dict[str, int]:
    """Apply retention policies once. Returns table → rows-deleted map.

    Best-effort: if the DB is unavailable the function returns {} after logging.
    """
    deleted: dict[str, int] = {}
    try:
        from .session import async_session_factory
    except Exception as exc:
        log.info("retention: skipped — DB session module unavailable: %s", exc)
        return deleted

    try:
        async with async_session_factory() as session:
            for table, days in DEFAULT_RETENTION_DAYS.items():
                if days <= 0:
                    continue  # 0/negative disables pruning for that table
                try:
                    # Use parameter binding for the days value; the table name
                    # is from a hardcoded dict so it's safe to interpolate.
                    sql = text(
                        f"DELETE FROM {table} WHERE created_at < NOW() - make_interval(days => :days)"
                    )
                    result = await session.execute(sql, {"days": days})
                    n = result.rowcount or 0
                    deleted[table] = n
                    if n > 0:
                        log.info("retention: pruned %d rows from %s older than %d days",
                                 n, table, days)
                except Exception as table_exc:
                    log.warning("retention: prune failed for %s: %s", table, table_exc)
            await session.commit()
    except Exception as exc:
        log.warning("retention: top-level failure (DB down?): %s", exc)
    return deleted


async def schedule_periodic(interval_hours: float = 24.0) -> None:
    """Long-lived task: prune now, then every `interval_hours` hours.

    Wrap the create_task in `task_supervisor.supervise()` so the loop's
    failure surfaces clearly.
    """
    log.info("retention: scheduler started — pruning every %.1fh", interval_hours)
    while True:
        try:
            await prune_once()
        except Exception as exc:
            log.warning("retention: prune iteration failed: %s", exc)
        await asyncio.sleep(interval_hours * 3600)
