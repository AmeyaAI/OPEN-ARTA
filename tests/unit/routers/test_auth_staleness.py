"""R72.5 — unit test for the auth-staleness endpoint.

The endpoint reports cookie TTL state so the operator gets advance
notice (stale_soon) before the auth-refresh manual touchpoint becomes
a blocker (expired). Verifies the 4 states: fresh / stale_soon /
expired / unknown.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from src.api.routers.discovery import auth_staleness
from src.api.routers.projects import _PROJECTS


def _synth_jwt(exp_offset_s: int, iat_offset_s: int = -86400) -> str:
    """Build an unsigned JWT with controllable exp/iat for testing."""
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode())
        .rstrip(b"=")
        .decode()
    )
    payload = json.dumps(
        {
            "exp": int(time.time() + exp_offset_s),
            "iat": int(time.time() + iat_offset_s),
            "user_id": "test",
        }
    ).encode()
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"{header}.{payload_b64}.SIGNATURE"


@pytest.fixture
def with_project(request):
    """Inject a test project into _PROJECTS and remove on teardown."""
    pid, cookie = request.param
    _PROJECTS[pid] = {
        "id": pid,
        "environments": {
            "staging": {"auth": {"credentials": {"cookie_value": cookie}}}
        },
    }
    yield pid
    _PROJECTS.pop(pid, None)


@pytest.mark.parametrize(
    "with_project,expected_state",
    [
        (("pid-fresh", _synth_jwt(72000, -14400)), "fresh"),   # 20h of 24h
        (("pid-stale", _synth_jwt(3600, -86400)), "stale_soon"),   # 1h of 25h = 4%
        (("pid-expired", _synth_jwt(-3600, -86400)), "expired"),
        (("pid-empty", ""), "unknown"),
        (("pid-redacted", "REPLACE_ME"), "unknown"),
    ],
    indirect=["with_project"],
)
def test_auth_staleness_states(with_project: str, expected_state: str) -> None:
    result = asyncio.run(auth_staleness(with_project, environment="staging"))
    assert result["state"] == expected_state, (
        f"For project {with_project}: expected state={expected_state} but got "
        f"{result['state']} (ttl={result.get('ttl_remaining_hours')}h, "
        f"pct={result.get('ttl_pct_remaining')}%)"
    )


def test_stale_soon_payload_has_actionable_hint() -> None:
    """The stale_soon hint must mention the remaining hours so the
    operator can plan the refresh."""
    pid = "pid-stale-hint-check"
    _PROJECTS[pid] = {
        "id": pid,
        "environments": {
            "staging": {
                "auth": {"credentials": {"cookie_value": _synth_jwt(3600, -86400)}}
            }
        },
    }
    try:
        result = asyncio.run(auth_staleness(pid, environment="staging"))
        assert result["state"] == "stale_soon"
        assert result["hint"]
        assert "h" in result["hint"]  # mentions a duration in hours
        assert "Refresh" in result["hint"]  # actionable verb
    finally:
        _PROJECTS.pop(pid, None)


# R75.2 — opaque-cookie (non-JWT) fallback via last_paste_at + ttl_hours
# ─────────────────────────────────────────────────────────────────────

def _opaque_creds(last_paste_offset_hours: float, ttl_hours: float | None = None) -> dict:
    """Build a non-JWT cookie creds dict with a synthetic last_paste_at."""
    from datetime import datetime, timezone, timedelta
    paste_at = datetime.now(timezone.utc) + timedelta(hours=last_paste_offset_hours)
    creds = {
        "cookie_value": "opaque_session_token_not_a_jwt",
        "last_paste_at": paste_at.isoformat(),
    }
    if ttl_hours is not None:
        creds["ttl_hours"] = ttl_hours
    return creds


def test_R75_2_opaque_recent_paste_is_fresh() -> None:
    """Opaque cookie pasted 2h ago, default TTL 24h → fresh."""
    pid = "pid-r75-2-fresh"
    _PROJECTS[pid] = {
        "id": pid,
        "environments": {
            "staging": {"auth": {"credentials": _opaque_creds(-2)}}
        },
    }
    try:
        result = asyncio.run(auth_staleness(pid, environment="staging"))
        assert result["state"] == "fresh", f"got {result}"
        assert result["ttl_remaining_hours"] is not None
        assert result["ttl_remaining_hours"] > 20  # ~22h remaining
    finally:
        _PROJECTS.pop(pid, None)


def test_R75_2_opaque_recent_but_short_ttl_is_stale_soon() -> None:
    """Opaque cookie pasted 7h ago, configured TTL 8h → stale_soon (12% remaining)."""
    pid = "pid-r75-2-stale"
    _PROJECTS[pid] = {
        "id": pid,
        "environments": {
            "staging": {"auth": {"credentials": _opaque_creds(-7, ttl_hours=8)}}
        },
    }
    try:
        result = asyncio.run(auth_staleness(pid, environment="staging"))
        assert result["state"] == "stale_soon", f"got {result}"
        assert result["ttl_pct_remaining"] is not None
        assert result["ttl_pct_remaining"] <= 25.0
    finally:
        _PROJECTS.pop(pid, None)


def test_R75_2_opaque_old_paste_is_expired() -> None:
    """Opaque cookie pasted 30h ago, default TTL 24h → expired."""
    pid = "pid-r75-2-expired"
    _PROJECTS[pid] = {
        "id": pid,
        "environments": {
            "staging": {"auth": {"credentials": _opaque_creds(-30)}}
        },
    }
    try:
        result = asyncio.run(auth_staleness(pid, environment="staging"))
        assert result["state"] == "expired", f"got {result}"
        assert result["ttl_remaining_seconds"] == 0
    finally:
        _PROJECTS.pop(pid, None)


def test_R75_2_opaque_no_paste_stamp_is_unknown() -> None:
    """Opaque cookie + no `last_paste_at` → state=unknown (preserves
    pre-R75.2 behavior for projects that haven't paste since the
    stamp was introduced)."""
    pid = "pid-r75-2-no-stamp"
    _PROJECTS[pid] = {
        "id": pid,
        "environments": {
            "staging": {
                "auth": {"credentials": {"cookie_value": "opaque_no_stamp"}}
            }
        },
    }
    try:
        result = asyncio.run(auth_staleness(pid, environment="staging"))
        assert result["state"] == "unknown"
        assert "last_paste_at" in (result.get("hint") or "")
    finally:
        _PROJECTS.pop(pid, None)
