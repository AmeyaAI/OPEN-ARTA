"""
ARTA Platform — Database Adapter
Provides a unified interface for routers to access DB or fall back to mocks.
Import `try_db()` in any router to get an async session, or None if DB unavailable.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("arta.db_adapter")

_db_available: bool | None = None  # cached after first check


@asynccontextmanager
async def try_db() -> AsyncGenerator[AsyncSession | None, None]:
    """Context manager: yields an AsyncSession if DB is reachable, else None.

    Usage:
        async with try_db() as db:
            if db:
                # use real DB
            else:
                # use mock fallback
    """
    global _db_available

    # Fast-fail if we already know DB is down (re-check every 60s via lifespan)
    if _db_available is False:
        yield None
        return

    # F4-3: A previous version of this block re-yielded None inside the outer
    # exception handler, which raised "generator didn't stop after athrow()" any
    # time a query inside the with block raised (and surfaced as test failures
    # plus the F3-2 rollback 500). Now we only `yield None` BEFORE the connection
    # attempt — once the session is established, exceptions propagate normally
    # so callers can decide whether to swallow or re-raise.
    try:
        from ..db.session import async_session_factory
    except Exception as exc:
        if _db_available is None:
            log.info("Database unavailable, using in-memory fallback: %s", exc)
        _db_available = False
        yield None
        return

    try:
        async with async_session_factory() as session:
            _db_available = True
            try:
                yield session
                await session.commit()
            except GeneratorExit:
                # Caller abandoned the generator — just close cleanly
                try:
                    await session.rollback()
                except Exception:
                    pass
            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    pass
                raise
    except Exception as exc:
        # Connection-level failure (couldn't open session) → mark DB down so
        # subsequent calls fast-fail. Query-level errors propagate above.
        if _db_available is not True:
            if _db_available is None:
                log.info("Database unavailable, using in-memory fallback: %s", exc)
            _db_available = False
            yield None
        else:
            log.warning("DB query failed (connection ok): %s", exc)
            # Connection is fine — re-raise so callers see the real error.
            raise


def reset_db_check() -> None:
    """Called at startup to re-probe DB availability."""
    global _db_available
    _db_available = None
