"""
ARTA Platform — Authentication Router
JWT OAuth2 password flow with DB-backed users (mock fallback if DB unavailable).

Endpoints:
  POST /api/auth/login    → TokenResponse
  POST /api/auth/refresh  → new access token
  POST /api/auth/logout   → revoke refresh token
  GET  /api/auth/me       → current user profile
  PUT  /api/auth/me       → update own profile
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

try:
    from jose import jwt
    from passlib.context import CryptContext
    _JWT_AVAILABLE = True
except ImportError:
    _JWT_AVAILABLE = False
    jwt = None  # type: ignore[assignment]

from ...models.user import AcceptInviteRequest, RefreshRequest, TokenResponse, UserPublic, UserUpdate

log = logging.getLogger("arta.auth")
router = APIRouter()

# ── Config ────────────────────────────────────────────────────────────────────

_SECRET_KEY    = os.environ.get("JWT_SECRET", "arta-dev-secret-change-in-production")
_ALGORITHM     = "HS256"
_ACCESS_EXPIRE = int(os.environ.get("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
_REFRESH_DAYS  = int(os.environ.get("JWT_REFRESH_TOKEN_EXPIRE_DAYS",   "7"))

# sha256_crypt is the default — bcrypt is listed for read-back compatibility
# only (passlib 1.7.x can't hash with bcrypt 5.x because the wheel dropped
# `__about__`, but verify() of legacy hashes still works). New password
# hashes (login change, accept-invite) all use sha256_crypt.
_pwd_ctx = CryptContext(
    schemes=["sha256_crypt", "bcrypt"],
    default="sha256_crypt",
    deprecated="auto",
) if _JWT_AVAILABLE else None

# ── Mock fallback (when DB is unavailable) ────────────────────────────────────
# F6-17: In production, the seed users (admin@arta.dev / demo1234 etc.) MUST
# NOT serve auth requests. We materialise the dict only outside production so
# every existing call site (`if email in _MOCK_USERS`, `_MOCK_USERS[email]`)
# behaves correctly without per-site conditional logic. In production the dict
# is empty, so all lookups miss and auth falls through to the DB-or-401 path.

_IS_PROD = os.environ.get("ENVIRONMENT", "development").lower() == "production"

_DEV_ONLY_USERS: dict[str, dict[str, Any]] = {
    "admin@arta.dev": {
        "id": "u1", "full_name": "ARTA Admin", "is_admin": True,
        "is_active": True, "role": "admin",
        "avatar_url": None, "created_at": "2026-01-01T00:00:00Z",
    },
    "alice@arta.dev": {
        "id": "u2", "full_name": "Alice Chen", "is_admin": False,
        "is_active": True, "role": "qa_lead",
        "avatar_url": None, "created_at": "2026-01-02T00:00:00Z",
    },
    "bob@arta.dev": {
        "id": "u3", "full_name": "Bob Martinez", "is_admin": False,
        "is_active": True, "role": "tester",
        "avatar_url": None, "created_at": "2026-01-03T00:00:00Z",
    },
    "viewer@arta.dev": {
        "id": "u4", "full_name": "Eve Viewer", "is_admin": False,
        "is_active": False, "role": "viewer",
        "avatar_url": None, "created_at": "2026-01-04T00:00:00Z",
    },
    "architect@arta.dev": {
        "id": "u5", "full_name": "Carol Architect", "is_admin": False,
        "is_active": True, "role": "test_architect",
        "avatar_url": None, "created_at": "2026-01-05T00:00:00Z",
    },
    "dev@arta.dev": {
        "id": "u6", "full_name": "Dan Developer", "is_admin": False,
        "is_active": True, "role": "developer",
        "avatar_url": None, "created_at": "2026-01-06T00:00:00Z",
    },
}

# Demo users require an EXPLICIT opt-in (ARTA_DEMO_MODE=1) and never
# materialize in production. Without demo mode the dict is empty, so every
# mock lookup misses and auth uses the DB-or-401 path only.
_DEMO_MODE = (not _IS_PROD) and os.environ.get("ARTA_DEMO_MODE") == "1"
_MOCK_USERS: dict[str, dict[str, Any]] = dict(_DEV_ONLY_USERS) if _DEMO_MODE else {}
_MOCK_PASSWORD = "demo1234" if _DEMO_MODE else None

import logging as _logging_auth
if _DEMO_MODE:
    _logging_auth.getLogger("arta.auth").warning(
        "ARTA_DEMO_MODE=1 — demo users enabled (admin@arta.dev / demo1234). Never use in production."
    )
else:
    _logging_auth.getLogger("arta.auth").info(
        "Demo users disabled (set ARTA_DEMO_MODE=1 for a no-database demo login)."
    )


async def ensure_bootstrap_admin() -> None:
    """First-run admin bootstrap: when the users table is empty and
    ARTA_BOOTSTRAP_ADMIN_EMAIL + ARTA_BOOTSTRAP_ADMIN_PASSWORD are set,
    create the first admin account. Called once at API startup."""
    email = os.environ.get("ARTA_BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ARTA_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not email or not password:
        return
    async with _try_get_db() as db:
        if not db:
            return
        try:
            from sqlalchemy import func, select
            from ...db.models import User
            from ...db.repository import UserRepo
            count = (await db.execute(select(func.count()).select_from(User))).scalar() or 0
            if count:
                return
            repo = UserRepo(db)
            user = await repo.create({
                "email": email,
                "full_name": "Bootstrap Admin",
                "is_active": True,
                "is_admin": True,
                "role": "admin",
            })
            if _pwd_ctx:
                user.hashed_password = _pwd_ctx.hash(password)
            await db.commit()
            _logging_auth.getLogger("arta.auth").warning(
                "Bootstrap admin created for %s (users table was empty). "
                "Unset ARTA_BOOTSTRAP_ADMIN_* after first login.", email)
        except Exception as exc:  # noqa: BLE001 — bootstrap must never block boot
            _logging_auth.getLogger("arta.auth").warning("bootstrap admin skipped: %s", exc)
_REFRESH_STORE: dict[str, dict[str, Any]] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(minutes=_ACCESS_EXPIRE)
    if _JWT_AVAILABLE:
        return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)
    return data.get("sub", "")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _user_to_public_from_orm(user) -> UserPublic:
    return UserPublic(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        avatar_url=user.avatar_url,
        is_admin=user.is_admin,
        is_active=user.is_active,
        created_at=user.created_at.isoformat() if user.created_at else "",
    )


def _user_to_public_from_mock(email: str) -> UserPublic:
    u = _MOCK_USERS[email]
    return UserPublic(
        id=u["id"], email=email, full_name=u["full_name"],
        avatar_url=u["avatar_url"], is_admin=u["is_admin"],
        is_active=u["is_active"], created_at=u["created_at"],
    )


from contextlib import asynccontextmanager as _asynccontextmanager


@_asynccontextmanager
async def _try_get_db():
    """Async context manager: yields a DB session, or None if DB unavailable.

    Was previously a plain async function returning a session that callers
    used without `async with` — every call leaked a connection. The pool's
    GC then logged
    `"AsyncAdaptedQueuePool: trying to clean up non-checked-in connection"`
    and forcibly terminated the connection. After repeated leaks the pool
    exhausted and new requests stalled.

    Usage:
        async with _try_get_db() as db:
            if db:
                # use real DB
            else:
                # use mock fallback
    """
    session = None
    try:
        from ...db.session import async_session_factory
        session = async_session_factory()
        # Quick connectivity probe so callers can fast-fall-back when DB is down.
        from sqlalchemy import text
        await session.execute(text("SELECT 1"))
    except Exception:
        # Connection-level SETUP failure → close any partial session and yield None
        # (mock fallback). RETURN immediately so the generator yields EXACTLY ONCE —
        # the real-session yield below must not also run on this path.
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
        yield None
        return
    # DB is up. Yield the real session OUTSIDE the setup try/except. ★ BUGFIX: previously
    # this yield sat inside the same `try` whose `except Exception` was meant only for
    # connectivity failures — so an HTTPException raised by the CALLER (e.g. login's 401
    # "Incorrect email or password") was thrown INTO the generator here, CAUGHT by that
    # except (HTTPException is an Exception), and answered with a SECOND `yield None`
    # during athrow() → "RuntimeError: generator didn't stop after athrow()" → a clean 401
    # surfaced as a 500. Keeping the yield out of the except lets caller exceptions
    # propagate cleanly; `finally` still returns the connection to the pool (no leak).
    try:
        yield session
    finally:
        try:
            await session.close()
        except Exception:
            pass


# ── Auth dependency (local, avoids circular import with dependencies.py) ──────

from fastapi.security import OAuth2PasswordBearer
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


async def _get_current_user_dep(token: str | None = Depends(_oauth2_scheme)):
    """Decode JWT and return user (DB or mock)."""
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if _JWT_AVAILABLE:
        try:
            payload = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
    else:
        payload = {"sub": token}

    email = payload.get("sub", "")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid token")

    async with _try_get_db() as db:
        if db:
            from ...db.repository import UserRepo
            repo = UserRepo(db)
            user = await repo.get_by_email(email)
            if user:
                if not user.is_active:
                    raise HTTPException(status_code=403, detail="Account inactive")
                return user

    # Fallback: mock
    if email in _MOCK_USERS:
        u = _MOCK_USERS[email]
        if not u["is_active"]:
            raise HTTPException(status_code=403, detail="Account inactive")
        # Return a mock object with attributes
        class _MockUser:
            pass
        mu = _MockUser()
        for k, v in u.items():
            setattr(mu, k, v)
        mu.email = email  # type: ignore[attr-defined]
        return mu

    raise HTTPException(status_code=401, detail="User not found")


# Public alias for import by other routers
get_current_user = _get_current_user_dep


def require_role(*allowed_roles: str):
    """FastAPI dependency factory for role-based access control.

    Usage: ``Depends(require_role("admin", "test_architect", "tester"))``
    Admins always bypass role checks.
    """
    async def _check(current_user=Depends(_get_current_user_dep)):
        # Admins bypass all role checks
        is_admin = getattr(current_user, "is_admin", False)
        if is_admin:
            return current_user
        # Check user-level role (mock users carry a .role attribute)
        user_role = getattr(current_user, "role", None)
        if user_role and user_role in allowed_roles:
            return current_user
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return _check


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse, summary="Login — get JWT access token")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    email = form.username.lower().strip()

    async with _try_get_db() as db:
        if db:
            try:
                from ...db.repository import UserRepo, RefreshTokenRepo
                repo = UserRepo(db)
                user = await repo.get_by_email(email)

                if not user:
                    raise HTTPException(status_code=401, detail="Incorrect email or password")
                if user.hashed_password and _pwd_ctx:
                    if not _pwd_ctx.verify(form.password, user.hashed_password):
                        raise HTTPException(status_code=401, detail="Incorrect email or password")
                elif _MOCK_PASSWORD is None or form.password != _MOCK_PASSWORD:
                    # No password hash on the row and demo mode is off → never
                    # authenticate (prevents the empty-password bypass).
                    raise HTTPException(status_code=401, detail="Incorrect email or password")
                if not user.is_active:
                    raise HTTPException(status_code=403, detail="Account inactive")

                user.last_login = datetime.now(timezone.utc)
                access_token = _create_access_token({"sub": email})

                refresh_token = secrets.token_hex(32)
                rt_repo = RefreshTokenRepo(db)
                await rt_repo.create(
                    user_id=str(user.id),
                    token_hash=_hash_token(refresh_token),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=_REFRESH_DAYS),
                )
                await db.commit()

                return TokenResponse(
                    access_token=access_token,
                    expires_in=_ACCESS_EXPIRE * 60,
                    user=_user_to_public_from_orm(user),
                )
            except HTTPException:
                raise  # Re-raise auth errors (401/403)
            except Exception as exc:
                import logging
                logging.getLogger("arta").warning("DB auth failed, falling back to mock: %s", exc)
                # Fall through to mock path below

    # Fallback: mock store
    if email not in _MOCK_USERS or form.password != _MOCK_PASSWORD:
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not _MOCK_USERS[email]["is_active"]:
        raise HTTPException(status_code=403, detail="Account inactive")

    access_token = _create_access_token({"sub": email})
    refresh_token = secrets.token_hex(32)
    _REFRESH_STORE[refresh_token] = {
        "user_email": email,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=_REFRESH_DAYS),
    }
    return TokenResponse(
        access_token=access_token,
        expires_in=_ACCESS_EXPIRE * 60,
        user=_user_to_public_from_mock(email),
    )


@router.post("/refresh", response_model=TokenResponse, summary="Refresh access token")
async def refresh_token(body: RefreshRequest):
    async with _try_get_db() as db:
        if db:
            from ...db.repository import UserRepo, RefreshTokenRepo
            rt_repo = RefreshTokenRepo(db)
            token_hash = _hash_token(body.refresh_token)
            rt = await rt_repo.get_by_hash(token_hash)
            if not rt:
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            if rt.expires_at < datetime.now(timezone.utc):
                await rt_repo.delete_by_hash(token_hash)
                await db.commit()
                raise HTTPException(status_code=401, detail="Refresh token expired")

            user_repo = UserRepo(db)
            user = await user_repo.get_by_id(str(rt.user_id))
            if not user:
                raise HTTPException(status_code=401, detail="User not found")

            access_token = _create_access_token({"sub": user.email})
            return TokenResponse(
                access_token=access_token,
                expires_in=_ACCESS_EXPIRE * 60,
                user=_user_to_public_from_orm(user),
            )

    # Fallback: mock
    entry = _REFRESH_STORE.get(body.refresh_token)
    if not entry:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if entry["expires_at"] < datetime.now(timezone.utc):
        _REFRESH_STORE.pop(body.refresh_token, None)
        raise HTTPException(status_code=401, detail="Refresh token expired")
    email = entry["user_email"]
    access_token = _create_access_token({"sub": email})
    return TokenResponse(
        access_token=access_token,
        expires_in=_ACCESS_EXPIRE * 60,
        user=_user_to_public_from_mock(email),
    )


@router.post("/accept-invite", response_model=TokenResponse,
             summary="Accept an invite — set password and sign in")
async def accept_invite(body: AcceptInviteRequest):
    """Public endpoint — the invite token is the credential. Validates the
    raw token against the stored bcrypt hashes, sets the user's password,
    activates them, optionally assigns the project role carried on the
    invite, and returns a fresh login session.
    """
    if _pwd_ctx is None:
        raise HTTPException(status_code=503, detail="Auth subsystem unavailable (passlib not installed)")

    # Use db_adapter.try_db (not _try_get_db) so HTTPException propagation
    # works cleanly — _try_get_db has a known generator-didn't-stop bug
    # when the body raises (see db_adapter.py F4-3 fix history).
    from ..db_adapter import try_db
    async with try_db() as db:
        if not db:
            raise HTTPException(status_code=503, detail="Invite acceptance requires the database")

        from ...db.repository import (
            InviteTokenRepo,
            ProjectRoleRepo,
            RefreshTokenRepo,
            UserRepo,
        )

        invite_repo = InviteTokenRepo(db)
        # SHA-256 hex lookup — same hashing as RefreshToken so we get O(1)
        # equality search instead of scanning all unaccepted invites.
        invite = await invite_repo.get_by_hash(_hash_token(body.token))
        if invite is None or invite.accepted_at is not None:
            raise HTTPException(status_code=400, detail="Invalid or expired invite token")
        if invite.expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="Invite has expired")

        user_repo = UserRepo(db)
        user = await user_repo.get_by_id(str(invite.user_id))
        if not user:
            raise HTTPException(status_code=400, detail="Invite is no longer valid")

        user.hashed_password = _pwd_ctx.hash(body.password)
        user.is_active = True
        user.last_login = datetime.now(timezone.utc)
        await db.flush()

        if invite.project_id and invite.project_role:
            role_repo = ProjectRoleRepo(db)
            await role_repo.assign(
                str(user.id),
                str(invite.project_id),
                invite.project_role,
                str(invite.invited_by) if invite.invited_by else None,
            )

        await invite_repo.mark_accepted(invite.id)

        access_token = _create_access_token({"sub": user.email})
        refresh_token = secrets.token_hex(32)
        rt_repo = RefreshTokenRepo(db)
        await rt_repo.create(
            user_id=str(user.id),
            token_hash=_hash_token(refresh_token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=_REFRESH_DAYS),
        )
        # try_db commits on clean exit — no explicit commit needed.

        log.info("invite accepted: email=%s user=%s project=%s",
                 user.email, str(user.id),
                 str(invite.project_id) if invite.project_id else "-")

        return TokenResponse(
            access_token=access_token,
            expires_in=_ACCESS_EXPIRE * 60,
            user=_user_to_public_from_orm(user),
        )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke refresh token")
async def logout(body: RefreshRequest):
    async with _try_get_db() as db:
        if db:
            from ...db.repository import RefreshTokenRepo
            rt_repo = RefreshTokenRepo(db)
            await rt_repo.delete_by_hash(_hash_token(body.refresh_token))
            await db.commit()
            return

    _REFRESH_STORE.pop(body.refresh_token, None)


@router.get("/me", response_model=UserPublic, summary="Current user profile")
async def get_me(current_user=Depends(_get_current_user_dep)):
    return _user_to_public_from_orm(current_user)


@router.put("/me", response_model=UserPublic, summary="Update own profile")
async def update_me(body: UserUpdate, current_user=Depends(_get_current_user_dep)):
    async with _try_get_db() as db:
        if db:
            from ...db.repository import UserRepo
            repo = UserRepo(db)
            updates = {}
            if body.full_name is not None:
                updates["full_name"] = body.full_name
            if body.avatar_url is not None:
                updates["avatar_url"] = body.avatar_url
            if updates:
                await repo.update(str(current_user.id), updates)
                await db.commit()
                user = await repo.get_by_id(str(current_user.id))
                return _user_to_public_from_orm(user)
    # Mock fallback
    email = current_user.email if hasattr(current_user, "email") else current_user.get("email", "")
    if email in _MOCK_USERS:
        if body.full_name is not None:
            _MOCK_USERS[email]["full_name"] = body.full_name
        if body.avatar_url is not None:
            _MOCK_USERS[email]["avatar_url"] = body.avatar_url
        return _user_to_public_from_mock(email)
    return _user_to_public_from_orm(current_user)


# ── OAuth Flow ────────────────────────────────────────────────────────────────

import base64
import urllib.parse

from fastapi.responses import RedirectResponse

_OAUTH_PROVIDERS = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://www.googleapis.com/oauth2/v2/userinfo",
        "scope": "openid email profile",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "scope": "user:email",
        "client_id_env": "GITHUB_CLIENT_ID",
        "client_secret_env": "GITHUB_CLIENT_SECRET",
    },
    "microsoft": {
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "userinfo_url": "https://graph.microsoft.com/oidc/userinfo",
        "scope": "openid email profile",
        "client_id_env": "MICROSOFT_CLIENT_ID",
        "client_secret_env": "MICROSOFT_CLIENT_SECRET",
    },
}

_FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
_OAUTH_REDIRECT_BASE = os.environ.get("OAUTH_REDIRECT_BASE", "http://localhost:8000")


@router.get("/oauth/{provider}", summary="Start OAuth flow — redirects to provider")
async def oauth_redirect(provider: str):
    if provider not in _OAUTH_PROVIDERS:
        raise HTTPException(400, f"Unsupported provider: {provider}")

    cfg = _OAUTH_PROVIDERS[provider]
    client_id = os.environ.get(cfg["client_id_env"])
    if not client_id:
        raise HTTPException(503, f"{provider} OAuth not configured — set {cfg['client_id_env']}")

    state = secrets.token_urlsafe(32)
    redirect_uri = f"{_OAUTH_REDIRECT_BASE}/api/auth/oauth/{provider}/callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": cfg["scope"],
        "state": state,
        "response_type": "code",
    }
    url = f"{cfg['authorize_url']}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url)


@router.get("/oauth/{provider}/callback", summary="OAuth callback — exchanges code for token")
async def oauth_callback(provider: str, code: str, state: str | None = None):
    if provider not in _OAUTH_PROVIDERS:
        raise HTTPException(400, f"Unsupported provider: {provider}")

    cfg = _OAUTH_PROVIDERS[provider]
    client_id = os.environ.get(cfg["client_id_env"], "")
    client_secret = os.environ.get(cfg["client_secret_env"], "")
    redirect_uri = f"{_OAUTH_REDIRECT_BASE}/api/auth/oauth/{provider}/callback"

    # Exchange code for access token
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            headers = {"Accept": "application/json"}
            token_resp = await client.post(cfg["token_url"], data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }, headers=headers)
            token_data = token_resp.json()
            provider_token = token_data.get("access_token")
            if not provider_token:
                raise HTTPException(401, "OAuth token exchange failed")

            # Fetch user profile
            auth_header = {"Authorization": f"Bearer {provider_token}"}
            if provider == "github":
                auth_header["Accept"] = "application/json"
            profile_resp = await client.get(cfg["userinfo_url"], headers=auth_header)
            profile = profile_resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"OAuth provider communication failed: {e}")

    # Extract user info
    if provider == "google":
        oauth_id = profile.get("id", "")
        email = profile.get("email", "")
        full_name = profile.get("name", email.split("@")[0])
        avatar_url = profile.get("picture")
    elif provider == "github":
        oauth_id = str(profile.get("id", ""))
        email = profile.get("email") or f"{profile.get('login', 'user')}@github.com"
        full_name = profile.get("name") or profile.get("login", "GitHub User")
        avatar_url = profile.get("avatar_url")
    elif provider == "microsoft":
        oauth_id = profile.get("sub", "")
        email = profile.get("email") or profile.get("upn", "")
        full_name = profile.get("name", email.split("@")[0])
        avatar_url = profile.get("picture")
    else:
        raise HTTPException(400, "Unsupported provider")

    # Find or create user
    user_public = None
    async with _try_get_db() as db:
        if db:
            from ...db.repository import UserRepo
            repo = UserRepo(db)
            user = await repo.get_by_email(email)
            if not user:
                user = await repo.create({
                    "email": email,
                    "full_name": full_name,
                    "avatar_url": avatar_url,
                    "oauth_provider": provider,
                    "oauth_id": oauth_id,
                    "is_active": True,
                })
                await db.commit()
            else:
                if not user.oauth_provider:
                    user.oauth_provider = provider
                    user.oauth_id = oauth_id
                    await db.commit()
            user_public = _user_to_public_from_orm(user)
        else:
            # Mock fallback
            if email not in _MOCK_USERS:
                _MOCK_USERS[email] = {
                    "id": f"u-oauth-{oauth_id[:8]}",
                    "full_name": full_name,
                    "is_admin": False,
                    "is_active": True,
                    "role": "viewer",
                    "avatar_url": avatar_url,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            user_public = _user_to_public_from_mock(email)

    # Issue JWT
    access_token = _create_access_token({"sub": email})
    user_json_b64 = base64.urlsafe_b64encode(
        user_public.model_dump_json().encode()
    ).decode()

    # Redirect to frontend with token
    return RedirectResponse(
        f"{_FRONTEND_URL}/oauth-callback?token={access_token}&user={user_json_b64}"
    )
