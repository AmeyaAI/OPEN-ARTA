"""R216 — REAL analytics backend client for the pytest analytics pillar.

The default `arta_runtime.analytics_client` is `_DefaultAnalyticsClient` (a refusal
STUB whose responses carry `_is_stub_default=True`, which makes the generated
adversarial tests SKIP). So with no real backend wired, the pytest analytics
pillar measures NOTHING about the SUT.

This module provides a REAL client that hits the SUT's analytics query engine, so
the analytics tests actually MEASURE the SUT's analytics quality. Wire it via:

    ARTA_ANALYTICS_BACKEND=src.automation.python_tests.arta_runtime.analytics_backend:client

(`arta_runtime._resolve_from_env()` plugs it at import; ARTA's execution router
injects the env var into the pytest subprocess when a real backend is enabled.)

Design (mission: "execute flawlessly → report SUT quality"):
  • SUT-agnostic discovery — the analytics base URL + IDs + auth are read from the
    SAME storage-state the other tools (k6/newman) use; nothing is hardcoded to one
    SUT beyond fallbacks.
  • `ask(query)` → POST the NL query to `.../query-engine/event/query`; if the SUT
    answers asynchronously (returns a correlation_id), consume the
    `.../response-stream` SSE to completion. Map the REAL response → AnalyticsResponse
    (`_is_stub_default=False`), tolerant of unknown field names; log the raw payload
    once so the mapping can be GROUNDED/refined against the real shape.
  • TRUTHFUL failure — on ANY error (HTTP 4xx/5xx, timeout, malformed, or a SUT
    auth-server degradation like the observed 400 "Cant Connect to Authourization
    Server"), return a REAL (non-stub) AnalyticsResponse so the test FAILS honestly
    (reporting the SUT analytics is broken) — NEVER a stub SKIP.

Killswitch (consumed by the dispatch, not here): absent `ARTA_ANALYTICS_BACKEND`
→ the stub stays (current behavior, truthful SKIP).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from . import AnalyticsResponse, Insight

log = logging.getLogger("arta.analytics.backend")

_REQUEST_TIMEOUT = float(os.environ.get("ARTA_ANALYTICS_REQUEST_TIMEOUT", "60"))
# ★ R218.AM.4 — the EXCEL (and other tabular-LLM) engines do multi-step reasoning that
# routinely exceeds 60s ("Thinking..." for tens of seconds), while mongo returns fast. The
# 60s client read-timeout was firing on the response-stream SSE for excel → ReadTimeout,
# even though the record is structurally IDENTICAL to a working SPA record (same bucket,
# SigV2, key path, status:Success, tokens_usage) and the .xlsx parses cleanly in polars. So
# the ReadTimeout was an ARTA client-timeout defect, NOT a SUT/ingestion problem. Give the
# streamed answer a generous read window (the POST ack returns fast; the CEILING only bounds
# a genuinely-hung SUT). Env-overridable per SUT.
_STREAM_TIMEOUT = float(os.environ.get("ARTA_ANALYTICS_STREAM_TIMEOUT", "300"))
_shape_logged = False  # log the raw SUT response once (grounding aid)

# The SUT session-token cookie name — read from the SAME platform contract the
# other tools use (TARGET_AUTH_COOKIE_NAME, set by discovery/env_block); generic
# default when unset. The session token is the browser-session wrapper JWT the
# SUT's SPA carries as an httpOnly-style cookie.
_SESSION_COOKIE_NAME = os.environ.get("TARGET_AUTH_COOKIE_NAME") or "session-token"

# Auth-chain MATCH prefix for analytics-family paths. These path strings are used
# ONLY as keys for the per-path auth-chain matcher (src.agents.auth_chain
# auth_for_path) — they must align with the deployed chain's `match` entries.
# Override per SUT via ARTA_ANALYTICS_AUTH_PATH_PREFIX.
_AUTH_MATCH_PREFIX = (os.environ.get("ARTA_ANALYTICS_AUTH_PATH_PREFIX")
                      or "/analytics/v1").rstrip("/")


def auth_path(suffix: str) -> str:
    """Build an analytics-family path key for auth-chain matching (NOT a request
    URL). Centralized so the whole runtime matches one configurable prefix."""
    return f"{_AUTH_MATCH_PREFIX}/{str(suffix).lstrip('/')}"


# ── auth + endpoint discovery (reuse the validated session machinery) ─────────

def _load_storage() -> dict | None:
    """Read the most-recent storage-state (same source k6/newman/PW use)."""
    try:
        from src.agents.auth_refresher import _find_storage_state_path, _read_storage_state
        sp = _find_storage_state_path(None)
        return _read_storage_state(sp) if sp else None
    except Exception as exc:  # pragma: no cover - import/env guard
        log.warning("R216: storage-state load failed: %s", exc)
        return None


def _jwt_claims(tok: str | None) -> dict:
    """Decode a JWT payload (no verify) → claims dict; {} on failure."""
    import base64
    if not tok or tok.count(".") != 2:
        return {}
    try:
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}


def _engine_type(claims: dict) -> str | None:
    """The agent token's analytics resource binding. SPA-minted analytics tokens
    carry `resource_type.engine_type == "analytics_tool"`; some SUTs put it at the
    top level. Returns the value (possibly "") or None when the claim is absent."""
    rt = claims.get("resource_type") if isinstance(claims, dict) else None
    if isinstance(rt, dict) and "engine_type" in rt:
        return rt.get("engine_type")
    if isinstance(claims, dict) and "engine_type" in claims:
        return claims.get("engine_type")
    return None


def _is_explicitly_unbound(claims: dict) -> bool:
    """A7.3 — True ONLY when the token JWT carries an `engine_type` claim that is
    PRESENT but EMPTY (the live signature of the failing token `87663e4c` →
    400 "Cant Connect to Authourization Server"). An ABSENT claim is "unknown",
    not unbound, so minimal/legacy tokens are never falsely rejected."""
    et = _engine_type(claims)
    return et is not None and not str(et).strip()


def _resolve_context(storage: dict | None) -> dict:
    """Resolve {base_url, subscriber_id, subscription_id, tokens, account_match}
    from the session — SUT-agnostically.

    A4 (R218): the `{agent_api_token}` is resolved FRESH and account-matched —
    preferring the session→agent token-exchange result (`TARGET_AUTH_AGENT_TOKEN`,
    set by execution.py's EEE step) over the STALE `agent-user-token` cached in storage
    (which the live 400 traced to: its JWT was for a different account than the
    session). The actual header/cookie assembly is delegated to the discovered
    auth chain (A3) in `ask()` — this function only gathers the raw tokens + ids.
    """
    ctx: dict = {"base_url": None, "subscriber_id": None, "subscription_id": None}
    try:
        from src.agents.auth_chain import harvest_session_ids_from_storage
        ids = harvest_session_ids_from_storage(storage) if storage else {}
    except Exception:
        ids = {}
    ctx["subscriber_id"] = ids.get("subscriber_id")
    ctx["subscription_id"] = ids.get("subscription_id")

    # analytics base url — SUT's own localStorage, then env override (SUT-agnostic)
    base = os.environ.get("TARGET_ANALYTICS_BASE_URL")
    if not base and storage:
        for o in storage.get("origins", []) or []:
            for it in o.get("localStorage", []) or []:
                if (it.get("name") or "").lower() == "analytics_base_url":
                    v = it.get("value") or ""
                    try:
                        v = json.loads(v)
                    except Exception:
                        pass
                    if isinstance(v, str) and v.startswith("http"):
                        base = v
    ctx["base_url"] = (base or "").rstrip("/") or None

    # ── raw tokens + dataset descriptor for the analytics request ────────────
    session_tok = None
    storage_tokenid = None  # agent-api-token.tokenId — the SPA's reused Bearer
    storage_jwt = None
    dataset_ctx: dict = {}
    if storage:
        # refresh appends a new cookie without evicting the prior one. The old loop
        # took the LAST match, so a leftover stale duplicate (expired wrapper) won
        # and tripped a FALSE `session_stale` self-block even right after a good
        # refresh. Pick the FRESHEST (max wrapper `exp`) so duplicates can't block.
        _sess_cands = [c.get("value") for c in (storage.get("cookies") or [])
                       if (c.get("name") or "") == _SESSION_COOKIE_NAME and c.get("value")]
        if _sess_cands:
            session_tok = max(_sess_cands, key=lambda t: float(_jwt_claims(t).get("exp") or 0))
        for o in storage.get("origins", []) or []:
            for it in o.get("localStorage", []) or []:
                nm = it.get("name") or ""
                if nm in ("agent-api-token", "agent-user-token"):
                    try:
                        _j = json.loads(it["value"])
                    except Exception:
                        _j = {}
                    # agent-api-token wins over agent-user-token for analytics.
                    if nm == "agent-api-token":
                        storage_tokenid = _j.get("tokenId") or storage_tokenid
                        storage_jwt = _j.get("token") or storage_jwt
                    else:
                        storage_tokenid = storage_tokenid or _j.get("tokenId")
                        storage_jwt = storage_jwt or _j.get("token")
                elif nm in ("dataset_id", "dataset_name", "dataset_type", "app_id"):
                    _v = it.get("value")
                    try:
                        _v = json.loads(_v)
                    except Exception:
                        pass
                    if isinstance(_v, str):
                        dataset_ctx[nm] = _v
    session_tok = session_tok or os.environ.get("TARGET_AUTH_COOKIE_VALUE")

    # A5/G2 LIVE-GROUNDED (R218) — the analytics query engine accepts
    # `Bearer {agent-api-token.tokenId}` (the SPA's long-lived record id; live 422
    # = past auth). The token JWT is REJECTED (→400 "Cant Connect to Authourization
    # Server"); the freshly-EXCHANGED token also passes but flaps 500/400/timeout on
    # the SUT's token endpoint. So PREFER the storage tokenId (what the SPA reuses);
    # fall back to the exchanged token, then the storage JWT.
    fresh_agent = (os.environ.get("TARGET_AUTH_AGENT_TOKEN")
                   or os.environ.get("agent_token") or os.environ.get("AUTH_TOKEN"))
    # A7.3 (R218 KEYSTONE) — ROOT CAUSE of the analytics 400: a storage
    # `agent-api-token` whose JWT carries `resource_type.engine_type == ""` is an
    # UNBOUND token (minted via the wrong endpoint / placeholder resource ids); the
    # SUT's authz layer can't resolve it → 400 "Cant Connect to Authourization
    # Server". A properly SPA-minted token carries `engine_type == "analytics_tool"`
    # (live 422 = past-auth). So when the storage tokenId is EXPLICITLY unbound,
    # prefer the freshly-MINTED bound token (`TARGET_AUTH_AGENT_TOKEN`, set by the
    # A7.1 EEE `agent_user_token` mint) over the dead-on-arrival storage tokenId.
    # An ABSENT engine_type claim is "unknown", NOT rejected (back-compat).
    _api_unbound = _is_explicitly_unbound(_jwt_claims(storage_jwt))
    if storage_tokenid and not _api_unbound:
        agent_api_token = storage_tokenid
    elif fresh_agent:
        agent_api_token = fresh_agent
    else:
        agent_api_token = storage_tokenid or storage_jwt
    # A7.4 — never SILENTLY send a known-unbound token and mis-report the SUT: when
    # the only Bearer we have is the explicitly-unbound storage token (no minted
    # fallback), flag it so ask() fails TRUTHFULLY instead of emitting a 400 row.
    ctx["auth_unbound"] = (
        _api_unbound and agent_api_token == storage_tokenid and not fresh_agent)

    ctx["tokens"] = {
        k: v for k, v in {
            "session_token": session_tok,
            "cookie_value": session_tok,
            "agent_api_token": agent_api_token,
            "organization_id": ids.get("organization_id"),
        }.items() if v
    }
    ctx["dataset"] = dataset_ctx
    # AUTH-CYCLE (R218) — inputs for the analytics QUERY-token mint. The query
    # login-scoped agent-USER token, NOT the admin agent-api-token (which only
    # authorizes `user-management.*` → the query 400s "Cant Connect to Authourization
    # Server"). ARTA mints the user token the widget's way (create_user_agent_user_token):
    #  • service token  = the storage agent-api-token JWT (server structurally decodes
    #    it for project/account claims — no signature check — so ARTA's own admin token
    #    JWT qualifies);
    #    no-auth path — the server then assigns a fresh user_id);
    _dp = _jwt_claims(session_tok) if session_tok else {}
    ctx["service_token_jwt"] = storage_jwt
    # schema_id is REQUIRED for _mint_query_token (the LOGIN-scoped agent-USER token) —
    # some sessions DROP it (live-confirmed) → mint skipped → the admin token → query-side
    # 400s. Fall back to the MANIFEST collections schema (SUT-agnostic, always available).
    def _manifest_schema():
        try:
            from . import analytics_manifest as _m
            return _m.identifier(_m.load_manifest(), "schema_id", None)
        except Exception:
            return None
    ctx["schema_id"] = (os.environ.get("TARGET_AUTH_SCHEMA_ID")
                        or os.environ.get("ARTA_AN_COLLECTIONS_SCHEMA")
                        or _dp.get("schema_id") or _dp.get("collections_schema")
                        or (_jwt_claims(storage_jwt).get("schema_id"))
                        or _manifest_schema())
    ctx["account_id"] = _dp.get("root_account_id") or _dp.get("account_id")
    ctx["third_party_token"] = _dp.get("third_party_token") or _dp.get("id_token")
    # AUTH-CYCLE (R218) — the analytics QUERY authorizes via the minted LOGIN-scoped
    # ★ R218.AM.2 (LIVE-PROVEN 2026-07-06): a query with an EXPIRED inner id_token
    # (third_party_token) SUCCEEDS (returned "There are 991 records…") — the mint accepts
    # the expired id_token structurally, and the minted token authorizes the query. So the
    # prior guard (which blocked whenever the INNER id_token was stale via
    # `_is_session_expired`) was OVER-CONSERVATIVE — it failed queries that actually work
    # (the chronic false "session id_token EXPIRED" self-block). Gate ONLY on the WRAPPER
    # session token TTL; the inner id_token TTL is irrelevant to the analytics query.
    ctx["session_stale"] = False
    if session_tok and os.environ.get("ARTA_SESSION_STALE_GUARD_DISABLE") != "1":
        try:
            from src.agents.auth_refresher import _is_jwt_expired as _wrap_exp
            ctx["session_stale"] = _wrap_exp(session_tok, leeway_s=30)   # WRAPPER, not inner id_token
        except Exception:
            pass
    # H4 (R218) — tenant consistency. A wrong-tenant agent token makes the
    # analytics call MEASURE THE WRONG TENANT (or 400, as the stale `0aee6bd7`
    # token did) → a corrupt SUT verdict. The Bearer is the opaque tokenId, but the
    # storage agent-api-token JWT (`.token`) carries that record's tenant claims —
    # subscriber/account, flag a mismatch so the test FAILS truthfully instead of
    # silently measuring the wrong tenant.
    # the current session's source of truth (harvest_session_ids may merge the
    # agent claims into ctx, which would compare a token to itself).
    _tok_claims = _jwt_claims(storage_jwt)
    _sess_claims = _jwt_claims(session_tok)
    _sess_sub = _sess_claims.get("subscriber_id")
    _sess_acct = _sess_claims.get("root_account_id") or _sess_claims.get("account_id")
    _mismatch = None
    if _tok_claims and _sess_sub and _tok_claims.get("subscriber_id") not in (None, _sess_sub):
        _mismatch = (f"agent-token subscriber {_tok_claims.get('subscriber_id')!r} "
                     f"!= session {_sess_sub!r}")
    elif _tok_claims and _sess_acct and _tok_claims.get("account_id") not in (None, _sess_acct):
        _mismatch = (f"agent-token account {_tok_claims.get('account_id')!r} "
                     f"!= session {_sess_acct!r}")
    if _mismatch:
        log.warning("R218 H4: TENANT MISMATCH — %s; analytics call would measure the "
                    "WRONG tenant — failing truthfully.", _mismatch)
    ctx["tenant_mismatch"] = _mismatch
    return ctx


# ── response mapping (tolerant; logs the raw shape for grounding) ─────────────

def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _map_response(payload: dict, queried_dataset_id: str | None = None) -> AnalyticsResponse:
    """Map a REAL SUT analytics payload → AnalyticsResponse. Field names are not
    captured (OpenAPI lacks body schemas), so match common shapes tolerantly and
    log the raw payload ONCE for grounding/refinement on SUT recovery."""
    global _shape_logged
    if not _shape_logged:
        log.info("R216: first real analytics payload shape (ground the mapping here): %s",
                 json.dumps(payload, default=str)[:1200])
        _shape_logged = True
    refused = bool(_first(payload, "refused", "blocked", "declined", "is_refused", default=False))
    answer = _first(payload, "answer", "response", "message", "text", "content", "result", "summary", default="")
    if isinstance(answer, dict):
        # LIVE-GROUNDED chat shape {"type":"chat","data":{"msg":...}} (R218)
        _d = answer.get("data") if isinstance(answer.get("data"), dict) else None
        if _d and isinstance(_d.get("msg"), str):
            answer = _d["msg"]
        else:
            answer = _first(answer, "text", "content", "message", "value", "msg", default="") or json.dumps(answer)[:500]
    ins = _first(payload, "insight", "data", "result", default={}) or {}
    insight = Insight()
    if isinstance(ins, dict):
        for f in ("value", "metric", "schema", "combined", "direction", "magnitude_pct",
                  "source_page", "document_id", "section_id"):
            v = ins.get(f)
            if v is not None and hasattr(insight, f):
                setattr(insight, f, v)
    # R233 — the SUT's returned rows, if any (many query shapes carry a list).
    _rows = _first(payload, "results", "rows", "records", "data_rows", default=None)
    if not isinstance(_rows, list):
        _rows = None
    _answer = str(answer or "")
    return AnalyticsResponse(
        refused=refused,
        clarification_requested=bool(_first(payload, "clarification_requested", "needs_clarification", default=False)),
        confidence=float(_first(payload, "confidence", "score", default=0.0) or 0.0),
        answer=_answer,
        metric=_first(payload, "metric"),
        direction=_first(payload, "direction"),
        magnitude_pct=_first(payload, "magnitude_pct"),
        sources=_first(payload, "sources", "citations", default=[]) or [],
        _is_stub_default=False,   # REAL backend — the pillar now measures the SUT
        insight=insight,
        queried_dataset_id=queried_dataset_id,
        # R233 — faithful direct-read fields (narrative view built in __post_init__).
        text=_answer,
        results=_rows,
        query_valid=bool(_answer and not refused),
    )


def _error_response(detail: str) -> AnalyticsResponse:
    """A REAL (non-stub) error response so the test FAILS truthfully — the SUT
    analytics is unreachable/degraded, which IS a SUT-quality signal to report."""
    return AnalyticsResponse(
        refused=False, clarification_requested=False, confidence=0.0,
        answer=f"ARTA real-backend error (SUT analytics did not answer): {detail}"[:500],
        _is_stub_default=False, _is_error=True, insight=Insight(),
    )


# ── AUTH-CYCLE (R218): analytics QUERY-token mint (login-scoped agent-user) ─────

_QUERY_TOKEN_CACHE: dict = {}   # (service_jwt_tail, sid, subid, schema) → token_id


def _resolve_mint_base(ctx: dict) -> str | None:
    """The BACKEND host that serves `create_user_agent_user_token`.

    ★ R218.AM.2 (LIVE-PROVEN 2026-07-06): the mint route lives on the SUT's
    BACKEND host (the manifest `hosts.backend`), NOT the token ISSUER. The prior
    order used the session/service token `iss` claim first, which points at the
    identity provider (not the API) → the mint POST 404/500'd and
    `_mint_query_token` silently returned None (the chronic "LIVE 2xx unproven"
    mint). The SUT's own chat widget posts its token mint to the backend base URL
    (source-verified in the SUT's auth handler), which for that deployment is the
    backend host. So resolve: explicit env → the manifest BACKEND host → (last
    resort) the analytics base_url netloc. The token `iss` is NEVER the API host — drop it.
    SUT-agnostic: `TARGET_AUTH_MINT_BASE` (manifest `hosts.backend.env`) overrides."""
    from urllib.parse import urlsplit
    cand = os.environ.get("TARGET_AUTH_MINT_BASE")
    if cand:
        return cand.rstrip("/")
    # the manifest BACKEND host (media + cm + the mint all live here) — the correct API host.
    try:
        from . import analytics_manifest as _man  # type: ignore
        bk = _man.host_base(_man.load_manifest(), "backend")
        if bk:
            return bk.rstrip("/")
    except Exception:
        pass
    base = ctx.get("base_url")
    if base:
        sp = urlsplit(base)
        if sp.scheme and sp.netloc:
            return f"{sp.scheme}://{sp.netloc}"
    return None


def _mint_query_token(ctx: dict) -> str | None:
    """Mint (+cache) the login-scoped analytics agent-USER token that authorizes the
    query resource. Returns the Bearer RECORD id (`__auto_id__`), or None on failure.
    Tries the authenticated path (with the session token's nested id_token) first, then the
    no-auth path (null → the server assigns a fresh user_id)."""
    service_jwt = ctx.get("service_token_jwt")
    sid, subid = ctx.get("subscriber_id"), ctx.get("subscription_id")
    schema = ctx.get("schema_id")
    if not (service_jwt and sid and subid and schema):
        log.info("AUTH-CYCLE: cannot mint query token (service_jwt=%s sub=%s "
                 "subscription=%s schema=%s)", bool(service_jwt), bool(sid),
                 bool(subid), bool(schema))
        return None
    ck = (service_jwt[-24:], sid, subid, schema)
    if ck in _QUERY_TOKEN_CACHE:
        return _QUERY_TOKEN_CACHE[ck]
    api_base = _resolve_mint_base(ctx)
    if not api_base:
        log.info("AUTH-CYCLE: no mint base host resolvable → cannot mint query token")
        return None
    try:
        from src.agents.auth_chain import mint_query_user_token
    except Exception:
        try:
            from auth_chain import mint_query_user_token  # type: ignore
        except Exception:
            return None
    tmpl = os.environ.get("TARGET_AUTH_MINT_ENDPOINT_TEMPLATE") or None
    tpt = ctx.get("third_party_token")
    prov = None
    if tpt:
        iss = str(_jwt_claims(tpt).get("iss") or "").lower()
        if "google" in iss:
            prov = "google"
        elif any(s in iss for s in ("microsoft", "azure", "sts.windows", "login.live")):
            prov = "azure"
    attempts = []
    if tpt and prov:
        attempts.append((tpt, prov))
    attempts.append((None, None))   # no-auth fallback
    for tp, pv in attempts:
        res = mint_query_user_token(
            api_base=api_base, subscriber_id=sid, subscription_id=subid,
            schema_id=schema, service_agent_user_token=service_jwt,
            third_party_token=tp, token_provider=pv, endpoint_template=tmpl)
        if res and res.get("token_id"):
            _QUERY_TOKEN_CACHE[ck] = res["token_id"]
            return res["token_id"]
    return None


# ── the client ────────────────────────────────────────────────────────────────

class AnalyticsClient:
    """Real SUT analytics client. `.ask(query)` returns an AnalyticsResponse that
    reflects the real SUT (or a truthful error response when the SUT is down)."""

    def _build_auth(self, path: str, ctx: dict) -> tuple[dict, bool]:
        """A3 (R218) — assemble request auth from the DISCOVERED auth chain
        (composite Bearer + Cookie for analytics), not hardcoded. Returns
        (headers, resolved). SUT-agnostic: whatever the chain (source-derived in
        A2, or the module's bundled template) says for this path is what we send."""
        headers = {"Content-Type": "application/json"}
        try:
            from src.agents import auth_chain as _acm
            raw = os.environ.get("ARTA_AUTH_CHAIN")
            if raw:
                chain_cfg = json.loads(raw)
            else:
                # The auth-chain module's bundled default chain template. Resolved by
                # suffix so this runtime does not pin the constant's exact name.
                chain_cfg = next(
                    (getattr(_acm, _n) for _n in dir(_acm)
                     if _n.endswith("_DISCOVERED_CHAIN")), None)
            chain = _acm.AuthChain.from_config({"chain": chain_cfg})
            a = _acm.auth_for_path(path, chain=chain, tokens=ctx.get("tokens", {}))
            if a.get("header_value"):
                headers[a.get("header_name", "Authorization")] = a["header_value"]
            cookie_parts = [f"{c['name']}={c['value']}" for c in (a.get("cookies") or [])]
            if a.get("scheme") == "cookie" and a.get("cookie_value"):
                cookie_parts.append(
                    f"{a.get('cookie_name', _SESSION_COOKIE_NAME)}={a['cookie_value']}")
            if cookie_parts:
                headers["Cookie"] = "; ".join(cookie_parts)
            return headers, bool(a.get("resolved"))
        except Exception as exc:
            log.warning("R218 A3: auth-chain resolution failed (%s); no auth applied", exc)
            return headers, False

    def ask(self, query: str, **kwargs) -> AnalyticsResponse:  # noqa: ARG002
        import httpx
        import time as _rt_time
        _rt_t0 = _rt_time.time()   # R233 — client-measured time-to-answer
        storage = _load_storage()
        ctx = _resolve_context(storage)
        base, sid, subid = ctx["base_url"], ctx["subscriber_id"], ctx["subscription_id"]
        # H4 (R218) — refuse to measure the WRONG tenant. A mismatch between the
        # agent token's tenant and the live session would otherwise silently query
        # a different tenant (or 400), corrupting the SUT verdict. Fail truthfully.
        if ctx.get("tenant_mismatch") and os.environ.get("ARTA_H4_TENANT_GUARD_DISABLE") != "1":
            return _error_response(f"tenant mismatch — {ctx['tenant_mismatch']}")
        # AUTH-CYCLE — the session's inner id_token (third_party_token) is expired;
        # the SUT would 400 "Cant Connect to Authourization Server". Fail truthfully
        if ctx.get("session_stale"):
            return _error_response(
                "session id_token (session-token third_party_token) EXPIRED — the "
                "analytics authz server would reject this (400); the run must refresh "
                "the session token (R162 pre-flight / refresh_if_expired) before querying")
        # AUTH-CYCLE (R218 KEYSTONE) — the analytics QUERY resource
        # agent-USER token; the admin agent-api-token ARTA holds only authorizes
        # `user-management.*` (which is why get-list-of-apps/create-app 200 but the
        # query 400s "Cant Connect to Authourization Server"). Mint the user token the
        # widget's way (create_user_agent_user_token) and use its record id as the
        # already applies to the working user-management calls. Killswitch
        # ARTA_AN_USER_TOKEN_MINT_DISABLE=1 reverts to the legacy admin-token behavior.
        _mint_enabled = os.environ.get("ARTA_AN_USER_TOKEN_MINT_DISABLE") != "1"
        _mint_applicable = bool(ctx.get("service_token_jwt") and sid and subid
                                and ctx.get("schema_id"))
        if _mint_enabled and _mint_applicable:
            _qtok = _mint_query_token(ctx)
            if _qtok:
                ctx.setdefault("tokens", {})["agent_api_token"] = _qtok
                ctx["auth_unbound"] = False   # a freshly-minted bound token supersedes
            elif os.environ.get("ARTA_AN_QUERY_TOKEN_GUARD_DISABLE") != "1":
                return _error_response(
                    "analytics query-user-token mint FAILED — the query resource "
                    "(query-engine.query) needs a LOGIN-scoped agent-USER token minted "
                    "via create_user_agent_user_token; the admin agent-api-token alone "
                    "400s 'Cant Connect to Authourization Server'. Verify the mint host "
                    "(TARGET_AUTH_MINT_BASE), schema_id, and the service token JWT.")
        # A7.4 (R218) — the only Bearer available is an explicitly UNBOUND analytics
        # token (engine_type==""), which the SUT rejects with 400 "Cant Connect to
        # Authourization Server". Fail TRUTHFULLY (so it surfaces as a real
        # auth-unresolved BLOCK, not a confusing 400 measured as SUT quality) rather
        # than send a token we KNOW the authz layer can't bind. The fix is upstream:
        # the agent-user-token mint must supply a bound token. Killswitch
        # ARTA_A7_BINDING_GUARD_DISABLE=1.
        if ctx.get("auth_unbound") and os.environ.get("ARTA_A7_BINDING_GUARD_DISABLE") != "1":
            return _error_response(
                "analytics agent token is UNBOUND (engine_type==''); SUT authz "
                "cannot resolve it (→400) — needs a bound agent_user_token mint (A7.1)")
        analytics_path = auth_path(
            f"subscriber/{sid}/subscription/{subid}/function/query-engine/event/query")
        headers, auth_ok = self._build_auth(analytics_path, ctx)
        if not (base and sid and subid and auth_ok):
            return _error_response(
                f"missing/unresolved analytics auth context (base={bool(base)} "
                f"sub={bool(sid)} subscription={bool(subid)} auth_resolved={auth_ok})")
        # A8.3 (R218 KEYSTONE) — the analytics query is APP-SCOPED. A null `app_id`
        # is the live 422 root cause. Resolve a VALID app_id READ-ONLY (env > storage
        # > get-list-of-apps select; create-app only under R154). Lazy + memoized;
        # resolve_app_id fast-returns None without a live context (unit-safe).
        _ds = ctx.get("dataset") or {}
        if not (os.environ.get("ARTA_ANALYTICS_APP_ID") or _ds.get("app_id")) \
                and os.environ.get("ARTA_ANALYTICS_APP_AUTORESOLVE") != "0":
            try:
                from .dataset_client import resolve_app_id
                _aid = resolve_app_id(ctx)
                if _aid:
                    ctx.setdefault("dataset", {})["app_id"] = _aid
                    _ds = ctx["dataset"]
            except Exception as _a8exc:
                log.debug("A8: app_id autoresolve skipped: %s", _a8exc)
        _app_id = os.environ.get("ARTA_ANALYTICS_APP_ID") or _ds.get("app_id")
        # A8.4 — never send app_id=null and measure the resulting 422 as SUT quality.
        if not _app_id and os.environ.get("ARTA_A8_APP_GUARD_DISABLE") != "1":
            return _error_response(
                "analytics app_id UNRESOLVED — the query is app-scoped but no app "
                "could be listed/selected (get-list-of-apps empty) or created (R154 "
                "off). The SUT would 422; resolve an app (A8) before querying.")
        # AUTH-CYCLE (R218) — match the SPA query contract: send session_id="" on the
        # FIRST turn; the SERVER creates the session and RETURNS {session_id,
        # correlation_id} (chat_handler: `if not session_id: session_id=uuid4()`),
        # which the client then reuses. `correlation_id` is NOT a request field (the
        # server generates it as `f"{dataset_type}_{uuid4()}"`), so it is NOT sent in
        # the body — the old client-fabricated random session_id/correlation_id was a
        # contract inaccuracy (though the 400 was the token-type issue, now fixed).
        first_session_id = os.environ.get("ARTA_ANALYTICS_SESSION_ID", "")
        q_url = f"{base}/subscriber/{sid}/subscription/{subid}/function/query-engine/event/query"
        # A5/G2 LIVE-GROUNDED body shape (422-discovered, R218): the query engine
        # requires `user_query` (NOT user_message) + the DATASET DESCRIPTOR
        # (app_id, dataset_type, dataset_id, dataset_name). dataset_id comes from
        # storage localStorage; the rest are dataset-selection-specific and
        # env-overridable (ARTA_ANALYTICS_*). The analytics query is dataset-scoped
        # — generated tests must carry which dataset they probe.
        # AL.0 (R218 KEYSTONE) — the SEEDED dataset MUST win. A correctness test seeds
        # dataset Y (env ARTA_ANALYTICS_DATASET_ID); the pasted storage-state usually
        # carries a DIFFERENT pre-existing dataset_id. Storage-first (the old code) made
        # the SUT answer about the WRONG data → the ground-truth comparison was
        # meaningless (silent false pass/fail). Env-first, consistent with app_id/name.
        _seeded_ds = os.environ.get("ARTA_ANALYTICS_DATASET_ID")
        _store_ds = _ds.get("dataset_id")
        _dataset_id = _seeded_ds or _store_ds
        if _seeded_ds and _store_ds and _seeded_ds != _store_ds:
            log.warning("AL.0: IGNORING storage dataset_id %s — querying the SEEDED "
                        "dataset %s (correctness must verify OUR uploaded data)",
                        _store_ds, _seeded_ds)
        # dataset_type — the SPA derives it as `dataset_id.split("_")[0]`; env override
        # wins, then storage, then the id-prefix, then "structured".
        _dataset_type = (os.environ.get("ARTA_ANALYTICS_DATASET_TYPE")
                         or _ds.get("dataset_type")
                         or (_dataset_id.split("_")[0] if _dataset_id and "_" in _dataset_id else None)
                         or "structured")
        # ★ R218.AM.5 — the query MUST carry `source_type` aligned to the dataset type.
        # SUFFIXES the computed project_user_id with "@!#cas#!@" when `request.source_type ==
        # "files"` (the model DEFAULT, user_schema.py:18) — but EXCEL/mongo (tabular) files
        # are stored under the PLAIN project_user_id (user_master). ARTA sent dataset_type but
        # NOT source_type → it defaulted to "files" → the query resolved the SUFFIXED user_id
        # → get_source_files found nothing → the excel consumer answered "No Excel files
        # found" (is_error=False, ~10s — NOT a timeout). Sending source_type=dataset_type
        # keeps the user_id PLAIN for excel/mongo (else-branch) and correctly suffixed only
        # R231 — resolve the dataset NAME (READ-ONLY) when it's absent. The query 422s on
        # a null `dataset_name`, but the storage carries only a dataset_id; the SUT's
        # get-list-of-data-sets returns the name (+ prefixed id → type). Match the stored
        # id → name/type. Env/storage still win. Killswitch ARTA_R231_DATASET_RESOLVE_DISABLE=1.
        _dataset_name = os.environ.get("ARTA_ANALYTICS_DATASET_NAME") or _ds.get("dataset_name")
        if not _dataset_name and _dataset_id and os.environ.get("ARTA_R231_DATASET_RESOLVE_DISABLE") != "1":
            _rid, _rname, _rtype = self._resolve_dataset(ctx, _dataset_id)
            if _rname:
                _dataset_name = _rname
                if _rid:
                    _dataset_id = _rid
                if _rtype:
                    _dataset_type = _rtype
                log.info("R231: resolved dataset_name=%r type=%s id=%s",
                         _rname, _rtype, str(_rid)[:18])
        _source_type = os.environ.get("ARTA_ANALYTICS_SOURCE_TYPE") or _dataset_type
        body = {
            "user_query": query,
            "session_id": first_session_id,
            "dataset_id": _dataset_id,
            "dataset_name": _dataset_name,
            "dataset_type": _dataset_type,
            "source_type": _source_type,
            "app_id": _app_id,
            "realm": {},
            "priority": int(os.environ.get("ARTA_ANALYTICS_PRIORITY", "10") or 10),
            "mode": os.environ.get("ARTA_ANALYTICS_MODE", "").strip().lower() in ("1", "true", "yes"),
        }
        # AL.0 half-2 (R265) — stamp the ROUTED dataset_id onto every success
        # response so a correctness test can assert its query was scoped to OUR
        # seeded data (regression guard on the env-first precedence above).
        def _mr(_payload):
            _resp = _map_response(_payload, queried_dataset_id=_dataset_id)
            # R233 — stamp the client-measured wallclock so `response.response_time_ms`
            # (and the narrative view's) is a real value, not None (was TypeError on
            # `<= 120000`). Faithful: it's ARTA's genuine time-to-answer for the query.
            _rt_ms = (_rt_time.time() - _rt_t0) * 1000.0
            try:
                _resp.response_time_ms = _rt_ms
                if getattr(_resp, "narrative", None) is not None:
                    _resp.narrative.response_time_ms = _rt_ms
            except Exception:
                pass
            return _resp
        try:
            # R218.AM.4 — long READ window (excel LLM reasoning is slow); connect stays
            # short so a dead host fails fast. Covers BOTH a slow synchronous POST answer
            # AND the async response-stream SSE read below.
            _tmo = httpx.Timeout(_STREAM_TIMEOUT, connect=min(15.0, _REQUEST_TIMEOUT))
            with httpx.Client(timeout=_tmo, verify=False) as c:
                r = c.post(q_url, headers=headers, json=body)
                if r.status_code >= 400:
                    return _error_response(f"query HTTP {r.status_code}: {r.text[:200]}")
                # Synchronous answer?
                try:
                    payload = r.json()
                except Exception:
                    payload = {}
                # The server ISSUES session_id + correlation_id on the first turn; use
                # THOSE for the response-stream (the SPA reuses the server's ids).
                srv_sid = (payload.get("session_id") if isinstance(payload, dict) else None) or first_session_id
                srv_cid = (payload.get("correlation_id") if isinstance(payload, dict) else None) or ""
                if isinstance(payload, dict) and any(
                    k in payload for k in ("answer", "response", "message", "refused", "insight")):
                    return _mr(payload)
                # Async/SSE: consume the response-stream to completion.
                stream_url = (f"{base}/subscriber/{sid}/subscription/{subid}"
                              f"/function/query-engine/event/response-stream")
                params = {"correlation_id": srv_cid, "session_id": srv_sid, "user_message": query}
                # LIVE-GROUNDED SSE shape (R218, verified against the SUT): each frame is
                #   data: {"response": {"type":"chat","data":{"msg":"<chunk>","done":bool}},
                #          "session_id":..., "message_id":...}
                # The answer is STREAMED — msg chunks must be CONCATENATED across frames
                # (the old `final.update(ev)` overwrote each frame → only the last, empty
                # `done:true` frame survived → blank answer). The last frame carries done:true.
                final: dict = {}
                answer_parts: list[str] = []
                buf: list[str] = []
                with c.stream("GET", stream_url, headers=headers, params=params) as s:
                    if s.status_code >= 400:
                        # Fall back to whatever the POST returned.
                        return _mr(payload) if payload else _error_response(
                            f"response-stream HTTP {s.status_code}")
                    for line in s.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk in ("", "[DONE]"):
                            continue
                        try:
                            ev = json.loads(chunk)
                        except Exception:
                            buf.append(chunk)
                            continue
                        if not isinstance(ev, dict):
                            continue
                        # carry top-level metadata (session_id, message_id, …)
                        final.update({k: v for k, v in ev.items() if k != "response"})
                        # accumulate the streamed chat message; ground the {response:{data:{msg}}}
                        # shape AND a flat {data:{msg}} / {msg} for resilience across SUTs.
                        resp_obj = ev.get("response") if isinstance(ev.get("response"), dict) else ev
                        _data = resp_obj.get("data") if isinstance(resp_obj.get("data"), dict) else None
                        _msg = (_data.get("msg") if _data else None) or resp_obj.get("msg")
                        if isinstance(_msg, str) and _msg:
                            answer_parts.append(_msg)
                        final["response"] = resp_obj
                if answer_parts:
                    return _mr({"answer": "".join(answer_parts),
                                          **{k: final[k] for k in ("session_id", "message_id")
                                             if final.get(k)}})
                if final.get("response") is not None:
                    return _mr(final)
                if buf:
                    return _mr({"answer": " ".join(buf)})
                return _mr(payload) if payload else _error_response("empty analytics response")
        except Exception as exc:
            return _error_response(f"{type(exc).__name__}: {str(exc)[:160]}")

    # R230 — interface completeness (mirrors the stub). Generated analytics tests
    # call execute_query/generate_insight/generate_narrative; the real client must
    # expose them or it AttributeErrors. All route through ask() (the SUT query
    # path), so they measure the SUT when it answers and fail TRUTHFULLY (the same
    # `_error_response`) when auth/dataset can't resolve — never a hard crash.
    def _resolve_dataset(self, ctx: dict, dataset_id: str | None) -> tuple:
        """R231 — read-only dataset descriptor resolution. Returns
        (full_dataset_id, dataset_name, dataset_type) or (dataset_id, None, None) on any
        failure so the caller falls through to the existing truthful-422 behaviour.
        GETs the SUT's get-list-of-data-sets (read-only, R154-safe) and matches the stored
        id (which may be a bare uuid while the list carries a `<type>_<uuid>` id)."""
        import httpx
        base, sid, subid = ctx.get("base_url"), ctx.get("subscriber_id"), ctx.get("subscription_id")
        if not (base and sid and subid):
            return (dataset_id, None, None)
        url = (f"{base}/subscriber/{sid}/subscription/{subid}"
               f"/function/user-management/event/get-list-of-data-sets")
        headers, ok = self._build_auth(auth_path("get-list-of-data-sets"), ctx)
        if not ok:
            return (dataset_id, None, None)
        try:
            with httpx.Client(timeout=25, verify=False) as c:
                r = c.get(url, headers=headers)
                if r.status_code >= 400 or not r.content:
                    log.info("R231 get-list-of-data-sets HTTP %s", r.status_code)
                    return (dataset_id, None, None)
                j = r.json()
                items = ((j.get("data_sets") or j.get("datasets") or j.get("data") or [])
                         if isinstance(j, dict) else (j if isinstance(j, list) else []))
                if not isinstance(items, list) or not items:
                    return (dataset_id, None, None)
                def _name(d):
                    return d.get("dataset_name") or d.get("data_set_name") or d.get("name")
                bare = str(dataset_id or "").split("_")[-1]
                chosen = None
                if bare:
                    chosen = next((d for d in items
                                   if bare in str(d.get("dataset_id") or d.get("id") or "")), None)
                if not chosen:  # fall back to the first dataset that has a usable name
                    chosen = next((d for d in items if _name(d)), None)
                if not chosen:
                    return (dataset_id, None, None)
                full_id = chosen.get("dataset_id") or chosen.get("id") or dataset_id
                dtype = full_id.split("_")[0] if full_id and "_" in full_id else None
                return (full_id, _name(chosen), dtype)
        except Exception as exc:
            log.info("R231: dataset resolution failed: %s", exc)
            return (dataset_id, None, None)

    def execute_query(self, query: str = "", **kwargs) -> AnalyticsResponse:  # noqa: ARG002
        return self.ask(str(query))

    def generate_insight(self, *args, **kwargs) -> AnalyticsResponse:  # noqa: ARG002
        return self.ask(str(args[0]) if args else "")

    def generate_narrative(self, insight: Any = None, **kwargs) -> AnalyticsResponse:  # noqa: ARG002
        # A narrative is derived from an insight; feed the insight's text/value back
        # through ask() so a wired SUT narrates it (truthful error otherwise).
        q = ""
        try:
            q = (getattr(insight, "text", None) or getattr(insight, "value", None)
                 or (insight if isinstance(insight, str) else "") or "")
        except Exception:
            q = ""
        return self.ask(str(q))


client = AnalyticsClient()
