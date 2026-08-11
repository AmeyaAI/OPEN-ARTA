"""Guards on DELETE /api/users/{id} (deactivate) + the new reactivate endpoint.

Context: the deactivate endpoint is a SOFT-delete (sets is_active=False). It was
easy to fire against a real account with no un-delete path, and it had no guard
against an admin locking the platform. These tests pin the hardening:
  - you cannot deactivate your own account,
  - you cannot deactivate the last active admin,
  - a normal deactivate still works,
  - reactivate restores a soft-deleted user.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.api.routers import users


@asynccontextmanager
async def _db_ctx(db="DB"):
    yield db


def _admin(uid="admin-1"):
    return SimpleNamespace(id=uid, is_admin=True, email="admin@arta.dev")


def _make_repo(target, admin_count=2):
    repo = AsyncMock()
    repo.get_by_id = AsyncMock(return_value=target)
    repo.count_active_admins = AsyncMock(return_value=admin_count)
    repo.deactivate = AsyncMock(return_value=True)
    repo.reactivate = AsyncMock(return_value=True)
    return repo


def _run(coro_fn, repo=None):
    async def _inner():
        patches = [patch("src.api.db_adapter.try_db", lambda: _db_ctx())]
        if repo is not None:
            patches.append(patch("src.db.repository.UserRepo", lambda db: repo))
        for p in patches:
            p.start()
        try:
            return await coro_fn()
        finally:
            for p in reversed(patches):
                p.stop()
    return asyncio.run(_inner())


def test_cannot_deactivate_self():
    """Self-deactivation is refused BEFORE any DB work (would lock the admin out)."""
    with pytest.raises(HTTPException) as ei:
        _run(lambda: users.deactivate_user("admin-1", current_user=_admin("admin-1")))
    assert ei.value.status_code == 400
    assert "own account" in ei.value.detail


def test_cannot_deactivate_last_admin():
    """Deactivating the last active admin is refused; deactivate() is never called."""
    target = SimpleNamespace(id="admin-2", is_admin=True, is_active=True)
    repo = _make_repo(target, admin_count=1)
    with pytest.raises(HTTPException) as ei:
        _run(lambda: users.deactivate_user("admin-2", current_user=_admin("admin-1")), repo)
    assert ei.value.status_code == 400
    assert "last active admin" in ei.value.detail
    repo.deactivate.assert_not_called()


def test_deactivate_second_admin_allowed():
    """With >1 active admin, deactivating one admin is allowed."""
    target = SimpleNamespace(id="admin-2", is_admin=True, is_active=True)
    repo = _make_repo(target, admin_count=2)
    _run(lambda: users.deactivate_user("admin-2", current_user=_admin("admin-1")), repo)
    repo.deactivate.assert_awaited_once_with("admin-2")


def test_deactivate_normal_user_succeeds():
    """A non-admin target deactivates without touching the admin-count guard."""
    target = SimpleNamespace(id="u-9", is_admin=False, is_active=True)
    repo = _make_repo(target, admin_count=3)
    _run(lambda: users.deactivate_user("u-9", current_user=_admin("admin-1")), repo)
    repo.deactivate.assert_awaited_once_with("u-9")
    repo.count_active_admins.assert_not_called()  # short-circuited: target not admin


def test_deactivate_missing_user_404():
    repo = _make_repo(None)
    with pytest.raises(HTTPException) as ei:
        _run(lambda: users.deactivate_user("ghost", current_user=_admin("admin-1")), repo)
    assert ei.value.status_code == 404


def test_reactivate_user_succeeds():
    repo = _make_repo(None)
    result = _run(lambda: users.reactivate_user("u-9", current_user=_admin("admin-1")), repo)
    assert result == {"id": "u-9", "is_active": True}
    repo.reactivate.assert_awaited_once_with("u-9")


def test_reactivate_missing_user_404():
    repo = _make_repo(None)
    repo.reactivate = AsyncMock(return_value=False)  # get_by_id inside repo returns None
    with pytest.raises(HTTPException) as ei:
        _run(lambda: users.reactivate_user("ghost", current_user=_admin("admin-1")), repo)
    assert ei.value.status_code == 404
