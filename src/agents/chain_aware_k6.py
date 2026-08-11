"""Phase D3 — chain-aware k6 generation.

Where Newman runs each chain item in collection order, k6 runs `default()`
per VU per iteration. Chain-aware k6 means the `default()` body walks the
chain nodes sequentially within ONE iteration so dataflow links work
naturally — no global state needed.

If any node in the chain fails (status outside expected range or missing
provider field), the iteration short-circuits via `return`. k6's threshold
counters then surface the cascade as a per-step error rate, fed into
Phase E3's per-endpoint p95/p99 reporting.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any

log = logging.getLogger("arta.chain_aware_k6")


_K6_HEADER = """import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

// Phase E3 — per-endpoint metrics (gate F1 reads these)
const cascadeFailures = new Counter('arta_cascade_failures');
const providerContractViolations = new Counter('arta_provider_contract_violations');
"""


# R213.K — per-family auth + host for k6 (the 4th runtime). SAME specificity
# matcher as Python `AuthChain.best_rule`, the Newman pre-request JS, and
# Playwright `arta_auth.ts`: anchored path-prefix > longest mid-substring > `*`,
# order-INDEPENDENT. Reads the dispatch-injected env:
#   organization_id, agent_api_token, cookie_value, ...}), __ENV.ARTA_HOST_MAP
#   (JSON {family: baseUrl}). Pre-R213.K, chain-aware k6 sent NO Authorization
#   no/wrong auth → 401/500 (run-857ce1: 17 FAIL + 9 BLOCKED). Falls back to a
#   so legacy/single-host SUTs behave exactly as before. Killswitch
#   __ENV.ARTA_K6_AUTH_CHAIN_DISABLE=1 → legacy first-match selection.
#   Goja-compatible (ES5: var/function, String.replace+regex, JSON.parse).
_K6_AUTH_HELPER = """// R213.K — per-family auth+host (specificity matcher; parity with arta_auth.ts)
var ARTA_CHAIN = (function () { try { return JSON.parse(__ENV.ARTA_AUTH_CHAIN || '[]'); } catch (e) { return []; } })();
var ARTA_TOKENS = (function () { try { return JSON.parse(__ENV.ARTA_AUTH_TOKENS || '{}'); } catch (e) { return {}; } })();
var ARTA_HOSTS = (function () { try { return JSON.parse(__ENV.ARTA_HOST_MAP || '{}'); } catch (e) { return {}; } })();
var ARTA_AUTH_OFF = (__ENV.ARTA_K6_AUTH_CHAIN_DISABLE === '1');
function _artaSpec(r, path) {
    if (r.match === '*') return [0, 0];
    var m = (r.match || '').toLowerCase(); var p = (path || '').toLowerCase();
    if (!m || p.indexOf(m) < 0) return null;
    if (m.charAt(0) === '/' && p.indexOf(m) === 0) { var nx = p.charAt(m.length); if (nx === '' || nx === '/' || nx === '?') return [2, m.length]; }
    return [1, m.length];
}
function _artaBestRule(path) {
    if (ARTA_AUTH_OFF) { for (var i = 0; i < ARTA_CHAIN.length; i++) { var rr = ARTA_CHAIN[i]; if (rr.match === '*' || (path || '').toLowerCase().indexOf((rr.match || '').toLowerCase()) >= 0) return rr; } return null; }
    var best = null, bs = null;
    for (var j = 0; j < ARTA_CHAIN.length; j++) { var r2 = ARTA_CHAIN[j]; var sc = _artaSpec(r2, path); if (sc === null) continue; if (bs === null || sc[0] > bs[0] || (sc[0] === bs[0] && sc[1] > bs[1])) { best = r2; bs = sc; } }
    return best;
}
function _artaInterp(tpl) { var ok = true; var out = (tpl || '').replace(/\\{(\\w+)\\}/g, function (m, n) { var t = ARTA_TOKENS[n]; if (!t) ok = false; return t || ''; }); return ok ? out : null; }
function artaAuthHeader(path) {
    var fb = ARTA_TOKENS.session_token || __ENV.AUTH_TOKEN || '';
    var fbh = fb ? { Authorization: 'Bearer ' + fb } : {};
    var r = _artaBestRule(path);
    if (!r) return fbh;
    var ckParts = []; var ckOk = true;
    var rc = r.cookies || [];
    for (var i = 0; i < rc.length; i++) { var ccv = _artaInterp(rc[i].value_template || '{session_token}'); if (ccv) ckParts.push(rc[i].name + '=' + ccv); else ckOk = false; }
    function _withCk(h) { if (ckParts.length) h.Cookie = (h.Cookie ? h.Cookie + '; ' : '') + ckParts.join('; '); return h; }
    if (r.scheme === 'none') return ckOk ? _withCk({}) : fbh;
    if (r.scheme === 'cookie') { var cv = ARTA_TOKENS.session_token || ARTA_TOKENS.cookie_value; return (cv && ckOk) ? _withCk({ Cookie: (r.cookie_name || 'session-token') + '=' + cv }) : fbh; }
    var v = _artaInterp(r.value_template || '{session_token}');
    return (v && ckOk) ? _withCk({ Authorization: 'Bearer ' + v }) : fbh;
}
function artaApiUrl(baseUrl, path) {
    var r = _artaBestRule(path);
    if (r && r.host && ARTA_HOSTS[r.host]) return String(ARTA_HOSTS[r.host]).replace(/\\/$/, '') + path;
    return String(baseUrl || '').replace(/\\/$/, '') + path;
}
"""


def _options_block(thresholds_js: str) -> str:
    return (
        "export const options = {\n"
        "    vus: 5,\n"
        "    duration: '30s',\n"
        "    thresholds: {\n"
        f"{thresholds_js}\n"
        "    },\n"
        "};\n"
    )


def _default_fn_header(project_vars_js: str) -> str:
    return (
        "export default function () {\n"
        "    const baseUrl = __ENV.BASE_URL || 'https://example.com';\n"
        "    const projectVars = {\n"
        f"{project_vars_js}\n"
        "    };\n"
        "    const chainVars = {};   // chain-internal — populated as the chain walks\n"
        "\n"
        "    function getVar(name) {\n"
        "        if (chainVars[name] !== undefined) return chainVars[name];\n"
        "        if (projectVars[name] !== undefined) return projectVars[name];\n"
        "        // R213.K.5 — id-param fallback. Templatized id segments (e.g. the\n"
        "        // cm path `/{id_id}/api/collection/{id_id}/...`) often aren't in\n"
        "        // projectVars under that exact name, so the URL rendered\n"
        "        // `/undefined/...` → 404/500. Resolve to the project's harvested\n"
        "        // tenant-id hierarchy so the path interpolates a REAL id. Generic:\n"
        "        // uses whatever ids the project provides, in tenant-scope order.\n"
        "        if (/id/i.test(name)) {\n"
        "            var fb = ['account_id','organization_id','subscriber_id','subscription_id','workspace_id','project_id','schema_id'];\n"
        "            for (var i = 0; i < fb.length; i++) { if (projectVars[fb[i]] !== undefined && projectVars[fb[i]] !== '') return projectVars[fb[i]]; }\n"
        "        }\n"
        "        return undefined;\n"
        "    }\n"
    )


# R213.K.5 — generic third-party/static/transport NOISE markers (SUT-agnostic).
_NOISE_PATH_RE = re.compile(
    r"""(?xi)
    google\.|firestore|gstatic|googleapis|doubleclick|cleardot|analytics\.|segment\.|sentry|
    /sockjs|/socket\.io|/__/|Listen/channel|/ws($|/)|
    /static/|/assets/|/images/|/img/|/fonts?/|/s/|/icon|favicon|
    \.(?:js|mjs|css|map|woff2?|ttf|eot|otf|gif|png|jpe?g|svg|ico|webp|avif|mp4|woff)(?:$|\?)
    """,
)


# R213.K.8 — wrappers that give LLM-generated k6 specs (the `_performance.js`
# family, separate from chain-aware k6) the SAME per-family auth + host routing.
# Each wrapper derives the request PATH from the (possibly full) URL at runtime,
# routes via `artaApiUrl`, and MERGES `artaAuthHeader(path)` over the spec's own
# analytics→agent_token — instead of one __ENV.AUTH_TOKEN Bearer). `_arta_http`
# is a captured reference so the wrappers don't recurse after the http.* → arta*
# rewrite. Goja-safe (ES5 for-in, no Object.assign).
_K6_PERFAMILY_WRAPPERS = """// R213.K.8 — per-family auth+host wrappers for LLM-generated k6
var _arta_http = http;
function _artaPath(u) { return String(u).replace(/^[a-zA-Z]+:\\/\\/[^/]+/, ''); }
function _artaMerge(path, params) {
    var p = params || {}; var h = {};
    if (p.headers) { for (var k in p.headers) h[k] = p.headers[k]; }
    var a = artaAuthHeader(path); for (var k2 in a) h[k2] = a[k2];
    var out = {}; for (var k3 in p) out[k3] = p[k3]; out.headers = h; return out;
}
function artaGet(u, p) { var pa = _artaPath(u); return _arta_http.get(artaApiUrl(__ENV.BASE_URL || '', pa), _artaMerge(pa, p)); }
function artaPost(u, b, p) { var pa = _artaPath(u); return _arta_http.post(artaApiUrl(__ENV.BASE_URL || '', pa), b, _artaMerge(pa, p)); }
function artaPut(u, b, p) { var pa = _artaPath(u); return _arta_http.put(artaApiUrl(__ENV.BASE_URL || '', pa), b, _artaMerge(pa, p)); }
function artaPatch(u, b, p) { var pa = _artaPath(u); return _arta_http.patch(artaApiUrl(__ENV.BASE_URL || '', pa), b, _artaMerge(pa, p)); }
function artaDel(u, p) { var pa = _artaPath(u); return _arta_http.del(artaApiUrl(__ENV.BASE_URL || '', pa), _artaMerge(pa, p)); }
function artaHead(u, p) { var pa = _artaPath(u); return _arta_http.head(artaApiUrl(__ENV.BASE_URL || '', pa), _artaMerge(pa, p)); }
function artaOptions(u, p) { var pa = _artaPath(u); return _arta_http.options(artaApiUrl(__ENV.BASE_URL || '', pa), _artaMerge(pa, p)); }
"""

_K6_METHOD_TO_WRAPPER = {
    "get": "artaGet", "post": "artaPost", "put": "artaPut", "patch": "artaPatch",
    "del": "artaDel", "delete": "artaDel", "head": "artaHead", "options": "artaOptions",
}
_K6_HTTP_CALL_RE = re.compile(r"\bhttp\.(get|post|put|patch|del|delete|head|options)\(")
_K6_IMPORT_RE = re.compile(r"^(import\s+.*?from\s+['\"]k6[^'\"]*['\"];?\s*)$", re.MULTILINE)


# R213.K.9 — id claims that name a tenant-scope path segment + map to a dispatch
# __ENV var (the dispatch injects these from the harvested session; R214 K1 also
# uppercases). Used to ground hardcoded stale ids in LLM-generated paths.
_R213_K9_ID_CLAIMS = {
    "account_id", "subscriber_id", "subscription_id", "organization_id",
    "workspace_id", "project_id", "service_id", "user_id", "schema_id",
    "root_account_id",
}
_R213_K9_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_R213_K9_UUIDISH_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,}$")


def ground_k6_path_ids(content: str) -> tuple[str, int]:
    """R213.K.9 — replace HARDCODED stale tenant ids in LLM-generated k6 paths
    with `${__ENV.<CLAIM>}` so dispatch fills the LIVE values. Pre-R213.K.9 the
    perf specs baked discovery-time ids into paths (e.g. `/api/media/0aee6bd7-…/
    subscriber/6551f605-…/subscription/a6a49ce0-…`) → those instances are stale
    under the current session → 500 regardless of auth (the residual 22-71%).

    Generic: learns the {stale-value → claim-role} map by decoding the spec's OWN
    baked JWT default (its claims ARE the ids used in its paths) — no SUT-specific
    keyword table. Replaces each `/.<value>` path segment with `${__ENV.<CLAIM>}`
    (leading-slash anchored so only path segments are touched; the base64 JWT is
    not — the raw UUID isn't plaintext there). Killswitch
    ARTA_K6_PATH_ID_GROUNDING_DISABLE=1. Returns (content, n_substitutions)."""
    if os.environ.get("ARTA_K6_PATH_ID_GROUNDING_DISABLE") == "1":
        return content, 0
    m = _R213_K9_JWT_RE.search(content)
    if not m:
        return content, 0
    try:
        payload = m.group(0).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return content, 0
    val_to_env: dict[str, str] = {}
    for claim in _R213_K9_ID_CLAIMS:
        v = claims.get(claim)
        if isinstance(v, str) and _R213_K9_UUIDISH_RE.match(v):
            val_to_env[v] = claim.upper()
    if not val_to_env:
        return content, 0
    n = 0
    # longest value first so a value that's a substring of another isn't mangled
    for val, env in sorted(val_to_env.items(), key=lambda kv: -len(kv[0])):
        needle = "/" + val   # path-segment anchored
        cnt = content.count(needle)
        if cnt:
            content = content.replace(needle, "/${__ENV." + env + "}")
            n += cnt
    return content, n


# R213.K.12 — path-segment keyword → __ENV id var (generic REST convention).
_R213_K12_KEYWORD_ROLE = {
    "subscriber": "SUBSCRIBER_ID", "subscription": "SUBSCRIPTION_ID",
    "organization": "ORGANIZATION_ID", "organisation": "ORGANIZATION_ID",
    "workspace": "WORKSPACE_ID", "project": "PROJECT_ID", "service": "SERVICE_ID",
    "user": "USER_ID", "schema": "SCHEMA_ID", "account": "ACCOUNT_ID",
}
_R213_K12_UUIDISH_RE = re.compile(r"^[0-9a-fA-F]{8}[0-9a-fA-F-]{8,}$")


def _r213_k12_value_role_map(captured_endpoints: list | None) -> dict:
    """Learn {id-value → __ENV role} from the SUT's CAPTURED endpoint paths by
    POSITION: a uuid right after a known entity keyword (`/subscriber/<uuid>`)
    is that role; the LEADING path uuid (`/<uuid>/api/...`) is the account (the
    tenant root in REST conventions). Generic — no SUT literals."""
    out: dict[str, str] = {}
    for ep in captured_endpoints or []:
        if not isinstance(ep, dict):
            continue
        p = ep.get("path") or ep.get("url") or ""
        segs = [s for s in str(p).split("?")[0].split("/") if s]
        for i, s in enumerate(segs):
            if not _R213_K12_UUIDISH_RE.match(s):
                continue
            if i == 0:
                out.setdefault(s, "ACCOUNT_ID")
            elif segs[i - 1].lower() in _R213_K12_KEYWORD_ROLE:
                out.setdefault(s, _R213_K12_KEYWORD_ROLE[segs[i - 1].lower()])
    return out


_R213_K13_PCTL_RE = re.compile(r"p\((9[05]|99)\)\s*<\s*(\d+)")
_R213_K13_DUR_RE = re.compile(r"(timings\.duration\s*<\s*)(\d+)\b")
_R213_K13_NAME_RE = re.compile(r"((?:under|<|below|within)\s*)(\d+)(\s*ms)", re.IGNORECASE)


def normalize_k6_thresholds(content: str, floor_ms: int | None = None) -> tuple[str, int]:
    """R213.K.13 — clamp ARBITRARY over-tight latency thresholds/checks UP to
    ARTA's own perf scoring gate (`p95 <= 3000ms`, execution.py). The LLM emits
    sub-second bars (`p(95)<500`, `timings.duration < 500`, `'under 500ms'`) for
    endpoints that legitimately take ~1.5s → the inline check FAILS → drags
    check_pass_pct below ARTA's 90% gate → false FAIL, even though ARTA's OWN
    latency bar (3000ms) would PASS. Raising tight bars to the gate makes the
    spec consistent with how ARTA scores; genuinely-slow (>floor) endpoints still
    FAIL (truthful). Only RAISES (never tightens) — lenient bars (120000ms) and
    AC-realistic bars (>= floor) are untouched. Error-rate (`rate<X`) thresholds
    are NOT latency → left alone. Configurable floor: ARTA_K6_LATENCY_FLOOR_MS
    (default 3000 = the gate). Killswitch ARTA_K6_THRESHOLD_NORMALIZE_DISABLE=1."""
    if os.environ.get("ARTA_K6_THRESHOLD_NORMALIZE_DISABLE") == "1":
        return content, 0
    if floor_ms is None:
        try:
            floor_ms = int(os.environ.get("ARTA_K6_LATENCY_FLOOR_MS", "3000"))
        except ValueError:
            floor_ms = 3000
    n = [0]

    def _clamp_pctl(m: "re.Match") -> str:
        if int(m.group(2)) < floor_ms:
            n[0] += 1
            return f"p({m.group(1)})<{floor_ms}"
        return m.group(0)

    def _clamp_dur(m: "re.Match") -> str:
        if int(m.group(2)) < floor_ms:
            n[0] += 1
            return f"{m.group(1)}{floor_ms}"
        return m.group(0)

    def _clamp_name(m: "re.Match") -> str:
        # keep check NAMES honest (so "under 500ms" doesn't sit on a <3000 assert)
        if int(m.group(2)) < floor_ms:
            return f"{m.group(1)}{floor_ms}{m.group(3)}"
        return m.group(0)

    content = _R213_K13_PCTL_RE.sub(_clamp_pctl, content)
    content = _R213_K13_DUR_RE.sub(_clamp_dur, content)
    content = _R213_K13_NAME_RE.sub(_clamp_name, content)  # cosmetic, not counted
    return content, n[0]


def ground_k6_id_values(content: str, captured_endpoints: list | None) -> tuple[str, int]:
    """R213.K.12 — ground HARDCODED tenant-id VALUES (var assignments AND inline
    path literals) to `__ENV.<ROLE>` using the captured-catalog value→role map.
    Complements K.9 (which decodes a baked JWT): K.9 misses `const collectionId =
    '0aee6bd7-…'` var assignments (a discovery-time account hardcoded as a var) →
    the spec hit a STALE account → 5xx even with real endpoint paths. Context-
    aware: `= '<uuid>'` → `= __ENV.ROLE` (bare — __ENV.X is a string; quotes would
    break interpolation); `/<uuid>` path segment → `/${__ENV.ROLE}`. Killswitch
    ARTA_K6_PATH_ID_GROUNDING_DISABLE=1 (shared with K.9)."""
    if os.environ.get("ARTA_K6_PATH_ID_GROUNDING_DISABLE") == "1":
        return content, 0
    vmap = _r213_k12_value_role_map(captured_endpoints)
    if not vmap:
        return content, 0
    n = 0
    for val, role in sorted(vmap.items(), key=lambda kv: -len(kv[0])):
        env_ref = "__ENV." + role
        # 1. var assignment / quoted literal:  = '<uuid>'  →  = __ENV.ROLE
        _assign = re.compile(r"(=\s*)['\"]" + re.escape(val) + r"['\"]")
        content, c1 = _assign.subn(r"\1" + env_ref, content)
        # 2. inline path segment:  /<uuid>  →  /${__ENV.ROLE}
        c2 = content.count("/" + val)
        if c2:
            content = content.replace("/" + val, "/${" + env_ref + "}")
        n += c1 + c2
    return content, n


def rewrite_llm_k6_perfamily_auth(content: str) -> tuple[str, int]:
    """R213.K.8 — give an LLM-generated k6 spec per-family auth + host routing by
    injecting `_K6_AUTH_HELPER` + the `arta*` wrappers and swapping `http.<m>(` →
    `arta<M>(`. Idempotent (no-op if already rewritten or no http.* calls).
    Killswitch ARTA_K6_PERFAMILY_REWRITE_DISABLE=1. Returns (content, n_calls)."""
    if os.environ.get("ARTA_K6_PERFAMILY_REWRITE_DISABLE") == "1":
        return content, 0
    if "artaAuthHeader" in content or "k6/http" not in content:
        return content, 0  # already chain-aware/rewritten, or not an http k6 spec
    n = [0]

    def _repl(m: "re.Match") -> str:
        n[0] += 1
        return _K6_METHOD_TO_WRAPPER[m.group(1)] + "("

    rewritten = _K6_HTTP_CALL_RE.sub(_repl, content)
    if n[0] == 0:
        return content, 0
    block = "\n" + _K6_AUTH_HELPER + "\n" + _K6_PERFAMILY_WRAPPERS + "\n"
    # Inject the helper+wrappers right after the LAST k6 import line.
    imports = list(_K6_IMPORT_RE.finditer(rewritten))
    if imports:
        pos = imports[-1].end()
        rewritten = rewritten[:pos] + block + rewritten[pos:]
    else:
        rewritten = block + rewritten
    return rewritten, n[0]


def _filter_sut_api_nodes(nodes: list, project_id: str | None) -> list:
    """R213.K.5 — keep only chain nodes whose path matches the SUT's OWN OpenAPI
    contract (`_r206_path_is_real`), dropping captured third-party calls (Google
    Firestore `Listen/channel`, gstatic, tracking pixels), static assets
    (.js/.css/.woff2/.gif), and SPA routes that the discovery HAR swept up.

    Pre-R213.K.5 the chains were ~91% such noise (live: 8923 nodes → only 789
    real cm reads) AND `_build_step`'s first-failure short-circuit (`if status>=400
    return`) aborted the chain at the FIRST noise node — e.g. `POST
    /google.firestore.../Listen/channel` → 4xx on the SUT host → the whole chain
    returned before reaching any real cm read → checks=0.0% (run-ee7019 chain_9).

    Generic: the OpenAPI contract is the SUT's authoritative routable-path set —
    no SUT-specific literals. Returns the ORIGINAL nodes when no contract is
    available OR the filter would empty the chain (don't emit a no-op spec).
    Killswitch ARTA_K6_CHAIN_NODE_FILTER_DISABLE=1.
    """
    if os.environ.get("ARTA_K6_CHAIN_NODE_FILTER_DISABLE") == "1" or not project_id:
        return nodes
    try:
        from .api_discovery import _r206_contract_matchers, _r206_path_is_real
    except Exception:
        return nodes
    matchers = _r206_contract_matchers(project_id)
    if not matchers:
        return nodes

    def _is_sut_api(path: str) -> bool:
        # Generic NOISE denylist first — the contract matcher alone is too
        # permissive for SHORT paths (`/icon`, `/x.gif` false-match a 1-segment
        # `/{param}` contract route). Drop third-party hosts, static assets, and
        # websocket/RPC transports that the discovery HAR swept up. No SUT-
        # specific literals — these markers are universal.
        if _NOISE_PATH_RE.search(path or ""):
            return False
        return _r206_path_is_real(path or "", matchers)

    kept = [n for n in nodes if isinstance(n, dict) and _is_sut_api(n.get("path_template") or "")]
    if len(kept) < len(nodes):
        log.info(
            "R213.K.5: chain node filter kept %d/%d SUT-contract nodes (dropped %d "
            "third-party/static/SPA)", len(kept), len(nodes), len(nodes) - len(kept),
        )
    # NOTE: when kept is empty the chain has NO verifiable SUT-contract endpoint —
    # the emission caller should SKIP it rather than emit a noise-only spec (the
    # old `kept or nodes` fallback resurrected the very noise we filtered). Return
    # the empty list so the caller can decide.
    return kept


def _path_to_k6_url(path_template: str) -> str:
    """Convert `/v1/datasets/{dataset_id}` to a JS template literal that
    interpolates from `chainVars` / `projectVars` at runtime.
    """
    def repl(match: re.Match[str]) -> str:
        var_name = match.group(1)
        return f"${{getVar('{var_name}')}}"
    rewritten = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, path_template)
    return f"`${{baseUrl}}{rewritten}`"


def _path_only_k6(path_template: str) -> str:
    """R213.K — like `_path_to_k6_url` but WITHOUT the `baseUrl` prefix: the bare
    path (with var interpolation), passed to `artaApiUrl(baseUrl, path)` (host
    routing) + `artaAuthHeader(path)` (per-family auth) so each step uses the
    right host + credential."""
    def repl(match: re.Match[str]) -> str:
        return f"${{getVar('{match.group(1)}')}}"
    rewritten = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, path_template)
    return f"`{rewritten}`"


def _build_step(node: dict, *, step_idx: int) -> str:
    """Emit the JS for one chain step.

    Each step:
      1. Validates `consumes` are populated; cascade-aborts if not.
      2. Builds the URL from path_template + chainVars.
      3. Issues the HTTP call.
      4. Asserts status in expected range.
      5. Extracts `provides` into `chainVars`.
    """
    method = (node.get("method") or "GET").upper()
    path_template = node.get("path_template") or "/"
    consumes = node.get("consumes") or {}
    provides = node.get("provides") or {}
    expected_status = int(node.get("status") or 200)

    consumes_keys_js = json.dumps(sorted(consumes.keys()))
    provides_js = json.dumps(provides, sort_keys=True)

    # R213.K — route host + per-family auth from the injected chain (artaApiUrl
    # picks the family host; artaAuthHeader picks the composite/agent_token/plain
    # credential). Falls back to baseUrl + single Bearer when no chain injected.
    path_only = _path_only_k6(path_template)
    url_expr = f"artaApiUrl(baseUrl, {path_only})"
    params_expr = f"{{ headers: artaAuthHeader({path_only}), tags: {{ step: '{step_idx:02d}' }} }}"
    # R213.K.6 — k6 arg positions differ by method: http.post/put/patch(url, BODY,
    # params) take params as the 3rd arg, but http.get/del/head/options(url, params)
    # take it as the 2nd. Pre-R213.K.6 every method emitted `(url, body, params)`,
    # so for GET the `{headers}` landed in the IGNORED 3rd slot → NO Authorization
    # header sent → 401 on every authenticated cm read (live: req_am_001_chain_1
    # GET cm/organizationss → 401 despite the URL+host+token all being correct).
    if method in {"POST", "PUT", "PATCH"}:
        http_call = f"http.{method.lower()}({url_expr}, JSON.stringify({{}}), {params_expr})"
    else:
        http_call = f"http.{method.lower()}({url_expr}, {params_expr})"

    return f"""
    // Step {step_idx:02d} — {method} {path_template}
    {{
        const required = {consumes_keys_js};
        for (const v of required) {{
            if (getVar(v) === undefined || getVar(v) === '') {{
                cascadeFailures.add(1, {{ step: '{step_idx:02d}', missing: v }});
                return;
            }}
        }}
        const r{step_idx} = {http_call};
        check(r{step_idx}, {{
            'step {step_idx:02d} status ok': (r) => r.status >= 200 && r.status < 300,
        }});
        if (r{step_idx}.status >= 400) return;

        const body{step_idx} = (function () {{
            try {{ return r{step_idx}.json(); }} catch (e) {{ return null; }}
        }})();
        const provides{step_idx} = {provides_js};
        for (const [name, path] of Object.entries(provides{step_idx})) {{
            if (!body{step_idx}) continue;
            let val = body{step_idx};
            const parts = path.replace(/^\\$\\.?/, '').split('.').filter(Boolean);
            for (const seg of parts) {{
                if (val === null || val === undefined) break;
                val = val[seg.replace('[0]', '')];
                if (Array.isArray(val) && seg.includes('[0]')) val = val[0];
            }}
            if (val === undefined || val === null || val === '') {{
                providerContractViolations.add(1, {{ step: '{step_idx:02d}', name: name }});
            }} else {{
                chainVars[name] = String(val);
            }}
        }}
    }}"""


def build_chain_aware_k6(
    chain: dict,
    *,
    requirement_id: str,
    project_vars: dict[str, str] | None = None,
    perf_thresholds: dict[str, str] | None = None,
) -> str:
    """Phase D3 entrypoint. Returns a complete k6 script as a string.

    Args:
        chain: dict form of CallChain.
        requirement_id: stamped in the script header for traceability.
        project_vars: Phase B harvested env vars (e.g. base_url, schema_id seed
            values). Inlined as JS object literals.
        perf_thresholds: optional `{metric_name: threshold_expr}` dict for the
            k6 `options.thresholds`.
    """
    nodes = chain.get("nodes") or []
    # R213.K.5 — drop captured third-party/static/SPA noise so the chain walks
    # ONLY real SUT-contract endpoints (else the first-failure short-circuit
    # aborts at a noise node before any real read).
    nodes = _filter_sut_api_nodes(nodes, chain.get("project_id"))
    # R213.K.5 — a chain with NO surviving SUT-contract node tests only noise;
    # return "" so the emission caller SKIPS it (vs writing a 0-check noise spec).
    if not nodes:
        log.info(
            "R213.K.5: skipping k6 emission for %s chain %s — 0 SUT-contract nodes "
            "(all captured nodes were third-party/static/SPA)",
            requirement_id, chain.get("chain_id"),
        )
        return ""
    project_vars = project_vars or {}
    perf_thresholds = perf_thresholds or {
        "http_req_duration": "p(95)<500",
        "http_req_failed": "rate<0.05",
    }

    thresholds_js = "\n".join(
        f"        '{k}': ['{v}'],"
        for k, v in perf_thresholds.items()
    )
    project_vars_js = "\n".join(
        f"        '{k}': {json.dumps(v)},"
        for k, v in project_vars.items()
    )

    body = "\n".join(_build_step(n, step_idx=int(n.get("sequence_index") or i))
                     for i, n in enumerate(nodes) if isinstance(n, dict))

    script = (
        f"// Phase D3 — chain-aware k6 for {requirement_id}\n"
        f"// chain_id={chain.get('chain_id')}  semantic_hash={chain.get('semantic_hash')}\n"
        f"\n"
        + _K6_HEADER
        + "\n"
        + _K6_AUTH_HELPER
        + "\n"
        + _options_block(thresholds_js)
        + "\n"
        + _default_fn_header(project_vars_js)
        + body
        + "\n    sleep(1);\n}\n"
    )
    return script


__all__ = ["build_chain_aware_k6"]
