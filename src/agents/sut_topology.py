"""R159 — SUT API topology discovery (per-family host map) from runtime config.

Layer 2b of the API Topology & Auth Resolver. Many SPAs split their API across
multiple backend hosts (auth / analytics / extraction / monitoring / …), each
with its own base URL injected at deploy time. ARTA historically assumed ONE
host (`api_base_url`) for all calls — so requests to the wrong-family host 404
(live evidence: `/svc/*` + `/extraction/*` returned 404 on
app.example.internal because they actually live on api.example.internal).

SUTs expose this differently — there's no single convention — so this module
tries the common runtime-config shapes, deterministically:
  - `window.__ENV__ = {...}`  (runtime env-config script — React/Vite/Angular)
  - `window._env_ = {...}`
  - bare `KEY: "https://..."` pairs in a `config.js` / `env-config.js`

`parse_runtime_config()` is pure + deterministic (no network). `build_host_map()`
classifies the URL-valued keys into API families by keyword. Secrets in the
config (DB URIs, API keys) are NEVER returned — only http(s) URL values are kept.
This composes with the frontend axios-interceptor scheme map (Layer 2a) and the
HAR-observed map (Layer 1, primary) in the resolver.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("arta.sut_topology")

# Keyword → canonical API family. Ordered: first hit wins. Keys are matched
# case-insensitively against the config var name. SUT-agnostic vocabulary.
_FAMILY_KEYWORDS = [
    ("auth", "auth"),
    ("analytic", "analytics"),
    ("extraction", "extraction"),
    ("monitor", "monitoring"),
    ("stripe", "stripe"),
    ("mcp", "mcp"),
    ("backend", "backend"),
    ("api", "api"),
]

_URL_RE = re.compile(r"^https?://", re.I)


def parse_runtime_config(text: str) -> dict[str, str]:
    """Extract a {KEY: value} map from a runtime-config script. Handles
    `window.__ENV__ = {json}` / `window._env_ = {json}` (parsed as JSON) and,
    failing that, bare `KEY: "value"` / `"KEY": "value"` pairs via regex.
    Returns only string values (caller filters for URLs). Empty on no match."""
    if not text:
        return {}
    # 1. window.__ENV__ / window._env_ = { ... }  → JSON object
    m = re.search(r"window\.(?:__ENV__|_env_|ENV)\s*=\s*(\{.*?\})\s*;?", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return {str(k): v for k, v in obj.items() if isinstance(v, str)}
        except Exception:
            pass  # fall through to regex
    # 2. bare KEY: "value" / "KEY": "value" pairs
    out: dict[str, str] = {}
    for km in re.finditer(r'["\']?([A-Z][A-Z0-9_]{2,})["\']?\s*[:=]\s*["\']([^"\']+)["\']', text):
        out.setdefault(km.group(1), km.group(2))
    return out


def build_host_map(env_config: dict[str, str]) -> dict[str, str]:
    """Classify URL-valued config keys into API families by keyword.

    Returns {family: host_base_url}. Only http(s) values are considered (so DB
    URIs / API keys / secrets are excluded). When several keys map to the same
    family, the first by config order wins. SUT-agnostic.
    """
    # R216 — the generic `api` family keyword is over-broad: a config key like
    # `api_base_url` just means "the API base" (usually == base_url), NOT a
    # family-specific host like `analytics_base_url`/`extraction_base_url`.
    # Mapping the `/api/v1/*` PATH-family to `api_base_url` mis-routed those
    # requests to a backend host that 404s them, while they actually live on
    # spec_drift_404 blocks). Dropping the generic `api` family lets `/api/v1/*`
    # fall back to base_url (the default host). Specific families
    # (auth/analytics/extraction/monitoring/stripe/mcp/backend) are unaffected.
    # Killswitch ARTA_R216_KEEP_API_FAMILY=1 → pre-R216 behavior.
    import os as _os_r216
    _keep_api = _os_r216.environ.get("ARTA_R216_KEEP_API_FAMILY", "").lower() in ("1", "true")
    host_map: dict[str, str] = {}
    for key, val in (env_config or {}).items():
        if not isinstance(val, str) or not _URL_RE.match(val):
            continue
        kl = key.lower()
        for needle, family in _FAMILY_KEYWORDS:
            if needle in kl:
                if family == "api" and not _keep_api:
                    break  # generic api base → let paths fall back to base_url
                host_map.setdefault(family, val.rstrip("/"))
                break
    return host_map


async def discover_runtime_config(base_url: str, *, client=None) -> dict[str, str]:
    """Fetch the SPA's runtime config from `base_url` and parse it.

    Tries the index.html-referenced config script first, then common fallback
    paths. Best-effort: returns {} on any failure (caller degrades to Layer 2a
    source-parse / operator config). Reuses a caller-provided httpx client when
    given, else opens its own.
    """
    import httpx

    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=15, follow_redirects=True, verify=False)
    base = (base_url or "").rstrip("/")
    try:
        candidates: list[str] = []
        try:
            idx = (await client.get(base + "/")).text
            for src in re.findall(r'<script[^>]+src="([^"]+)"', idx):
                if any(k in src.lower() for k in ("env", "config", "runtime")):
                    candidates.append(src if src.startswith("http") else base + "/" + src.lstrip("/"))
        except Exception as exc:
            log.debug("R159: index fetch failed for %s: %s", base, exc)
        candidates += [base + p for p in
                       ("/config.js", "/env-config.js", "/env.js",
                        "/assets/env-config.js", "/static/env-config.js")]
        seen: set[str] = set()
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            try:
                r = await client.get(url)
            except Exception:
                continue
            if r.status_code != 200 or not r.text:
                continue
            cfg = parse_runtime_config(r.text)
            if cfg:
                log.info("R159: parsed runtime config from %s (%d keys)", url, len(cfg))
                return cfg
        return {}
    finally:
        if own:
            await client.aclose()


async def discover_host_map(base_url: str, *, client=None) -> dict[str, str]:
    """Convenience: runtime config → family→host map. {} on failure."""
    return build_host_map(await discover_runtime_config(base_url, client=client))


# ── R159 — unified resolver + Newman wiring ──────────────────────────────────

def resolve_api_topology(
    *,
    har_auth: dict | None = None,
    runtime_host_map: dict[str, str] | None = None,
    source_chain: list[dict] | None = None,
    operator: dict | None = None,
) -> dict:
    """Fuse the discovery layers into one topology, highest-trust first:

      Layer 1 (har_auth from `auth_chain.harvest_auth_from_har`) — OBSERVED,
        gives per-host auth templates + harvested opaque tokens. Wins.
      Layer 2b (runtime_host_map from `build_host_map`) — family→host.
      Layer 2a (source_chain — axios-interceptor-derived AuthRule dicts).
      Layer 3 (operator — explicit {host_map, chain} override) — wins outright
        when present (operator knows best for the un-discoverable).

    Returns {"host_map": {family|host: url}, "auth_chain": [rule dicts],
             "harvested_tokens": {host: token}}. Each layer is optional; an
    empty result reproduces legacy single-token behavior downstream.
    """
    op = operator or {}
    # Operator override wins outright when it supplies both.
    host_map: dict[str, str] = dict(runtime_host_map or {})
    if op.get("host_map"):
        host_map.update(op["host_map"])  # operator entries win
    # auth chain: source (2a) as base, operator override on top.
    chain: list[dict] = list(source_chain or [])
    if op.get("chain"):
        chain = list(op["chain"])
    harvested = dict((har_auth or {}).get("harvested_tokens") or {})
    return {"host_map": host_map, "auth_chain": chain, "harvested_tokens": harvested}


def build_newman_prerequest(host_map: dict[str, str], auth_chain: list[dict]) -> str:
    """Emit a Newman/Postman collection-level pre-request script (JS) that, per
    request, (1) rewrites the URL host to the family host and (2) sets the
    Authorization header from the per-family rule — using collection variables
    the session token, `agent_api_token`, `organization_id`, etc. injected at dispatch.

    This is the DISPATCH-TIME 'both' option: works on existing collections, one
    runtime source of truth. The rules/host_map are serialized into the script
    as data, so the same generator serves any SUT.
    """
    import json as _json
    import os as _os
    hm = _json.dumps(host_map or {})
    rules = _json.dumps(auth_chain or [])
    # R213.J — pick the MOST-SPECIFIC rule (anchored-prefix > longest-substring
    # > `*`), NOT first-match, so routing is order-independent (parity with the
    # Python AuthChain.best_rule). Gen-time killswitch ARTA_AUTH_SPECIFICITY_DISABLE=1
    # emits the legacy first-match selector.
    if _os.environ.get("ARTA_AUTH_SPECIFICITY_DISABLE") == "1":
        selector = (
            "function bestRule(path){for(var i=0;i<RULES.length;i++){var r=RULES[i];\n"
            "  if(r.match==='*'||(path||'').toLowerCase().indexOf((r.match||'').toLowerCase())>=0)return r;}\n"
            "  return null;}\n"
        )
    else:
        selector = (
            "function spec(r,path){if(r.match==='*')return [0,0];\n"
            "  var m=(r.match||'').toLowerCase();var p=(path||'').toLowerCase();\n"
            "  if(!m||p.indexOf(m)<0)return null;\n"
            "  if(m.charAt(0)==='/'&&p.indexOf(m)===0){var nx=p.charAt(m.length);\n"
            "    if(nx===''||nx==='/'||nx==='?')return [2,m.length];}\n"
            "  return [1,m.length];}\n"
            "function bestRule(path){var best=null,bs=null;\n"
            "  for(var i=0;i<RULES.length;i++){var r=RULES[i];var sc=spec(r,path);\n"
            "    if(sc===null)continue;\n"
            "    if(bs===null||sc[0]>bs[0]||(sc[0]===bs[0]&&sc[1]>bs[1])){best=r;bs=sc;}}\n"
            "  return best;}\n"
        )
    # NOTE: kept dependency-free (no template literals that clash with {{ }}).
    return (
        "// R159/R213.J per-family auth+host (ARTA-injected, specificity-matched)\n"
        "var HOST_MAP = " + hm + ";\n"
        "var RULES = " + rules + ";\n"
        "function tok(n){return pm.variables.get(n)||pm.environment.get(n)||'';}\n"
        + selector +
        "var url = pm.request.url.toString();\n"
        "var path = url.replace(/^https?:\\/\\/[^\\/]+/, '');\n"
        "var r = bestRule(path);\n"
        "if (r){\n"
        "  // host rewrite: prefer explicit r.host family key, else derive from match\n"
        "  var fam=r.host || r.match.replace(/[^a-z]/g,'');\n"
        "  if (HOST_MAP[fam]){ try{ var u=new URL(url); var h=new URL(HOST_MAP[fam]);\n"
        "    u.protocol=h.protocol; u.host=h.host;\n"
        "    // when the mapped host carries a base path, prepend it once\n"
        "    var bp=h.pathname.replace(/\\/$/,'');\n"
        "    if(bp && bp!=='' && u.pathname.indexOf(bp)!==0){ u.pathname=bp+u.pathname; }\n"
        "    pm.request.url=u.toString(); }catch(e){} }\n"
        "  if (r.scheme==='none'){ pm.request.headers.remove('Authorization'); }\n"
        "  else if (r.scheme==='cookie'){ }\n"
        "  else { var v=r.value_template; var ok=true;\n"
        "    v=v.replace(/\\{(\\w+)\\}/g,function(m,n){var t=tok(n); if(!t)ok=false; return t;});\n"
        "    if(ok){ pm.request.headers.upsert({key:'Authorization', value:'Bearer '+v.replace(/^Bearer /,'')}); } }\n"
        "  // A1 (R218) — additive cookies for composite auth (Bearer agent + session cookie).\n"
        "  var cks=r.cookies||[]; var cp=[]; var ckok=true;\n"
        "  for(var ci=0;ci<cks.length;ci++){ var cv=(cks[ci].value_template||'{session_token}').replace(/\\{(\\w+)\\}/g,function(m,n){var t=tok(n); if(!t)ckok=false; return t;}); if(ckok)cp.push(cks[ci].name+'='+cv); }\n"
        "  if(cp.length && ckok){ var ex=pm.request.headers.get('Cookie'); pm.request.headers.upsert({key:'Cookie', value:(ex?ex+'; ':'')+cp.join('; ')}); }\n"
        "}\n"
    )


# ── R160.F — OpenAPI contract discovery (authoritative endpoint surface) ──────

def parse_openapi_spec(spec: dict, *, base_prefix: str = "") -> list[dict]:
    """Extract endpoints from an OpenAPI/Swagger spec — the SUT's AUTHORITATIVE
    contract (real paths + methods + REQUIRED query params). Far more reliable
    than runtime capture (which needs interaction) or source-repo route grep.

    `base_prefix` is the service mount (e.g. `/svc/analytics/v1`) prepended to
    each spec path. Returns `[{method, path, query_params:[{name,required}]}]`.
    Deterministic. SUT-agnostic (any OpenAPI 2/3 spec).
    """
    out: list[dict] = []
    bp = (base_prefix or "").rstrip("/")
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        full = bp + path
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete", "head"):
                continue
            params = (op.get("parameters") or []) if isinstance(op, dict) else []
            qp = [
                {"name": p.get("name"), "required": bool(p.get("required")),
                 "type": (p.get("schema") or {}).get("type")}
                for p in params
                if isinstance(p, dict) and p.get("in") == "query" and p.get("name")
            ]
            out.append({"method": method.upper(), "path": full, "query_params": qp})
    return out


async def fetch_openapi_endpoints(base_url: str, *, client=None) -> list[dict]:
    """Fetch + parse a service's OpenAPI spec from common locations
    (`/openapi.json`, `/swagger.json`, `/v3/api-docs`). `base_url` is the full
    service base (e.g. `https://api.example.internal/svc/analytics/v1`); its
    path becomes the endpoint prefix. Returns `[{method, path, query_params}]`
    or [] (graceful). Best-effort; no auth needed for the spec itself."""
    import httpx
    from urllib.parse import urlparse
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=15, follow_redirects=True, verify=False)
    base = (base_url or "").rstrip("/")
    prefix = urlparse(base).path
    try:
        for loc in ("/openapi.json", "/swagger.json", "/v3/api-docs", "/openapi.yaml"):
            try:
                r = await client.get(base + loc)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            try:
                spec = r.json()
            except Exception:
                continue
            eps = parse_openapi_spec(spec, base_prefix=prefix)
            if eps:
                log.info("R160.F: %d endpoints from OpenAPI at %s", len(eps), base + loc)
                return eps
        return []
    finally:
        if own:
            await client.aclose()
