"""Per-API-family auth chain (single source of truth).

ARTA's API dispatch historically injected ONE `auth_token` Bearer for the
whole collection. But real SPAs auth DIFFERENTLY per API family — each HTTP
client in the frontend may carry its own scheme. A discovered example shape:

  - `/billing/*`  → `Bearer svc_<organization_id>_<session_token>`  (COMPOSITE)
  - `/analytics/*` `/extraction/*` `/monitor*` → `Bearer <agent_api_token>`
  - default       → `Bearer <session_token>`
  - payment-provider paths → NONE

Sending a bare session Bearer to a composite-scheme family can 500 while the
composite format returns a real business response — per-family auth is a
dominant cause of API-test 5xx/401 waves on multi-service SUTs.

This module is SUT-AGNOSTIC: the rules are DATA (an ordered list of
`AuthRule`), discovered from the SUT's frontend interceptors (or operator
config) and stored under `env_block.auth.chain`. `auth_for_path()` resolves the
right header/cookie for a given request path using the available tokens. The
default chain (no rules configured) reproduces the legacy single-Bearer
behavior, so undiscovered SUTs are unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("arta.auth_chain")

# Auth schemes a rule can specify.
SCHEME_BEARER = "bearer"        # Authorization: Bearer <value_template resolved>
SCHEME_COOKIE = "cookie"
SCHEME_NONE = "none"            # no auth (public endpoints)
_VALID_SCHEMES = {SCHEME_BEARER, SCHEME_COOKIE, SCHEME_NONE}


@dataclass
class AuthRule:
    """One match rule. `match` is a substring OR `/`-anchored path-prefix tested
    against the request PATH (lowercased); `*` matches everything (catch-all).
    `value_template` is interpolated with the available tokens (e.g.
    `svc_{organization_id}_{session_token}`). `host` (optional) is the family key
    into the env's `host_map` — so the SAME matched rule drives BOTH host + auth.

    R213.J — rule selection is SPECIFICITY-based, not declaration order (see
    `AuthChain.best_rule`): an anchored path-prefix beats a mid-path substring,
    and a longer substring beats a shorter one. This removes the operator's
    burden to hand-order rules (the old first-match-wins footgun) and makes
    path→(host,auth) routing generic for ANY SUT."""
    match: str
    scheme: str = SCHEME_BEARER
    value_template: str = "{session_token}"
    cookie_name: str = "session-token"
    host: "str | None" = None
    # A1 (R218) — multi-credential: a rule may carry ADDITIONAL cookies alongside
    # its primary scheme, so one rule expresses a composite like
    # analytics client actually sends — discovered from source, A2). Each entry is
    # single-credential rules (fully back-compat). Additive to ANY scheme.
    cookies: "list[dict]" = field(default_factory=list)

    def matches(self, path: str) -> bool:
        if self.match == "*":
            return True
        return self.match.lower() in (path or "").lower()

    def specificity(self, path: str) -> "tuple[int, int] | None":
        """R213.J — return a comparable (tier, length) specificity for this
        rule against `path`, or None when it doesn't match. Higher wins:
          tier 2 = anchored path-prefix (segment-aligned, e.g. `/composite_svc`
                   matching `/composite_svc/…`) — the SERVICE prefix that routes;
          tier 1 = mid-path substring (e.g. `composite_default`);
          tier 0 = `*` catch-all.
        Length tie-breaks within a tier (so `composite_default` beats `/example_sut`).
        Order-independent: depends only on the rule + path, never declaration
        position."""
        if self.match == "*":
            return (0, 0)
        m = (self.match or "").lower()
        p = (path or "").lower()
        if not m or m not in p:
            return None
        # anchored, segment-aligned path-prefix → highest tier
        if m.startswith("/") and p.startswith(m):
            nxt = p[len(m):len(m) + 1]
            if nxt in ("", "/", "?"):
                return (2, len(m))
        return (1, len(m))


@dataclass
class AuthChain:
    rules: list[AuthRule] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: Any) -> "AuthChain":
        """Build from `env_block.auth.chain` (a list of dicts) or a bare list.
        Unknown/invalid entries are skipped (graceful). Returns an empty chain
        when no config — caller falls back to legacy single-Bearer. Preserves
        the per-rule `host` (R213.J — needed so one matched rule yields host+auth)."""
        raw = cfg.get("chain") if isinstance(cfg, dict) else cfg
        rules: list[AuthRule] = []
        for r in raw or []:
            if not isinstance(r, dict):
                continue
            scheme = (r.get("scheme") or SCHEME_BEARER).lower()
            if scheme not in _VALID_SCHEMES:
                continue
            match = r.get("match")
            if not isinstance(match, str) or not match:
                continue
            # A1 (R218) — parse additive cookies (each {name, value_template});
            # tolerate malformed entries (skip non-dicts / missing name).
            _cookies = []
            for _c in (r.get("cookies") or []):
                if isinstance(_c, dict) and _c.get("name"):
                    _cookies.append({
                        "name": _c["name"],
                        "value_template": _c.get("value_template") or "{session_token}",
                    })
            rules.append(AuthRule(
                match=match,
                scheme=scheme,
                value_template=r.get("value_template") or "{session_token}",
                cookie_name=r.get("cookie_name") or "session-token",
                host=r.get("host"),
                cookies=_cookies,
            ))
        return cls(rules=rules)

    def best_rule(self, path: str) -> "AuthRule | None":
        """R213.J — select the MOST-SPECIFIC matching rule for `path`,
        deterministically and INDEPENDENT of declaration order (anchored
        path-prefix > longest mid-path substring > `*`). Returns None when
        nothing matches (no `*` present). Killswitch
        ARTA_AUTH_SPECIFICITY_DISABLE=1 reverts to legacy first-match-wins."""
        import os as _os
        if _os.environ.get("ARTA_AUTH_SPECIFICITY_DISABLE") == "1":
            for rule in self.rules:
                if rule.matches(path):
                    return rule
            return None
        best = None
        best_score = None
        for rule in self.rules:
            sc = rule.specificity(path)
            if sc is None:
                continue
            if best_score is None or sc > best_score:
                best, best_score = rule, sc
        return best

    def is_empty(self) -> bool:
        return not self.rules


# it's a default DATA template a discovery step (frontend axios-interceptor
# parse) or operator can install into env_block.auth.chain. Ordered: most
# specific first, catch-all last.
EXAMPLE_DISCOVERED_CHAIN = [
    {"match": "composite_default", "scheme": SCHEME_BEARER, "value_template": "{session_token}",
     "host": "billing"},
    {"match": "/billing", "scheme": SCHEME_BEARER,
     "value_template": "svc_{organization_id}_{session_token}", "host": "billing"},
    # Analytics-style families often need BOTH a service token and the session
    # cookie (live pattern: the bare agent token 400s without the cookie).
    {"match": "/analytics", "scheme": SCHEME_BEARER,
     "value_template": "{agent_api_token}",
     "cookies": [{"name": "session-token", "value_template": "{session_token}"}]},
    {"match": "/extraction", "scheme": SCHEME_BEARER, "value_template": "{agent_api_token}"},
    {"match": "/monitor", "scheme": SCHEME_BEARER, "value_template": "{agent_api_token}"},
    {"match": "*", "scheme": SCHEME_BEARER, "value_template": "{session_token}"},
]

# A2 (2026-07-25) — the GENERIC fallback chain: a single catch-all Bearer rule, NOT
NEUTRAL_BEARER_CHAIN = [
    {"match": "*", "scheme": SCHEME_BEARER, "value_template": "{agent_api_token}"},
]


def select_auth_chain(discovered_chain, *, has_session_token: bool):
    """Choose the per-path auth chain, genericity-first + regression-safe.

    Precedence: (1) the DISCOVERED chain (source-derived interceptor parse or
    operator env_block.auth.chain) always wins; (2) else, ONLY when the
    session-cookie shape is present AND the operator opts in via
    `ARTA_COMPOSITE_TEMPLATE_FALLBACK=1`, fall back to the composite EXAMPLE
    template; (3) else a neutral single-Bearer default.

    Returns ``(chain, source)`` where source ∈ {"discovered", "example_sut_template",
    "neutral_bearer"}; the caller LOGs when a non-discovered source fires (the
    charter forbids silent SUT-specific fallbacks). The composite template is a
    reference/bootstrap example an operator (or the source-discovery step)
    copies into `env_block.auth.chain` — it is never a silent live fallback."""
    if discovered_chain:
        return discovered_chain, "discovered"
    if has_session_token and os.environ.get("ARTA_COMPOSITE_TEMPLATE_FALLBACK") == "1":
        return EXAMPLE_DISCOVERED_CHAIN, "example_sut_template"
    return NEUTRAL_BEARER_CHAIN, "neutral_bearer"


def _interpolate(template: str, tokens: dict[str, str]) -> str | None:
    """Fill `{name}` placeholders from tokens. Returns None if any placeholder
    is missing or empty — so the caller can fall back instead of shipping a
    half-resolved header like `composite_svc__<session_token>` (which 500s)."""
    out = template
    for name in re.findall(r"\{(\w+)\}", template):
        val = tokens.get(name)
        if not val:
            return None
        out = out.replace("{" + name + "}", val)
    return out


def mint_agent_user_token(
    *,
    api_base: str,
    account_id: str,
    subscriber_id: str,
    subscription_id: str,
    schema_id: str,
    admin_token_id: str,
    user_id: str,
    endpoint_template: str | None = None,
    body_key: str = "agen_api_token",
    timeout: float = 15.0,
) -> str | None:
    """R160.D — autonomously GENERATE a user agent token by calling the SUT's
    mint endpoint (the token is signed server-side, so ARTA requests it rather
    than re-signing).

    Discovered from the SUT source (composite_svc-1.0 example_sut_backend_api): the route
    `POST {api_base}/api/example_sut/account/{account_id}/subscriber/{subscriber_id}/
    subscription/{subscription_id}/schema/{schema_id}/agent_user_token` with body
    `{agen_api_token: <existing admin agent-token RECORD id>, user_id}` returns
    `{"success": true, "token_id": <new id>}`. The returned `token_id` is the
    Bearer the SPA sends to analytics/extraction. `admin_token_id` is the
    long-lived record id from the session (`agent-user-token.tokenId`).

    `endpoint_template` overrides the path for other SUTs (placeholders:
    {api_base}/{account_id}/{subscriber_id}/{subscription_id}/{schema_id}).
    Returns the minted token_id or None (graceful). Live-proven on the example SUT.
    """
    import httpx
    # wasted live call). Fail-CLOSED: only apply the built-in default when the
    # SUTs must pass an explicit `endpoint_template` (from project env_block
    # auth.mint) or the mint is skipped (returns None, auth chain proceeds without
    # an agent token — correct for Auth0/bearer SUTs). GENERIC.
    tmpl = endpoint_template
    if not tmpl:
        # A2 platform/SUT-separation — the built-in default below is the
        # account/subscriber/subscription/schema agent-token mint SHAPE. Gate it
        # on the STRUCTURAL presence of that full id-set (a DATA signal), NOT a
        # caller that supplies all four ids is mint-shaped by construction, while
        # any other SUT (Auth0/bearer — no subscriber/schema) yields None and the
        # chain proceeds tokenless (correct). Other SUTs install their own
        # `env_block.auth.mint.endpoint_template`, which always wins above.
        # Killswitch ARTA_BUILTIN_AGENT_MINT_DISABLE=1 forces skip.
        _mint_shaped = all((account_id, subscriber_id, subscription_id, schema_id))
        if _mint_shaped and os.environ.get("ARTA_BUILTIN_AGENT_MINT_DISABLE") != "1":
            tmpl = (
                "{api_base}/api/example_sut/account/{account_id}/subscriber/{subscriber_id}"
                "/subscription/{subscription_id}/schema/{schema_id}/agent_user_token"
            )
        else:
            log.debug("R160.D: no agent-token mint endpoint (mint-shaped=%s, "
                      "api_base=%s) — skipping mint (pass env_block auth.mint."
                      "endpoint_template to enable)", _mint_shaped, (api_base or "")[:60])
            return None
    url = tmpl.format(
        api_base=(api_base or "").rstrip("/"), account_id=account_id,
        subscriber_id=subscriber_id, subscription_id=subscription_id,
        schema_id=schema_id,
    )
    try:
        r = httpx.post(url, json={body_key: admin_token_id, "user_id": user_id},
                       timeout=timeout, verify=False)
        if 200 <= r.status_code < 300:
            try:
                j = r.json()
            except Exception:
                return None
            tid = j.get("token_id") or j.get("__auto_id__") \
                or ((j.get("payload") or {}).get("__auto_id__") if isinstance(j.get("payload"), dict) else None)
            if isinstance(tid, str) and tid:
                log.info("R160.D: minted user agent token (id=%s) via %s", tid[:12], url.split("/account/")[0])
                return tid
        else:
            log.info("R160.D: agent-token mint -> %s %s", r.status_code, r.text[:120])
    except Exception as exc:
        log.debug("R160.D: agent-token mint failed: %s", exc)
    return None


def mint_query_user_token(
    *,
    api_base: str,
    subscriber_id: str,
    subscription_id: str,
    schema_id: str,
    service_agent_user_token: str,
    third_party_token: str | None = None,
    token_provider: str | None = None,
    endpoint_template: str | None = None,
    timeout: float = 30.0,
) -> dict | None:
    """AUTH-CYCLE (R218) — mint the analytics agent-USER token the way the SUT's
    chat widget does, so the analytics QUERY resource authorizes.

    ROOT CAUSE this fixes: the analytics query-engine (`query-engine/event/query`)
    runs the SAME auth dependency as `user-management.*`, but authorizes the
    presented token against a DIFFERENT resource — `system.example_sut.analytics
    .query-engine.query`. The admin `agent-api-token` (`create_agent_api_token`,
    account/subscription-scoped) authorizes `user-management.*` (→200) but is NOT a
    valid subject for the login/user-scoped query resource → the gRPC authorize
    errors → the handler raises HTTP 400 "Cant Connect to Authourization Server".
    The widget avoids this by minting a login-scoped **agent-user token** via
    `create_user_agent_user_token`; THAT token authorizes the query. (Source:
    session_token-example_sut-chatbot `authHandle.js` + composite_svc-1.0 `example_sut_backend_api`
    `UserAgentUserToken.post`; the server only STRUCTURALLY decodes the service
    token — no signature check — reading its project/account claims, so ARTA's own
    active analytics `agent-api-token` JWT is a valid `service_agent_user_token`.)

    Contract (the example SUT default; `endpoint_template` overrides for other SUTs):
      POST {api_base}/api/example_sut/subscriber/{subscriber_id}/subscription/
           {subscription_id}/schema/{schema_id}/create_user_agent_user_token
      headers: Content-Type only (NO Authorization — gated by the body JWT)
      body: {service_agent_user_token, third_party_token, token_provider}
      resp: {agent_user_token: <jwt>, __auto_id__: <record id>, ...}

    Returns {"token_id": <__auto_id__>, "token": <agent_user_token jwt>} or None.
    The Bearer the caller then sends is the RECORD id (`token_id`), with whatever
    host prefix the auth chain already applies (the example SUT: `composite_svc_`)."""
    import httpx
    if not (api_base and subscriber_id and subscription_id and schema_id
            and service_agent_user_token):
        return None
    tmpl = endpoint_template or (
        "{api_base}/api/example_sut/subscriber/{subscriber_id}/subscription/"
        "{subscription_id}/schema/{schema_id}/create_user_agent_user_token"
    )
    url = tmpl.format(
        api_base=(api_base or "").rstrip("/"), subscriber_id=subscriber_id,
        subscription_id=subscription_id, schema_id=schema_id,
    )
    body = {
        "service_agent_user_token": service_agent_user_token,
        "third_party_token": third_party_token,
        "token_provider": token_provider,
    }
    try:
        r = httpx.post(url, json=body, timeout=timeout, verify=False,
                       headers={"Content-Type": "application/json"})
    except Exception as exc:
        log.info("AUTH-CYCLE: query-user-token mint request failed: %s", exc)
        return None
    if not (200 <= r.status_code < 300):
        log.info("AUTH-CYCLE: query-user-token mint -> HTTP %s %s",
                 r.status_code, r.text[:160])
        return None
    try:
        j = r.json()
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    tok_id = (j.get("__auto_id__") or j.get("token_id") or j.get("tokenId")
              or ((j.get("payload") or {}).get("__auto_id__")
                  if isinstance(j.get("payload"), dict) else None))
    tok = (j.get("agent_user_token") or j.get("token")
           or ((j.get("payload") or {}).get("token")
               if isinstance(j.get("payload"), dict) else None))
    if not (isinstance(tok_id, str) and tok_id):
        log.info("AUTH-CYCLE: query-user-token mint returned no record id: %s",
                 json.dumps(j, default=str)[:160])
        return None
    log.info("AUTH-CYCLE: minted analytics agent-USER token (id=%s) via %s",
             tok_id[:12], url.rsplit("/subscriber/", 1)[0])
    return {"token_id": tok_id, "token": tok}


def harvest_agent_token_from_storage(storage_state: dict) -> str | None:
    """R160.C — read the SPA's client-minted agent token straight out of the
    session's localStorage (no HAR, no re-implementing the mint flow).

    SPAs that mint a per-session API token cache it in localStorage; the value
    the SPA then sends as `Bearer` is often a record id, not the JWT. For the example SUT
    `localStorage["agent-api-token"] = {"tokenId": <__auto_id__>, "token": <jwt>}`
    and the interceptor sends `Bearer <tokenId>`. So we prefer `tokenId`, then
    fall back to `token`. Scans all origins. Returns the Bearer value or None.

    This only works when the stored session includes the full localStorage (not
    just the auth cookie) — i.e. the operator pasted a complete browser session.
    SUT-agnostic key set covers common namings.
    """
    if not isinstance(storage_state, dict):
        return None
    _KEYS = ("agent-api-token", "agent_api_token", "agentApiToken",
             "agent-user-token", "agent_user_token", "agentUserToken",
             "agent-token", "agentToken")
    for origin in storage_state.get("origins") or []:
        for kv in origin.get("localStorage") or []:
            if (kv.get("name") or "") not in _KEYS:
                continue
            raw = kv.get("value") or ""
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    for k in ("tokenId", "token_id", "__auto_id__", "id", "token", "access_token"):
                        v = obj.get(k)
                        if isinstance(v, str) and v:
                            return v
            except Exception:
                pass
            if isinstance(raw, str) and raw and "{" not in raw:
                return raw  # bare token string
    return None


def _decode_jwt_claims(token: str) -> dict:
    """Best-effort decode of a JWT payload (no signature check). Returns {} on
    any failure. SUT-agnostic; used only to READ id claims, never to trust."""
    import base64 as _b64
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        payload = json.loads(_b64.urlsafe_b64decode(seg).decode("utf-8", "replace"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


# Resource-id claim names ARTA injects as Newman path/query vars (R167). Each
# cookie JWT and the agent-user-token JWT are scanned; the agent token is
# preferred (it carries the per-service account/workspace/project/schema ids the
# SPA actually sends). organizations[0] also feeds organization_id.
_R167_ID_CLAIMS = (
    "account_id", "root_account_id", "subscriber_id", "subscription_id",
    "organization_id", "schema_id", "service_id", "workspace_id",
    "project_id", "user_id", "login_id",
)


def harvest_session_ids_from_storage(storage_state: dict) -> dict[str, str]:
    """R167 KEYSTONE — extract every REAL resource id from the session's token
    claims so dispatch injects real path/query param VALUES instead of R43
    synthetic fake UUIDs (which cause SUT 500s on lookups of non-existent
    resources, masquerading as SUT bugs).

    The SPA's own requests use these ids; they live in the tokens the operator
    pasted: the session_token cookie JWT and `localStorage["agent-user-token"].token`
    JWT. Returns {var_name: value} for any id-claim present (see
    `_R167_ID_CLAIMS`), plus `organization_id` from `organizations[0]` and an
    `account_id` alias for `root_account_id` when only the latter is present.

    Precedence: agent-user-token claims override session_token claims (more specific).
    SUT-agnostic + safe: returns {} when storage lacks tokens; never raises.
    """
    if not isinstance(storage_state, dict):
        return {}

    def _inner(v):
        try:
            return json.loads(v)
        except Exception:
            return v

    session_token_jwt = None
    agent_jwt = None
    for c in storage_state.get("cookies") or []:
        if isinstance(c, dict) and c.get("name") == "session-token" and c.get("value"):
            session_token_jwt = c["value"]
    for origin in storage_state.get("origins") or []:
        for kv in origin.get("localStorage") or []:
            name = kv.get("name") or ""
            raw = kv.get("value") or ""
            if name == "session-token" and not session_token_jwt:
                v = _inner(raw)
                if isinstance(v, str):
                    session_token_jwt = v
            elif name in ("agent-user-token", "agent_user_token", "agent-api-token"):
                obj = _inner(raw)
                if isinstance(obj, dict) and isinstance(obj.get("token"), str):
                    agent_jwt = obj["token"]

    out: dict[str, str] = {}
    for jwt in (session_token_jwt, agent_jwt):
        claims = _decode_jwt_claims(jwt) if jwt else {}
        if not claims:
            continue
        for name in _R167_ID_CLAIMS:
            v = claims.get(name)
            if isinstance(v, (str, int)) and str(v) and str(v).lower() != "none":
                out[name] = str(v)
        # organizations[0] → organization_id
        orgs = claims.get("organizations")
        if isinstance(orgs, list) and orgs and isinstance(orgs[0], (str, int)):
            out.setdefault("organization_id", str(orgs[0]))
            out["organization_id"] = str(orgs[0]) if jwt is agent_jwt else out["organization_id"]
    # account_id falls back to root_account_id when the token only carries the
    if "account_id" not in out and out.get("root_account_id"):
        out["account_id"] = out["root_account_id"]
    # only root_account_id, but the agent-user-token JWT (scanned second, higher
    # precedence) can carry a DIFFERENT account_id minted for another context
    # root_account_id=`955934e1…`). The SUT's path-scoped reads use the ROOT
    # account — HAR evidence: 48 of the SUT's own 200 requests use `955934e1…`,
    # ZERO use `0aee6bd7…`. So when both are present and differ, the root account
    # is the path-correct one; the agent-token account_id 500s on cm/collection
    # reads (a previous-session/other-context account). Only fires for tokens
    # Killswitch ARTA_R217_ACCOUNT_ROOT_OVERRIDE_DISABLE=1.
    if (os.environ.get("ARTA_R217_ACCOUNT_ROOT_OVERRIDE_DISABLE") != "1"
            and out.get("root_account_id")
            and out.get("account_id") != out["root_account_id"]):
        out["account_id"] = out["root_account_id"]
    return out


def harvest_auth_from_har(
    entries: list[dict],
    known_values: dict[str, str],
    *,
    min_value_len: int = 6,
) -> dict:
    """R159 Layer 1 (PRIMARY) — derive per-host auth from OBSERVED traffic.

    For each HAR request carrying an `Authorization` header, generalize the
    value into a `{claim}`-template by substituting known session values
    (longest-first so `{session_token}` wins over a substring), and capture any opaque
    Bearer token that maps to NO known value (e.g. a minted `agent_api_token`
    record id) keyed by host — so the agent-token families can REUSE the real
    token observed in traffic instead of re-implementing the mint flow.

    `known_values`: {claim_name: concrete_value} from the session — session_token,
    organization_id, subscriber_id, subscription_id, account_id, user_id, …

    Returns:
      {
        "host_auth": {host: {"scheme","template","count"}},  # → AuthChain rules
        "harvested_tokens": {host: "<opaque bearer observed>"},  # → reuse
      }
    Ground truth + SUT-agnostic: this is what the working client actually sent.
    """
    from urllib.parse import urlparse
    kv = sorted(
        ((v, k) for k, v in (known_values or {}).items()
         if isinstance(v, str) and len(v) >= min_value_len),
        key=lambda x: -len(x[0]),
    )
    known_set = {v for v, _ in kv}
    host_auth: dict[str, dict] = {}
    harvested: dict[str, str] = {}
    for e in entries or []:
        req = e.get("request", {}) if isinstance(e, dict) else {}
        url = req.get("url", "")
        host = urlparse(url).netloc
        if not host:
            continue
        hdrs = {h.get("name", "").lower(): h.get("value", "")
                for h in req.get("headers", []) if isinstance(h, dict)}
        auth = hdrs.get("authorization", "")
        if not auth:
            continue
        template = auth
        for val, name in kv:
            if val and val in template:
                template = template.replace(val, "{" + name + "}")
        ent = host_auth.setdefault(host, {"scheme": SCHEME_BEARER, "template": None, "count": 0})
        ent["count"] += 1
        if ent["template"] is None:
            ent["template"] = template
        # Opaque Bearer that contains NO known value → a reusable minted token
        # (agent_api_token). Record it so analytics/extraction can use the real
        # observed Bearer rather than re-deriving it.
        if auth.lower().startswith("bearer ") and "{" not in template:
            tok = auth[7:].strip()
            if tok and tok not in known_set and len(tok) >= min_value_len:
                harvested.setdefault(host, tok)
    return {"host_auth": host_auth, "harvested_tokens": harvested}


_ID_SEG_RE = re.compile(r"^([0-9a-fA-F]{8,}|[0-9a-fA-F-]{16,}|\d{6,})$")


def derive_auth_chain_from_har(entries, known_values, *, min_value_len: int = 6) -> dict:
    """R213.J Part 2 — build a full `{chain, host_map}` from OBSERVED traffic so
    a NEW SUT needs NO hand-written auth chain. For each request carrying an
    Authorization header: bucket by (host, stable-path-prefix), generalize the
    auth into a `{claim}`-template (longest-known-value first), and emit a rule
    `{match: prefix, scheme, value_template, host: slug}` + `host_map[slug]`.
    The prefix is the FIRST stable (non-id-shaped) path segment — id-shaped
    segments (uuids / long hex / long digit runs) are skipped so prefixes
    generalize across runs. Most-frequent template per (host,prefix) wins. A
    `*` catch-all is appended from the single most-common template.

    Rule ORDER is irrelevant downstream — `AuthChain.best_rule` (R213.J) selects
    by specificity, not order. SUT-agnostic: ground truth is what the working
    client actually sent. Returns {} when no authed traffic was observed (caller
    keeps its existing chain / the built-in fallback).
    """
    from urllib.parse import urlparse
    kv = sorted(
        ((v, k) for k, v in (known_values or {}).items()
         if isinstance(v, str) and len(v) >= min_value_len),
        key=lambda x: -len(x[0]),
    )

    def _stable_prefix(path: str) -> str:
        segs = [s for s in (path or "").split("/") if s]
        for s in segs:
            if not _ID_SEG_RE.match(s):
                return "/" + s.lower()
        return "/"

    def _slug(host: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (host or "").split(".")[0].lower()) or "host"

    # buckets[(host, prefix)][template] = count ; host_scheme[host]=scheme
    buckets: dict[tuple, dict] = {}
    for e in entries or []:
        req = e.get("request", {}) if isinstance(e, dict) else {}
        pu = urlparse(req.get("url", ""))
        host = pu.netloc
        if not host:
            continue
        hdrs = {h.get("name", "").lower(): h.get("value", "")
                for h in req.get("headers", []) if isinstance(h, dict)}
        auth = hdrs.get("authorization", "")
        if not auth or not auth.lower().startswith("bearer "):
            continue
        template = auth[7:].strip()
        for val, name in kv:
            if val and val in template:
                template = template.replace(val, "{" + name + "}")
        key = (host, _stable_prefix(pu.path))
        buckets.setdefault(key, {})
        buckets[key][template] = buckets[key].get(template, 0) + 1

    if not buckets:
        return {}

    host_map: dict[str, str] = {}
    rules: list[dict] = []
    for (host, prefix), tcounts in buckets.items():
        slug = _slug(host)
        host_map[slug] = "https://%s" % host
        template = max(tcounts.items(), key=lambda kv: kv[1])[0]
        rules.append({
            "match": prefix, "scheme": SCHEME_BEARER,
            "value_template": template, "host": slug,
            "_count": sum(tcounts.values()),
        })
    rules.sort(key=lambda r: -r["_count"])
    catch_all_tmpl = rules[0]["value_template"]
    for r in rules:
        r.pop("_count", None)
    # collapse duplicate (match,template,host) rules; append a catch-all
    seen = set()
    deduped = []
    for r in rules:
        sig = (r["match"], r["value_template"], r["host"])
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(r)
    if not any(r["match"] == "*" for r in deduped):
        deduped.append({"match": "*", "scheme": SCHEME_BEARER, "value_template": catch_all_tmpl})
    return {"chain": deduped, "host_map": host_map}


def auth_for_path(
    path: str,
    *,
    chain: "AuthChain | None",
    tokens: dict[str, str],
) -> dict:
    """Resolve the auth for `path` using `chain` + available `tokens`
    (e.g. {session_token, agent_api_token, organization_id, cookie_value}).

    R213.J — selects the MOST-SPECIFIC matching rule (`AuthChain.best_rule`),
    NOT the first in declaration order, so routing is correct regardless of how
    the operator (or auto-derivation) ordered the rules.

    Returns a dict:
      {"scheme", "header_name"?, "header_value"?, "cookie_name"?, "cookie_value"?,
       "host"?, "rule", "resolved": bool}
    `host` is the matched rule's family key (None when the rule has none) — so
    callers resolve host + auth from ONE match. `resolved=False` means the
    matching rule's template had a missing token (caller should fall back to the
    legacy single Bearer). An empty/None chain or no match → scheme=none /
    resolved=False so callers keep legacy behavior.
    """
    if chain is None or chain.is_empty():
        return {"scheme": SCHEME_NONE, "resolved": False, "rule": None, "host": None, "cookies": []}
    rule = chain.best_rule(path)
    if rule is None:
        return {"scheme": SCHEME_NONE, "resolved": False, "rule": None, "host": None, "cookies": []}

    # A1 (R218) — resolve the rule's ADDITIVE cookies (composite auth). Each is
    # emitted only when its template fully interpolates; an unresolvable additive
    # cookie marks the whole result unresolved (the caller must not ship a
    # analytics just as the bare agent token did).
    extra_cookies: list[dict] = []
    cookies_ok = True
    for _c in (rule.cookies or []):
        cv = _interpolate(_c.get("value_template") or "{session_token}", tokens)
        if cv:
            extra_cookies.append({"name": _c["name"], "value": cv})
        else:
            cookies_ok = False

    if rule.scheme == SCHEME_NONE:
        return {"scheme": SCHEME_NONE, "resolved": cookies_ok, "rule": rule.match,
                "host": rule.host, "cookies": extra_cookies}
    if rule.scheme == SCHEME_COOKIE:
        cv = tokens.get("session_token") or tokens.get("cookie_value")
        return {
            "scheme": SCHEME_COOKIE,
            "cookie_name": rule.cookie_name,
            "cookie_value": cv,
            "resolved": bool(cv) and cookies_ok,
            "rule": rule.match,
            "host": rule.host,
            "cookies": extra_cookies,
        }
    # bearer (+ optional additive cookies = the composite)
    value = _interpolate(rule.value_template, tokens)
    return {
        "scheme": SCHEME_BEARER,
        "header_name": "Authorization",
        "header_value": f"Bearer {value}" if value else None,
        "resolved": value is not None and cookies_ok,
        "rule": rule.match,
        "host": rule.host,
        "cookies": extra_cookies,
    }
