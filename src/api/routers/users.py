"""
ARTA Platform — User Management Router (Admin)
Admin-only CRUD for users + per-project role assignment.

Endpoints:
  GET    /api/users                            → list all users
  POST   /api/users                            → create user
  POST   /api/users/invite                     → invite user (admin)
  GET    /api/users/{user_id}                  → get user profile
  PUT    /api/users/{user_id}                  → update user
  DELETE /api/users/{user_id}                  → deactivate user
  GET    /api/users/{user_id}/projects         → list project roles for user
  POST   /api/projects/{project_id}/roles      → assign role to user
  DELETE /api/projects/{project_id}/roles/{uid}→ revoke project role
  GET    /api/projects/{project_id}/members    → list all members with roles
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import smtplib
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...models.user import (
    AcceptInviteRequest,
    InviteRequest,
    InviteResponse,
    ProjectRoleAssignment,
    UserCreate,
    UserPublic,
    UserUpdate,
)
from .auth import _MOCK_USERS, _MOCK_PASSWORD, get_current_user, _pwd_ctx, _hash_token

log = logging.getLogger("arta.users")

router = APIRouter()

# ── In-memory project member store (prototype) ────────────────────────────────

# project_id → list of {"user_id": str, "email": str, "role": str, "granted_by": str}
_PROJECT_MEMBERS: dict[str, list[dict[str, Any]]] = {
    "proj-1": [
        {"user_id": "u1", "email": "admin@arta.dev",  "role": "admin",   "granted_by": "system"},
        {"user_id": "u2", "email": "alice@arta.dev",  "role": "qa_lead", "granted_by": "u1"},
        {"user_id": "u3", "email": "bob@arta.dev",    "role": "tester",  "granted_by": "u1"},
    ],
    "proj-2": [
        {"user_id": "u1", "email": "admin@arta.dev",  "role": "admin",   "granted_by": "system"},
        {"user_id": "u2", "email": "alice@arta.dev",  "role": "qa_lead", "granted_by": "u1"},
    ],
    "proj-3": [
        {"user_id": "u1", "email": "admin@arta.dev",  "role": "admin",   "granted_by": "system"},
        {"user_id": "u3", "email": "bob@arta.dev",    "role": "tester",  "granted_by": "u1"},
        {"user_id": "u4", "email": "viewer@arta.dev", "role": "viewer",  "granted_by": "u1"},
    ],
}

_ROLE_ORDER = {"viewer": 0, "developer": 1, "tester": 2, "qa_lead": 3, "test_architect": 4, "admin": 5}


# ── Access control dependencies ───────────────────────────────────────────────

async def require_admin(current_user=Depends(get_current_user)):
    # get_current_user returns either an ORM User (DB-backed) or a _MockUser
    # (mock fallback) — both expose `.is_admin` as an attribute. The dict
    # path only existed in earlier mocked tests; we never see it in practice.
    is_admin = (
        current_user.get("is_admin") if isinstance(current_user, dict)
        else getattr(current_user, "is_admin", False)
    )
    if not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


def _require_min_role(user_id: str, project_id: str, min_role: str) -> bool:
    members = _PROJECT_MEMBERS.get(project_id, [])
    for m in members:
        if m["user_id"] == user_id:
            return _ROLE_ORDER.get(m["role"], -1) >= _ROLE_ORDER.get(min_role, 0)
    return False


# ── User helpers ──────────────────────────────────────────────────────────────

def _email_by_id(user_id: str) -> str | None:
    for email, u in _MOCK_USERS.items():
        if u["id"] == user_id:
            return email
    return None


def _user_public(email: str) -> dict:
    u = _MOCK_USERS[email]
    return {
        "id": u["id"],
        "email": email,
        "full_name": u["full_name"],
        "avatar_url": u["avatar_url"],
        "is_admin": u["is_admin"],
        "is_active": u["is_active"],
        "created_at": u["created_at"],
    }


# ── User endpoints ────────────────────────────────────────────────────────────

@router.get("/users", summary="List all users (admin only)")
async def list_users(_: dict = Depends(require_admin)):
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import UserRepo, _to_dict
            repo = UserRepo(db)
            rows = await repo.list()
            return [_to_dict(r) for r in rows]

    return [_user_public(email) for email in _MOCK_USERS]


@router.post("/users", status_code=status.HTTP_201_CREATED, summary="Create user (admin only)")
async def create_user(body: UserCreate, _: dict = Depends(require_admin)):
    from ..db_adapter import try_db

    email = body.email.lower().strip()

    async with try_db() as db:
        if db:
            from ...db.repository import UserRepo, _to_dict
            repo = UserRepo(db)
            existing = await repo.get_by_email(email)
            if existing:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
            user = await repo.create({
                "email": email,
                "full_name": body.full_name,
                "is_admin": body.is_admin,
                "is_active": True,
            })
            return _to_dict(user)

    if email in _MOCK_USERS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    new_id = f"u{len(_MOCK_USERS) + 1}"
    from datetime import datetime, timezone
    _MOCK_USERS[email] = {
        "id": new_id,
        "full_name": body.full_name,
        "is_admin": body.is_admin,
        "is_active": True,
        "role": "admin" if body.is_admin else "viewer",
        "avatar_url": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return _user_public(email)


# ── Invite flow ───────────────────────────────────────────────────────────────

_INVITE_EXPIRY_HOURS = 72
_FRONTEND_URL_ENV = "ARTA_FRONTEND_URL"
_FRONTEND_URL_DEFAULT = "http://localhost:3000"


def _frontend_base_url(request: Request | None = None) -> str:
    """Derive the public frontend URL.

    Priority order:
      1. ARTA_FRONTEND_URL env var — explicit override for ops.
      2. The admin's browser Origin / Referer header — handles dev/staging/prod
         without any config (the admin who clicked Invite is sitting at the
         right URL).
      3. localhost:3000 fallback — only relevant for non-browser callers.
    """
    explicit = os.environ.get(_FRONTEND_URL_ENV)
    if explicit:
        return explicit.rstrip("/")
    if request is not None:
        origin = request.headers.get("origin")
        if origin:
            return origin.rstrip("/")
        referer = request.headers.get("referer")
        if referer:
            # Strip the path/query so /admin/whatever still yields the bare host.
            from urllib.parse import urlparse
            parsed = urlparse(referer)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
    return _FRONTEND_URL_DEFAULT.rstrip("/")


_INVITE_EMAIL_SUBJECT = "You're invited to ARTA"


def _build_invite_email(to_email: str, invite_url: str, full_name: str) -> tuple[str, str]:
    """Shared template for the invite email body. Used both by the SMTP path
    and surfaced in the API response so the admin can copy/paste the same
    text into their mail client when SMTP is not configured.
    """
    body = (
        f"Hi {full_name or to_email},\n\n"
        f"You've been invited to ARTA. Set your password and sign in here:\n"
        f"{invite_url}\n\n"
        f"This link expires in {_INVITE_EXPIRY_HOURS} hours.\n"
    )
    return _INVITE_EMAIL_SUBJECT, body


def _send_invite_email(to_email: str, invite_url: str, full_name: str) -> bool:
    """Best-effort SMTP delivery. Returns True on send, False when SMTP env
    is missing or send fails. Errors are logged at debug — admin can always
    fall back to the URL/body the API returns."""
    host = os.environ.get("SMTP_HOST")
    if not host:
        log.debug("invite email skipped (SMTP_HOST not set) — admin must share URL")
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("MAIL_FROM") or user
    if not sender:
        log.debug("invite email skipped (MAIL_FROM/SMTP_USER not set)")
        return False
    subject, body = _build_invite_email(to_email, invite_url, full_name)
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                # Server doesn't support STARTTLS — proceed anyway (Mailtrap-style).
                pass
            if user and password:
                smtp.login(user, password)
            smtp.sendmail(sender, [to_email], msg.as_string())
        log.info("invite email sent to %s", to_email)
        return True
    except Exception as exc:
        log.warning("invite email failed for %s: %s", to_email, exc)
        return False


@router.post("/users/invite", status_code=status.HTTP_201_CREATED,
             response_model=InviteResponse, summary="Invite user (admin only)")
async def invite_user(body: InviteRequest, request: Request, current_user=Depends(require_admin)):
    """Admin → Invite User. Creates an inactive user (or reuses an existing
    inactive row), issues a single-use invite token, optionally emails it via
    SMTP, and returns the invite URL so the admin can copy/share it.

    project_id + project_role are stamped on the invite_token row only — the
    actual project_roles entry is created at acceptance time so we never have
    role grants pointing at unaccepted users.
    """
    if _pwd_ctx is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth subsystem unavailable (passlib not installed)",
        )
    if (body.project_id and not body.project_role) or (body.project_role and not body.project_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project_id and project_role must be provided together",
        )

    email = body.email.lower().strip()
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=_INVITE_EXPIRY_HOURS)

    from ..db_adapter import try_db

    async with try_db() as db:
        if not db:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Invite flow requires the database — mock fallback not supported",
            )

        from ...db.repository import InviteTokenRepo, UserRepo
        user_repo = UserRepo(db)
        existing = await user_repo.get_by_email(email)
        if existing and existing.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already registered to an active user",
            )

        if existing:
            # Re-invite an inactive user — refresh full_name + admin flag.
            existing.full_name = body.full_name or existing.full_name
            existing.is_admin = body.is_admin
            await db.flush()
            user = existing
        else:
            user = await user_repo.create({
                "email": email,
                "full_name": body.full_name,
                "is_admin": body.is_admin,
                "is_active": False,
                "hashed_password": None,
            })

        # SHA-256 hex (same primitive RefreshToken uses) — bcrypt is wrong for
        # high-entropy random tokens (input length cap, no benefit over hashing).
        token_hash = _hash_token(raw_token)
        invite_repo = InviteTokenRepo(db)
        invite = await invite_repo.create(
            user_id=str(user.id),
            token_hash=token_hash,
            expires_at=expires_at,
            invited_by=str(current_user.id) if hasattr(current_user, "id") else None,
            project_id=body.project_id,
            project_role=body.project_role,
        )
        await db.commit()
        invite_id = str(invite.id)
        user_id = str(user.id)

    invite_url = f"{_frontend_base_url(request)}/accept-invite?token={raw_token}"
    email_sent = _send_invite_email(email, invite_url, body.full_name)
    subject, email_body = _build_invite_email(email, invite_url, body.full_name)

    log.info(
        "invite issued: email=%s user=%s expires=%s by=%s email_sent=%s",
        email, user_id, expires_at.isoformat(),
        getattr(current_user, "email", "?"), email_sent,
    )

    return InviteResponse(
        invite_id=invite_id,
        user_id=user_id,
        email=email,
        expires_at=expires_at.isoformat(),
        invite_url=invite_url,
        email_sent=email_sent,
        email_subject=subject,
        email_body=email_body,
    )


@router.get("/users/{user_id}", summary="Get user profile (admin or self)")
async def get_user(user_id: str, current_user: dict = Depends(get_current_user)):
    if not current_user.get("is_admin") and current_user.get("id") != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import UserRepo, _to_dict
            repo = UserRepo(db)
            row = await repo.get_by_id(user_id)
            if row:
                return _to_dict(row)

    email = _email_by_id(user_id)
    if not email:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return _user_public(email)


@router.put("/users/{user_id}", summary="Update user (admin only)")
async def update_user(user_id: str, body: UserUpdate, _: dict = Depends(require_admin)):
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import UserRepo, _to_dict
            repo = UserRepo(db)
            updates = {}
            if body.full_name is not None:
                updates["full_name"] = body.full_name
            if body.avatar_url is not None:
                updates["avatar_url"] = body.avatar_url
            if updates:
                row = await repo.update(user_id, updates)
                if not row:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
                return _to_dict(row)
            row = await repo.get_by_id(user_id)
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            return _to_dict(row)

    email = _email_by_id(user_id)
    if not email:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if body.full_name is not None:
        _MOCK_USERS[email]["full_name"] = body.full_name
    if body.avatar_url is not None:
        _MOCK_USERS[email]["avatar_url"] = body.avatar_url
    return _user_public(email)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Deactivate user (admin only)")
async def deactivate_user(user_id: str, current_user=Depends(require_admin)):
    """Soft-delete: sets is_active=False (reversible via the reactivate
    endpoint). Two guards prevent an admin from locking the platform:
    you can't deactivate your own account, and you can't deactivate the
    last remaining active admin."""
    acting_id = str(getattr(current_user, "id", "") or "")
    if acting_id and acting_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate your own account",
        )

    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import UserRepo
            repo = UserRepo(db)
            target = await repo.get_by_id(user_id)
            if not target:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            if target.is_admin and await repo.count_active_admins() <= 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot deactivate the last active admin",
                )
            await repo.deactivate(user_id)
            return

    email = _email_by_id(user_id)
    if not email:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    _MOCK_USERS[email]["is_active"] = False


@router.post("/users/{user_id}/reactivate", summary="Reactivate a deactivated user (admin only)")
async def reactivate_user(user_id: str, current_user=Depends(require_admin)):
    """Restore a soft-deleted (is_active=False) user. Idempotent — reactivating
    an already-active user is a no-op success. Password / project roles / refresh
    tokens are preserved by the soft-delete, so the account comes back intact."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import UserRepo
            repo = UserRepo(db)
            ok = await repo.reactivate(user_id)
            if not ok:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            return {"id": user_id, "is_active": True}

    email = _email_by_id(user_id)
    if not email:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    _MOCK_USERS[email]["is_active"] = True
    return _user_public(email)


@router.get("/users/{user_id}/projects", summary="List project roles for a user")
async def user_projects(user_id: str, current_user: dict = Depends(get_current_user)):
    if not current_user.get("is_admin") and current_user.get("id") != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import ProjectRoleRepo, _to_dict
            repo = ProjectRoleRepo(db)
            rows = await repo.list_for_user(user_id)
            return [
                {"project_id": str(r.project_id), "role": r.role}
                for r in rows
            ]

    result = []
    for project_id, members in _PROJECT_MEMBERS.items():
        for m in members:
            if m["user_id"] == user_id:
                result.append({"project_id": project_id, "role": m["role"]})
    return result


# ── Project member endpoints ──────────────────────────────────────────────────

@router.get("/projects/{project_id}/members", summary="List project members with roles")
async def list_members(project_id: str, current_user: dict = Depends(get_current_user)):
    if not current_user.get("is_admin") and not _require_min_role(
        current_user["id"], project_id, "viewer"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this project")

    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import ProjectRoleRepo
            repo = ProjectRoleRepo(db)
            rows = await repo.list_for_project(project_id)
            return [
                {
                    "user_id": str(r.user_id),
                    "email": r.user.email if r.user else None,
                    "role": r.role,
                    "granted_by": str(r.granted_by) if r.granted_by else None,
                }
                for r in rows
            ]

    return _PROJECT_MEMBERS.get(project_id, [])


@router.post("/projects/{project_id}/roles", status_code=status.HTTP_201_CREATED,
             summary="Assign role to user (admin only)")
async def assign_role(
    project_id: str,
    body: ProjectRoleAssignment,
    current_user: dict = Depends(require_admin),
):
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import ProjectRoleRepo, UserRepo
            user_repo = UserRepo(db)
            user = await user_repo.get_by_id(body.user_id)
            if not user:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            repo = ProjectRoleRepo(db)
            role = await repo.assign(body.user_id, project_id, body.role, current_user.get("id"))
            return {
                "user_id": str(role.user_id),
                "email": user.email,
                "role": role.role,
                "granted_by": str(role.granted_by) if role.granted_by else None,
            }

    email = _email_by_id(body.user_id)
    if not email:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    members = _PROJECT_MEMBERS.setdefault(project_id, [])
    for m in members:
        if m["user_id"] == body.user_id:
            m["role"] = body.role
            m["granted_by"] = current_user["id"]
            return m

    entry = {
        "user_id": body.user_id,
        "email": email,
        "role": body.role,
        "granted_by": current_user["id"],
    }
    members.append(entry)
    return entry


@router.delete("/projects/{project_id}/roles/{user_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Revoke project role (admin only)")
async def revoke_role(project_id: str, user_id: str, _: dict = Depends(require_admin)):
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import ProjectRoleRepo
            repo = ProjectRoleRepo(db)
            ok = await repo.revoke(user_id, project_id)
            if not ok:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role assignment not found")
            return

    members = _PROJECT_MEMBERS.get(project_id, [])
    updated = [m for m in members if m["user_id"] != user_id]
    if len(updated) == len(members):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role assignment not found")
    _PROJECT_MEMBERS[project_id] = updated
