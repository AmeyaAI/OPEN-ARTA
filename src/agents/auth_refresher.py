"""R11 — Auth Refresher

Detects expired auth credentials in a project's storage-state file and
attempts to refresh them before discovery (K1) runs Playwright. The SaaS
SPAs we drive typically wrap an upstream identity
provider (Google / GitHub OAuth). When the SaaS-issued JWT cookie expires:

  1. The user's REFRESH token may still be valid — many SaaS apps expose
     a `/auth/refresh` endpoint that exchanges it for a fresh JWT.
  2. If the refresh token came from Google OAuth, Google's token
     endpoint can mint a new ID token directly.
  3. If both fail, the operator must re-do the OAuth dance once. Tool
     `tools/refresh_oauth_storage.py` automates that with headed codegen.

Why this lives in K1:
  Pre-R11, expired credentials manifested as "harvested 0 env vars" → all
  generated tests cascade-skipped. R10 added a clear AUTH-EXPIRED diagnosis
  log line. R11 takes the next step: don't just diagnose, try to FIX it
  automatically. ARTA's self-healing pillar is supposed to recover from
  drift without operator intervention; an expired token is the highest-
  frequency drift in any SaaS test pipeline.

Public API:
  refresh_if_expired(project) -> RefreshResult

  Returns a structured result. Never raises — discovery proceeds in either
  case. The caller logs `result.message` so operators see what happened.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("arta.auth_refresher")

# Where storage-state files live. Operators keep `<env>-storage.json`
# siblings here. K1 + Playwright runs read from this directory.
_DEFAULT_ENVS_DIR = Path(".arta/environments")

# Google OAuth refresh tokens always start with `1//`. GitHub OAuth tokens
# start with `gho_`/`ghu_`/`ghs_`. We use these prefixes to pick the right
# fallback when the SaaS app's own refresh endpoint is unknown or fails.
_GOOGLE_PREFIX = "1//"
_GITHUB_PREFIXES = ("gho_", "ghu_", "ghs_")

# Google's OAuth 2.0 token endpoint. Used by Strategy B when the SaaS
# app's own refresh endpoint isn't reachable but the refresh token is
# Google-issued AND we have the OAuth client credentials.
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


@dataclass
class RefreshResult:
    refreshed: bool
    reason: str
    new_storage_path: Path | None = None
    diagnostic_lines: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        head = "REFRESHED" if self.refreshed else "NO-OP"
        return f"auth_refresher: {head} — {self.reason}"


# ── Cookie / JWT inspection ────────────────────────────────────────────────


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Best-effort base64url-decode of a JWT's payload. Returns None on any
    malformed input — caller treats it as "not a JWT, can't tell"."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        pad = "=" * ((4 - len(parts[1]) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return None


def _is_jwt_expired(token: str, *, leeway_s: int = 30) -> bool:
    """A JWT is "expired enough to refresh" when `exp` is past, OR within
    `leeway_s` of now. The leeway prevents a race where the token expires
    mid-discovery (verify-then-spawn → token expires → spawn fails)."""
    payload = _decode_jwt_payload(token)
    if not payload:
        return False  # not a JWT we can reason about; assume valid
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return float(exp) - leeway_s <= time.time()


def _session_effective_exp(token: str) -> float | None:
    """The EFFECTIVE expiry of a session cookie. A wrapper-style session cookie WRAPS a
    third-party id_token (`third_party_token`, e.g. Google — ~1h TTL); the SUT's
    authz server validates that INNER token, which expires long before the outer
    WRAPPER (~days). Return the EARLIEST exp of {wrapper, nested id_token} — the
    expiry the refresh cycle must actually track. None when no exp is decodable."""
    exps: list[float] = []
    payload = _decode_jwt_payload(token)
    if payload:
        if isinstance(payload.get("exp"), (int, float)):
            exps.append(float(payload["exp"]))
        inner = payload.get("third_party_token") or payload.get("id_token")
        inner_p = _decode_jwt_payload(inner) if isinstance(inner, str) else None
        if inner_p and isinstance(inner_p.get("exp"), (int, float)):
            exps.append(float(inner_p["exp"]))
    return min(exps) if exps else None


def _is_session_expired(token: str, *, leeway_s: int = 30) -> bool:
    """True when the session cookie's EFFECTIVE expiry (min of the wrapper cookie AND
    its nested third_party_token) is past or within `leeway_s`. This FIXES the auth
    workflow bug where a fresh session WRAPPER masked an expired inner Google id_token:
    `refresh_if_expired` checked only the wrapper (~days), so it never refreshed, the
    inner ~1h token expired mid-session, and the SUT analytics returned 400 'Cant
    Connect to Authourization Server'. Killswitch ARTA_SESSION_INNER_EXP_DISABLE=1
    reverts to wrapper-only."""
    if os.environ.get("ARTA_SESSION_INNER_EXP_DISABLE") == "1":
        return _is_jwt_expired(token, leeway_s=leeway_s)
    exp = _session_effective_exp(token)
    if exp is None:
        return False
    return exp - leeway_s <= time.time()


def _find_storage_state_path(env_name: str | None = None) -> Path | None:
    """Find the most recent storage-state file for an env (or globally)."""
    envs_dir = Path(os.environ.get("ARTA_ENVIRONMENTS_DIR",
                                    str(_DEFAULT_ENVS_DIR))).resolve()
    if not envs_dir.is_dir():
        return None
    if env_name:
        target = envs_dir / f"{env_name}-storage.json"
        if target.is_file():
            return target
    # Fallback: newest *-storage.json
    candidates = sorted(envs_dir.glob("*-storage.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _read_storage_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        log.warning("auth_refresher: couldn't parse storage-state %s: %s",
                    path, exc)
        return None


def get_active_cookie(
    env_name: str | None,
    cookie_name: str | None = None,
) -> dict | None:
    """R28.0a — return the active session cookie from the env's storage
    state file when present + non-expired.

    Storage state is the canonical runtime auth source after R15 paste:
    R15b writes to `.arta/environments/<env>-storage.json` and never
    touches `projects.json` `auth.credentials.cookie_value` (that field
    is a deliberate redaction marker `***` so projects.json can be
    committed to git without leaking secrets).

    Pre-R28.0, R21a/R21b treated projects.json as authoritative —
    making R15 paste invisible to subsequent runs (the modal re-popped
    every time, and R21a's scrub emptied the cookie env var). This
    helper inverts the precedence: callers consult storage state
    FIRST, fall back to projects.json's static value only when this
    returns None.

    Returns:
      {"name": cookie_name, "value": jwt_or_opaque, "domain": "..."}
      OR None when:
        - env_name unknown / no storage file
        - cookie not in `cookies[]`
        - cookie's JWT exp is in the past

    Opaque (non-JWT) cookies pass through (we can't tell expiry).
    """
    if not env_name:
        return None
    storage_path = _find_storage_state_path(env_name)
    if not storage_path or not storage_path.is_file():
        return None
    storage = _read_storage_state(storage_path)
    if not storage:
        return None
    for c in storage.get("cookies") or []:
        if not isinstance(c, dict):
            continue
        cname = c.get("name") or ""
        # Match strategies:
        #   1. Exact match on caller-supplied name (preferred)
        #      session-token, JWT_token, etc.) — matches R15's bookmarklet
        if cookie_name:
            if cname != cookie_name:
                continue
        else:
            if not cname.lower().endswith("token"):
                continue
            # R334 — a REFRESH token is not an API access bearer (it just outlives
            # + precedes the access token in the jar, so this first-match loop
            # SUT-agnostic; killswitch ARTA_R334_REFRESH_EXCLUDE_DISABLE=1.
            if (os.environ.get("ARTA_R334_REFRESH_EXCLUDE_DISABLE") != "1"
                    and "refresh" in cname.lower()):
                continue
        cval = c.get("value")
        if not isinstance(cval, str) or not cval:
            continue
        # Reject expired JWTs. Opaque cookies (no decodable payload)
        # are accepted at face value — only the SUT can tell us if
        # expires long before the wrapper).
        if _decode_jwt_payload(cval) and _is_session_expired(cval):
            log.debug(
                "R28.0a: storage state cookie %r is expired — skipping",
                cname,
            )
            return None
        return {
            "name": cname,
            "value": cval,
            "domain": c.get("domain"),
        }
    return None


def _extract_refresh_token(
    storage: dict, project_auth: dict | None, variables: dict | list | None = None,
) -> str | None:
    """Refresh token may live in:
      - storage-state's `origins[].localStorage[]` for key `refresh-token`
        / `refresh_token` / `refreshToken`
      - project config's `auth.credentials.localStorage.refresh-token`
      - M3 (DURABLE): `auth.credentials.refresh_token` and `variables.refresh_token`
        string forms — the copy that SURVIVES a storage-state wipe (e.g. an api_key
        persisted in projects.json). Appended last so ephemeral localStorage wins.
    Strip wrapping quotes that Playwright's localStorage often double-encodes.
    """
    candidates: list[str] = []

    for origin in (storage or {}).get("origins") or []:
        for ls in origin.get("localStorage") or []:
            name = (ls.get("name") or "").lower()
            if "refresh" in name and "token" in name:
                v = ls.get("value")
                if isinstance(v, str):
                    candidates.append(v)

    if isinstance(project_auth, dict):
        creds = project_auth.get("credentials") or {}
        ls_map = creds.get("localStorage") or {}
        if isinstance(ls_map, dict):
            for k, v in ls_map.items():
                if "refresh" in k.lower() and "token" in k.lower() and isinstance(v, str):
                    candidates.append(v)

    # M3 — recover the refresh token from the DURABLE config (string forms), so the
    # server-side refresher self-heals after a storage-state wipe the same way the
    # still wins. Killswitch ARTA_DURABLE_REFRESH_SRC_DISABLE=1.
    if os.environ.get("ARTA_DURABLE_REFRESH_SRC_DISABLE") != "1":
        if isinstance(project_auth, dict):
            _cred_rt = (project_auth.get("credentials") or {}).get("refresh_token")
            if isinstance(_cred_rt, str) and _cred_rt.strip():
                candidates.append(_cred_rt)
        # `variables` may be a dict OR a list-of-{name,value} (same shape handling as
        # _refresh_cfg_from_env_variables).
        _vars_map: dict = {}
        if isinstance(variables, dict):
            _vars_map = variables
        elif isinstance(variables, list):
            for _e in variables:
                if isinstance(_e, dict) and _e.get("name"):
                    _vars_map[_e["name"]] = _e.get("value")
        for _vk in ("refresh_token", "REFRESH_TOKEN"):
            _vv = _vars_map.get(_vk)
            if isinstance(_vv, str) and _vv.strip():
                candidates.append(_vv)

    for raw in candidates:
        cleaned = raw.strip()
        # Playwright stores the value with surrounding double-quotes when the
        # site stored it via JSON.stringify; unwrap.
        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1]
        if cleaned:
            return cleaned
    return None


def _detect_provider(refresh_token: str) -> str:
    """Best-effort provider tag based on token prefix."""
    if refresh_token.startswith(_GOOGLE_PREFIX):
        return "google"
    if any(refresh_token.startswith(p) for p in _GITHUB_PREFIXES):
        return "github"
    return "saas-app"


# ── Refresh strategies ─────────────────────────────────────────────────────


def _refresh_cfg_from_env_variables(env_block: dict | None) -> dict | None:
    """R253.PW.3 — synthesize an explicit ``auth.refresh`` config from the env
    block's ``arta_refresh_*`` variables.

    SINGLE SOURCE OF TRUTH: the dispatch env-var path (R156.J.3 in
    execution.py) already reads these SAME variables to drive the in-spec
    (Playwright TS) ``refreshAuthIfExpiring``. Pre-R253.PW.3 the SERVER-SIDE
    refresher (``refresh_if_expired``) only inspected ``auth.refresh`` — so when
    a SUT's refresh flow was declared via variables (e.g.:
    ``arta_refresh_endpoint=/v1/regions/global/auth/token``) with no
    ``auth.refresh`` block, the server-side path fell through to the WRONG
    heuristic sweep (``/oauth/token`` → 307, ``/api/v1/oauth/token`` → 404),
    NO-OP'd, and the run's token expired mid-flight → 401 storm on the later
    (post-TTL) specs. Reading the same variables aligns both paths onto the one
    configured endpoint. GENERIC across SUTs (any project that declared the
    variable-form refresh flow)."""
    if not isinstance(env_block, dict):
        return None
    _vars = env_block.get("variables") or {}
    if not isinstance(_vars, dict):
        try:
            _vars = {d.get("name"): d.get("value") for d in _vars if isinstance(d, dict)}
        except Exception:
            return None
    endpoint = _vars.get("arta_refresh_endpoint") or _vars.get("ARTA_REFRESH_ENDPOINT")
    if not endpoint or not isinstance(endpoint, str):
        return None
    # Normalize "POST /path" → bare url/path
    if " " in endpoint:
        endpoint = endpoint.split(" ", 1)[1]
    body_field = _vars.get("arta_refresh_request_body_field") or "refresh_token"
    access_field = _vars.get("arta_refresh_response_access_field") or "access_token"
    body: dict[str, Any] = {body_field: "{refresh_token}"}
    # Extra static body fields some SUTs require on the refresh POST
    _extra = _vars.get("arta_refresh_extra_body")
    if _extra:
        try:
            _extra_obj = json.loads(_extra) if isinstance(_extra, str) else _extra
            if isinstance(_extra_obj, dict):
                for _k, _v in _extra_obj.items():
                    body.setdefault(str(_k), _v)
        except Exception:
            pass
    return {
        "endpoint": endpoint,
        "method": "POST",
        "content_type": "application/json",
        "body": body,
        "access_token_paths": [
            f"$.{access_field}", "$.access_token", "$.token", "$.id_token",
        ],
    }


def _candidate_refresh_endpoints(
    api_base_url: str | None,
    base_url: str | None,
    project_auth: dict | None,
    subst: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build the ordered list of refresh-endpoint attempts.

    Operators can declare an explicit endpoint under
    `project.environments.<env>.auth.refresh` — when present, that's
    tried FIRST and exclusively. When absent, we sweep common patterns.

    The returned dicts have keys:
      url, method, body, query, headers, access_token_paths, cookie_header

    `subst` (when provided) is the placeholder map resolved from the
    EXPIRED cookie's own JWT claims (`{subscription_id}`, `{provider}`,
    `{schema_id}`, `{user_id}`, …) plus `{refresh_token}` / `{api_base_url}`.
    It is applied to the explicit endpoint's url/query/body/headers so a
    path+query-templated GET refresh (e.g.: `/refresh/{subscription_id}/
    {provider}/{schema_id}/{user_id}?refresh_token=…`) resolves fully — the
    expired token still carries the identifiers needed to mint its successor.
    """
    out: list[dict[str, Any]] = []
    _sub = (lambda t: _substitute(t, subst)) if subst else (lambda t: t)

    # 1. Explicit operator-declared endpoint
    if isinstance(project_auth, dict):
        refresh_cfg = project_auth.get("refresh") or {}
        url = refresh_cfg.get("endpoint")
        if url:
            method = (refresh_cfg.get("method") or "POST").upper()
            # A GET refresh carries no request body — the identifiers live in
            # the path + query string. POST keeps the legacy default body.
            raw_body = refresh_cfg.get("body")
            if raw_body is None:
                raw_body = None if method == "GET" else {"refresh_token": "{refresh_token}"}
            out.append({
                "url": _sub(url),
                "method": method,
                "body": _sub(raw_body) if raw_body is not None else None,
                "query": _sub(refresh_cfg.get("query") or {}),
                "headers": _sub(refresh_cfg.get("headers") or {}),
                "access_token_paths": refresh_cfg.get("access_token_paths")
                    or ["$.access_token", "$.token", "$.id_token", "$.session_token"],
                "cookie_header": refresh_cfg.get("cookie_header") or "set-cookie",
                # R253.PW.5 — thread the declared content_type so
                # _try_refresh_endpoint honors JSON-vs-form explicitly instead
                # of guessing from the presence of `grant_type`.
                "content_type": refresh_cfg.get("content_type"),
                "explicit": True,
            })
            return out  # operator-declared is exclusive

    # 2. Heuristic sweep — try API host first, then UI host
    hosts: list[str] = []
    for h in (api_base_url, base_url):
        if h and h not in hosts:
            hosts.append(h.rstrip("/"))

    common_paths = [
        "/api/v1/auth/refresh",
        "/api/auth/refresh",
        "/auth/refresh",
        "/oauth/token",
        "/api/v1/oauth/token",
    ]
    for host in hosts:
        for p in common_paths:
            out.append({
                "url": host + p,
                "method": "POST",
                # Two body shapes: SaaS-style {refresh_token:..} and
                # OAuth-style grant_type=refresh_token. The retry loop
                # tries body[0] first; if that 4xxs we fall through to
                # body[1] on the same URL via the `body_alt` field.
                "body": {"refresh_token": "{refresh_token}"},
                "body_alt": {
                    "grant_type": "refresh_token",
                    "refresh_token": "{refresh_token}",
                },
                "headers": {},
                "access_token_paths": ["$.access_token", "$.token", "$.id_token"],
                "cookie_header": "set-cookie",
                "explicit": False,
            })
    return out


def _read_jsonpath(body: Any, path: str) -> Any:
    """Tiny jsonpath subset: '$.foo.bar' walks dict keys. Returns None on miss."""
    if not isinstance(path, str) or not path.startswith("$."):
        return None
    cur = body
    for seg in path[2:].split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _try_refresh_endpoint(
    candidate: dict[str, Any],
    refresh_token: str,
    *,
    timeout_s: float = 4.0,
) -> dict[str, Any] | None:
    """Try one endpoint with primary body, then alt body if the primary 4xxs.

    REVIEW-V6: returns a dict with `_outcome` tag so the caller can
    distinguish "endpoint 4xx'd" (try other paths) from "host
    unreachable" (skip ALL paths on this host).

    Returns:
      - {"_outcome": "ok", ...response fields...} on 2xx
      - {"_outcome": "network_error", "error": "..."} on connection / TLS / DNS failure
      - None on 4xx/5xx (endpoint exists but rejected request)

    Timeout dropped from 8s → 4s; with 10 endpoints × 2 body shapes a
    fully-failed sweep is now 80s → 40s worst case.
    """
    method = (candidate.get("method") or "POST").upper()
    # Query params (substitute {refresh_token} in case it was templated into
    # the query rather than resolved at candidate-build time — idempotent
    # when already resolved). Used by GET refresh endpoints.
    query = candidate.get("query") or None
    if query:
        query = _substitute(query, {"refresh_token": refresh_token})

    # GET (or any body-less) refresh has no body to send; POST sweeps the
    # primary body then the OAuth-form alt.
    if method == "GET" or candidate.get("body") is None:
        bodies: list[Any] = [None]
    else:
        bodies = [candidate["body"]]
        if candidate.get("body_alt"):
            bodies.append(candidate["body_alt"])

    for body_template in bodies:
        # Substitute {refresh_token} into the body template (no-op for None)
        body = (
            _substitute(body_template, {"refresh_token": refresh_token})
            if body_template is not None else None
        )
        # REVIEW-V4: OAuth-standard refresh requests use
        # `application/x-www-form-urlencoded` (RFC 6749 §6). Detect by
        # presence of `grant_type` in the body — when present, send as
        # form data; otherwise send JSON (the SaaS-app convention).
        # Pre-fix we always sent JSON, which strict OAuth servers reject
        # with `unsupported_grant_type` or `invalid_request`.
        is_form_body = isinstance(body, dict) and "grant_type" in body
        # R253.PW.5 — an EXPLICIT content_type on the candidate overrides the
        # grant_type→form heuristic. The REVIEW-V4 heuristic assumes any body
        # carrying `grant_type` is RFC-6749 OAuth form-encoded, but plenty of
        # SaaS auth endpoints take a JSON body that HAPPENS to include a
        # `{"grant_type":"refresh_token","refresh_token":…}` as JSON — form
        # encoding 4xxs it). When the operator/variables declared
        # `content_type`, trust it: `*json*` → JSON, `*form*`/`*urlencoded*` →
        # form. Heuristic still applies when no content_type is declared
        # (backward compatible with the heuristic-sweep candidates).
        _decl_ct = (candidate.get("content_type") or "").lower()
        if _decl_ct:
            if "json" in _decl_ct:
                is_form_body = False
            elif "form" in _decl_ct or "urlencoded" in _decl_ct:
                is_form_body = True
        # SUT refresh endpoints run on internal/sandbox hosts that routinely
        # rest of ARTA's SUT-facing traffic (R123.C health probe, ZAP, PW, k6)
        # already uses verify=False for exactly this reason — the refresher was
        # the lone outlier, so a valid-but-untrusted cert surfaced as a bogus
        # "host unreachable (TLS/connect failure)" and silently blocked every
        # auto-refresh. Match the platform convention: don't verify the SUT's
        # cert. (Google-direct Strategy B keeps strict verification — public CA.)
        # Killswitch ARTA_REFRESH_TLS_VERIFY=1 restores strict verification.
        _verify = os.environ.get("ARTA_REFRESH_TLS_VERIFY") == "1"
        try:
            resp = httpx.request(
                method,
                candidate["url"],
                params=query,
                json=body if (isinstance(body, dict) and not is_form_body) else None,
                data=body if (body is not None and (is_form_body or not isinstance(body, dict))) else None,
                headers=candidate["headers"],
                timeout=timeout_s,
                follow_redirects=False,
                verify=_verify,
            )
        except Exception as exc:
            # REVIEW-V6: surface network errors distinctly so the caller
            # can mark the host unreachable and skip remaining paths.
            log.info("auth_refresher: %s -> network error: %s",
                     candidate["url"], exc)
            return {"_outcome": "network_error", "error": str(exc)}
        if 200 <= resp.status_code < 300:
            try:
                parsed = resp.json()
            except Exception:
                parsed = {"_raw_text": resp.text}
            # REVIEW-V3: httpx returns Set-Cookie as a list via get_list.
            # `dict(resp.headers)` collapses multi-value headers into a
            # single comma-joined string — splitting on `,` then breaks
            # because cookie attributes themselves contain commas
            # (e.g. `Expires=Wed, 09 Jun 2027 ...`). Use get_list to
            # preserve each Set-Cookie header as a separate string.
            try:
                set_cookie_list = resp.headers.get_list("set-cookie") or []
            except AttributeError:
                # httpx < 0.x compatibility — fall back to the legacy collapse
                raw = resp.headers.get("set-cookie", "")
                set_cookie_list = [s.strip() for s in raw.split("\n") if s.strip()] if raw else []
            return {"_outcome": "ok",
                    "status": resp.status_code, "body": parsed,
                    "set_cookie_list": set_cookie_list,
                    "headers": dict(resp.headers)}
        log.info("auth_refresher: %s body=%s -> %d",
                 candidate["url"], list(body.keys()) if isinstance(body, dict) else type(body).__name__,
                 resp.status_code)
    return None


def _try_google_oauth_direct(
    refresh_token: str,
    project_auth: dict | None,
    diags: list[str],
    *,
    timeout_s: float = 8.0,
) -> dict[str, Any] | None:
    """Strategy B — exchange the Google refresh token for a fresh ID token.

    Requires OAuth client_id + client_secret. Looks for them in (in order):
      1. project.environments.<env>.auth.oauth.google.client_id / client_secret
      2. env vars GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET

    Returns Google's response body on 2xx (containing access_token,
    id_token, optionally a rotated refresh_token), or None on any failure.

    Documented: ARTA NEVER stores client_secret in projects.json (operator
    handles that via env var injection at runtime). The project-config
    path here is a deliberate escape hatch for self-hosted Google OAuth
    setups where the secret is non-sensitive in the operator's deployment.
    """
    oauth_cfg = ((project_auth or {}).get("oauth") or {}).get("google") or {}
    client_id = oauth_cfg.get("client_id") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = oauth_cfg.get("client_secret") or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        diags.append(
            "Strategy B (Google direct) skipped: client_id/client_secret not "
            "in project config or env vars (GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET)."
        )
        return None

    body = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    try:
        resp = httpx.post(_GOOGLE_TOKEN_URL, data=body, timeout=timeout_s)
    except Exception as exc:
        diags.append(f"Strategy B (Google direct) network error: {exc}")
        return None
    if 200 <= resp.status_code < 300:
        try:
            return resp.json()
        except Exception:
            diags.append("Strategy B (Google direct) 2xx but body wasn't JSON.")
            return None
    try:
        err_body = resp.json()
        err_msg = f"{err_body.get('error')}: {err_body.get('error_description','')}"
    except Exception:
        err_msg = resp.text[:200]
    diags.append(f"Strategy B (Google direct) {resp.status_code}: {err_msg}")
    return None


def _substitute(template: Any, vars: dict[str, str]) -> Any:
    """Recursively substitute `{var}` placeholders in a dict/list/str template."""
    if isinstance(template, str):
        out = template
        for k, v in vars.items():
            out = out.replace("{" + k + "}", v)
        return out
    if isinstance(template, dict):
        return {k: _substitute(v, vars) for k, v in template.items()}
    if isinstance(template, list):
        return [_substitute(x, vars) for x in template]
    return template


# ── R162 — auto-derive refresh config from captured SUT traffic ──────────────
# The SaaS SPA keeps itself logged in by calling its OWN refresh endpoint
# {schema_id}/{user_id}?refresh_token=… → {"token": <fresh JWT>}). That call
# is captured during discovery and recorded in discovered_endpoints. Rather
# than hand-wire `auth.refresh` per project (violates "discover from the SUT,
# don't hand-wire"), R162 derives it: find the captured token-returning
# refresh route, pull one CONCRETE example from its HAR, and VALUE-template
# the path + query against the expired cookie's own JWT claims so it re-fills
# for the current session. SUT-agnostic: any SPA whose refresh route returns
# a token and is captured gets autonomous refresh.

_REFRESH_TOKEN_KEYS = ("token", "access_token", "id_token", "session_token", "jwt", "idToken")


def _looks_like_refresh_endpoint(ep: dict) -> int:
    """Score a captured endpoint as a refresh candidate (0 = not, higher = better)."""
    path = str(ep.get("path") or ep.get("url") or "").lower()
    if not path:
        return 0
    shape = ep.get("response_body_shape") or {}
    props = (shape.get("properties") or {}) if isinstance(shape, dict) else {}
    returns_token = any(k in props for k in _REFRESH_TOKEN_KEYS)
    score = 0
    if "refresh" in path:
        score += 3
    if returns_token:
        score += 2
    if any(s in path for s in ("/token", "/oauth", "/auth/")):
        score += 1
    # Must plausibly return a token to be usable as a refresh source.
    return score if (returns_token or "refresh" in path) else 0


def _har_find_concrete_url(har_path: Path, path_template: str,
                           *, max_bytes: int = 350_000_000) -> tuple[str, str] | None:
    """Chunked regex scan of a (possibly huge) HAR for a concrete request URL
    matching ``path_template`` (e.g. ``/refresh/{a}/google/{b}``). Avoids a
    full JSON parse. Returns (method, full_url) or None.

    Literal path segments anchor the match; ``{param}`` segments become
    ``[^/]+``. We capture the URL up to the closing quote (host + path +
    query). Method is read from the nearby ``"method"`` field when present.
    """
    import re
    segs = [s for s in str(path_template).split("/") if s != ""]
    parts = []
    for s in segs:
        if s.startswith("{") and s.endswith("}"):
            parts.append(r"[^/\"]+")
        else:
            parts.append(re.escape(s))
    anchor = r"/" + r"/".join(parts)
    url_re = re.compile(r'"(https?://[^"]*?' + anchor + r'[^"]*)"')
    method_re = re.compile(r'"method"\s*:\s*"([A-Z]+)"')
    try:
        size = har_path.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        log.warning("R162: HAR %s is %d bytes (> %d cap) — skipping scan",
                    har_path.name, size, max_bytes)
        return None
    chunk = 4 * 1024 * 1024
    tail = ""
    last_method = "GET"
    with open(har_path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            window = tail + buf
            for mm in method_re.finditer(window):
                last_method = mm.group(1)
            m = url_re.search(window)
            if m:
                return (last_method, m.group(1))
            tail = window[-4096:]  # keep overlap so a split URL still matches
    return None


def _har_scan_refresh_url(har_path: Path, *, max_bytes: int = 350_000_000) -> tuple[str, str] | None:
    """R162.B — chunk-scan a HAR for a refresh-shaped request URL whose path
    carries a ``refresh`` / ``oauth/token`` / ``auth/refresh`` segment. Returns
    (method, full_url) or None.

    Generic fallback for SUTs whose refresh call is captured in the HAR but is
    ABSENT from the cleaned ``discovered_endpoints`` file — the refresh route
    typically lives on the AUTH host (e.g. ``auth.example.internal/refresh/…``)
    while the endpoints file is the API-surface (backend host), so R206/R221
    surface-cleaning drops it. Without this fallback R162's endpoint-file scan
    returns None and auto-refresh silently fails even though the working route
    is right there in the traffic.
    """
    import re
    # HAR JSON commonly escapes forward slashes as `\/` (RFC 8259 permits it).
    # Normalize per-window before matching so `https:\/\/host\/refresh\/…`
    # is found (a raw regex misses it — the reason auto-refresh silently
    # failed while the working route sat in the traffic).
    #
    # `(?!token/)` skips the backend Google-token shape `/refresh/token/…`
    # (returns a Google access_token, not a fresh session cookie) in favour of
    # the auth-host session-refresh shape `/refresh/<id>/<provider>/…`. Also
    # accept oauth/token + auth/refresh as generic alternates.
    url_re = re.compile(
        r'"(https?://[^"]*?/(?:refresh/(?!token/)[^"]*|oauth/token|auth/refresh)[^"]*)"',
        re.I)
    method_re = re.compile(r'"method"\s*:\s*"([A-Z]+)"')
    try:
        if har_path.stat().st_size > max_bytes:
            log.warning("R162.B: HAR %s over size cap — skipping refresh scan", har_path.name)
            return None
    except OSError:
        return None
    chunk = 4 * 1024 * 1024
    tail = ""
    last_method = "GET"
    with open(har_path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            window = (tail + buf).replace("\\/", "/")  # normalize escaped slashes
            for mm in method_re.finditer(window):
                last_method = mm.group(1)
            m = url_re.search(window)
            if m:
                return (last_method, m.group(1))
            tail = window[-4096:]  # overlap so a split URL still matches
    return None


def _template_url_against_claims(url: str, claims: dict[str, Any],
                                 refresh_token: str | None) -> dict[str, Any]:
    """Replace concrete path segments + query values that equal a JWT-claim
    value (or the refresh token) with ``{claim_name}`` / ``{refresh_token}``
    placeholders, so the existing ``subst`` machinery re-fills it for the
    current session. Returns an ``auth.refresh``-shaped config dict.
    """
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, quote

    # value -> placeholder name (longest values first to avoid partial hits).
    val2name: dict[str, str] = {}
    for k, v in (claims or {}).items():
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            sv = str(v)
            if len(sv) >= 3:
                val2name[sv] = k
    if refresh_token:
        val2name[refresh_token] = "refresh_token"

    sp = urlsplit(url)
    # path
    new_segs = []
    for seg in sp.path.split("/"):
        from urllib.parse import unquote
        dec = unquote(seg)
        new_segs.append("{" + val2name[dec] + "}" if dec in val2name else seg)
    new_path = "/".join(new_segs)
    # query
    qpairs = parse_qsl(sp.query, keep_blank_values=True)
    query_tmpl: dict[str, str] = {}
    _RT_NAMES = {"refresh_token", "refreshtoken", "refresh-token", "rt"}
    for name, val in qpairs:
        if val in val2name:
            query_tmpl[name] = "{" + val2name[val] + "}"
        elif name.lower() in _RT_NAMES:
            # The captured HAR's refresh token is from a DIFFERENT (prior)
            # session, so it won't value-match the current one — but it's
            # positionally the refresh-token param. Template it by NAME so it
            # re-fills for the current session.
            query_tmpl[name] = "{refresh_token}"
        else:
            query_tmpl[name] = val
    endpoint = urlunsplit((sp.scheme, sp.netloc, new_path, "", ""))
    return {
        "endpoint": endpoint,
        "method": "GET",
        "query": query_tmpl,
        "access_token_paths": ["$.token", "$.access_token", "$.id_token", "$.session_token"],
        "_derived": "R162",
    }


def derive_refresh_config_from_traffic(
    project_id: str | None,
    claims: dict[str, Any],
    refresh_token: str | None,
    *,
    captured_dir: Path | None = None,
) -> dict[str, Any] | None:
    """R162 KEYSTONE — build an ``auth.refresh`` config from captured traffic.

    Scans ``discovered_endpoints/<pid>.json`` for the best token-returning
    refresh route, finds a concrete example URL in its source HAR, and
    value-templates it against ``claims`` + ``refresh_token``. Returns the
    config (ready to drop under ``env_block.auth.refresh``) or None.
    """
    if not project_id:
        return None
    cap_dir = captured_dir or Path(
        os.environ.get("ARTA_CAPTURED_ENDPOINTS_DIR", ".arta/discovered_endpoints"))
    cap_file = cap_dir / f"{project_id}.json"
    if not cap_file.exists():
        return None
    try:
        data = json.loads(cap_file.read_text())
    except (OSError, ValueError):
        return None
    eps = data if isinstance(data, list) else (data.get("endpoints") or data.get("items") or [])
    scored = sorted(
        ((_looks_like_refresh_endpoint(e), e) for e in eps if isinstance(e, dict)),
        key=lambda t: t[0], reverse=True,
    )
    for score, ep in scored:
        if score <= 0:
            break
        path_tmpl = str(ep.get("path") or ep.get("url") or "")
        if not path_tmpl:
            continue
        # Prefer a stored concrete example; else scan the source HAR.
        concrete = ep.get("raw_url") or ep.get("example_url")
        method = (ep.get("method") or "GET").upper()
        if not concrete:
            har = ep.get("source_har")
            if har and Path(har).exists():
                found = _har_find_concrete_url(Path(har), path_tmpl)
                if found:
                    method, concrete = found
        if not concrete:
            continue
        cfg = _template_url_against_claims(concrete, claims, refresh_token)
        cfg["method"] = method
        # Only useful if templating bound at least one identifier or the
        # refresh_token (otherwise it's a static URL that won't re-fill).
        templated_any = ("{" in cfg["endpoint"]) or any(
            "{" in str(v) for v in (cfg.get("query") or {}).values())
        if templated_any:
            log.info("R162: derived refresh config from captured traffic: %s %s",
                     cfg["method"], cfg["endpoint"])
            return cfg

    # R162.B — the cleaned endpoints file had no refresh route (it lives on the
    # auth host, which API-surface cleaning drops). Fall back to scanning the
    # discovery HAR directly for the refresh-shaped URL. The HAR path is taken
    # from any captured endpoint's `source_har` (project-scoped — never guess
    # cross-project). This closes the gap where auto-refresh silently failed
    # while the working route sat in the captured traffic.
    _har_path = None
    for ep in eps:
        if isinstance(ep, dict):
            _sh = ep.get("source_har")
            if _sh and Path(_sh).exists():
                _har_path = Path(_sh)
                break
    if _har_path is not None:
        found = _har_scan_refresh_url(_har_path)
        if found:
            method, concrete = found
            cfg = _template_url_against_claims(concrete, claims, refresh_token)
            cfg["method"] = method
            templated_any = ("{" in cfg["endpoint"]) or any(
                "{" in str(v) for v in (cfg.get("query") or {}).values())
            if templated_any:
                log.info("R162.B: derived refresh config from HAR scan: %s %s",
                         cfg["method"], cfg["endpoint"])
                return cfg
    return None


# ── Storage-state writer ───────────────────────────────────────────────────


def _update_storage_state(
    storage_path: Path,
    storage: dict,
    *,
    cookie_name: str | None,
    new_cookie_value: str | None,
    set_cookie_headers: list[str],
    cookie_domain: str | None,
    new_refresh_token: str | None = None,
) -> Path:
    """Write a fresh storage-state with the new cookie value(s).

    Backup the previous file as `<file>.pre-refresh.bak`.
    Replace any existing cookie with name `cookie_name` (the SaaS session
    cookie). If `set_cookie_headers` are present, parse them too and merge.
    """
    backup = storage_path.with_suffix(storage_path.suffix + ".pre-refresh.bak")
    try:
        shutil.copy2(storage_path, backup)
    except Exception as exc:
        log.warning("auth_refresher: backup failed for %s: %s — proceeding",
                    storage_path, exc)

    # REVIEW-V5: derive cookie expiry from the new JWT's `exp` claim when
    # the new value is itself a JWT (the common case for SaaS session
    # cookies). Pre-fix used `now + 24h` regardless — if the real JWT
    # only lasts 1h we'd report it valid for 23 extra hours, suppressing
    # refresh attempts during that window.
    new_expiry = time.time() + 24 * 3600  # default fallback
    if new_cookie_value:
        new_payload = _decode_jwt_payload(new_cookie_value)
        if new_payload and isinstance(new_payload.get("exp"), (int, float)):
            new_expiry = float(new_payload["exp"])

    cookies = list(storage.get("cookies") or [])
    if cookie_name and new_cookie_value:
        # Replace the existing entry or append.
        replaced = False
        for c in cookies:
            if c.get("name") == cookie_name:
                c["value"] = new_cookie_value
                c["expires"] = new_expiry
                # R144.B.3 — also update cookie_domain on the REPLACE
                # path. Pre-R144.B.3: only value+expires were updated,
                # paste replaced an existing cookie entry. Result: the
                # SPA-side cross-subdomain cookie fix that R84 was
                # supposed to deliver never reached disk. Verified live
                # 2026-05-29: post-paste storage state still showed
                # Only update when the passed `cookie_domain` carries a
                # widened (leading-dot) value AND the existing entry
                # doesn't already match — idempotent + conservative.
                if (cookie_domain
                        and cookie_domain != c.get("domain")
                        and (cookie_domain.startswith(".")
                             or not (c.get("domain") or "").startswith("."))):
                    c["domain"] = cookie_domain
                replaced = True
                break
        if not replaced:
            cookies.append({
                "name": cookie_name,
                "value": new_cookie_value,
                "domain": cookie_domain or "",
                "path": "/",
                "expires": new_expiry,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            })

    # Set-Cookie header parsing — pull each cookie name=value pair and
    # merge in. Crude (no domain/path/expiry parsing) but sufficient for
    # SaaS apps that re-issue the session cookie on /auth/refresh.
    for raw in set_cookie_headers or []:
        if not raw:
            continue
        first_segment = raw.split(";", 1)[0].strip()
        if "=" not in first_segment:
            continue
        name, value = first_segment.split("=", 1)
        # Replace existing
        replaced = False
        for c in cookies:
            if c.get("name") == name:
                c["value"] = value
                replaced = True
                break
        if not replaced:
            cookies.append({
                "name": name,
                "value": value,
                "domain": cookie_domain or "",
                "path": "/", "expires": time.time() + 24 * 3600,
                "httpOnly": True, "secure": True, "sameSite": "Lax",
            })

    # R261 — normalize every cookie's `sameSite` to a Playwright-valid enum.
    # Browser-exported / pasted cookies routinely carry `sameSite: null` (unset),
    # but Playwright's `request.newContext({storageState})` HARD-REJECTS anything
    # other than "Strict"/"Lax"/"None" ("storageState.cookies[0].sameSite:
    # expected one of (Strict|Lax|None)") → every PW API-context spec FAILS at
    # setup. A secure cross-site auth cookie is semantically `None`; otherwise
    # default to `Lax`. Idempotent + covers ALL cookies, not just the refreshed
    # one (the stale null could be on any entry).
    for _c in cookies:
        _ss = _c.get("sameSite")
        if _ss not in ("Strict", "Lax", "None"):
            _c["sameSite"] = "None" if _c.get("secure") else "Lax"

    storage["cookies"] = cookies

    # JSON.parse()s the value before jwt-decoding it for `root_account_id`
    # refresher rewrote ONLY the cookie, so after a refresh the SPA read the
    # URL built with `null` path params (`/null/api/collection/null/...`) ->
    # backend ERR_FAILED -> the axios interceptor redirected to /login. Result:
    # EVERY authenticated PW spec auth_stale-skipped even though the cookie was
    # fresh and the replay mechanism worked. The value MUST be JSON-encoded
    # (quoted) to match how the SPA persisted it — a raw unquoted JWT makes the
    # SPA's JSON.parse throw, yielding the same null-accountId failure.
    # SPA stays on /organizations (AUTHENTICATED) and the backend call returns
    # 200 with the real account_id. Killswitch: ARTA_R182_LS_SYNC_DISABLE=1.
    if new_cookie_value and os.environ.get("ARTA_R182_LS_SYNC_DISABLE") != "1":
        _session_cookie_keys = ("session-token", "session_token", "sessionToken")
        _rt_keys = ("refresh-token", "refresh_token", "refreshToken")
        _synced = 0
        for origin in storage.get("origins") or []:
            ls_list = origin.get("localStorage")
            if not isinstance(ls_list, list):
                continue
            for kv in ls_list:
                nm = kv.get("name") or ""
                if nm in _session_cookie_keys:
                    kv["value"] = json.dumps(new_cookie_value)
                    _synced += 1
                elif new_refresh_token and nm in _rt_keys:
                    kv["value"] = json.dumps(new_refresh_token)
        if _synced:
            log.info("auth_refresher R182: synced fresh session cookie into %d "
                     "localStorage origin(s) (JSON-quoted for SPA JSON.parse)",
                     _synced)

    # R240.B — refresh CLIENT-SIDE session-timing localStorage keys.
    # CLIENT-SIDE session-expiry check reading localStorage timing keys
    # (issue-side: LoginTime/_lastUpdatedTime/.issued; expiry-side:
    # _expiry/.expires). A token-only refresh leaves these STALE → the SPA
    # renders the login page even with a fresh token: the discovery probe
    # then catalogs only login chrome, and authed PW specs auth_stale-skip.
    # Refresh them, PRESERVING each SUT's original session window
    # (expiry - issue) so we never guess the TTL; fall back to the token's
    # own `exp` when the original window is unparseable. Conservative
    # exact-name match (case-insensitive) → SUTs without these keys (e.g.
    # try/except-guarded so a parse quirk can never break the refresh write.
    # Killswitch: ARTA_R240B_SESSION_TIMING_DISABLE=1.
    if os.environ.get("ARTA_R240B_SESSION_TIMING_DISABLE") != "1":
        try:
            from email.utils import formatdate, parsedate_to_datetime
            _now_s = time.time()
            _now_ms = int(_now_s * 1000)
            _issue_ms_keys = {"logintime", "_lastupdatedtime", "lastactivity",
                              "issuedat", "logints"}
            _expiry_ms_keys = {"_expiry", "sessionexpiry", "expiresat", "expiryts"}
            _issue_rfc_keys = {".issued"}
            _expiry_rfc_keys = {".expires"}
            _tok_ttl_s = None
            if new_cookie_value:
                _p = _decode_jwt_payload(new_cookie_value)
                if _p and isinstance(_p.get("exp"), (int, float)):
                    _tok_ttl_s = max(60.0, float(_p["exp"]) - _now_s)
            _r240b_total = 0
            for origin in storage.get("origins") or []:
                ls_list = origin.get("localStorage")
                if not isinstance(ls_list, list):
                    continue
                _by = {(kv.get("name") or "").lower(): kv for kv in ls_list}

                def _as_int(v):
                    s = str(v or "").strip()
                    return int(s) if s.lstrip("-").isdigit() else None

                # epoch-ms family: window = max(expiry) - max(issue)
                _iss_ms = [x for k in _issue_ms_keys if k in _by
                           for x in [_as_int(_by[k].get("value"))] if x is not None]
                _exp_ms = [x for k in _expiry_ms_keys if k in _by
                           for x in [_as_int(_by[k].get("value"))] if x is not None]
                _win_ms = (max(_exp_ms) - max(_iss_ms)) if (_iss_ms and _exp_ms) else None
                if (_win_ms is None or _win_ms <= 0) and _tok_ttl_s:
                    _win_ms = int(_tok_ttl_s * 1000)
                # rfc-date family: window = .expires - .issued
                _win_rfc_s = None
                try:
                    if ".issued" in _by and ".expires" in _by:
                        _di = parsedate_to_datetime(_by[".issued"]["value"]).timestamp()
                        _de = parsedate_to_datetime(_by[".expires"]["value"]).timestamp()
                        if _de > _di:
                            _win_rfc_s = _de - _di
                except Exception:
                    _win_rfc_s = None
                if _win_rfc_s is None and _tok_ttl_s:
                    _win_rfc_s = _tok_ttl_s

                for kv in ls_list:
                    low = (kv.get("name") or "").lower()
                    if low in _issue_ms_keys:
                        kv["value"] = str(_now_ms); _r240b_total += 1
                    elif low in _expiry_ms_keys and _win_ms:
                        kv["value"] = str(_now_ms + int(_win_ms)); _r240b_total += 1
                    elif low in _issue_rfc_keys:
                        kv["value"] = formatdate(_now_s, usegmt=True); _r240b_total += 1
                    elif low in _expiry_rfc_keys and _win_rfc_s:
                        kv["value"] = formatdate(_now_s + _win_rfc_s, usegmt=True)
                        _r240b_total += 1
            if _r240b_total:
                log.info("auth_refresher R240.B: refreshed %d client-side "
                         "session-timing key(s) (window-preserved) so the SPA "
                         "session-expiry check passes with the fresh token",
                         _r240b_total)
        except Exception as exc:
            log.warning("auth_refresher R240.B: session-timing refresh "
                        "skipped (non-fatal): %s", exc)

    # Atomic write via temp file + rename
    tmp = storage_path.with_suffix(storage_path.suffix + ".tmp")
    tmp.write_text(json.dumps(storage, indent=2))
    tmp.replace(storage_path)
    log.info("auth_refresher: wrote refreshed storage-state to %s "
             "(backup: %s)", storage_path, backup.name)
    return storage_path


# ── Main entrypoint ────────────────────────────────────────────────────────


def _select_env_block(project: dict, environment: str | None) -> tuple[str | None, dict]:
    """REVIEW-V1: pick the env block that matches the run's `environment`.
    Resolution: exact match → suffix match → first block.
    """
    envs = project.get("environments") or {}
    if not isinstance(envs, dict) or not envs:
        return None, {}
    if environment and environment in envs:
        return environment, envs[environment] or {}
    if environment:
        for name, block in envs.items():
            if environment.endswith(name) or name.endswith(environment):
                return name, block or {}
    for name, block in envs.items():
        if isinstance(block, dict):
            return name, block
    return None, {}


def refresh_if_expired(
    project: dict | None,
    *,
    environment: str | None = None,
    min_remaining_s: int = 0,
) -> RefreshResult:
    """K1's pre-discovery hook. Returns a result describing what happened.

    `environment` is the run-level env name. When provided (REVIEW-V1),
    picks the matching env block; when None, falls back to first block.

    Steps:
      1. Locate the project's storage-state file (per env).
      2. Inspect its session cookie. If not a JWT or not expired, no-op.
      3. Locate a refresh token (storage state OR project config).
      4. Run candidate refresh endpoints (operator-declared first).
      5. On 2xx: extract new access token + Set-Cookie, atomically
         rewrite the storage-state.
      6. On all failures: surface a structured RefreshResult.
    """
    diags: list[str] = []
    if not isinstance(project, dict):
        return RefreshResult(False, "no project context", diagnostic_lines=diags)

    # REVIEW-V1: pick the matching env block (was: first-seen).
    env_name, env_block = _select_env_block(project, environment)
    auth = env_block.get("auth") or {}
    # R253.PW.3 — when no explicit auth.refresh is declared, synthesize it from
    # the env block's arta_refresh_* variables (the SAME source R156.J.3 uses to
    # drive the in-spec Playwright refresh). Keeps the server-side and in-spec
    # refresh paths on ONE configured endpoint instead of letting the server-side
    # path fall through to the wrong heuristic sweep.
    if not auth.get("refresh"):
        _var_refresh = _refresh_cfg_from_env_variables(env_block)
        if _var_refresh:
            auth = {**auth, "refresh": _var_refresh}
            diags.append(
                "R253.PW.3: synthesized auth.refresh from arta_refresh_* "
                f"variables (endpoint={_var_refresh['endpoint'][:80]})."
            )
    cookie_name = ((auth.get("credentials") or {}).get("cookie_name")) or None
    api_base = env_block.get("api_base_url")
    base = env_block.get("base_url")

    # 1. Storage state — prefer the matching env's file; fallback to newest.
    storage_path = _find_storage_state_path(env_name)
    if not storage_path:
        return RefreshResult(False, "no storage-state file found",
                              diagnostic_lines=diags)
    storage = _read_storage_state(storage_path)
    if not storage:
        return RefreshResult(False, f"storage-state at {storage_path.name} unparseable",
                              diagnostic_lines=diags)

    # 2. Find session cookie + check expiry.
    # R231.A — a refresh appends a fresh session cookie without evicting the prior
    # one, so the storage can hold a stale + a fresh copy under the same name. The
    # old "first match wins" loop could pick the STALE duplicate → think the session
    # is expired → hit the SUT refresh endpoint UNNECESSARILY (which then 500s while
    session_cookie_value = None
    cookie_domain = None
    def _cookie_exp(c):
        v = c.get("value") or ""
        if v.count(".") != 2:
            return -1.0
        try:
            seg = v.split(".")[1]; seg += "=" * (-len(seg) % 4)
            return float(json.loads(base64.urlsafe_b64decode(seg)).get("exp") or 0)
        except Exception:
            return 0.0
    if cookie_name:
        matches = [c for c in (storage.get("cookies") or []) if c.get("name") == cookie_name]
    else:
        # k0r_refresh_token outlives k0r-access-token, so `max(exp)` below wrongly
        # picked it → 403 (valid-but-forbidden) on every /v1/* call (356 Newman
        # fails). Exclude *refresh* names; prefer *access* names. SUT-agnostic;
        # killswitch ARTA_R334_REFRESH_EXCLUDE_DISABLE=1.
        _cands = [c for c in (storage.get("cookies") or [])
                  if "token" in (c.get("name") or "").lower()]
        if os.environ.get("ARTA_R334_REFRESH_EXCLUDE_DISABLE") != "1":
            _non_refresh = [c for c in _cands
                            if "refresh" not in (c.get("name") or "").lower()]
            _access = [c for c in _non_refresh
                       if "access" in (c.get("name") or "").lower()]
            _cands = _access or _non_refresh or _cands
        matches = _cands
    if matches:
        best = max(matches, key=_cookie_exp)
        session_cookie_value = best.get("value")
        cookie_domain = best.get("domain")
        if not cookie_name:
            cookie_name = best.get("name")

    if not session_cookie_value:
        return RefreshResult(False, "no session cookie found in storage state",
                              diagnostic_lines=diags)

    # `min_remaining_s` lets a long run refresh PROACTIVELY: treat the cookie
    # as refresh-due when it has less than that many seconds of life left, so
    # a run that would otherwise cross the TTL mid-flight starts fresh.
    if not _is_session_expired(session_cookie_value, leeway_s=max(30, min_remaining_s)):
        return RefreshResult(False, "session cookie not expired (or not a JWT)",
                              diagnostic_lines=diags)

    # Distinguish which token is stale (the inner id_token is the usual culprit for
    # the analytics 400 'Cant Connect to Authourization Server').
    _wrap = _is_jwt_expired(session_cookie_value, leeway_s=max(30, min_remaining_s))
    diags.append(f"Session cookie '{cookie_name}' is refresh-due "
                 f"({'wrapper' if _wrap else 'nested third_party_token (id_token)'} expired).")

    # 3. Refresh token (M3 — also from the durable env `variables`, so the api_key
    # survives a storage-state wipe).
    refresh_token = _extract_refresh_token(storage, auth, variables=env_block.get("variables"))
    if not refresh_token:
        diags.append("No refresh token found in storage state OR project auth config.")
        return RefreshResult(False, "no refresh token available — operator must re-login",
                              diagnostic_lines=diags)

    provider = _detect_provider(refresh_token)
    diags.append(f"Refresh token detected (provider={provider}).")

    # Build the placeholder map from the EXPIRED cookie's own JWT claims so a
    # path/query-templated refresh endpoint resolves fully. The dead token
    # subscription_id / provider / schema_id / user_id). Scalar claims only.
    claims = _decode_jwt_payload(session_cookie_value) or {}
    subst: dict[str, str] = {
        str(k): str(v) for k, v in claims.items()
        if isinstance(v, (str, int, float)) and not isinstance(v, bool)
    }
    subst["refresh_token"] = refresh_token
    subst.setdefault("provider", provider)
    if api_base:
        subst["api_base_url"] = api_base.rstrip("/")
    if base:
        subst["base_url"] = base.rstrip("/")
    # AUTH-CYCLE — some refresh-endpoint placeholders are CONFIG values, not token
    # `{api_base_url}/refresh/{subscription_id}/{provider}/{collections_schema}/{user_id}`).
    # Merge the env block's resolved variables + relevant env so the template fills.
    _ebvars = env_block.get("variables") or {}
    _ebitems = (_ebvars.items() if isinstance(_ebvars, dict)
                else ((d.get("name"), d.get("value")) for d in _ebvars if isinstance(d, dict)))
    for _vk, _vv in _ebitems:
        if _vk and isinstance(_vv, str) and _vv and _vv != "REPLACE_ME" and str(_vk) not in subst:
            subst[str(_vk)] = _vv
    for _envk, _sk in (("ARTA_AN_COLLECTIONS_SCHEMA", "collections_schema"),
                       ("ARTA_AN_OAUTH_SERVER_URL", "oauth_server_url")):
        if os.environ.get(_envk):
            subst[_sk] = os.environ[_envk]

    # 4. Try refresh endpoints
    candidates = _candidate_refresh_endpoints(api_base, base, auth, subst=subst)

    # R162 — ALWAYS derive a refresh endpoint from the SUT's OWN captured
    # traffic (the route the SPA actually uses to stay logged in) and try it IN
    # ADDITION to any configured/source-grepped one. Captured traffic is ground
    # truth: it beats R157's source-grep guess, which can target the wrong
    # → a Google token on the backend host; the SPA actually calls
    # Value-templated against the expired cookie's claims so it re-fills.
    try:
        derived = derive_refresh_config_from_traffic(
            project.get("id"), claims, refresh_token)
    except Exception as _r162_exc:  # never block refresh on derivation
        derived = None
        diags.append(f"R162 derive errored: {_r162_exc}")
    if derived:
        derived_candidate = {
            "url": _substitute(derived["endpoint"], subst),
            "method": (derived.get("method") or "GET").upper(),
            "body": None,
            "query": _substitute(derived.get("query") or {}, subst),
            "headers": {},
            "access_token_paths": derived.get("access_token_paths")
                or ["$.token", "$.access_token", "$.id_token", "$.session_token"],
            "cookie_header": "set-cookie",
            "explicit": True,
            "_r162": True,
        }
        if derived_candidate["url"] not in {c.get("url") for c in candidates}:
            # Prefer the captured-traffic endpoint: try it FIRST.
            candidates.insert(0, derived_candidate)
            diags.append(
                f"R162: added captured-traffic refresh endpoint "
                f"{derived_candidate['method']} {derived_candidate['url']} (tried first).")

    if not candidates:
        return RefreshResult(False, "no refresh endpoints to try (project config lacks api_base_url)",
                              diagnostic_lines=diags)

    log.info("auth_refresher: trying %d refresh endpoint(s) for %s",
             len(candidates), storage_path.name)
    # REVIEW-V6: short-circuit when a host is unreachable. Pre-fix, an
    # unreachable host (e.g. residential IP rotation, SUT TLS outage)
    # burned 8s × N-paths × 2-body-shapes ≈ 80s on every K1 run. Track
    # consecutive network errors per host; after the second connection
    # failure, skip remaining candidates on that host.
    from urllib.parse import urlparse as _urlparse
    successful = None
    host_unreachable: set[str] = set()
    for c in candidates:
        host = _urlparse(c["url"]).netloc
        if host in host_unreachable:
            log.info("auth_refresher: %s skipped (host marked unreachable)",
                     c["url"])
            continue
        # An EXPLICIT endpoint (operator-declared / R162 captured-traffic) is a
        # known-good target — worth waiting for. The 4s default exists to bound
        # the HEURISTIC sweep (10 paths × 2 bodies), but a real SUT refresh route
        # "host unreachable (TLS/connect failure)", silently killing auto-refresh.
        # Give explicit endpoints a generous timeout; keep heuristic guesses fast.
        _to = 20.0 if c.get("explicit") else 4.0
        result = _try_refresh_endpoint(c, refresh_token, timeout_s=_to)
        if result is not None and result.get("_outcome") == "ok":
            successful = (c, result)
            break
        if result is not None and result.get("_outcome") == "network_error":
            # Two consecutive network errors on the same host = mark down.
            # First error already happened (this candidate); count it.
            host_unreachable.add(host)
            diags.append(f"Host {host} unreachable (TLS/connect failure); "
                          f"skipping remaining paths on it.")

    # Strategy B — when Strategy A failed AND token is Google-format,
    # try Google's token endpoint directly. Requires OAuth client_id +
    # client_secret in project config or environment (e.g.
    # `auth.oauth.google.client_id` + `auth.oauth.google.client_secret`,
    # or env vars `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`).
    # Without those, we cannot impersonate the SaaS app's OAuth client —
    # by design (Google's API rejects unknown clients).
    if not successful and provider == "google":
        google_result = _try_google_oauth_direct(refresh_token, auth, diags)
        if google_result:
            new_id_token = google_result.get("id_token")
            if new_id_token:
                new_storage_path = _update_storage_state(
                    storage_path, storage,
                    cookie_name=cookie_name,
                    new_cookie_value=new_id_token,
                    set_cookie_headers=[],
                    cookie_domain=cookie_domain,
                )
                # Persist the new refresh token if Google rotated it
                new_rt = google_result.get("refresh_token")
                if new_rt and new_rt != refresh_token:
                    diags.append("Google rotated the refresh token — storage state updated.")
                diags.append(f"Strategy B succeeded: minted fresh ID token via {_GOOGLE_TOKEN_URL}.")
                return RefreshResult(True,
                                     "refreshed via google oauth direct",
                                     new_storage_path=new_storage_path,
                                     diagnostic_lines=diags)

    if not successful:
        operator_action = (
            "Operator action: declare auth.refresh.endpoint in project config "
            "OR run `python tools/refresh_oauth_storage.py <project>` to re-do the OAuth login."
        )
        if provider == "google":
            operator_action = (
                "Operator action (Google OAuth): EITHER (a) add "
                "`auth.oauth.google.client_id` + `auth.oauth.google.client_secret` "
                "to project.environments.<env> so Strategy B can call Google directly, "
                "OR (b) run `python tools/refresh_oauth_storage.py <project>` to "
                "re-do the headed login flow. Option (b) is the path most operators take."
            )
        diags.append(
            f"Tried {len(candidates)} refresh endpoint(s); none returned 2xx. " + operator_action
        )
        return RefreshResult(False, "all refresh endpoints failed",
                              diagnostic_lines=diags)

    candidate, result = successful
    body = result.get("body") or {}
    new_token = None
    for path in candidate.get("access_token_paths") or []:
        v = _read_jsonpath(body, path)
        if isinstance(v, str) and v:
            new_token = v
            break

    # when it looks like a JWT (≥3 dot-delimited segments starting `eyJ`).
    if not new_token:
        raw = (
            body if isinstance(body, str)
            else body.get("_raw_text") if isinstance(body, dict)
            else None
        )
        if isinstance(raw, str):
            raw = raw.strip().strip('"')
            if raw.startswith("eyJ") and raw.count(".") >= 2:
                new_token = raw
                diags.append("Refresh response body parsed as a raw JWT (session token).")

    # REVIEW-V3: use the proper multi-value list assembled in
    # _try_refresh_endpoint — splitting `dict(headers)["set-cookie"]`
    # on commas mangles cookies whose Expires attribute contains a comma.
    set_cookies: list[str] = list(result.get("set_cookie_list") or [])

    if not new_token and not set_cookies:
        diags.append(
            f"Endpoint {candidate['url']} returned 2xx but neither an access "
            f"token at known paths nor a Set-Cookie header was found. "
            f"Body keys: {list(body.keys()) if isinstance(body, dict) else 'non-dict'}.")
        return RefreshResult(False, "refresh response missing token + cookie",
                              diagnostic_lines=diags)

    # R182 — capture a rotated refresh token from the response body so the
    # critical path.
    _new_rt = None
    if isinstance(body, dict):
        for _rk in ("refresh_token", "refreshToken", "refresh-token"):
            _v = body.get(_rk)
            if isinstance(_v, str) and _v:
                _new_rt = _v
                break

    # 5. Update storage state
    new_storage_path = _update_storage_state(
        storage_path, storage,
        cookie_name=cookie_name,
        new_cookie_value=new_token,
        set_cookie_headers=set_cookies,
        cookie_domain=cookie_domain,
        new_refresh_token=_new_rt,
    )
    diags.append(f"Endpoint {candidate['url']} succeeded.")
    return RefreshResult(True,
                         f"refreshed via {candidate['url']}",
                         new_storage_path=new_storage_path,
                         diagnostic_lines=diags)
