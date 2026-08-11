"""Regression: `_try_get_db` (auth router) must let a CALLER's exception propagate.

Bug: the `yield session` sat inside the same `try` whose `except Exception` was meant only
for DB-connectivity setup failures. An `HTTPException(401)` raised by a caller inside
`async with _try_get_db() as db:` (e.g. login on a wrong password / not-yet-accepted invite)
was thrown INTO the generator, caught by that except (HTTPException IS an Exception), and
answered with a SECOND `yield None` during athrow() → `RuntimeError: generator didn't stop
after athrow()` → a clean 401 surfaced to the client as a 500 "Internal server error"
(observed on POST /api/auth/login after a successful invite).
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from src.api.routers import auth


class _FakeSession:
    async def execute(self, *a, **k):   # the SELECT 1 connectivity probe succeeds
        return None
    async def close(self):
        pass


def test_caller_httpexception_propagates_not_500():
    """DB is up; a caller raises HTTPException(401) inside the ctx → it must propagate
    unchanged (NOT become RuntimeError 'generator didn't stop after athrow()')."""
    async def _run():
        with patch("src.db.session.async_session_factory", lambda: _FakeSession()):
            async with auth._try_get_db() as db:
                assert db is not None
                raise HTTPException(status_code=401, detail="Incorrect email or password")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_run())
    assert ei.value.status_code == 401 and "Incorrect" in ei.value.detail


def test_generic_caller_exception_propagates():
    """Any caller exception (not just HTTPException) must also propagate cleanly."""
    async def _run():
        with patch("src.db.session.async_session_factory", lambda: _FakeSession()):
            async with auth._try_get_db() as db:
                assert db is not None
                raise ValueError("boom")
    with pytest.raises(ValueError):
        asyncio.run(_run())


def test_db_down_yields_none_fallback():
    """DB connectivity failure at setup → the ctx yields None (mock fallback), exactly one
    yield, no RuntimeError."""
    def _boom():
        raise ConnectionError("db down")
    async def _run():
        with patch("src.db.session.async_session_factory", _boom):
            async with auth._try_get_db() as db:
                return db
    assert asyncio.run(_run()) is None
