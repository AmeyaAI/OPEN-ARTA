"""
API Endpoint Discovery — Multi-strategy discovery of actual API endpoints.

Tries multiple approaches in priority order:
1. OpenAPI/Swagger spec from the running application
2. GitHub source code route extraction (multi-repo aware)
3. Previously captured network traffic (from Playwright runs)
4. Returns empty if nothing found (LLM falls back to Gherkin inference)

Results are cached per project and fed to the AutomationEngineerAgent
so Newman/k6 scripts use real endpoints instead of guessed ones.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

import httpx

log = logging.getLogger("arta.api_discovery")

# Cache: {project_id: list[dict]}
_ENDPOINT_CACHE: dict[str, list[dict]] = {}
# H3 (R218) — per-project cache timestamp (monotonic). The in-memory cache was
# returned for the whole process lifetime with no time bound (only a manual
# `.clear()`), so a long-running process kept serving a stale discovery surface
# after re-discovery / operator edits. A TTL bounds the staleness.
# `ARTA_ENDPOINT_CACHE_TTL_S` (default 600s); 0 = cache forever (legacy).
_ENDPOINT_CACHE_TS: dict[str, float] = {}


def _endpoint_cache_fresh(project_id: str) -> bool:
    """True when a cached entry exists AND is within the TTL (or TTL disabled)."""
    if project_id not in _ENDPOINT_CACHE:
        return False
    try:
        ttl = float(os.environ.get("ARTA_ENDPOINT_CACHE_TTL_S", "600"))
    except ValueError:
        ttl = 600.0
    if ttl <= 0:
        return True  # legacy: cache for the process lifetime
    return (time.monotonic() - _ENDPOINT_CACHE_TS.get(project_id, 0.0)) < ttl


def _endpoint_cache_set(project_id: str, endpoints: list) -> None:
    _ENDPOINT_CACHE[project_id] = endpoints
    _ENDPOINT_CACHE_TS[project_id] = time.monotonic()


@dataclass
class APIEndpoint:
    method: str          # GET, POST, PUT, DELETE, PATCH
    path: str            # /api/v1/auth/register
    summary: str = ""    # Brief description
    params: list[str] | None = None      # Query/path parameter names
    request_body: dict | None = None      # JSON schema snippet
    response_schema: dict | None = None   # JSON schema snippet
    source: str = ""     # "openapi" | "github" | "network" | "manual"
    service: str = ""    # which repo/service this belongs to
    auth_required: bool = True   # detected from code decorators
    response_type: str = "json"  # json | stream | sse | websocket | mcp


async def discover_endpoints(project: dict) -> list[dict]:
    """Discover API endpoints using multiple strategies.

    Returns list of endpoint dicts (serializable), cached per project.
    """
    project_id = project.get("id", "")
    if _endpoint_cache_fresh(project_id):  # H3 — TTL-bounded, not forever
        return _ENDPOINT_CACHE[project_id]

    integrations = project.get("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()

    # Resolve target URL
    environments = project.get("environments", {})
    env_config = environments.get("staging", environments.get("local", {}))
    if hasattr(env_config, "model_dump"):
        env_config = env_config.model_dump()
    base_url = (
        env_config.get("api_base_url")
        or env_config.get("base_url")
        or integrations.get("base_url", "")
    )

    endpoints: list[dict] = []

    # Strategy 1: OpenAPI spec from running app
    if base_url:
        openapi_endpoints = await _try_openapi(base_url)
        if openapi_endpoints:
            endpoints = openapi_endpoints
            log.info("Discovered %d endpoints via OpenAPI from %s", len(endpoints), base_url)
            _endpoint_cache_set(project_id, endpoints)
            return endpoints

    # Strategy 2: GitHub source code route extraction (multi-repo)
    github_endpoints = await _try_github_routes(project)
    if github_endpoints:
        endpoints = github_endpoints
        log.info("Discovered %d endpoints via GitHub routes", len(endpoints))
        _endpoint_cache_set(project_id, endpoints)
        return endpoints

    # Strategy 3: Previously captured network traffic
    captured = _load_captured_endpoints(project_id)
    if captured:
        endpoints = captured
        log.info("Loaded %d previously captured endpoints for project %s", len(endpoints), project_id)
        _endpoint_cache_set(project_id, endpoints)
        return endpoints

    log.info("No API endpoints discovered for project %s — LLM will infer from Gherkin", project_id)
    return []


# ── Strategy 1: OpenAPI/Swagger ─────────────────────────────────────────────

async def _try_openapi(base_url: str) -> list[dict]:
    """Fetch and parse OpenAPI/Swagger spec from the target application."""
    spec_paths = [
        "/openapi.json", "/swagger.json", "/api-docs",
        "/docs/openapi.json", "/v1/openapi.json", "/api/openapi.json",
    ]

    for spec_path in spec_paths:
        try:
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                url = f"{base_url.rstrip('/')}{spec_path}"
                resp = await client.get(url)
                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "")
                    if "json" in content_type or resp.text.strip().startswith("{"):
                        spec = resp.json()
                        return _parse_openapi_spec(spec, base_url)
        except Exception:
            continue

    return []


def _openapi_auth_type(scheme: dict) -> str:
    """Phase 4 — map an OpenAPI securityScheme object to a canonical auth type."""
    t = (scheme.get("type") or "").lower()
    if t == "http":
        return ("http-" + (scheme.get("scheme") or "").lower()).rstrip("-") or "http"
    if t == "oauth2":
        return "oauth2"
    if t == "openidconnect":
        return "openIdConnect"
    if t == "apikey":
        return "apiKey-" + (scheme.get("in") or "header")
    if t == "basic":          # swagger 2
        return "http-basic"
    return t or "unknown"


def _extract_security_schemes(spec: dict) -> dict:
    """Phase 4 — name→auth-type map from components.securitySchemes (OpenAPI 3)
    or securityDefinitions (Swagger 2)."""
    comp = (spec.get("components") or {}).get("securitySchemes")
    if not comp:
        comp = spec.get("securityDefinitions") or {}
    return {name: _openapi_auth_type(s or {}) for name, s in (comp or {}).items()}


def _param_detail(p: dict) -> dict:
    """Phase 4 — extract a parameter's constraint metadata (required, type,
    enum, min/max, pattern, format) — Swagger 2 puts these on the param, OpenAPI
    3 nests them under `schema`."""
    sch = p.get("schema") or {}
    d = {"name": p.get("name"), "in": p.get("in"), "required": bool(p.get("required"))}
    for k in ("type", "format", "pattern", "enum", "minimum", "maximum",
              "minLength", "maxLength", "default"):
        v = sch.get(k, p.get(k))
        if v is not None:
            d[k] = v
    return d


def _parse_openapi_spec(spec: dict, base_url: str) -> list[dict]:
    """Parse OpenAPI 3.x or Swagger 2.x spec into endpoint list.

    Phase 4 — beyond endpoint names, now captures per-parameter constraints
    (`params_detail`: required/enum/min-max/pattern/format), request-body
    `required` fields, and per-operation auth scheme types so generated tests
    respect real param constraints + auth, and the Architecture Discovery
    api/auth graphs gain richer node attributes.
    """
    endpoints: list[dict] = []
    paths = spec.get("paths", {})
    sec_schemes = _extract_security_schemes(spec)
    global_security = spec.get("security") or []

    for path, methods in paths.items():
        for method, details in methods.items():
            if method.upper() not in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
                continue
            raw_params = details.get("parameters", []) or []
            params = [p.get("name", "") for p in raw_params]
            params_detail = [_param_detail(p) for p in raw_params if p.get("name")]
            request_body = None
            request_body_required: list = []
            rb = details.get("requestBody", {})
            if rb:
                content = rb.get("content", {})
                json_schema = content.get("application/json", {}).get("schema", {})
                if json_schema:
                    props = json_schema.get("properties", {})
                    if props:
                        request_body = {"properties": list(props.keys())[:10]}
                    request_body_required = list(json_schema.get("required", []) or [])[:20]
            # Per-operation auth (falls back to spec-level security).
            op_security = details.get("security", global_security) or []
            auth_schemes: list[str] = []
            for req_obj in op_security:
                for name in (req_obj or {}).keys():
                    auth_schemes.append(sec_schemes.get(name, name))
            response_schema = None
            responses = details.get("responses", {})
            success_resp = responses.get("200", responses.get("201", {}))
            if success_resp:
                content = success_resp.get("content", {})
                json_resp = content.get("application/json", {}).get("schema", {})
                if json_resp and json_resp.get("properties"):
                    response_schema = {"properties": list(json_resp["properties"].keys())[:10]}

            # Detect streaming responses
            resp_type = "json"
            if success_resp.get("content", {}).get("text/event-stream"):
                resp_type = "sse"

            ep = asdict(APIEndpoint(
                method=method.upper(), path=path,
                summary=details.get("summary", details.get("description", ""))[:100],
                params=params if params else None,
                request_body=request_body, response_schema=response_schema,
                source="openapi", response_type=resp_type,
                auth_required=bool(auth_schemes) if op_security else True,
            ))
            # Phase 4 — additive enrichment (consumers use .get()).
            if params_detail:
                ep["params_detail"] = params_detail
            if request_body_required:
                ep["request_body_required"] = request_body_required
            if auth_schemes:
                ep["auth_schemes"] = sorted(set(auth_schemes))
            endpoints.append(ep)

    return endpoints


# ── Strategy 2: GitHub Route Extraction (Multi-Repo) ───────────────────────

async def _try_github_routes(project: dict) -> list[dict]:
    """Extract API routes from GitHub source code across all project repos."""
    integrations = project.get("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()

    repos = integrations.get("repositories", [])
    github_token = integrations.get("github_token", "")

    # Fallback: single repo from github_repo field
    if not repos and integrations.get("github_repo"):
        repos = [{"repo": integrations["github_repo"], "branch": "main", "name": "main"}]

    if not repos:
        # Try legacy approach via fetch_code_context
        try:
            from .github_context import fetch_code_context
            code_context = await fetch_code_context(project)
            if code_context:
                return _extract_routes_from_code(code_context)
        except Exception as e:
            log.debug("Legacy GitHub route extraction failed: %s", e)
        return []

    all_endpoints: list[dict] = []
    all_code_context = ""  # Accumulate for auth detection

    for repo_entry in repos:
        owner_repo = repo_entry.get("repo", "")
        branch = repo_entry.get("branch", "main")
        service_name = repo_entry.get("name", owner_repo.split("/")[-1] if owner_repo else "unknown")

        if not owner_repo:
            continue

        try:
            route_files = await _fetch_route_files_from_github(owner_repo, branch, github_token)
            for file_path, content in route_files:
                routes = _extract_routes_from_code(content, service=service_name)
                # Detect auth requirements per route
                for route in routes:
                    if "Depends(authorize)" in content or "@auth_required" in content:
                        route["auth_required"] = True
                    if "authorize1" in content and "# " in content:  # commented out auth
                        route["auth_required"] = False
                all_endpoints.extend(routes)
                all_code_context += content + "\n"

            # Also fetch main.py to extract router prefixes
            prefix_content = await _fetch_file_from_github(owner_repo, branch, github_token, "main.py")
            if not prefix_content:
                prefix_content = await _fetch_file_from_github(owner_repo, branch, github_token, "app.py")
            if not prefix_content:
                # Search in src/ subdirectories
                for prefix_path in ["src/entrypoint.py", "src/main.py", "src/app.py"]:
                    prefix_content = await _fetch_file_from_github(owner_repo, branch, github_token, prefix_path)
                    if prefix_content:
                        break

            if prefix_content:
                prefixes = _extract_router_prefixes(prefix_content)
                all_code_context += prefix_content + "\n"
                # Apply prefixes to routes from this repo
                for ep in all_endpoints:
                    if ep.get("service") == service_name and not ep["path"].startswith("/entityextraction"):
                        for router_var, prefix in prefixes.items():
                            # Heuristic: if the route was from a file matching the router variable name
                            if router_var.lower() in service_name.lower():
                                ep["path"] = prefix.rstrip("/") + ep["path"]
                                break

        except Exception as e:
            log.debug("GitHub route extraction failed for %s: %s", owner_repo, e)

    # Detect response types (SSE, MCP, WebSocket)
    for ep in all_endpoints:
        ep["response_type"] = _detect_response_type_from_code(
            all_code_context, ep.get("service", ""), path=str(ep.get("path") or ""))

    # Detect auth flow from accumulated code
    _detected_auth = _detect_auth_flow(all_code_context)
    # Store auth flow in cache for prompt formatting
    if _detected_auth:
        _AUTH_FLOW_CACHE[project.get("id", "")] = _detected_auth

    # Deduplicate
    seen = set()
    deduped = []
    for ep in all_endpoints:
        key = f"{ep['method']}:{ep['path']}"
        if key not in seen:
            seen.add(key)
            deduped.append(ep)

    return deduped


async def _fetch_route_files_from_github(owner_repo: str, branch: str, token: str) -> list[tuple[str, str]]:
    """Fetch route/router Python/JS files from a GitHub repo."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    found_files: list[tuple[str, str]] = []

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Get repo tree
            tree_url = f"https://api.github.com/repos/{owner_repo}/git/trees/{branch}?recursive=1"
            resp = await client.get(tree_url, headers=headers)
            if resp.status_code != 200:
                return []

            tree = resp.json().get("tree", [])
            route_patterns = ["route", "router", "endpoint", "controller", "views"]

            for item in tree:
                if item["type"] != "blob":
                    continue
                path = item["path"]
                basename = path.split("/")[-1].lower()

                # Match route files (Python, JS, TS)
                if not any(path.endswith(ext) for ext in (".py", ".js", ".ts")):
                    continue
                if not any(p in basename for p in route_patterns):
                    continue
                # Skip test files and __pycache__
                if "test" in basename or "__pycache__" in path or "node_modules" in path:
                    continue

                content = await _fetch_file_content(client, owner_repo, path, branch, headers)
                if content:
                    found_files.append((path, content))

                # Rate limit: max 20 files per repo
                if len(found_files) >= 20:
                    break

    except Exception as e:
        log.debug("Failed to fetch route files from %s: %s", owner_repo, e)

    return found_files


async def _fetch_file_from_github(owner_repo: str, branch: str, token: str, file_path: str) -> str | None:
    """Fetch a single file from GitHub."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            return await _fetch_file_content(client, owner_repo, file_path, branch, headers)
    except Exception:
        return None


async def _fetch_file_content(client: httpx.AsyncClient, owner_repo: str, path: str, branch: str, headers: dict) -> str | None:
    """Fetch a file's content from GitHub API."""
    url = f"https://api.github.com/repos/{owner_repo}/contents/{path}?ref={branch}"
    resp = await client.get(url, headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        if data.get("encoding") == "base64" and data.get("content"):
            return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
    return None


def _extract_routes_from_code(code: str, service: str = "") -> list[dict]:
    """Extract API route definitions from source code text."""
    endpoints: list[dict] = []
    seen = set()

    # Python FastAPI/Flask patterns
    py_patterns = [
        r'@(?:app|router|api_router|blueprint|cmn_router|an_router)\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
    ]

    # Node.js Express patterns
    js_patterns = [
        r'(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
    ]

    for pattern in py_patterns + js_patterns:
        for match in re.finditer(pattern, code, re.IGNORECASE):
            method = match.group(1).upper()
            path = match.group(2)
            key = f"{method}:{path}"
            if key not in seen:
                seen.add(key)
                # Check auth from surrounding code
                auth_req = True
                # Look for auth decorator near this route
                route_start = max(0, match.start() - 200)
                context = code[route_start:match.start()]
                if "# " in context and "authorize" in context:
                    auth_req = False  # Commented out auth
                if "noAuthRequestHandler" in code or "no_auth" in context.lower():
                    auth_req = False

                endpoints.append(asdict(APIEndpoint(
                    method=method,
                    path=path,
                    source="github",
                    service=service,
                    auth_required=auth_req,
                )))

    return endpoints


def _extract_router_prefixes(code: str) -> dict[str, str]:
    """Extract include_router/app.use prefix mappings from main.py/app.py."""
    prefixes: dict[str, str] = {}
    patterns = [
        # FastAPI: app.include_router(router, prefix="/api/v1")
        r'include_router\s*\(\s*(\w+)\s*,\s*prefix\s*=\s*["\']([^"\']+)["\']',
        # Express: app.use('/api', router)
        r'\.use\s*\(\s*["\']([^"\']+)["\']\s*,\s*(\w+)',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, code):
            if "use" in pattern:
                # Express: first group is prefix, second is router
                prefixes[match.group(2)] = match.group(1)
            else:
                # FastAPI: first group is router, second is prefix
                prefixes[match.group(1)] = match.group(2)
    return prefixes


# ── Auth Flow Detection ───────────────────────────────────────────────────

_AUTH_FLOW_CACHE: dict[str, dict] = {}


def _detect_auth_flow(code_context: str) -> dict:
    """Auto-detect the project's auth flow from frontend/backend code."""
    patterns = {
        "token_exchange": [r"create.*token|get.*token|exchange.*token|agent.*token|getAgent"],
        "oauth": [r"OAuth|authorize.*callback|callback.*provider"],
        "api_key": [r"X-API-Key|apiKey|x-api-key"],
        "bearer": [r"Bearer|Authorization.*Bearer"],
        "cookie": [r"Cookie|session-token|session.*cookie"],
        "mcp": [r"mcp|sse_client|ClientSession|FastMCP"],
        "streaming": [r"StreamingResponse|EventSource|text/event-stream|ReadableStream"],
    }
    detected: dict[str, bool] = {}
    for auth_type, regexes in patterns.items():
        for regex in regexes:
            if re.search(regex, code_context, re.IGNORECASE):
                detected[auth_type] = True
                break
    return detected


def _detect_response_type_from_code(code_context: str, service: str, path: str = "") -> str:
    """Detect the response type for a service based on code patterns.

    R330 P3 — pre-fix this grepped the ENTIRE code context, so ONE
    StreamingResponse anywhere marked every endpoint of the service `sse`
    (false-positive source). When `path` is given and its last meaningful
    segment appears in the context, streaming/WS markers are honored only
    within a ±40-line window around those mentions. When the path never
    appears, the service-level heuristic remains (no better evidence)."""
    service_lower = service.lower()
    if "mcp" in service_lower or "analytics" in service_lower:
        if re.search(r"FastMCP|mcp.*server|sse_client", code_context, re.IGNORECASE):
            return "mcp"
    scoped = code_context
    seg = ""
    if path:
        segs = [s for s in str(path).split("/")
                if s and not (s.startswith("{") or s.isdigit())]
        seg = segs[-1] if segs else ""
    if seg and code_context:
        lines = code_context.splitlines()
        hits = [i for i, ln in enumerate(lines) if seg.lower() in ln.lower()]
        if hits:
            scoped = "\n".join(
                "\n".join(lines[max(0, i - 40):i + 40]) for i in hits[:20])
    if re.search(r"StreamingResponse|text/event-stream", scoped, re.IGNORECASE):
        return "sse"
    if re.search(r"WebSocket|websocket", scoped, re.IGNORECASE):
        return "websocket"
    return "json"


# ── Strategy 3: Captured Network Traffic ────────────────────────────────────

_CAPTURED_DIR = Path(".arta/discovered_endpoints")
# Phase C3 — chain persistence: one JSON file per project, keyed by chain_id.
# Cap at 200 chains (LRU on `last_observed_at`); same dir layout as
# `_CAPTURED_DIR` so operators have one place to look.
_CHAINS_DIR = Path(".arta/discovered_chains")
_MAX_CHAINS_PER_PROJECT = 200

# ── R206: OpenAPI-grounded captured-endpoint cleanser ────────────────────────
_OPENAPI_DIR = Path(".arta/openapi")
_R206_MATCHER_CACHE: dict[str, list] = {}


# ── E-OpenAPI — live OpenAPI/Swagger ingestion (PRIMARY endpoint-grounding) ──
#
# The discovery probe walks only a shell subset of the SPA, so real endpoints
# never captured → generated tests targeting them are BLOCKED as
# unknown_endpoint. But the SUT PUBLISHES its own contract: `GET /v3/api-docs`
# → 66 correctly-prefixed paths; per-service Swagger docs exist. That is the
# SUT's own DECLARED surface with correct deployment prefixes → the ideal
# grounding source: ZERO fabrication (it's the contract), ZERO SPA-crash
# (read-only GET), deterministic. It composes with existing machinery:
# `source="openapi"` endpoints are kept authoritatively by R206 (:618).
#
# Verified live: the effective path is the OpenAPI server-URL joined with each
# + key(`/sso/api/login`). Killswitch ARTA_OPENAPI_FETCH_DISABLE=1.

_OPENAPI_DOC_CANDIDATES = (
    "/v3/api-docs", "/swagger/v1/swagger.json", "/openapi.json",
    "/api-docs", "/swagger.json", "/v2/api-docs",
)
_OPENAPI_CFG_CANDIDATES = ("/v3/api-docs/swagger-config", "/swagger-resources")
_OPENAPI_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options"}


def _openapi_server_prefix(spec: dict) -> str:
    """The deployment prefix a doc's paths sit under: OpenAPI 3 `servers[0].url`
    path component, or Swagger 2 `basePath`. Returns "" when neither is set
    (paths are already absolute)."""
    from urllib.parse import urlsplit
    try:
        for s in (spec.get("servers") or []):
            u = (s or {}).get("url") or ""
            pp = urlsplit(u).path if "://" in u else u
            pp = "/" + (pp or "").strip("/")
            if pp and pp != "/":
                return pp.rstrip("/")
        bp = spec.get("basePath")   # Swagger 2.0
        if bp and str(bp).strip("/"):
            return "/" + str(bp).strip("/")
    except Exception:
        pass
    return ""


def _openapi_endpoints_from_spec(spec: dict) -> list[dict]:
    """Turn one OpenAPI/Swagger doc into {method, path, source:'openapi',
    query_params} rows, server-URL-joined so the path carries the correct
    deployment prefix.

    EO-2 — the paths+methods+query_params extraction is DELEGATED to
    `sut_topology.parse_openapi_spec` (single source of truth for OpenAPI 2/3
    parsing; it also surfaces the REQUIRED query params, which ground request
    construction). E-OpenAPI keeps only the verified server-URL join
    (`_openapi_server_prefix` — proven: `/portal` + `/sso/api/login` == the
    real login) and the `source="openapi"` stamp. Falls back to the local
    walker if sut_topology is unavailable.
    """
    prefix = _openapi_server_prefix(spec)
    try:
        from .sut_topology import parse_openapi_spec as _parse
        rows = _parse(spec, base_prefix=prefix)
        out: list[dict] = []
        for r in rows:
            e = {"method": r.get("method"), "path": r.get("path"), "source": "openapi"}
            if r.get("query_params"):
                e["query_params"] = r["query_params"]
            out.append(e)
        return out
    except Exception:
        # fallback — behaviour-preserving local walker
        import re as _re
        out = []
        for pkey, methods in (spec.get("paths") or {}).items():
            if not isinstance(methods, dict) or not isinstance(pkey, str):
                continue
            full = f"{prefix}/{pkey.lstrip('/')}" if prefix else pkey
            full = _re.sub(r"/{2,}", "/", full)
            for m in methods:
                if isinstance(m, str) and m.lower() in _OPENAPI_HTTP_VERBS:
                    out.append({"method": m.upper(), "path": full, "source": "openapi"})
        return out


async def fetch_openapi_contracts(
    project_id: str, base_url: str, headers: dict | None = None,
    *, service_names: list[str] | None = None, timeout: float = 10.0,
) -> list[dict]:
    """E-OpenAPI — fetch the SUT's own published OpenAPI/Swagger docs and return
    deduped {method, path, source:'openapi'} endpoints with correct prefixes.

    Doc discovery (SUT-agnostic): swagger-config `url`/`urls[]`, a fixed
    candidate list, and per-service `/{Service}/v3/api-docs` for each service
    seen in observed traffic. Read-only GETs only; never a caller-supplied URL
    (SSRF guard — only base_url + fixed suffixes). Empty list when the SUT
    publishes no doc (e.g. the example SUT) → total no-op.
    """
    if os.environ.get("ARTA_OPENAPI_FETCH_DISABLE") == "1" or not base_url:
        return []
    headers = headers or {}
    base = base_url.rstrip("/")
    doc_paths: set[str] = set(_OPENAPI_DOC_CANDIDATES)
    for svc in (service_names or []):
        s = str(svc).strip("/")
        # a bare, safe service segment (SSRF guard: no path traversal / hosts)
        if s and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", s):
            # `/UserManagement/api-docs` (84 paths) which the prior two suffixes
            # missed. Springdoc uses /v3/api-docs OR /api-docs; .NET uses
            # /swagger/v1/swagger.json.
            doc_paths.add(f"/{s}/v3/api-docs")
            doc_paths.add(f"/{s}/api-docs")
            doc_paths.add(f"/{s}/swagger/v1/swagger.json")

    endpoints: list[dict] = []
    fetched = 0
    try:
        async with httpx.AsyncClient(verify=False, timeout=timeout,
                                     follow_redirects=True) as client:
            async def _get_json(rel: str):
                try:
                    r = await client.get(base + rel, headers=headers)
                    if r.status_code == 200 and "json" in (r.headers.get("content-type") or "").lower():
                        return r.json()
                except Exception:
                    return None
                return None

            # swagger-config advertises the real doc URL(s)
            for cfg in _OPENAPI_CFG_CANDIDATES:
                j = await _get_json(cfg)
                if isinstance(j, dict):
                    if isinstance(j.get("url"), str):
                        doc_paths.add(j["url"])
                    for entry in (j.get("urls") or []):
                        if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                            doc_paths.add(entry["url"])
                elif isinstance(j, list):     # older /swagger-resources shape
                    for entry in j:
                        if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                            doc_paths.add(entry["url"])

            for du in sorted(doc_paths):
                if not du.startswith("/"):
                    continue
                j = await _get_json(du)
                if isinstance(j, dict) and j.get("paths"):
                    fetched += 1
                    endpoints.extend(_openapi_endpoints_from_spec(j))
    except Exception as exc:
        log.debug("E-OpenAPI: fetch failed for %s: %s", project_id, exc)
        return []

    seen: set = set()
    dedup: list[dict] = []
    for e in endpoints:
        k = (e["method"], e["path"])
        if k not in seen:
            seen.add(k)
            dedup.append(e)
    if dedup:
        log.info("E-OpenAPI: %s — fetched %d doc(s), %d unique endpoint(s) "
                 "(correctly-prefixed, source=openapi)", project_id, fetched, len(dedup))
    else:
        log.info("E-OpenAPI: %s — no published OpenAPI doc found (no-op)", project_id)
    return dedup


def persist_openapi_doc(project_id: str, endpoints: list[dict]) -> None:
    """Merge E-OpenAPI's effective paths into `.arta/openapi/<pid>.json` so the
    R206 contract-matcher set (`_r206_contract_matchers`) also covers them —
    strengthening validation of probe/network endpoints. Idempotent merge."""
    if not project_id or not endpoints:
        return
    try:
        _OPENAPI_DIR.mkdir(parents=True, exist_ok=True)
        p = _OPENAPI_DIR / f"{project_id}.json"
        doc = {"openapi": "3.0.0", "info": {"title": "arta-merged", "version": "1"}, "paths": {}}
        if p.exists():
            try:
                _existing = json.loads(p.read_text())
                if isinstance(_existing, dict):
                    doc = _existing
                    doc.setdefault("paths", {})
            except Exception:
                pass
        for e in endpoints:
            path, method = e.get("path"), (e.get("method") or "get").lower()
            if not path:
                continue
            # R330 P2b — stub ops are TAGGED so param mining can tell them from
            # ops the SUT's real contract declared (stubs carry no parameters).
            doc["paths"].setdefault(path, {}).setdefault(
                method, {"summary": "arta-openapi-ingested", "x-arta-stub": True})
        # R330 P2b — preserve the file mtime: openapi_cache treats file age as
        # spec freshness (24h TTL). Every stub merge used to touch the mtime, so
        # a stub-diluted doc NEVER expired and the SUT's real spec (with the
        # param constraints P2a mines) was never re-fetched.
        _prev_stat = p.stat() if p.exists() else None
        p.write_text(json.dumps(doc, indent=2))
        if _prev_stat is not None:
            try:
                os.utime(p, (_prev_stat.st_atime, _prev_stat.st_mtime))
            except Exception:
                pass
        _R206_MATCHER_CACHE.pop(project_id, None)   # invalidate cached matchers
    except Exception as exc:
        log.debug("E-OpenAPI: persist_openapi_doc failed for %s: %s", project_id, exc)


def _r206_contract_matchers(project_id: str) -> list:
    """Build segment-matchers from the SUT's OpenAPI contract — the
    authoritative set of REAL routable path shapes. Each matcher is a list of
    segments where `None` marks a `{param}` (matches anything) and a string is
    a literal that must match. Cached per project. Returns [] when no OpenAPI."""
    if project_id in _R206_MATCHER_CACHE:
        return _R206_MATCHER_CACHE[project_id]
    matchers: list = []
    p = _OPENAPI_DIR / f"{project_id}.json"
    if p.is_file():
        try:
            spec = json.loads(p.read_text())
            for path in (spec.get("paths") or {}):
                segs = [s for s in str(path).split("/") if s]
                matchers.append([
                    None if (s.startswith("{") and s.endswith("}")) else s.lower()
                    for s in segs
                ])
        except Exception as exc:
            log.debug("R206: OpenAPI matcher build failed for %s: %s", project_id, exc)
    _R206_MATCHER_CACHE[project_id] = matchers
    return matchers


def _r206_path_is_real(path: str, matchers: list) -> bool:
    """A captured path is REAL iff it matches an OpenAPI template (literal
    segments equal, `{param}` positions match any value). Handles the
    collection-name param (e.g. `organizationss`) that appears as a literal in
    captured paths but is a `{collection_plural_api_id}` param in the contract."""
    segs = [s for s in (path or "").split("/") if s]
    if not segs:
        return False
    for m in matchers:
        if len(m) == len(segs) and all(
                ms is None or ms == seg.lower() for ms, seg in zip(m, segs)):
            return True
    return False


def _r206_clean_endpoints(endpoints: list, project_id: str) -> list:
    """R206 — drop NON-ROUTABLE / hallucinated captured endpoints so test-gen
    grounds only on real paths.

    Root cause it fixes (run-cf956e): the API-contract tests asserted against
    captured paths like `/user-access/data` that return 404 (not in the SUT's
    OpenAPI contract — probe self-guesses / relative-path / template-leak
    pollution). 102 of 186 PW FAILs were `Expected 401 → Received 404`. This
    keeps only endpoints whose path matches an OpenAPI template + drops
    URL-encoded / template-leak garbage. Falls back to the original list when
    no OpenAPI exists or the filter would empty the list (never ground on
    nothing). Killswitch ARTA_R206_ENDPOINT_CLEAN_DISABLE=1."""
    import os as _os
    if _os.environ.get("ARTA_R206_ENDPOINT_CLEAN_DISABLE") == "1":
        return endpoints
    if not endpoints:
        return endpoints
    matchers = _r206_contract_matchers(project_id)
    if not matchers:
        return endpoints   # no contract to validate against — leave untouched
    # R276 — a github-extracted path is only real if its PREFIX is corroborated.
    #
    # R221.B (below) made source="github" AUTHORITATIVE because the OpenAPI
    # contract "is often incomplete". E-OpenAPI has since TRIPLED that contract
    # (66 -> 194 endpoints), and the premise no longer holds — while the cost is
    # now measured. A github extraction carries the route SUFFIX but NOT the
    # deployment prefix (it lives in web.xml / server.servlet.context-path /
    # gateway config, which the extractor never reads), so an unrecovered one is
    # a path that CANNOT exist:
    #
    #   GET /api/gfSvc/redis/device      -> 404 "No static resource"  (source=github)
    #   GET /menumanagement/api/editMenu -> 404; the contract says
    #
    # Live cost: req_or_017 grounded on `/api/gfSvc/redis/device`, passed every
    # grounding gate because the path WAS "in the captured surface", and then
    # 286 endpoints (28%) are github-sourced; 56 have an uncorroborated prefix.
    #
    # Grounding on fiction is worse than having no endpoint: a fabricated path
    # produces FAILs that read as SUT defects, whereas dropping it produces a
    # truthful `unknown_endpoint` BLOCK. Report what we cannot verify; never
    # invent what we cannot ground. (The .NET families with no reachable Swagger
    # stay correctly BLOCKED — the honest ceiling until the OTP API doc lands.)
    #
    # E1's prefix-recovery already lifts corroborated github routes to their real
    #
    # R276.B — corroborate on the SERVICE prefix (first TWO segments), not the
    # first segment. Two corrections to R276's first cut, both measured:
    #   1. `/api` is a REAL first segment here (`/api/notice/...` is observed
    #      live), so a 1-segment rule trusted ALL of `/api/*` — including the
    #      `/api/gfSvc/*` fiction it was written to drop. It shipped
    #      correct-by-luck, not correct-by-reasoning.
    #   2. R276's message claimed `/menumanagement/api/editMenu` 404s because the
    #      the contract and never verified. Curled: BOTH forms return 400 (Bad
    #      Request = endpoint EXISTS, empty body rejected), not 404. The gateway
    #      serves prefixed AND unprefixed. The only VERIFIED 404 is `/api/gfSvc/*`.
    # `/api/notice` vs `/api/gfSvc` is the real discriminator, and the service
    # segment is what separates them.
    # Killswitch ARTA_R276_GITHUB_PREFIX_CORROBORATE_DISABLE=1.
    def _r276_service_prefix(p: str) -> str:
        _segs = [s for s in str(p or "").split("/") if s]
        return "/" + "/".join(_segs[:2]) if _segs else ""

    _trusted_prefixes: set[str] = set()
    if _os.environ.get("ARTA_R276_GITHUB_PREFIX_CORROBORATE_DISABLE") != "1":
        for ep in endpoints:
            if isinstance(ep, dict) and ep.get("source") != "github":
                _p = _r276_service_prefix(ep.get("path"))
                if _p:
                    _trusted_prefixes.add(_p)

    # R278 — corroboration basis for REQUIREMENT endpoints: service-prefixes of
    # NON-requirement (REAL: github/openapi/manual/observed) endpoints. Distinct
    # from _trusted_prefixes (built from non-github, which INCLUDES requirement — a
    # requirement can't be allowed to self-corroborate here).
    _r278_real_prefixes: set[str] = set()
    if _os.environ.get("ARTA_R278_REQ_CORROBORATE_DISABLE") != "1":
        for ep in endpoints:
            if isinstance(ep, dict) and ep.get("source") != "requirement":
                _p = _r276_service_prefix(ep.get("path"))
                if _p:
                    _r278_real_prefixes.add(_p)

    cleaned: list = []
    _r276_dropped: list = []
    _r278_dropped: list = []
    for ep in endpoints:
        if not isinstance(ep, dict):
            continue
        path = ep.get("path") or ""
        # Structural garbage: URL-encoded, template-leak, JSON-as-path.
        if "%" in path or "{{" in path or "'" in path or '"' in path or ",id" in path.lower():
            continue
        # R221.B — source-extracted endpoints (SUT's own code / OpenAPI) are
        # AUTHORITATIVE: keep them even if absent from the OpenAPI matcher set.
        # R276 narrows this for github ONLY: authoritative for the SUFFIX, but
        # the PREFIX must be corroborated by an observed/contract endpoint.
        if ep.get("source") == "github" and _trusted_prefixes:
            if _r276_service_prefix(path) in _trusted_prefixes:
                cleaned.append(ep)
            else:
                _r276_dropped.append(path)
        elif ep.get("source") == "requirement":
            # R278 — a requirement is the SPEC, but a requirement AUTHOR may write
            # the AC declared `/lease/recipient-accounts` + `/lease/create` but the
            # SUT serves `/AssetManagement/api/LeaseAccountRelationship/...` — the
            # idealizations 404 and POISON grounding, reading as SUT defects). When
            # REAL routes exist to corroborate against (`_trusted_prefixes` non-empty),
            # apply the SAME service-prefix corroboration as github (R276): keep the
            # requirement endpoint only if its prefix is corroborated. When NO real
            # routes exist (contract incomplete for that family — R277's original
            # case), keep it as the only signal. Killswitch ARTA_R278_REQ_CORROBORATE_DISABLE.
            if (_os.environ.get("ARTA_R278_REQ_CORROBORATE_DISABLE") == "1"
                    or not _r278_real_prefixes
                    or _r276_service_prefix(path) in _r278_real_prefixes):
                cleaned.append(ep)
            else:
                _r278_dropped.append(path)
        elif ep.get("source") in ("github", "openapi", "manual", "human_correction"):
            # Authoritative human/source/contract declaration — the OpenAPI-matcher
            # check below must never strip it (the contract can be incomplete).
            # R320: `human_correction` is a tester's verified SUT truth (the only
            # grounding source for a SAML-blocked-GitHub SUT) — as authoritative as
            # a manual/contract declaration.
            cleaned.append(ep)
        elif _r206_path_is_real(path, matchers):
            cleaned.append(ep)
    if _r276_dropped:
        log.info(
            "R276: dropped %d github endpoint(s) for %s whose prefix no observed/contract "
            "endpoint corroborates (they 404; grounding on them yields FAILs that read as "
            "SUT defects). Trusted prefixes: %s. Samples: %s",
            len(_r276_dropped), project_id, sorted(_trusted_prefixes)[:6], _r276_dropped[:4])
    if _r278_dropped:
        log.info(
            "R278: dropped %d REQUIREMENT-declared endpoint(s) for %s whose service-prefix "
            "no observed/contract route corroborates (author idealizations that 404; the "
            "real routes exist under a trusted prefix). Trusted: %s. Samples: %s",
            len(_r278_dropped), project_id, sorted(_trusted_prefixes)[:6], _r278_dropped[:4])
    if not cleaned:
        return endpoints   # safety: never strip to nothing
    if len(cleaned) < len(endpoints):
        log.info("R206: cleaned captured endpoints %d → %d (dropped %d non-routable) for %s",
                 len(endpoints), len(cleaned), len(endpoints) - len(cleaned), project_id)
    return cleaned


_RESPONSE_SHAPES_DIR = Path(".arta/discovered_response_shapes")


def _r212_skel(p: str) -> str:
    """Path skeleton: ids/params → '*' (so a captured concrete path and a probe
    response-shape URL match)."""
    import re as _re
    p = _re.sub(r"^https?://[^/]+", "", (p or "").split("?")[0]).rstrip("/")
    out = []
    for s in p.split("/"):
        sl = s.lower()
        if not s:
            continue
        if (s.startswith("{") or sl.isdigit()
                or _re.fullmatch(r"[0-9a-f]{6,}", sl)
                or _re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{8,}", sl)):
            out.append("*")
        else:
            out.append(sl)
    return "/" + "/".join(out)


def _r212_merge_response_shapes(endpoints: list, project_id: str) -> list:
    """R212 — merge the probe's directly-captured GET response shapes
    (`discovered_response_shapes/<pid>.json`, written by discovery_probe's
    `context.on('response')` handler) into captured endpoints that LACK a
    `response_body_shape`, matched by path skeleton. The HAR-based shape capture
    is sparse (~6/500), which starved dataset/analytics recipes (→ TimeoutError /
    schema-validation fail → degraded analytics tests). This fills the gap so the
    recipe can ground. Killswitch ARTA_R212_RESPONSE_MERGE_DISABLE=1."""
    import os
    if os.environ.get("ARTA_R212_RESPONSE_MERGE_DISABLE") == "1":
        return endpoints
    rpath = _RESPONSE_SHAPES_DIR / f"{project_id}.json"
    if not rpath.is_file():
        return endpoints
    try:
        shapes = json.loads(rpath.read_text())
    except Exception:
        return endpoints
    if not isinstance(shapes, list) or not shapes:
        return endpoints
    import re as _re_path
    by_skel: dict = {}
    shape_by_skel: dict = {}   # skel -> (path, method, shape) for ADD
    vals_by_skel: dict = {}    # R313 — skel -> {field: [values]} value-domain samples
    for s in shapes:
        if isinstance(s, dict) and s.get("url") and s.get("response_body_shape") is not None:
            _u = s["url"]
            _p = _re_path.sub(r"^https?://[^/]+", "", str(_u).split("?")[0].split("#")[0]) or "/"
            _sk = _r212_skel(_u)
            by_skel.setdefault(_sk, s["response_body_shape"])
            shape_by_skel.setdefault(_sk, (_p, (s.get("method") or "GET").upper(), s["response_body_shape"]))
            # R313 — union enum-like value samples per field across same-skeleton responses
            _vs = s.get("response_value_samples")
            if isinstance(_vs, dict):
                _acc = vals_by_skel.setdefault(_sk, {})
                for _f, _vv in _vs.items():
                    if isinstance(_vv, list):
                        _cur = set(_acc.get(_f, [])); _cur.update(str(x) for x in _vv)
                        _acc[_f] = sorted(_cur)[:12]
    if not by_skel:
        return endpoints
    merged = 0
    matched_skels: set = set()
    for e in endpoints:
        _esk = _r212_skel(e.get("path", "")) if isinstance(e, dict) else None
        if _esk in by_skel:
            matched_skels.add(_esk)
            if isinstance(e, dict) and not e.get("response_body_shape"):
                e["response_body_shape"] = by_skel[_esk]
                merged += 1
            # R313 — attach value-domain samples (additive; union onto any existing)
            if isinstance(e, dict) and vals_by_skel.get(_esk):
                _dst = e.setdefault("response_value_samples", {})
                for _f, _vv in vals_by_skel[_esk].items():
                    _cur = set(_dst.get(_f, [])); _cur.update(_vv); _dst[_f] = sorted(_cur)[:12]
    # ADD endpoints for captured shapes with NO existing match — these are REAL
    # cm endpoints the probe observed (analytics/extraction/document-type/roles
    # lists, etc.) that weren't in the prior captured surface. Without ADDING
    # them, the dataset/analytics recipe has no endpoint to ground against.
    added = 0
    for _sk, (_p, _m, _sh) in shape_by_skel.items():
        if _sk not in matched_skels:
            endpoints.append({
                "method": _m, "path": _p, "status": 200,
                "content_type": "application/json",
                "response_body_shape": _sh, "source": "r212_probe_response_capture",
                **({"response_value_samples": vals_by_skel[_sk]} if vals_by_skel.get(_sk) else {}),
            })
            matched_skels.add(_sk)
            added += 1
    if merged or added:
        log.info("R212: merged %d + ADDED %d probe-captured response shape(s) → captured "
                 "endpoints for %s", merged, added, project_id)
    return endpoints


def _r221_drop_selfguesses(eps: list[dict]) -> list[dict]:
    """R221 — drop probe self-guessed / gen-derived endpoints that carry NO
    capture evidence, SUT-agnostically.

    Two writers populate the store: real HAR captures
    (`harvest_envvars_from_har` → `source_har` + `discovered_at` set, no
    `summary`) and gen-derived self-guesses (executed-test URLs fed back +
    the probe's hardcoded the example SUT `API_PROBES` list → `source_har=None`,
    `discovered_at=None`, `summary` present). The latter poison grounding on
    every non-example SUT (a real-world run: /api/v1/datasets|schemas|insights…
    → 405/404). Drop iff NO evidence AND a summary marker — keys on PROVENANCE,
    not path shape, so the example SUT's genuine `/api/v1/*` (which DO carry `source_har`
    from real HAR capture) is preserved.

    Safety rails (mirror R206): killswitch, and never strip to empty — if the
    filter would remove everything (a HAR-less/summary-only project), return the
    input untouched so grounding is degraded, not destroyed.
    """
    import os as _os
    if _os.environ.get("ARTA_R221_SELFGUESS_FILTER_DISABLE") == "1":
        return eps
    kept = [
        ep for ep in eps
        if not ((not (ep.get("source_har") or ep.get("discovered_at")))
                and bool(ep.get("summary")))
    ]
    return kept if kept else eps


_REQUIREMENTS_FILE = Path(".arta/requirements.json")

# R277 — a METHOD + PATH stated in a requirement / acceptance criterion, e.g.
#   "when": "POST /Reefer/api/getReeferStatusData is called with Authorization,
#            acctGuid and uguid headers"
# High-precision by construction: an HTTP verb IMMEDIATELY followed by an
# absolute path. Prose that merely mentions a path (no verb) is ignored.
_R277_VERB_PATH_RE = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_/{}.\-]+)")


def _r277_requirement_endpoints(project_id: str) -> list[dict]:
    """R277 — endpoints DECLARED by the requirements themselves.

    The blocked families (.NET Account/Asset/Device/Geofence, Reefer) publish no
    gateway-reachable Swagger, and the probe never walks them — so ARTA BLOCKed
    every one as `unknown_endpoint` and the standing note said they need "the API
    document the OTP team promised". **The requirements ARE that document**: the
    ACs carry method, path, required headers and response shape, all authored by
    humans in Jira. Verified live 2026-07-17 (the 404/401 split is the proof):

        GET /Reefer/api/getReeferStatusData                       -> 401 (EXISTS)
        GET /AccountManagement/api/AccountRelationShip/GetAcc...  -> 401 (EXISTS)
        GET /AssetManagement/api/Asset/GetAllAssetsOfAccountId/0  -> 401 (EXISTS)
        GET /api/gfSvc/redis/device  (github-extracted fiction)   -> 404

    a vendor SUT: 49 endpoints new to the surface. the example SUT: 12. Generic — every SUT's
    requirements state the endpoints under test.

    Why this source is AUTHORITATIVE where `github` is not (R276): a
    requirement-declared endpoint is the SPEC. If the SUT does not serve it, that
    is a REAL SUT DEFECT worth reporting — the mission. A github-extracted path is
    ARTA's own inference, so its 404 is ARTA's bug and must never reach a spec.
    The two 404s look identical on a dashboard and mean opposite things.

    Killswitch: ARTA_R277_REQUIREMENT_ENDPOINTS_DISABLE=1.
    """
    if os.environ.get("ARTA_R277_REQUIREMENT_ENDPOINTS_DISABLE") == "1":
        return []
    if not project_id or not _REQUIREMENTS_FILE.is_file():
        return []
    try:
        data = json.loads(_REQUIREMENTS_FILE.read_text())
    except Exception as exc:
        log.debug("R277: requirements read failed: %s", exc)
        return []
    reqs = data.get(project_id) if isinstance(data, dict) else None
    if not isinstance(reqs, list):
        return []

    out: list[dict] = []
    seen: set[tuple] = set()
    for req in reqs:
        if not isinstance(req, dict):
            continue
        try:
            blob = "%s %s" % (req.get("description") or "",
                              json.dumps(req.get("acceptance_criteria") or []))
        except Exception:
            continue
        for method, raw_path in _R277_VERB_PATH_RE.findall(blob):
            p = raw_path.rstrip(".,);:")
            # Structural sanity — same bar the rest of the surface is held to.
            if len(p) < 2 or "%" in p or "{{" in p:
                continue
            key = (method.upper(), p)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "method": method.upper(),
                "path": p,
                "source": "requirement",
                "declared_by": req.get("req_id") or req.get("id"),
            })
    if out:
        log.info("R277: %d endpoint(s) declared by requirements for %s "
                 "(authoritative: the requirement IS the contract)",
                 len(out), project_id)
    return out


_R281_FABRICATED_MARKERS = re.compile(
    # Injection payloads that got recorded as "captured" from ARTA's OWN
    # security/negative test traffic (ZAP/PW/Newman hitting the SUT with these):
    r"(?:%3c|%3e|%27|<script|</script|'\s*or\s|\bor\s+'?1'?\s*=|--\s|;\s*drop\s|"
    r"union\s+select|%20or%20|1'\s*=\s*'1|\balert\()"
    # Explicit test-fabrication segments the LLM/probe invents for negative cases:
    r"|(?:/|-|_)(?:nonexistent|non-existent|fake|deleted|dummy|placeholder|"
    r"invalid|bogus|does-?not-?exist)(?:[-_/]|$)"
    r"|[-_]xyz(?:[-_/]|$)|xyz[-_]",
    re.IGNORECASE,
)


def _r281_drop_fabricated_endpoints(raw: object) -> list[dict]:
    """R281 — drop endpoints whose PATH carries an unmistakable fabrication
    marker (SUT-agnostic).

    Root cause (an SSR-SUT run): ARTA's own executed tests hit the SUT with
    negative-case / injection paths (`.../clusters/nonexistent-cluster-xyz`,
    `.../organizations/%3Cscript%3E...`, `.../clusters/cluster-001'%20OR%20'1'='1`)
    and the discovery capture recorded those requests back into
    discovered_endpoints — some even with a 200 status (captured before a SUT
    redeploy). Grounding then treats them as REAL routable endpoints, so the LLM
    regenerates specs asserting 200 on fabricated resources → 404 storm (34 of 41
    fails in run-a6ddd6). Status-based filtering is unreliable here (polluted
    entries carried mixed 200/None), so filter on the fabrication MARKERS
    themselves — injection payloads and explicit `nonexistent/fake/deleted/xyz`
    segments are never legitimate SUT resources on ANY SUT. Fabricated PARAM
    VALUES that look plausible (e.g. an invented region id) are NOT caught here —
    those need SUT-specific corroboration and stay the operator's data-clean job.
    Killswitch: ARTA_R281_DISABLE=1. GENERIC across SUTs."""
    if os.environ.get("ARTA_R281_DISABLE") == "1":
        return raw if isinstance(raw, list) else []
    eps = raw if isinstance(raw, list) else []
    out, dropped = [], 0
    for e in eps:
        p = (e.get("path") or e.get("url") or "") if isinstance(e, dict) else str(e)
        if p and _R281_FABRICATED_MARKERS.search(p):
            dropped += 1
            continue
        out.append(e)
    if dropped:
        log.info("R281: dropped %d fabricated/injection endpoint(s) from captured set", dropped)
    return out


# R305 — endpoint-grounding hygiene (SUT-agnostic). The captured store must
# contain ONLY endpoints ARTA genuinely observed the SUT serve as a JSON API.
# Two provenance-conflation pollution classes defeat grounding:
#   1. SPA/static routes harvested from the discovery HAR (document navigations
#      like GET /clusters with content_type text/html, or /_next/static/*.js) —
#      recorded as "endpoints" but they return HTML/assets, not JSON.
#   2. ARTA's OWN executed test traffic written back at run time (source=network,
#      summary="[API] <test title>") — including its failed 404/HTML guesses like
#      /v1/regions/global/infrastructure/servers. These make ARTA's mistakes its
#      own future grounding truth.
# S1 (execution write-back gate) + S2 (writer gate) stop this at the source;
# _r305_endpoint_hygiene remediates stores already polluted before those shipped.
_R305_NON_API_CONTENT_TYPES = re.compile(
    r"text/html|javascript|text/css|font/|image/|woff", re.IGNORECASE)
# Static/asset namespaces + file extensions are non-API on ANY SUT, even when
# the content_type wasn't captured (e.g. Next.js /_next/webpack-hmr SSE has no
# recorded type). `_next`/`static`/`assets` are reserved build-asset namespaces.
_R305_STATIC_PATH = re.compile(
    r"^/(?:_next|static|assets)(?:/|$)|"
    r"\.(?:js|mjs|css|map|woff2?|ttf|eot|png|jpe?g|gif|svg|ico|webp|avif|mp4|wasm)(?:\?|$)",
    re.IGNORECASE)
_R305_TEST_SUMMARY_MARKER = re.compile(
    r"^\s*\[(?:api|performance|ui|security|a11y|axe|zap)\]|\bAC-\d", re.IGNORECASE)


def _r305_drop_reason(ep: dict) -> str | None:
    """Classify a captured entry as non-API pollution. Returns a short drop-reason
    or None to KEEP. Real JSON APIs (application/json, 2xx) and requirement-declared
    templates (source=requirement, no content_type) are always kept; ambiguous /
    unknown content types abstain (kept)."""
    if not isinstance(ep, dict):
        return None
    p = str(ep.get("path") or "")
    if p and _R305_STATIC_PATH.search(p):
        return "html_or_static"
    ct = str(ep.get("content_type") or "")
    if ct and _R305_NON_API_CONTENT_TYPES.search(ct):
        return "html_or_static"
    summary = str(ep.get("summary") or "")
    if (ep.get("source") == "network" and summary
            and _R305_TEST_SUMMARY_MARKER.search(summary)):
        status = ep.get("status")
        if not (isinstance(status, int) and 200 <= status < 300):
            # ARTA's own executed request written back without proof the SUT
            # served it (404/HTML/None) — the self-poisoning echo.
            return "test_traffic_echo"
    return None


def _r305_endpoint_hygiene(raw: object) -> list[dict]:
    """R305 (R1) — remediate an ALREADY-polluted captured store at read time.
    Killswitch ARTA_R305_ENDPOINT_HYGIENE_DISABLE=1."""
    if os.environ.get("ARTA_R305_ENDPOINT_HYGIENE_DISABLE") == "1":
        return raw if isinstance(raw, list) else []
    eps = raw if isinstance(raw, list) else []
    out: list[dict] = []
    dropped: dict[str, int] = {}
    for e in eps:
        reason = _r305_drop_reason(e) if isinstance(e, dict) else None
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        out.append(e)
    if dropped:
        log.info("R305: dropped %d non-API captured entr(ies) %s",
                 sum(dropped.values()), dict(dropped))
    return out


def _load_captured_endpoints(project_id: str) -> list[dict]:
    """Load previously captured API endpoints from disk.

    R221: drop no-evidence self-guesses (probe API_PROBES + gen-derived
    write-back) so grounding uses REAL captured traffic only (SUT-agnostic).
    R206: the loaded list is cleaned against the SUT's OpenAPI contract so all
    consumers (PW/Newman gen + grounding validators) ground on REAL routable
    paths, not the probe-guessed 404-prone pollution.
    R212: merges probe-captured GET response shapes for recipe grounding."""
    path = _CAPTURED_DIR / f"{project_id}.json"
    eps: list[dict] = []
    if path.exists():
        try:
            raw = _r281_drop_fabricated_endpoints(
                _r221_drop_selfguesses(
                    _r305_endpoint_hygiene(json.loads(path.read_text()))))
            eps = _r212_merge_response_shapes(
                _r206_clean_endpoints(raw, project_id), project_id)
        except Exception:
            eps = []
    # R277 — union the endpoints the REQUIREMENTS declare. Added AFTER the
    # cleaners on purpose: they exist to strip ARTA's own inferences (probe
    # self-guesses, github prefix-less extractions), and a requirement-declared
    # endpoint is not an inference — it is the contract the SUT is being tested
    # against. Never overrides an already-captured entry.
    try:
        _have = {(str(e.get("method")).upper(), str(e.get("path")))
                 for e in eps if isinstance(e, dict)}
        _extra = [e for e in _r277_requirement_endpoints(project_id)
                  if (e["method"], e["path"]) not in _have]
        # R278 — corroborate the requirement UNION too. R277 re-adds requirement
        # endpoints AFTER the cleaner, which would otherwise resurrect the very
        # author IDEALIZATIONS R278 just dropped inside _r206_clean_endpoints (the
        # keep a merged requirement endpoint only if its prefix is corroborated by
        # a REAL (non-requirement) route already in `eps`; when no real routes exist,
        # keep all (contract incomplete — R277's original case).
        # Killswitch ARTA_R278_REQ_CORROBORATE_DISABLE.
        if os.environ.get("ARTA_R278_REQ_CORROBORATE_DISABLE") != "1" and _extra:
            def _svc_pfx(p: str) -> str:
                _s = [x for x in str(p or "").split("/") if x]
                return "/" + "/".join(_s[:2]) if _s else ""
            _real_pfx = {_svc_pfx(e.get("path")) for e in eps
                         if isinstance(e, dict) and e.get("source") != "requirement"}
            _real_pfx.discard("")
            if _real_pfx:
                _before = len(_extra)
                _extra = [e for e in _extra if _svc_pfx(e.get("path")) in _real_pfx]
                if len(_extra) < _before:
                    log.info("R278: filtered %d uncorroborated requirement endpoint(s) "
                             "from the union for %s (author idealizations)",
                             _before - len(_extra), project_id)
        eps = eps + _extra
    except Exception as _r277_exc:
        log.debug("R277: requirement-endpoint merge skipped for %s: %s",
                  project_id, _r277_exc)
    # Stamp each endpoint's PROTOCOL (rest/sse/grpc/graphql/websocket/…) so the
    # classification is a DURABLE property every gen path sees — previously it was
    # computed only inside the architecture api_graph (which staleable/separate),
    # so gen read `endpoint.get("protocol")` as None → defaulted to REST and the
    # SSE/gRPC/GraphQL gen paths never fired even when the endpoint was captured
    # (e.g. an SSE `.../event/response-stream`). classify_protocol is a cheap pure
    # token/mime heuristic (protocol_discovery — leaf module, no import cycle);
    # never overrides an already-tagged endpoint. Killswitch
    # ARTA_ENDPOINT_PROTOCOL_TAG_DISABLE.
    if os.environ.get("ARTA_ENDPOINT_PROTOCOL_TAG_DISABLE") != "1":
        try:
            from .protocol_discovery import classify_protocol as _classify_proto
            for _e in eps:
                if isinstance(_e, dict) and _e.get("path") and not _e.get("protocol"):
                    _e["protocol"] = _classify_proto(
                        _e.get("path") or "", _e.get("content_type"),
                        _e.get("summary") or "")
        except Exception as _proto_exc:
            log.debug("protocol tag skipped for %s: %s", project_id, _proto_exc)
    return eps


def _r261_recover_prefix(route: dict, obs_skels: set, obs_segs: list) -> str | None:
    """E1 (R263) — recover a mangled github-route's deployment prefix from
    observed traffic. GENERIC fallback for SUTs that publish no OpenAPI doc but
    expose observed suffixes.

    Identity-corroboration ONLY (never synthesis): find an observed skeleton
    whose SUFFIX equals the route's operation tail. A single unambiguous match
    (or several agreeing on the same full path) → adopt that OBSERVED path
    verbatim. Ambiguous / no match → None (the route then drops as today). This
    is a RELABEL of real traffic, never a guess — so it cannot reintroduce the
    ~190 fabricated-404s R261 was built to stop.

    Verified inert on a vendor SUT (no blocked-family suffix is observed); the value
    is on SUTs where the probe DID observe the suffix under a different service
    prefix than the extractor guessed. Killswitch ARTA_R261_RECOVER_DISABLE=1.
    """
    if os.environ.get("ARTA_R261_RECOVER_DISABLE") == "1":
        return None
    path = route.get("path") or route.get("url") or ""
    if not path:
        return None
    # Compare on NON-param segments only (drop `*`), so a trailing id slot on
    # either side doesn't break the suffix match.
    route_ns = [s for s in _r212_skel(str(path)).strip("/").split("/") if s and s != "*"]
    tail = route_ns[-2:]
    if not tail:
        return None
    candidates: set = set()
    for o in obs_segs:
        o_full = [s for s in o if s]
        o_ns = [s for s in o_full if s != "*"]
        if len(o_ns) >= len(tail) and o_ns[-len(tail):] == tail:
            # adopt the OBSERVED full skeleton, params rendered as {id} for gen
            candidates.add("/" + "/".join("{id}" if s == "*" else s for s in o_full))
    # unambiguous identity match (or agreement on one full path)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _r261_validate_extracted_routes(
    routes: list[dict], captured: list[dict], *, project_id: str = "",
) -> list[dict]:
    """R261 (WS4) — persist a SOURCE-EXTRACTED route only when real traffic
    corroborates it.

    Fix 2's multi-language extractor composes Java-Spring routes from a class
    `@RequestMapping` base + a method `@*Mapping` — but the SUT's real paths
    carry an additional per-service DEPLOYMENT prefix that appears in no
    annotation. So the extractor emits `/sso/api/logout` where the SUT actually
    serves `/portal/sso/api/login`. Those unroutable paths reached gen as
    "authoritative" grounding (R206) and produced ~190 Newman 404s: Newman
    58.9% -> 44.0% (-14.9pp). The extractor is not wrong to exist — its
    FastAPI/Express output is good — so this validates rather than kills it
    (ARTA_GH_ROUTE_EXTRACT_DISABLE=1 remains the blunt option).

    A route is kept when OBSERVED traffic corroborates it:
      • exact skeleton match                  → the SUT demonstrably serves it
      • shares a ≥2-segment skeleton prefix   → same real service surface

    Otherwise it is an UNVALIDATED GUESS and is dropped. Dropping is the
    conservative choice on purpose: a route ARTA never saw the SUT serve
    cannot be grounding, and injecting it produces exactly the fabricated-404
    noise that makes Pillar 4 unmeasurable (R259/R260).

    The drop count + a sample are logged, never silent — the operator needs to
    see when a whole service's extraction is being discarded.
    Killswitch ARTA_R261_ROUTE_VALIDATE_DISABLE=1 → pre-R261 (unvalidated).
    """
    if os.environ.get("ARTA_R261_ROUTE_VALIDATE_DISABLE") == "1":
        return routes
    if not routes:
        return routes
    # Corroboration comes ONLY from paths the SUT was actually seen serving.
    # `github` entries are excluded: a guess cannot validate another guess.
    obs_skels: set[str] = set()
    for e in (captured or []):
        if not isinstance(e, dict):
            continue
        if (e.get("source") or "") == "github":
            continue
        p = e.get("path") or e.get("url") or ""
        if p:
            obs_skels.add(_r212_skel(str(p)))
    if not obs_skels:
        # Nothing observed (cold start): no basis to judge — keep everything
        # rather than silently emptying the surface.
        log.info("R261: no observed traffic for %s — keeping all %d extracted "
                 "route(s) unvalidated (cold start)", project_id, len(routes))
        return routes

    obs_segs = [s.strip("/").split("/") for s in obs_skels]
    kept: list[dict] = []
    dropped: list[str] = []
    recovered_n = 0
    for r in routes:
        if not isinstance(r, dict):
            continue
        path = r.get("path") or r.get("url") or ""
        if not path:
            continue
        skel = _r212_skel(str(path))
        segs = skel.strip("/").split("/")
        ok = skel in obs_skels or any(
            len(segs) >= 2 and len(o) >= 2 and o[:2] == segs[:2] for o in obs_segs
        )
        if ok:
            kept.append(r)
        else:
            # E1 (R263) — before dropping, try to recover the real prefix from
            # observed traffic (identity-corroborated). Keep the corrected route
            # if recovered; else drop as before (fail-closed).
            _rec = _r261_recover_prefix(r, obs_skels, obs_segs)
            if _rec:
                _fixed = dict(r)
                _fixed["path"] = _rec
                kept.append(_fixed)
                recovered_n += 1
            else:
                dropped.append(f"{r.get('method', 'GET')} {path}")
    if recovered_n:
        log.info("R263: recovered %d source-extracted route(s) for %s by "
                 "identity-matching an observed traffic suffix (mangled prefix "
                 "corrected, not fabricated)", recovered_n, project_id)
    if dropped:
        log.warning(
            "R261: dropped %d of %d source-extracted route(s) for %s — no "
            "observed traffic corroborates them (the Java-Spring "
            "missing-deployment-prefix case). Kept %d (incl. %d recovered). "
            "Samples: %s",
            len(dropped), len(routes), project_id, len(kept), recovered_n, dropped[:5],
        )
    return kept


def save_captured_endpoints(project_id: str, endpoints: list[dict]) -> None:
    """Save captured API endpoints to disk.

    Phase B4: schema extension. Existing entries dedupe by `{method}:{path}`
    (unchanged); incoming entries with richer payload (request/response shape,
    discovered_at, source_har) MERGE into the matching record by:
      - bumping `evidence_count` (how many times we've observed this endpoint)
      - keeping the latest `request_body_shape` / `response_body_shape`
      - keeping the latest `discovered_at` / `source_har`

    Backwards-compat: callers passing the old shape (just method+path+status+
    content_type) still work — missing fields get `None`/0 defaults.
    """
    _CAPTURED_DIR.mkdir(parents=True, exist_ok=True)
    existing = _load_captured_endpoints(project_id)
    by_key = {f"{e.get('method')}:{e.get('path')}": e for e in existing if isinstance(e, dict)}

    polluted = 0
    hygiene_dropped: dict[str, int] = {}
    _r305_writer_on = os.environ.get("ARTA_R305_WRITER_HYGIENE_DISABLE") != "1"
    for ep in endpoints:
        if not isinstance(ep, dict):
            continue
        path = str(ep.get("path") or "")
        # Phase J post-review: defensive filter — paths with `__ARTA_UNSET_*__`
        # sentinels reflect Newman's missing-var fallback, not real SUT
        # traffic. Path-template `{var}` placeholders are valid (the harvester
        # writes those for templated endpoints), but raw Postman `{{var}}`
        # syntax is also a sign of unresolved substitution.
        if "__ARTA_UNSET_" in path or "{{" in path:
            polluted += 1
            continue
        # S2 (R305) — content-type/provenance gate at the WRITE choke-point.
        # Never persist SPA/static routes (content_type text/html, /_next/*.js)
        # or ARTA's own written-back test-traffic echo (source=network + test
        # summary, not 2xx). Both write paths (discovery harvest + execution
        # write-back) flow through here, so this stops the pollution at source.
        if _r305_writer_on:
            _r305_reason = _r305_drop_reason(ep)
            if _r305_reason:
                hygiene_dropped[_r305_reason] = hygiene_dropped.get(_r305_reason, 0) + 1
                continue
        key = f"{ep.get('method')}:{path}"
        if not key or key == "None:None" or not path:
            continue
        if key in by_key:
            cur = by_key[key]
            # Bump evidence count
            cur["evidence_count"] = int(cur.get("evidence_count") or 1) + int(ep.get("evidence_count") or 1)
            # Keep latest shape if provided (R330 P3: + protocol, so non-REST
            # classification survives the merge instead of being dropped)
            for shape_key in ("request_body_shape", "response_body_shape", "status",
                              "content_type", "discovered_at", "source_har", "protocol"):
                if ep.get(shape_key) is not None:
                    cur[shape_key] = ep[shape_key]
            # R160.B — union query-param names across observations
            if ep.get("query_params"):
                seen_q = {q.get("name") for q in cur.get("query_params") or []}
                for q in ep["query_params"]:
                    if isinstance(q, dict) and q.get("name") not in seen_q:
                        cur.setdefault("query_params", []).append(q)
                        seen_q.add(q.get("name"))
            # R313.D — union enum-like value-domain samples across observations so a
            # later save (e.g. a re-probe) never DROPS the captured value domain.
            if isinstance(ep.get("response_value_samples"), dict):
                _dst = cur.setdefault("response_value_samples", {})
                for _f, _vv in ep["response_value_samples"].items():
                    if isinstance(_vv, list):
                        _c = set(_dst.get(_f, [])); _c.update(str(x) for x in _vv)
                        _dst[_f] = sorted(_c)[:12]
        else:
            # Normalize new entry — fill defaults so dashboards don't see None
            new_entry = dict(ep)
            new_entry.setdefault("evidence_count", 1)
            new_entry.setdefault("request_body_shape", None)
            new_entry.setdefault("response_body_shape", None)
            new_entry.setdefault("discovered_at", None)
            new_entry.setdefault("source_har", None)
            by_key[key] = new_entry

    # E2 — capacity headroom. Deeper probe (E2) + E-OpenAPI ingestion add
    # endpoints, and the 500 cap could LRU-evict freshly-grounded routes before
    try:
        _cap = int(os.environ.get("ARTA_CAPTURED_CAP", "500"))
    except (TypeError, ValueError):
        _cap = 500
    merged = list(by_key.values())[-_cap:]   # LRU cap
    (_CAPTURED_DIR / f"{project_id}.json").write_text(json.dumps(merged, indent=2))
    if polluted:
        log.warning(
            "save_captured_endpoints: filtered %d sentinel-laden paths "
            "(__ARTA_UNSET_*) for project %s — these reflect Newman's "
            "missing-var fallback, not real SUT traffic",
            polluted, project_id,
        )
    if hygiene_dropped:
        log.warning(
            "save_captured_endpoints: R305 skipped %d non-API entr(ies) %s for "
            "project %s (SPA/static routes + ARTA test-traffic echo — kept out of "
            "the grounding surface at the source)",
            sum(hygiene_dropped.values()), dict(hygiene_dropped), project_id,
        )
    log.info("Saved %d captured endpoints for project %s", len(merged), project_id)


# ── R320 (Refinement Copilot) — human-correction grounding ──────────────────────
# A tester's correction of an AI-generated test becomes a FIRST-CLASS grounding
# fact with a TRUSTED, distinct provenance (source="human_correction"). It flows
# through the SAME write choke-point (save_captured_endpoints) + read loader
# (_load_captured_endpoints) as every other grounding source, so every validator /
# rewriter (R305 G1/G2, R118, PW/Newman grounding) consumes it with NO extra wiring.
# Provenance separation (R305 lesson): human facts are never conflated with ARTA's
# own traffic — the write-gate keeps them (source!=network) and R206's authoritative
# branch never strips them. The fact is shaped to survive the ENTIRE read chain:
#   • source=human_correction, content_type=application/json → R305 keeps
#   • discovered_at set + NO `summary`                        → R221 keeps
#   • no fabrication marker in path                           → R281 keeps
#   • source in R206 authoritative allowlist                  → R206 keeps

# Sources that represent ARTA's OWN reachable knowledge of the SUT. A correction
# whose route was ALREADY here means ARTA had it and gen ignored it → an upstream
# gen/grounding DEFECT (fix ARTA), not a genuine human-knowledge gap.
_R320_ARTA_ACCESSIBLE_SOURCES = ("openapi", "github", "network", "requirement")
# Provenances that are HUMAN-provided, not ARTA's own autonomous knowledge — a
# match against these is NOT evidence that ARTA should have known the route.
_R320_HUMAN_SOURCES = ("human_correction", "manual")


def classify_correction_provenance(
    project_id: str, method: str, path: str,
    *, kind: str = "endpoint", field: str | None = None, value: object | None = None,
) -> dict:
    """R320 KEYSTONE — was a correction something ARTA SHOULD HAVE KNOWN (already in
    an ARTA-accessible source → upstream gen/grounding DEFECT) vs GENUINE human
    knowledge (absent from every source ARTA can reach — e.g. a SAML-blocked-GitHub
    SUT)?  Reuses the SAME aggressive skeleton the grounding validators use.

    The verdict is KIND-AWARE (the fix for "endpoint-only" over-attribution):
      • endpoint    → arta_knew iff the ROUTE is already captured.
      • shape       → arta_knew iff the matched endpoint has a CAPTURED
                      response_body_shape (gen used a wrong shape despite one).
      • field_value → arta_knew iff the corrected FIELD is in the captured shape
                      OR the corrected VALUE is in the captured response_value_
                      samples (gen ignored a captured detail). If ARTA had the
                      endpoint but NOT the field/value, it genuinely could not
                      know → human_knowledge.

    Returns {"verdict", "matched_source", "matched_path"}.
    """
    from .defect_intel import _r258_skel  # local import avoids a circular module dep
    want = _r258_skel(path or "", aggressive=True)
    m = (method or "GET").upper()
    # Best non-human skeleton match; prefer an explicitly-accessible source.
    best: dict | None = None
    best_src: str | None = None
    for e in (_load_captured_endpoints(project_id) or []):
        if not isinstance(e, dict):
            continue
        if str(e.get("method") or "GET").upper() != m:
            continue
        if _r258_skel(str(e.get("path") or ""), aggressive=True) != want:
            continue
        src = str(e.get("source") or "")
        if src in _R320_HUMAN_SOURCES:
            continue  # a prior human fact isn't ARTA's own autonomous knowledge
        if best is None or (src in _R320_ARTA_ACCESSIBLE_SOURCES
                            and best_src not in _R320_ARTA_ACCESSIBLE_SOURCES):
            best, best_src = e, (src or "discovered")
        if src in _R320_ARTA_ACCESSIBLE_SOURCES and kind == "endpoint":
            break

    if best is None:
        return {"verdict": "human_knowledge", "matched_source": None, "matched_path": None}

    hk = {"verdict": "human_knowledge", "matched_source": None,
          "matched_path": best.get("path")}
    ak = {"verdict": "arta_knew", "matched_source": best_src, "matched_path": best.get("path")}

    if kind == "endpoint":
        return ak
    if kind == "shape":
        # ARTA had the endpoint AND captured a response shape → gen ignored it.
        return ak if best.get("response_body_shape") else hk
    if kind == "field_value":
        shape = best.get("response_body_shape")
        samples = best.get("response_value_samples") or {}
        captured = False
        if field and isinstance(shape, dict) and field in shape:
            captured = True
        if field and value is not None and str(value) in [str(x) for x in (samples.get(field) or [])]:
            captured = True
        # ARTA had the endpoint but the corrected field/value was NOT in what it
        # captured → it genuinely could not know it → human_knowledge.
        return ak if captured else hk
    return ak


def write_human_correction(
    project_id: str,
    *,
    method: str,
    path: str,
    response_value_samples: dict | None = None,
    response_body_shape: object | None = None,
    corrected_by: str | None = None,
    rationale: str | None = None,
) -> dict:
    """R320 — persist a tester correction as a durable grounding fact via the
    canonical write choke-point. Merges onto an existing (method,path) entry when
    present (unions value samples / sets shape). Killswitch
    ARTA_R320_HUMAN_CORRECTION_DISABLE=1 → no write.
    """
    if os.environ.get("ARTA_R320_HUMAN_CORRECTION_DISABLE") == "1":
        return {"written": False, "reason": "disabled"}
    from datetime import datetime as _dt, timezone as _tz
    fact: dict = {
        "method": (method or "GET").upper(),
        "path": path,
        "source": "human_correction",
        "content_type": "application/json",
        "discovered_at": _dt.now(_tz.utc).isoformat(),  # evidence stamp → R221 keeps
        "corrected_by": corrected_by,
        "rationale": rationale,
    }
    if isinstance(response_value_samples, dict) and response_value_samples:
        fact["response_value_samples"] = {
            k: (list(v) if isinstance(v, (list, tuple, set)) else [v])
            for k, v in response_value_samples.items()
        }
    if response_body_shape is not None:
        fact["response_body_shape"] = response_body_shape
    save_captured_endpoints(project_id, [fact])
    return {"written": True, "fact": fact, "key": f"{fact['method']}:{fact['path']}"}


def revert_human_correction(
    project_id: str, *, method: str, path: str,
    field: str | None = None, value: object | None = None,
) -> int:
    """R320 — revert a human correction. Two cases, so revert is TRUTHFUL for
    field/value corrections too (not just standalone endpoint facts):
      1. a STANDALONE `human_correction` entry → drop it entirely;
      2. a field/value the human MERGED onto a NON-human entry (openapi/github/…)
         → un-merge just that value from `response_value_samples[field]` (drop the
         field key / the samples dict when it empties), leaving the real capture
         intact.
    Never touches non-human capture beyond the specific human-added value.
    Returns the count of changes."""
    p = _CAPTURED_DIR / f"{project_id}.json"
    if not p.exists():
        return 0
    try:
        raw = json.loads(p.read_text())
    except Exception:
        return 0
    if not isinstance(raw, list):
        return 0
    m = (method or "GET").upper()
    out: list = []
    removed = 0
    for e in raw:
        if not isinstance(e, dict):
            out.append(e)
            continue
        same = (str(e.get("method") or "GET").upper() == m
                and str(e.get("path") or "") == path)
        # Case 1 — drop the standalone human fact.
        if same and str(e.get("source") or "") == "human_correction":
            removed += 1
            continue
        # Case 2 — un-merge the human's value from a real (non-human) entry.
        if (same and field and value is not None
                and isinstance(e.get("response_value_samples"), dict)):
            samples = e["response_value_samples"]
            vals = samples.get(field)
            if isinstance(vals, list) and any(str(x) == str(value) for x in vals):
                samples[field] = [x for x in vals if str(x) != str(value)]
                removed += 1
                if not samples[field]:
                    samples.pop(field, None)
                if not samples:
                    e.pop("response_value_samples", None)
        out.append(e)
    if removed:
        p.write_text(json.dumps(out, indent=2))
    return removed


# ── R330 (SUT-Understanding P1) — grounding coverage + per-endpoint provenance ──
# ARTA already tags every captured endpoint with its provenance (`source`). This
# makes that provenance HONEST + VISIBLE: how much of the SUT surface ARTA actually
# KNOWS (from the SUT's OpenAPI/source) vs merely OBSERVED vs DECLARED by a
# requirement vs HUMAN-corrected. The operator can then trust or challenge the
# grounding — and a low source-grounded share is a signal to fix gen at the source.

# Provenance strength, strongest→weakest. openapi/github = ARTA read the SUT's own
# contract/source; human_correction/manual = a human verified it; requirement =
# declared (may be idealized); network = merely observed once.
_R330_PROVENANCE_RANK = {
    "github": 6, "openapi": 6, "manual": 5, "human_correction": 5,
    "requirement": 3, "network": 2,
    # R330 P1d — probe-captured response evidence IS runtime observation; without
    # this entry it ranked 0 and real probe traffic was counted "unlabeled".
    "r212_probe_response_capture": 2,
}


def endpoint_provenance(endpoint: dict) -> str:
    """R330 — the honest provenance bucket for a captured endpoint."""
    e = endpoint or {}
    src = str(e.get("source") or "").lower()
    if src in ("openapi", "github"):
        return "source_grounded"      # ARTA read the SUT's contract/source
    if src in ("human_correction", "manual"):
        return "human_corrected"
    if src == "requirement":
        return "requirement_declared"
    if src in ("network", "r212_probe_response_capture"):
        return "observed"
    # No explicit `source`, but real HAR-capture evidence (the HAR-harvest write
    # path stamps source_har/discovered_at, not source="network") → it WAS observed
    # at runtime. Honest: this is REAL traffic, not "unlabeled/unknown".
    if e.get("source_har") or e.get("discovered_at"):
        return "observed"
    return "unlabeled"


def grounding_coverage(project_id: str) -> dict:
    """R330 — aggregate the captured-endpoint store by provenance so the operator
    sees how well-grounded ARTA's SUT understanding is. Reuses the SAME loader
    (`_load_captured_endpoints`) every validator/generator consumes, so the number
    can never disagree with what gen actually grounds on."""
    from collections import Counter
    eps = _load_captured_endpoints(project_id) or []
    buckets: Counter = Counter()
    by_source: Counter = Counter()
    for e in eps:
        if not isinstance(e, dict):
            continue
        buckets[endpoint_provenance(e)] += 1
        by_source[str(e.get("source") or "unlabeled")] += 1
    total = sum(buckets.values())
    # "grounded" = KNOWN-REAL: read from the SUT's contract/source, human-verified,
    # OR observed serving real traffic. requirement_declared is weaker (may be
    # idealized) and unlabeled/guess is unknown, so both are excluded.
    source_grounded = buckets.get("source_grounded", 0)
    grounded = source_grounded + buckets.get("human_corrected", 0) + buckets.get("observed", 0)
    return {
        "total_endpoints": total,
        "by_provenance": dict(buckets),
        "by_source": dict(by_source),
        "grounded_endpoints": grounded,
        "grounded_pct": round(grounded / total * 100, 1) if total else 0.0,
        # The ASPIRATIONAL target — understood from the SUT's OWN source/contract
        # (not merely observed). A low value on a source-blocked SUT is the gap to
        # close via GitHub access or R320 human correction.
        "source_grounded_endpoints": source_grounded,
        "source_grounded_pct": round(source_grounded / total * 100, 1) if total else 0.0,
    }


_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def _openapi_spec_for(project_id: str) -> dict:
    """Load the SUT's raw OpenAPI contract at .arta/openapi/<pid>.json (R206)."""
    p = _OPENAPI_DIR / f"{project_id}.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        log.debug("R330: OpenAPI spec load failed for %s: %s", project_id, exc)
        return {}


def _resolve_openapi_ref(spec: dict, node, _depth: int = 0):
    """Resolve a local `#/...` $ref (chained, bounded) against `spec`."""
    while isinstance(node, dict) and "$ref" in node and _depth < 8:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            break
        cur = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                return {}
        node = cur
        _depth += 1
    return node


def openapi_param_details(project_id: str) -> dict:
    """R330 P2 — mine per-endpoint param constraints (enum/min/max/pattern/
    required) from the SUT's OWN OpenAPI contract (.arta/openapi/<pid>.json,
    fetched via R206 but never extracted into params_detail). Keyed by the
    templated ``"METHOD /path"``. The contract DECLARES the allowed values;
    generation must use them, not guess. Returns {} when no contract."""
    spec = _openapi_spec_for(project_id)
    paths = spec.get("paths") if isinstance(spec, dict) else None
    if not isinstance(paths, dict):
        return {}
    out: dict = {}
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        common = methods.get("parameters") or []   # path-level params (all methods)
        for method, op in methods.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            # R330 P2b — ARTA-merged stub ops (persist_openapi_doc) are not the
            # SUT's contract; only ops the real spec declared may ground params.
            if op.get("x-arta-stub") or op.get("summary") == "arta-openapi-ingested":
                continue
            raw = list(common) + list(op.get("parameters") or [])
            details: list = []
            for prm in raw:
                prm = _resolve_openapi_ref(spec, prm)
                if not isinstance(prm, dict) or not prm.get("name"):
                    continue
                schema = _resolve_openapi_ref(spec, prm.get("schema") or {})
                d = {"name": prm["name"], "in": prm.get("in"),
                     "required": bool(prm.get("required"))}
                if isinstance(schema, dict):
                    for k in ("enum", "minimum", "maximum", "pattern", "type", "format"):
                        if schema.get(k) is not None:
                            d[k] = schema[k]
                # keep only params that carry a real, testable constraint
                if any(k in d for k in ("enum", "minimum", "maximum", "pattern")) or d["required"]:
                    details.append(d)
            if details:
                out[f"{method.upper()} {path}"] = details
    return out


def enrich_endpoints_with_openapi_params(project_id: str, endpoints: list[dict]) -> list[dict]:
    """R330 P2 — fill missing ``params_detail`` on captured endpoints from the
    SUT's OpenAPI contract so ``param_constraint_block`` feeds real constraints to
    gen. Matches by templated path (a contract ``{param}`` segment matches any
    captured value). Mutates only the transient in-memory gen list, never the
    persisted store. Killswitch ARTA_R330_PARAM_CONSTRAINTS_DISABLE=1."""
    if os.environ.get("ARTA_R330_PARAM_CONSTRAINTS_DISABLE") == "1":
        return endpoints
    detail_map = openapi_param_details(project_id)
    if not detail_map:
        return endpoints
    contract: list = []   # (method, [segments], details)
    for key, det in detail_map.items():
        m, _, pth = key.partition(" ")
        contract.append((m, [s for s in pth.split("/") if s], det))
    filled: list[dict] = []
    for e in endpoints:
        if not isinstance(e, dict) or e.get("params_detail"):
            continue
        em = (e.get("method") or "GET").upper()
        esegs = [s for s in (e.get("path") or "").split("/") if s]
        for m, segs, det in contract:
            if m != em or len(segs) != len(esegs):
                continue
            if all((cs.startswith("{") and cs.endswith("}")) or cs.lower() == es.lower()
                   for cs, es in zip(segs, esegs)):
                e["params_detail"] = det
                filled.append(e)
                break
    # R330 P2b — persist the mined constraints onto the EXISTING store entries so
    # validators/dispatch/dashboards see the same constraints gen does (they were
    # re-mined transiently on every call; params_detail was 0 on disk everywhere).
    # Annotates only entries already in the store — adds no endpoint, so the
    # S2/R305 write-gates (which guard PATH admission) are not bypassed.
    if filled:
        try:
            persist_params_detail(project_id, filled)
        except Exception as exc:
            log.debug("R330 P2b: params_detail persist skipped: %s", exc)
    return endpoints


def persist_params_detail(project_id: str, endpoints: list[dict]) -> int:
    """R330 P2b — write `params_detail` onto matching EXISTING entries of the
    captured-endpoint store (keyed by method+path). Never adds entries; never
    overwrites a non-empty params_detail. Returns entries updated."""
    path = _CAPTURED_DIR / f"{project_id}.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0
    det_by_key = {
        f"{(e.get('method') or 'GET').upper()}:{e.get('path')}": e["params_detail"]
        for e in endpoints
        if isinstance(e, dict) and e.get("path") and e.get("params_detail")
    }
    updated = 0
    for entry in data:
        if not isinstance(entry, dict) or entry.get("params_detail"):
            continue
        key = f"{(entry.get('method') or 'GET').upper()}:{entry.get('path')}"
        if key in det_by_key:
            entry["params_detail"] = det_by_key[key]
            updated += 1
    if updated:
        path.write_text(json.dumps(data, indent=2))
    return updated


def mine_path_param_values(paths: list[str]) -> dict[str, str]:
    """R330 P2b — {param_name: concrete_value} mined by matching a TEMPLATED
    captured path (`…/servers/{serverId}`) against a CONCRETE sibling of
    identical shape (`…/servers/server-1f9983ab`) and reading the differing
    segment. Algorithm lifted from dispatch's R312.B resolver so gen and
    dispatch share ONE miner (single source of truth); dispatch's
    _r312_params_from_captured_paths now delegates here."""
    clean: list[str] = []
    for p in paths or []:
        if isinstance(p, str) and p.startswith("/"):
            clean.append(p.split("?")[0].rstrip("/"))
    templated = [p for p in clean if "{" in p]
    concrete = [p for p in clean if "{" not in p]
    out: dict[str, str] = {}
    for tp in templated:
        tsegs = tp.split("/")
        tparam_pos = {i: seg[1:-1] for i, seg in enumerate(tsegs)
                      if seg.startswith("{") and seg.endswith("}")}
        if not tparam_pos:
            continue
        for cp in concrete:
            csegs = cp.split("/")
            if len(csegs) != len(tsegs):
                continue
            if any(tsegs[i] != csegs[i] for i in range(len(tsegs)) if i not in tparam_pos):
                continue
            for i, pname in tparam_pos.items():
                val = csegs[i]
                if pname not in out and val and "{" not in val:
                    out[pname] = val
    return out


def select_param_relevant_endpoints(
    captured: list, gherkin_text: str, top_n: int = 20,
) -> tuple[list, dict]:
    """R330 P2b — the Gherkin-relevance filter for the param block, with a
    TRUTHFUL fallback + counters. The original substring filter had no
    fallback, so a non-matching Gherkin emptied a fully-known constraint set
    SILENTLY (indistinguishable from "nothing known"). When the filter empties
    a non-empty set, fall back to the capped endpoints that actually CARRY
    constraints. Returns (endpoints, {"known": n, "relevant": n[, "fallback": n]})."""
    gwords = {w.lower() for w in re.findall(r"[A-Za-z_]{4,}", gherkin_text or "")}
    eps = [e for e in (captured or []) if isinstance(e, dict)]
    rel = [e for e in eps
           if any(w in (e.get("path") or "").lower() for w in gwords)]
    stats = {"known": len(eps), "relevant": len(rel)}
    if not rel and eps:
        rel = [e for e in eps
               if e.get("params_detail") or e.get("query_params")][:top_n]
        stats["fallback"] = len(rel)
    return rel, stats


def param_constraint_block(
    endpoints: list[dict], max_endpoints: int = 20, max_chars: int = 900,
) -> str:
    """R330 P2 — a deterministic block of the KNOWN param constraints + value
    domains for the given endpoints, so gen uses REAL values UPSTREAM (in the
    prompt) instead of guessing and relying on the flaky post-LLM R313 rewrite.

    TWO DISTINCT groundings, kept SEPARATE — conflating them makes the LLM send a
    response-field name as a request param (an invented ?status=Ready → 4xx):
      • REQUEST param constraints — OpenAPI `params_detail` (enum/min/max/pattern/
        required). Grounds how a request is CONSTRUCTED. The primary lever.
      • RESPONSE field value domains — observed `response_value_samples`. Grounds
        what an ASSERTION may expect (R313/R316 also reground these post-LLM).

    Char-budgeted (request params first, since they fix runtime 4xx) to avoid the
    documented prompt-bloat → truncation regression (R119.A / P1 endpoint-
    relevance 52.9%→38.6%). Returns "" when nothing is known — never fabricates.
    Killswitch ARTA_R330_PARAM_CONSTRAINTS_DISABLE=1."""
    if os.environ.get("ARTA_R330_PARAM_CONSTRAINTS_DISABLE") == "1":
        return ""
    # R330 P2b — path-param example values mined from concrete captured siblings
    # (the SAME miner dispatch uses to resolve ARTA_PP_*): grounds the URL itself
    # at gen time instead of leaving `${process.env.ARTA_PP_X || ''}` → 404.
    _mined = mine_path_param_values(
        [e.get("path") for e in (endpoints or []) if isinstance(e, dict)])
    req_lines: list[str] = []
    resp_lines: list[str] = []
    for e in (endpoints or [])[:max_endpoints]:
        if not isinstance(e, dict) or not e.get("path"):
            continue
        method = (e.get("method") or "GET").upper()
        path = e.get("path")
        rp: list[str] = []
        seen: set = set()
        for pd in (e.get("params_detail") or []):
            nm = pd.get("name")
            if not nm:
                continue
            c: list[str] = []
            if pd.get("enum"):
                c.append("∈ [" + ", ".join(str(x) for x in pd["enum"][:8]) + "]")
            if pd.get("minimum") is not None or pd.get("maximum") is not None:
                c.append(f"range {pd.get('minimum', '')}..{pd.get('maximum', '')}")
            if pd.get("pattern"):
                c.append(f"pattern /{pd['pattern']}/")
            has_value_constraint = bool(c)
            if pd.get("required"):
                c.append("required")
            # A path param with ONLY `required` is redundant — the URL template
            # already implies it. Keep query/header requireds (gen omits them →
            # 400s) and every real value constraint (enum/range/pattern).
            if c and not (pd.get("in") == "path" and not has_value_constraint):
                rp.append(f"{nm}: {'; '.join(c)}")
                seen.add(nm)
        # sut_topology `query_params` (EO-2): the REAL required query params +
        # CAPTURED example values (page_size=1000, the exact filters=[…] syntax) —
        # grounding data that already sits on the store endpoint but was injected
        # into gen NOWHERE. A captured value grounds request construction better
        # than any invented one. Skip redacted markers; length-cap long values.
        for qp in (e.get("query_params") or []):
            nm = qp.get("name")
            if not nm or nm in seen:
                continue
            val = qp.get("value")
            if val not in (None, "", "<<REDACTED_HEADER_VALUE>>"):
                sval = str(val)
                if len(sval) > 80:
                    sval = sval[:77] + "…"
                rp.append(f"{nm}=e.g. {sval}")
            else:
                c = []
                if qp.get("required"):
                    c.append("required")
                if qp.get("type"):
                    c.append(str(qp["type"]))
                if c:
                    rp.append(f"{nm}: {' '.join(c)}")
                else:
                    continue
            seen.add(nm)
        # R330 P2b — concrete example value for each {param} in THIS path.
        if "{" in (path or ""):
            for seg in path.split("/"):
                if seg.startswith("{") and seg.endswith("}"):
                    pname = seg[1:-1]
                    if pname and pname not in seen and pname in _mined:
                        rp.append(f"{pname}(path)=e.g. {_mined[pname]}")
                        seen.add(pname)
        if rp:
            req_lines.append(f"{method} {path} — " + " | ".join(rp))
        fp: list[str] = []
        for fld, vals in (e.get("response_value_samples") or {}).items():
            if isinstance(vals, list) and vals:
                fp.append(f"{fld} ∈ [" + ", ".join(str(x) for x in vals[:6]) + "]")
        if fp:
            resp_lines.append(f"{method} {path} — " + " | ".join(fp))
    if not req_lines and not resp_lines:
        return ""

    def _fit(lines: list[str], budget: int) -> list[str]:
        """Greedily keep whole lines while under the char budget."""
        kept, used = [], 0
        for ln in lines:
            if used + len(ln) + 1 > budget:
                break
            kept.append(ln)
            used += len(ln) + 1
        return kept

    sections: list[str] = []
    req_kept = _fit(req_lines, max_chars)   # request params get budget priority
    if req_kept:
        sections.append(
            "[HARD CONSTRAINT — REQUEST PARAM VALUES] For query/path/header params on\n"
            "these endpoints, use ONLY these SUT-declared/observed values; do NOT invent\n"
            "a param value or omit a required one (missing-required / out-of-enum → 4xx).\n"
            "`name=e.g. X` is a CAPTURED working value — reuse it:\n"
            + "\n".join(req_kept))
    resp_budget = max_chars - sum(len(x) + 1 for x in req_kept)
    resp_kept = _fit(resp_lines, resp_budget) if resp_budget > 120 else []
    if resp_kept:
        sections.append(
            "[GROUNDING — OBSERVED RESPONSE FIELD VALUES] When ASSERTING on a response\n"
            "field below, expect one of its observed values; these are RESPONSE data,\n"
            "NOT request params:\n" + "\n".join(resp_kept))
    return "\n\n".join(sections)


def purge_polluted_endpoints(project_id: str) -> int:
    """Phase J post-review: clean up an existing polluted endpoint store.

    Walks the persisted file and removes entries whose path contains
    `__ARTA_UNSET_` or `{{` (raw Postman var). Returns the count of
    removed entries.

    Called once at app startup OR by the discovery refresh route to
    drop sentinel pollution from prior runs that wrote sentinel URLs
    before the filter at line 481-489 was added.
    """
    path = _CAPTURED_DIR / f"{project_id}.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    before = len(data)
    cleaned = [
        e for e in data
        if isinstance(e, dict)
        and "__ARTA_UNSET_" not in str(e.get("path") or "")
        and "{{" not in str(e.get("path") or "")
    ]
    removed = before - len(cleaned)
    if removed:
        path.write_text(json.dumps(cleaned, indent=2))
        log.info(
            "purge_polluted_endpoints: removed %d/%d polluted entries for project %s",
            removed, before, project_id,
        )
    return removed


# ── Phase C3 — chain persistence ────────────────────────────────────────────


def _load_chains(project_id: str) -> dict[str, dict]:
    """Load chains keyed by chain_id. Returns {} when none persisted yet."""
    path = _CHAINS_DIR / f"{project_id}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("chain load failed for %s: %s", project_id, exc)
        return {}
    return {entry.get("chain_id"): entry for entry in (data or []) if isinstance(entry, dict) and entry.get("chain_id")}


def save_chains(project_id: str, chains: list) -> None:
    """Phase C3: dedup chains by `chain_id`, bump occurrence_count on duplicates.

    Args:
        chains: list of `CallChain` instances OR dicts (round-tripped via to_dict).
                Both shapes accepted so callers don't have to re-import the dataclass.

    LRU eviction: when more than `_MAX_CHAINS_PER_PROJECT` chains exist,
    drop oldest by `captured_at`. The Phase I5 chain-replay loop targets
    the top-N most-frequent chains, so frequency-weighted retention is the
    right policy.
    """
    _CHAINS_DIR.mkdir(parents=True, exist_ok=True)
    existing = _load_chains(project_id)

    for chain in chains:
        # Normalize input to dict
        if hasattr(chain, "to_dict") and callable(chain.to_dict):
            chain_dict = chain.to_dict()
        elif isinstance(chain, dict):
            chain_dict = chain
        else:
            continue
        cid = chain_dict.get("chain_id")
        if not cid:
            continue
        if cid in existing:
            existing[cid]["occurrence_count"] = int(existing[cid].get("occurrence_count") or 1) + 1
            existing[cid]["last_observed_at"] = chain_dict.get("captured_at") or existing[cid].get("last_observed_at")
            # Update node-level dataflow if newer chain has more links
            new_nodes = chain_dict.get("nodes") or []
            old_nodes = existing[cid].get("nodes") or []
            if len(new_nodes) == len(old_nodes):
                for old, new in zip(old_nodes, new_nodes):
                    # Merge provides/consumes — union of seen links
                    for k, v in (new.get("provides") or {}).items():
                        old.setdefault("provides", {}).setdefault(k, v)
                    for k, v in (new.get("consumes") or {}).items():
                        old.setdefault("consumes", {}).setdefault(k, v)
        else:
            chain_dict.setdefault("occurrence_count", 1)
            chain_dict.setdefault("last_observed_at", chain_dict.get("captured_at"))
            existing[cid] = chain_dict

    # LRU cap
    if len(existing) > _MAX_CHAINS_PER_PROJECT:
        ordered = sorted(
            existing.values(),
            key=lambda e: (e.get("last_observed_at") or "", e.get("occurrence_count") or 0),
            reverse=True,
        )
        existing = {e["chain_id"]: e for e in ordered[:_MAX_CHAINS_PER_PROJECT]}

    out_path = _CHAINS_DIR / f"{project_id}.json"
    out_path.write_text(json.dumps(list(existing.values()), indent=2, default=str))
    log.info("Saved %d chains for project %s (added/merged %d this run)",
             len(existing), project_id, len(chains))


def load_chains(project_id: str) -> list[dict]:
    """Public read-only accessor for Phase D (Newman generation reads chains)
    and Phase I5 (replay loop)."""
    return list(_load_chains(project_id).values())


# ── R19b — DOM-snapshot persistence (per-project, per-route) ────────────────
#
# Phase R19 motivation: 226 of 255 Playwright failures in run-23aa57 were
# `getByTestId('<not-in-DOM>')` — the LLM hallucinated plausible-sounding
# testids from AC text. The discovery probe (R19a) now captures every
# `[data-testid]` element + role landmarks per visited SPA route as
# sibling JSON files. R19b ingests those sidecars into a project-scoped
# catalog the ATDD designer (R19c) reads + the validator (R19d) checks.
#
# Storage: `.arta/discovery/{project_id}/dom_catalog.json` — single file,
# keyed by route. Overwrites on each discovery run (DOM is current state,
# not historical). When merging multiple runs, the LATEST snapshot wins
# per-route — testids come and go as the SPA evolves; stale entries
# would re-introduce hallucination risk.

_DOM_CATALOG_DIR = Path(".arta/discovery")


# ── R117 — DOM catalog cleanliness rules (single source of truth) ──
#
# Pre-R117: discovery probe captured raw `el.textContent` (entire subtree
# of an element) into the catalog. For `<main>` landmarks, this produced
# engine to get startedEXTRACT...". R47.1b injected these verbatim into
# the Playwright prompt; the LLM faithfully emitted
# which matches ZERO elements at runtime. R101.D/R102.A then stamped the
# spec as BLOCKED → Fix FF quarantined to .broken-dryrun.
#
# R117.A KEYSTONE: filter dirty entries at INGEST time so the catalog
# never persists smushed names. R117.G: dirty-catalog detector used by
# R117.F startup migration.

_R117_LANDMARK_ROLES = frozenset({
    "main", "banner", "complementary", "navigation",
    "contentinfo", "region",
})

# Button-like words: if a landmark role's text contains these, the
# textContent has captured nested interactive elements. Reject the
# landmark entry from the catalog.
_R117_BUTTON_LIKE_RE = re.compile(
    r"\b(?:Click|Submit|Save|Cancel|Delete|EXTRACT|Sign\s*[Ii]n|"
    r"Sign\s*[Uu]p|Login|Logout|Continue|Next|Back|Confirm|"
    r"OK|Apply|Search)\b",
)

# CamelCase boundary: `a-z` immediately followed by `A-Z` (e.g.,
# "CloudSelect"). Indicates two DOM fragments concatenated without
# whitespace. Reuses R95.3's logic (single source of truth).
_R117_CAMEL_BOUNDARY_RE = re.compile(r"[a-z][A-Z]")

# Multi-sentence: period+space+capital (`.` followed by ` A-Z`)
_R117_MULTI_SENTENCE_RE = re.compile(r"\.\s+[A-Z]")


def _r117_a_clean_catalog_entry(
    el: dict,
    _stats: dict | None = None,
) -> dict | None:
    """R117.A — clean a single DOM catalog entry. Returns the cleaned
    entry or None when the entry is smushed/landmark-with-everything
    and should be rejected from the catalog.

    Rules (priority order):
      1. Strip newlines + collapse multi-whitespace in text/ariaLabel
      2. Prefer ariaLabel > name > text (accessibility-first)
      3. Reject text >60 chars (single accessible-name shouldn't exceed)
      4. Reject text with camelCase boundary (`a-z[A-Z]`) — smushed
      5. Reject text with multi-sentence (period + space + capital)
      6. Reject landmark roles (main/banner/etc.) when text >40 chars
         OR contains button-like words (nested interactive content)
      7. Pass everything else through with normalized fields

    `_stats` dict (if provided) increments rejection counters by reason.
    """
    if not isinstance(el, dict):
        return None
    # Normalize text fields: strip newlines, collapse whitespace
    def _norm(s):
        if not isinstance(s, str):
            return s
        return " ".join(s.split())
    el_out = dict(el)
    for fld in ("text", "ariaLabel", "name"):
        if fld in el_out:
            val = el_out[fld]
            if isinstance(val, str):
                el_out[fld] = _norm(val) or None
    # Accessibility-first: prefer ariaLabel > name > text as the
    # effective "label" we evaluate against R117.A rules
    label = el_out.get("ariaLabel") or el_out.get("name") or el_out.get("text") or ""
    role = (el_out.get("role") or el_out.get("tag") or "").lower()

    # testid-bearing entries always pass through — they have a
    # canonical addressable selector regardless of label quality
    if el_out.get("testid"):
        if _stats is not None:
            _stats["passed_testid"] = _stats.get("passed_testid", 0) + 1
        return el_out

    if not label:
        # No label AND no testid → not useful as a selector candidate
        if _stats is not None:
            _stats["rejected_no_label"] = _stats.get("rejected_no_label", 0) + 1
        return None

    # Length cap: single accessible-name should never exceed 60 chars
    if len(label) > 60:
        if _stats is not None:
            _stats["rejected_too_long"] = _stats.get("rejected_too_long", 0) + 1
        return None

    # CamelCase boundary = smushed fragments
    if _R117_CAMEL_BOUNDARY_RE.search(label):
        if _stats is not None:
            _stats["rejected_smushed"] = _stats.get("rejected_smushed", 0) + 1
        return None

    # Multi-sentence
    if _R117_MULTI_SENTENCE_RE.search(label):
        if _stats is not None:
            _stats["rejected_multi_sentence"] = _stats.get("rejected_multi_sentence", 0) + 1
        return None

    # Landmark + nested content
    if role in _R117_LANDMARK_ROLES:
        if len(label) > 40 or _R117_BUTTON_LIKE_RE.search(label):
            if _stats is not None:
                _stats["rejected_landmark"] = _stats.get("rejected_landmark", 0) + 1
            return None

    if _stats is not None:
        _stats["normalized"] = _stats.get("normalized", 0) + 1
    return el_out


def _is_dirty_catalog(catalog: dict) -> bool:
    """R117.G — return True if any entry in this catalog violates R117.A
    cleanliness rules. Used by R117.F startup migration to decide whether
    to rebuild a catalog from cached HAR sidecars without operator action.

    Cheap O(N) scan; reuses the same regexes as R117.A (single source of
    truth). Safe to call on every catalog at every arta-api boot.
    """
    if not isinstance(catalog, dict):
        return False
    routes = catalog.get("routes") or {}
    if not isinstance(routes, dict):
        return False
    for elements in routes.values():
        if not isinstance(elements, list):
            continue
        for el in elements:
            if not isinstance(el, dict):
                continue
            text = el.get("text") or el.get("ariaLabel") or el.get("name") or ""
            if not isinstance(text, str):
                continue
            if len(text) > 60:
                return True
            if "\n" in text:
                return True
            if _R117_CAMEL_BOUNDARY_RE.search(text):
                return True
            if _R117_MULTI_SENTENCE_RE.search(text):
                return True
            role = (el.get("role") or el.get("tag") or "").lower()
            if role in _R117_LANDMARK_ROLES and (
                len(text) > 40 or _R117_BUTTON_LIKE_RE.search(text)
            ):
                return True
    return False


import os as _os_ad

# R268 — a route that legitimately SHOWS login chrome (the login page itself).
_R268_AUTH_ROUTE_RE = re.compile(
    r"(^|/)(login|signin|sign-in|sign_in|auth|logout|register|signup|sign-up"
    r"|forgetpassword|forgot-password|resetpassword|reset-password|activateuser)(/|$)",
    re.IGNORECASE,
)
# The unmistakable login-form signature: a sign-in control/heading paired with
# an email/username field. Deliberately narrow — a feature page that merely has
# a "Log in to continue" link must not be evicted.
_R268_LOGIN_LABEL_RE = re.compile(r"^\s*(sign\s*in|signin|log\s*in|login)\s*$", re.IGNORECASE)
_R268_ID_FIELD_RE = re.compile(r"\b(e-?mail|username|user\s*name)\b", re.IGNORECASE)


# R269.B — catalog-side twin of the probe's R269 derivation filter. Same two
# unambiguous non-page shapes; deliberately no verb heuristic (`/Account/GetX`
# survives here too — a `Get*` rule would also match `getOrCreateX`).
_R269_B_TMPL_RE = re.compile(r"\{[^}]*\}|(^|/):[A-Za-z]")
_R269_B_ASSET_RE = re.compile(
    r"\.(css|js|mjs|map|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|json|txt|xml)$"
    r"|(^|/)(css|scss|assets?|static|bootstrap|fonts?|images?|img|dist|build)(/|$)",
    re.IGNORECASE,
)


def _r269_b_is_nonpage_route(route: str) -> bool:
    """True when a cataloged route cannot be a real SPA page.

    `/{id}/Overview` is an API contract template (never a literal URL a browser
    can open); `/css` and `/bootstrap/4.1.3` are static assets. Both classes got
    into the catalog via the pre-R269 derivation and then survived every later
    run because nothing evicts them.
    """
    if not route or not isinstance(route, str) or route == "/":
        return False
    return bool(_R269_B_TMPL_RE.search(route) or _R269_B_ASSET_RE.search(route))


def _r268_is_login_chrome(elems) -> bool:
    """True when a cataloged route is really just the login wall.

    Requires BOTH a sign-in label AND an identity input — either alone is too
    weak (the R143.G false-positive class: any page with a 'Sign in' link would
    match). Small-page guard: a rich authenticated page that happens to contain
    a sign-in string is not login chrome.
    """
    if not isinstance(elems, list) or not elems or len(elems) > 15:
        return False
    labels = [
        str((e.get("text") or e.get("name") or e.get("ariaLabel") or ""))
        for e in elems if isinstance(e, dict)
    ]
    has_signin = any(_R268_LOGIN_LABEL_RE.match(t) for t in labels)
    has_identity = any(_R268_ID_FIELD_RE.search(t) for t in labels)
    return has_signin and has_identity


def ingest_dom_snapshots(project_id: str, har_path: str | Path) -> dict:
    """R19b — read every `dom*.json` file written by `discovery_probe.spec.ts`
    next to the HAR + persist into a project-scoped catalog.

    Returns the catalog dict for callers that want to inspect it inline:
        {
          "routes": {
            "/dashboard": [{testid, tag, role, text, visible}, ...],
            "/projects":  [...],
          },
          "captured_at": "...",
          "testid_count": 14,
        }

    Best-effort:
      - HAR dir doesn't exist → empty catalog
      - Sidecar JSON malformed → that route skipped, others ingested
      - Project_id empty → no-op
    """
    if not project_id:
        return {"routes": {}, "testid_count": 0}
    har_dir = Path(har_path).parent
    if not har_dir.is_dir():
        return {"routes": {}, "testid_count": 0}

    routes: dict[str, list[dict]] = {}
    captured_at: str | None = None
    spa_hash_routing = False  # R264.A — probe-detected HashRouter (R219.E)
    sidecars = sorted(har_dir.glob("dom*.json"))
    # R117.A — accumulate rejection telemetry across all sidecars
    r117_a_stats = {
        "total_input": 0,
        "passed_testid": 0,
        "normalized": 0,
        "rejected_no_label": 0,
        "rejected_too_long": 0,
        "rejected_smushed": 0,
        "rejected_multi_sentence": 0,
        "rejected_landmark": 0,
    }
    for sc in sidecars:
        try:
            data = json.loads(sc.read_text())
        except Exception as exc:
            log.debug("R19b: skipping malformed sidecar %s: %s", sc, exc)
            continue
        if not isinstance(data, dict):
            continue
        route = data.get("route")
        elements = data.get("elements")
        if not isinstance(route, str) or not isinstance(elements, list):
            continue
        # R264.A — carry the probe's R219.E routing detection into the catalog.
        # ANY sidecar reporting hash routing wins: the probe only flips the flag
        # ON (after detecting `location.hash` at `/`), and the `/` sidecar itself
        # is written before detection, so this is an OR across sidecars.
        if data.get("spa_hash_routing") is True:
            spa_hash_routing = True
        # R117.A — clean each entry BEFORE dedupe so dirty entries
        # don't poison the dedupe set (a smushed entry and a clean one
        # with same testid+role would dedupe to one of them; we want to
        # keep ONLY clean entries in the set).
        seen: set[tuple] = set()
        deduped: list[dict] = []
        for el in elements:
            r117_a_stats["total_input"] += 1
            el_clean = _r117_a_clean_catalog_entry(el, _stats=r117_a_stats)
            if el_clean is None:
                continue
            key = (el_clean.get("testid"), el_clean.get("text"), el_clean.get("role"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(el_clean)
        routes[route] = deduped
        if isinstance(data.get("captured_at"), str):
            captured_at = data["captured_at"]

    # R203 — non-destructive merge: never replace a previously-captured route
    # with a strictly SPARSER capture. A sparse probe window (login-wall /
    # partial hydration) or the R117.F boot rebuild picking a poor HAR
    # otherwise CLOBBERS a rich catalog (observed 30→2 testids between gen and
    # run). Keep the richer entry per route; add new routes; preserve existing
    # routes absent from this capture. Killswitch
    # ARTA_R203_CATALOG_MERGE_DISABLE=1 → legacy destructive write.
    import os as _os_r203
    if _os_r203.environ.get("ARTA_R203_CATALOG_MERGE_DISABLE") != "1":
        try:
            ex_routes = (load_dom_catalog(project_id).get("routes") or {})
            if ex_routes:
                def _r203_richness(elems):
                    if not isinstance(elems, list):
                        return (0, 0)
                    tids = sum(1 for e in elems if isinstance(e, dict) and e.get("testid"))
                    rns = sum(1 for e in elems if isinstance(e, dict)
                              and (e.get("role") or e.get("tag"))
                              and (e.get("text") or e.get("ariaLabel") or e.get("name")))
                    return (tids, rns)
                merged = dict(ex_routes)
                kept = 0
                for rt, new_elems in routes.items():
                    if rt not in merged or _r203_richness(new_elems) >= _r203_richness(merged[rt]):
                        merged[rt] = new_elems
                    else:
                        kept += 1   # keep the richer existing entry (sparser capture rejected)
                if kept or len(merged) > len(routes):
                    log.info(
                        "R203: catalog merge — %d new-capture route(s) + %d existing; "
                        "kept %d richer existing route(s) (sparse-clobber prevented)",
                        len(routes), len(ex_routes), kept)
                # R269.B — evict STALE non-page husks from the catalog.
                # R269 stops the probe DERIVING these from captured API paths,
                # but entries already in the catalog from earlier runs survive
                # forever: they are not login chrome, so R268 leaves them. On
                # `/{id}/Overview`), each a 2-element Yes/No husk that adds noise
                # to the grounding prompt. Post-R271 they also cost a FULL page
                # load each in the walk. Only evicted when this run produced no
                # fresh capture for them — a route the probe really walked stays.
                # Killswitch ARTA_R269_B_EVICT_NONPAGE_DISABLE=1.
                if _os_ad.environ.get("ARTA_R269_B_EVICT_NONPAGE_DISABLE") != "1":
                    husks = [
                        rt for rt in merged
                        if rt not in routes and _r269_b_is_nonpage_route(rt)
                    ]
                    for rt in husks:
                        merged.pop(rt, None)
                    if husks:
                        log.info(
                            "R269.B: evicted %d stale non-page route(s) from the catalog "
                            "for %s (no fresh capture + not an SPA page): %s",
                            len(husks), project_id, ", ".join(husks[:5]))
                # R268 — evict STALE login chrome for NON-auth routes.
                # R203's merge keeps an existing entry whenever the new walk
                # produced nothing for that route — and R180 skips a route
                # precisely when it looks unauthenticated. So one bad run stamps
                # login chrome onto a feature route and it survives every later
                # /portal/remote/* routes stuck on 'Sign In' long after the
                # session bug was fixed). A LOGIN route showing login chrome is
                # exactly those selectors. Killswitch ARTA_R268_EVICT_STALE_LOGIN_DISABLE=1.
                if _os_ad.environ.get("ARTA_R268_EVICT_STALE_LOGIN_DISABLE") != "1":
                    evicted = [
                        rt for rt, elems in merged.items()
                        if rt not in routes                    # no fresh capture this run
                        and _r268_is_login_chrome(elems)
                        and not _R268_AUTH_ROUTE_RE.search(rt)  # not a real login page
                    ]
                    for rt in evicted:
                        merged.pop(rt, None)
                    if evicted:
                        log.info(
                            "R268: evicted %d stale login-chrome route(s) from the catalog "
                            "for %s (no fresh capture + not an auth route): %s",
                            len(evicted), project_id, ", ".join(evicted[:5]))
                routes = merged
        except Exception as _r203_exc:
            log.debug("R203: catalog merge skipped for %s: %s", project_id, _r203_exc)

    testid_count = sum(
        1 for elems in routes.values() for el in elems if el.get("testid")
    )
    # R78.2 — additive selector counts for SPAs that don't emit testids
    # (Angular default, many React shops). Pre-R78.2 the catalog only
    # surfaced `testid_count`; R36.1/R67.C/R29.0 gates checked `>= 10`
    # which is structurally unsatisfiable for testid-less SPAs.
    # `role_name_count` counts elements with both a role/tag AND a
    # text/aria/name label — these become `getByRole({ name })`
    # candidates. `aria_label_count` is a subset (aria-label-only).
    # `stable_selector_count` is the inclusive sum the gates consume.
    role_name_count = sum(
        1 for elems in routes.values() for el in elems
        if isinstance(el, dict)
        and (el.get("role") or el.get("tag"))
        and (el.get("text") or el.get("ariaLabel") or el.get("name"))
        and not el.get("testid")
    )
    aria_label_count = sum(
        1 for elems in routes.values() for el in elems
        if isinstance(el, dict)
        and el.get("ariaLabel")
        and not el.get("testid")
    )
    catalog = {
        "project_id": project_id,
        "routes": routes,
        "captured_at": captured_at,
        "testid_count": testid_count,
        "role_name_count": role_name_count,
        "aria_label_count": aria_label_count,
        "stable_selector_count": testid_count + role_name_count,
        # R264.A — the SUT's SPA routing mode, as DETECTED by the probe
        # (R219.E). Load-bearing for gen: a HashRouter SUT with no server-side
        # SPA fallback answers plain deep-links with a hard 404, so generated
        # specs MUST navigate via `<base>/#<route>`. False/absent = normal
        "spa_hash_routing": spa_hash_routing,
        # R117.A — telemetry stamp. Surfaces what the catalog filter
        # rejected so operators can see at a glance whether the SUT's
        # DOM is producing clean accessible names OR smushed textContent
        # captures. _is_dirty_catalog() returns False when this catalog
        # is fully clean.
        "_r117_a_stats": r117_a_stats,
    }
    try:
        out_dir = _DOM_CATALOG_DIR / project_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "dom_catalog.json").write_text(json.dumps(catalog, indent=2))
        log.info(
            "R19b: persisted DOM catalog for %s — %d routes, %d testids "
            "(R117.A: total_input=%d, normalized=%d, rejected=%d)",
            project_id, len(routes), catalog["testid_count"],
            r117_a_stats["total_input"],
            r117_a_stats["normalized"],
            r117_a_stats["total_input"]
                - r117_a_stats["normalized"]
                - r117_a_stats["passed_testid"],
        )
    except Exception as exc:
        log.warning("R19b: dom_catalog write failed for %s: %s", project_id, exc)
    return catalog


def load_dom_catalog(project_id: str) -> dict:
    """R19c/R19d read accessor for the persisted catalog. Returns
    `{"routes": {}, "testid_count": 0}` when no catalog exists."""
    if not project_id:
        return {"routes": {}, "testid_count": 0}
    p = _DOM_CATALOG_DIR / project_id / "dom_catalog.json"
    if not p.is_file():
        return {"routes": {}, "testid_count": 0}
    try:
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log.debug("R19c: dom_catalog read failed for %s: %s", project_id, exc)
    return {"routes": {}, "testid_count": 0}


def spa_hash_routing_for(project_id: str) -> bool:
    """R264.A — SINGLE SOURCE OF TRUTH: is this SUT's SPA hash-routed?

    True only when the discovery probe actually OBSERVED a HashRouter for THIS
    project (R219.E: `location.hash` starts with `#/` after loading `/`) and
    stamped it into the project's catalog (R264.A).

    Why gen needs this: a hash-routed SUT whose server has no SPA fallback
    (a vendor SUT: `GET /portal` → 404 `"No static resource portal"`) renders ZERO
    elements for a plain deep-link, so every generated selector times out. The
    probe learned this and then threw it away pre-R264.

    Deliberately reads ONLY the per-project catalog — NOT `TARGET_SPA_HASH_ROUTING`.
    That env var is a PROBE-side knob, and one arta-api process serves every
    project: honouring it here would apply one SUT's routing mode to ALL of them
    (forcing it for a vendor SUT would rewrite the example SUT's history-routed URLs into
    broken hash URLs). The operator override still works, via the right channel:
    set it for the probe run and the probe STAMPS it into that project's catalog.

    Fails CLOSED (returns False) on a missing/unreadable catalog — an unknown
    SUT keeps the plain-navigation default rather than guessing a hash rewrite.
    """
    return bool(load_dom_catalog(project_id).get("spa_hash_routing") is True)


def _r202_normalize_route(route: str) -> str:
    """R202 — normalize a route for matching: lowercase, strip params
    (`:id`, `{id}`), collapse `//`, drop trailing slash."""
    import re as _re
    r = (route or "").strip().lower()
    r = _re.sub(r":[A-Za-z_][\w-]*", "", r)      # /:orgId  -> /
    r = _re.sub(r"\{[^}]*\}", "", r)             # /{orgId} -> /
    r = _re.sub(r"/+", "/", r)
    if len(r) > 1:
        r = r.rstrip("/")
    return r


def _r202_first_segment(route: str) -> str:
    """First non-empty path segment of a normalized route (the page family,
    e.g. `/analytics-home/x` → `analytics-home`)."""
    norm = _r202_normalize_route(route)
    parts = [p for p in norm.split("/") if p]
    return parts[0] if parts else ""


def r202_select_routes(all_routes: dict, target_routes) -> tuple[dict, bool]:
    """R202 — given the catalog's `{route: [elems]}` map and a list of target
    routes (the page(s) the current spec navigates to), return the subset of
    catalog routes relevant to the targets + whether ANY matched.

    Match is conservative: a catalog route is kept when it shares the first
    path segment with a target, or one normalizes to a prefix of the other
    (handles gherkin `/analytics` vs catalog `/analytics-home`, and inconsistent
    `:param` names). Returns (filtered_routes, matched). When `matched` is False
    the CALLER must fall back to the full set (never ground against nothing —
    that would lock the R57.1 retry loop) and surface the coverage gap.
    """
    if not isinstance(all_routes, dict) or not all_routes or not target_routes:
        return ({}, False)
    tgt_norm = {_r202_normalize_route(t) for t in target_routes if t}
    tgt_seg = {_r202_first_segment(t) for t in target_routes if t}
    tgt_norm.discard("")
    tgt_seg.discard("")
    def _seg_match(cs: str, ts: str) -> bool:
        # Exact, or one is a prefix of the other at ≥4 chars (handles gherkin
        # `/analytics` vs catalog `/analytics-home`). The length floor avoids
        # trivial single-letter over-matching.
        if not cs or not ts:
            return False
        if cs == ts:
            return True
        return len(min(cs, ts, key=len)) >= 4 and (cs.startswith(ts) or ts.startswith(cs))

    filtered: dict = {}
    for route, elems in all_routes.items():
        cn = _r202_normalize_route(route)
        cs = _r202_first_segment(route)
        if cs and any(_seg_match(cs, ts) for ts in tgt_seg):
            filtered[route] = elems
            continue
        if cn and any(cn == tn or cn.startswith(tn + "/") or tn.startswith(cn + "/")
                      for tn in tgt_norm):
            filtered[route] = elems
    return (filtered, bool(filtered))


def project_testids(project_id: str, *, routes=None) -> set[str]:
    """R19d helper — flat set of every testid known to be in the SUT
    DOM. Validator uses this to reject `getByTestId('...')` calls
    targeting testids that don't exist.

    R202: when `routes` is provided, scope to catalog routes matching those
    target routes (so a feature spec is grounded against ITS page's testids,
    not the union of all nav pages). Falls back to the full set when no catalog
    route matches (never ground against nothing). `routes=None` → legacy flatten.
    """
    catalog = load_dom_catalog(project_id)
    route_map = (catalog.get("routes") or {})
    if routes:
        scoped, matched = r202_select_routes(route_map, routes)
        if matched:
            route_map = scoped
    out: set[str] = set()
    for elems in route_map.values():
        if not isinstance(elems, list):
            continue
        for el in elems:
            if isinstance(el, dict) and isinstance(el.get("testid"), str):
                out.add(el["testid"])
    return out


def project_stable_selectors(project_id: str, *, routes=None) -> dict:
    """R78.6 — return the full set of grounded selector signatures the
    LLM is allowed to use. Pre-R78.6, R42.1 grounding only checked
    testids — SPAs without testids had no signal for the role-based
    selectors R78.2 instructs the LLM to emit. Without this helper,
    R42.1 would reject every `getByRole(...)` call as un-grounded,
    locking the regen retry loop into a permanent failure.

    Returns:
        {
            "testids": {"submit-btn", "user-list", ...},
            "role_names": {("button", "Submit"), ("link", "Sign Out"), ...},
            "aria_labels": {"Close dialog", "Open menu", ...},
            "texts": {"Submit", "Sign Out", ...},
        }

    Empty sets when no catalog exists for the project.
    """
    out = {
        "testids": set(),
        "role_names": set(),
        "aria_labels": set(),
        "texts": set(),
    }
    if not project_id:
        return out
    catalog = load_dom_catalog(project_id)
    route_map = (catalog.get("routes") or {})
    # R202 — route-scope when targets given (fall back to full on no match).
    if routes:
        scoped, matched = r202_select_routes(route_map, routes)
        if matched:
            route_map = scoped
    for elems in route_map.values():
        if not isinstance(elems, list):
            continue
        for el in elems:
            if not isinstance(el, dict):
                continue
            tid = el.get("testid")
            if isinstance(tid, str) and tid:
                out["testids"].add(tid)
                continue
            # R227 — map raw tag → implicit ARIA role so the validator's
            # valid-role set (built from this `role_names`) AGREES with the
            # prompt-format site (which now emits `getByRole('link', …)` not
            # `getByRole('a', …)`). Without this the validator would flag the
            # LLM's now-correct `link`/`combobox` role as hallucinated.
            role = implicit_aria_role(el.get("role") or el.get("tag"))
            label = el.get("ariaLabel") or el.get("text") or el.get("name")
            if isinstance(role, str) and isinstance(label, str) and label.strip():
                # Normalise whitespace; the probe truncates text to 80
                # chars so the spec's longer text may not exactly match.
                out["role_names"].add(
                    (role.strip().lower(), " ".join(label.split()))
                )
            if isinstance(el.get("ariaLabel"), str) and el["ariaLabel"]:
                out["aria_labels"].add(" ".join(el["ariaLabel"].split()))
            if isinstance(el.get("text"), str) and el["text"]:
                out["texts"].add(" ".join(el["text"].split()))
    return out


# R227 — HTML tag → implicit ARIA role. The discovery probe records role as
# `el.getAttribute('role') || el.tagName.toLowerCase()`, so a bare `<a href>`
# anchor lands as role `"a"` (and `<select>` as `"select"`, etc.). Fed verbatim
# into the LLM's HARD-CONSTRAINT block that becomes `getByRole('a', {name})`,
# then rubber-stamped by the grounding validator (which builds its valid-role set
# from the SAME catalog) — so the invalid role passes gen and only fails at PW
# runtime (there is no ARIA role `a`; anchors are `link`). This is the dominant
# every selector routes through role+name). Mapping at READ time heals catalogs
# already on disk (no re-discovery needed) AND is applied consistently at the
# prompt-format site + the validator so both agree. GENERIC across SUTs.
_IMPLICIT_ARIA_ROLE: dict[str, str] = {
    "a": "link", "button": "button", "select": "combobox", "textarea": "textbox",
    "nav": "navigation", "img": "img", "table": "table", "ul": "list", "ol": "list",
    "li": "listitem", "form": "form", "h1": "heading", "h2": "heading",
    "h3": "heading", "h4": "heading", "h5": "heading", "h6": "heading",
}


def implicit_aria_role(role_or_tag: str) -> str:
    """Return the implicit ARIA role for a captured role/tag string. Explicit
    ARIA roles (already valid, e.g. `button`, `link`, `textbox`) pass through
    unchanged; raw tag names (`a`, `select`) map to their implicit role. Unknown
    tags pass through (conservative — never invent a role). GENERIC (R227)."""
    if not role_or_tag:
        return role_or_tag
    return _IMPLICIT_ARIA_ROLE.get(role_or_tag.strip().lower(), role_or_tag)


def format_dom_catalog_for_prompt(project_id: str, *, max_chars: int = 3000, routes=None) -> str:
    """R19c — render the project's DOM catalog as a constraint block
    the LLM must respect when emitting Playwright selectors.

    Returns an empty string when no catalog is available (cold-start
    project, api-only project, or discovery never ran). Empty string
    means "no constraint" — the LLM falls back to its prior heuristics
    (preserves pre-R19 behaviour for projects without discovery).

    Single source of truth for prompt-time selector grounding. Both
    `atdd_designer._generate_for_requirement` (Gherkin generation) and
    `automation_engineer._generate_playwright` (TS spec generation)
    call this so the same constraint reaches BOTH the AC author AND
    the spec author — pre-R19c-final, only the Gherkin path saw the
    catalog and the spec generator relied on R19d (validation-time
    rejection + retry-with-hint). That works but burns 2-3 LLM
    retries per spec when one prompt-time injection prevents the
    hallucination upstream.
    """
    if not project_id:
        return ""
    catalog = load_dom_catalog(project_id)
    all_routes = catalog.get("routes") or {}
    if not all_routes:
        return ""
    # R202 — scope the rendered catalog to the spec's target routes when given
    # (so the "use ONLY these selectors" constraint lists the spec's PAGE, not
    # the union of nav pages). Fall back to full when no catalog route matches.
    if routes:
        _scoped, _matched = r202_select_routes(all_routes, routes)
        if _matched:
            all_routes = _scoped
    routes = all_routes

    lines: list[str] = [
        "",
        "[R19c — AVAILABLE PLAYWRIGHT SELECTORS — DO NOT INVENT NEW TESTIDS]",
        "The following selectors were captured live from the SUT during",
        "discovery. Generated tests MUST use ONLY these testids when",
        "asserting on UI elements. Inventing testids that don't exist",
        "is the #1 cause of Playwright failures (226/255 in run-23aa57).",
        "",
    ]
    for route, elements in routes.items():
        if not isinstance(elements, list):
            continue
        testids = [
            el for el in elements
            if isinstance(el, dict) and el.get("testid")
        ][:20]
        roles = [
            el for el in elements
            if isinstance(el, dict)
            and not el.get("testid")
            and (el.get("text") or el.get("ariaLabel"))
        ][:10]
        if not testids and not roles:
            continue
        lines.append(f"On route `{route}`:")
        for el in testids:
            t = (el.get("text") or "")[:40].replace("\n", " ")
            tag = el.get("tag") or "?"
            lines.append(
                f"  - getByTestId('{el['testid']}')                    "
                f"[{tag}] \"{t}\""
            )
        for el in roles:
            role = implicit_aria_role(el.get("role") or el.get("tag") or "?")
            label = el.get("ariaLabel") or el.get("text") or ""
            label = label[:40].replace("\n", " ")
            if label:
                lines.append(
                    f"  - getByRole('{role}', {{ name: '{label}' }})"
                )
        lines.append("")
        if sum(len(s) for s in lines) > max_chars:
            lines.append(f"... (catalog truncated at {max_chars} chars)")
            break

    lines.extend([
        "If the AC requires interaction with an element NOT listed above:",
        "  1. Prefer `getByRole('button', { name: '<exact AC text>' })`",
        "  2. Fall back to `getByText('<exact text from the AC>')`",
        "  3. Use page.locator('css selector') as last resort",
        "",
        "Tests using a testid not in the list above will be REJECTED at",
        "validation time (R19d) and force a regenerate-with-hint cycle.",
        "",
    ])
    return "\n".join(lines)


# ── Phase B4 — HAR-driven env-var harvest ───────────────────────────────────

# These segments are typically API path components, NOT path-param values.
# When walking a captured URL `/v1/datasets/abc123/snapshots/xyz789` we treat
# the segments before ID-shaped tokens as the "kind" hint.
_PATH_KIND_STOPWORDS = {
    "api", "v1", "v2", "v3", "v4", "graphql", "rest", "rpc",
    "auth", "login", "logout", "session", "oauth",
}

# A token looks like an opaque ID (UUID, ULID, hash, base32 slug, snowflake int)
# when it has digit content + mixed case / hex / specific lengths. We're more
# permissive than necessary — false positives become "extra" candidates that
# get dropped by the canonical-name match below.
_ID_LIKE_PAT = re.compile(
    r"^("
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"  # UUID
    r"|[0-9A-HJ-NP-Z]{20,30}"                                                       # ULID-ish
    r"|[a-zA-Z0-9_-]*\d[a-zA-Z0-9_-]*"                                              # any slug containing a digit
    r"|\d{6,20}"                                                                    # snowflake / numeric id
    r")$"
)
# Plain-English-shaped tokens (lowercase letters only, ≤ 12 chars, no digits)
# are almost always API path nouns, NOT path-param values. Filter them out
# even when the loose generic-slug pattern would otherwise match.
_PATH_NOUN_PAT = re.compile(r"^[a-z]{2,12}$")

# Tokens we never harvest as env-var values — they're per-request, not stable.
_NOISE_VALUE_PATS = (
    re.compile(r"^\d{4}-\d{2}-\d{2}"),    # ISO date prefix
    re.compile(r"^[0-9a-f]{32,}$"),       # very long hex (typical csrf/session)
    re.compile(r"^\d+$"),                  # pure digits — too generic alone
)


def _looks_like_id(segment: str) -> bool:
    if not segment or len(segment) < 4:
        return False
    if segment.lower() in _PATH_KIND_STOPWORDS:
        return False
    if _PATH_NOUN_PAT.match(segment):  # plain English nouns — datasets, users, etc.
        return False
    return bool(_ID_LIKE_PAT.match(segment))


def _is_noise_value(value: str) -> bool:
    return any(p.search(value) for p in _NOISE_VALUE_PATS)


def _canonical_var_name_for_kind(kind: str, known_names: set[str]) -> str | None:
    """Map a path-segment kind (`datasets`, `users`, `schemas`) to an env-var name.

    The convention used by ARTA env vars is `<singular>_id`:
      datasets  → dataset_id
      schemas   → schema_id
      users     → user_id

    If the project has no env-var with that exact name, return None (we don't
    want to invent names — only fill ones operators have already declared).
    """
    if not kind:
        return None
    singular = kind.lower().rstrip("s") if kind.lower().endswith("s") else kind.lower()
    candidate = f"{singular}_id"
    if candidate in known_names:
        return candidate
    # Also try the kind-as-is (rare, but `data_id` for `data`)
    candidate2 = f"{kind.lower()}_id"
    if candidate2 in known_names:
        return candidate2
    return None


def harvest_envvars_from_har(
    records: list[dict],
    project_id: str,
    *,
    known_envvar_names: set[str] | None = None,
    source_har: str | None = None,
) -> dict:
    """Phase B4: extract env-var values + endpoint shapes from HAR records.

    Voting rule: same name across ≥2 captures with the same value → persist.
    Conflicts (same name, different values) keep the first-seen value AND
    emit a `multi_value_warnings` entry — operator review surfaces it.

    Args:
        records: output of `sut_onboarding._ingest_har` (already redacted).
        project_id: bookkeeping for save_captured_endpoints.
        known_envvar_names: the env-var names operators have declared for the
            project. Discovery only fills these names; never invents new.
            When None, we still discover endpoints but harvest nothing.
        source_har: path to the source HAR file (audit trail).

    Returns:
        {
          "envvar_values": {name: value, ...},        # majority-vote winners
          "endpoints": [{method, path, ...}, ...],    # for save_captured_endpoints
          "shape_catalog": {endpoint_key: {request, response}, ...},
          "multi_value_warnings": [{name, values}, ...],
          "noise_skipped": int,
          "client_origin_count": int,
        }
    """
    known = set(known_envvar_names or set())
    # name → Counter({value: hits}) — votes accumulate per (var_name, value).
    votes: dict[str, dict[str, int]] = {}
    endpoints: dict[str, dict] = {}
    shape_catalog: dict[str, dict] = {}
    noise_skipped = 0
    client_origin = 0

    for rec in records or []:
        method = (rec.get("method") or "GET").upper()
        path = rec.get("path") or rec.get("url") or ""
        if not path:
            continue
        # Split URL path into segments; drop empties and the host portion.
        # Records from `_ingest_har` stamped both `path` and `url`; `path` is
        # already stripped of query.
        if "://" in path:
            # In case the caller passed full URL accidentally
            try:
                from urllib.parse import urlparse
                p_only = urlparse(path).path or "/"
            except Exception:
                p_only = path
        else:
            p_only = path
        segments = [s for s in p_only.split("/") if s]

        # is `/{account_id}/api/collection/{account_id}/user/private/cm/v<N>/<ns>/<type>`
        # — account_id in BOTH the leading slot AND the slot right after
        # `collection`. Pre-R214 the templater emitted `{id_id}` (empty kind_hint
        # at the root) and `{collection_id}` (kind=`collection` → not a known var →
        # placeholder); NEITHER resolves at dispatch → literal `{{collection_id}}`
        # → 404 (pass-rate drag) AND newman grounding EXPLOSIONS → gen fail-fast →
        # 0 tests (coverage loss). Detect the cm family and force those two slots
        # to `account_id` (the live-confirmed shape, mirroring the PW
        # `_resolve_cm_path` resolver) so they resolve via the existing account_id
        # maps. Killswitch ARTA_R214_CM_PATH_FIX_DISABLE=1.
        _r214_cm = (
            os.environ.get("ARTA_R214_CM_PATH_FIX_DISABLE", "").lower() not in ("1", "true")
            and "/api/collection/" in p_only and "/cm/" in p_only
        )
        # R214 N1 — the leading segment of a cm path IS the account id. Use its
        # VALUE to disambiguate the `/collection/{id}` slot: when that slot holds
        # live-confirmed account-in-both-slots); when it holds a DIFFERENT id
        # (e.g. a user collection like 16dc887c…), it's a GENUINE collection_id
        # that must NOT be forced to account_id (doing so would 404 on the wrong
        # id). Value-aware so both families resolve correctly.
        _r214_acct_val = (segments[0] if (_r214_cm and segments and _looks_like_id(segments[0]))
                          else None)
        # Walk segments — for each ID-shaped one, take the previous segment as
        # the kind hint and try to map to a known env-var name.
        path_template_parts: list[str] = []
        for i, seg in enumerate(segments):
            if _looks_like_id(seg):
                kind_hint = segments[i - 1] if i > 0 else ""
                var_name = _canonical_var_name_for_kind(kind_hint, known)
                # R214 N1 — leading cm slot → account_id (always); post-`collection`
                # slot → account_id ONLY when its value == the leading account.
                if _r214_cm and i == 0:
                    var_name = "account_id"
                elif (_r214_cm and i > 0 and segments[i - 1].lower() == "collection"
                      and _r214_acct_val and seg == _r214_acct_val):
                    var_name = "account_id"
                if var_name and not _is_noise_value(seg):
                    votes.setdefault(var_name, {}).setdefault(seg, 0)
                    votes[var_name][seg] += 1
                elif _is_noise_value(seg):
                    noise_skipped += 1
                else:
                    client_origin += 1
                # Replace with template placeholder for endpoint key
                placeholder = "{%s}" % (var_name or f"{kind_hint or 'id'}_id")
                path_template_parts.append(placeholder)
            else:
                path_template_parts.append(seg)
        path_template = "/" + "/".join(path_template_parts) if path_template_parts else p_only

        endpoint_key = f"{method}:{path_template}"
        # R160.B — preserve the REQUIRED query-param NAMES the SUT served with
        # (e.g. `file_path`, `app_slug`). Pre-R160.B the harvest dropped these,
        # so grounded/generated tests omitted them → the SUT returned 400
        # "<param> is required" (a real validation, mistaken for a failure).
        # Keep distinct param names + a sample value (for contract requests).
        rec_qps = [
            {"name": q.get("name"), "value": q.get("value")}
            for q in (rec.get("query_params") or [])
            if isinstance(q, dict) and q.get("name")
        ]
        ep_existing = endpoints.get(endpoint_key)
        if ep_existing:
            ep_existing["evidence_count"] += 1
            # Promote latest shape if present
            if rec.get("response_body_shape") and not ep_existing.get("response_body_shape"):
                ep_existing["response_body_shape"] = rec["response_body_shape"]
            if rec.get("request_body_shape") and not ep_existing.get("request_body_shape"):
                ep_existing["request_body_shape"] = rec["request_body_shape"]
            # Merge query-param names (union by name; keep first sample value)
            if rec_qps:
                seen_q = {q["name"] for q in ep_existing.get("query_params") or []}
                for q in rec_qps:
                    if q["name"] not in seen_q:
                        ep_existing.setdefault("query_params", []).append(q)
                        seen_q.add(q["name"])
        else:
            endpoints[endpoint_key] = {
                "method": method,
                "path": path_template,
                "status": rec.get("status"),
                "content_type": rec.get("content_type"),
                "request_body_shape": rec.get("request_body_shape"),
                "response_body_shape": rec.get("response_body_shape"),
                "query_params": rec_qps,
                "discovered_at": rec.get("started_at"),
                "source_har": source_har,
                "evidence_count": 1,
            }
            shape_catalog[endpoint_key] = {
                "request": rec.get("request_body_shape"),
                "response": rec.get("response_body_shape"),
            }

    # Voting: for each name, pick the value with the most observations.
    # Threshold 2: a value seen exactly once with no competition still wins
    # but we surface as a single-evidence warning so operators can audit.
    envvar_values: dict[str, str] = {}
    multi_value: list[dict] = []
    for name, vals in votes.items():
        if not vals:
            continue
        ranked = sorted(vals.items(), key=lambda kv: -kv[1])
        winner_value, winner_count = ranked[0]
        envvar_values[name] = winner_value
        if len(ranked) > 1:
            multi_value.append({
                "name": name,
                "winner": {"value": winner_value, "count": winner_count},
                "other_values": [{"value": v, "count": c} for v, c in ranked[1:]],
            })

    # R45.4 — harvest diagnostics. When envvar_values is empty despite
    # the HAR having authenticated traffic, the operator + ARTA need
    # to know WHY: was the path-param regex too strict? Did the SPA
    # navigate the wrong routes? Were the operator's declared var
    # names misspelled relative to the SUT's actual params?
    matched_var_names = set(envvar_values.keys())
    auth_count = sum(
        1 for r in records
        if isinstance(r, dict)
        and "json" in (r.get("content_type") or "").lower()
        and isinstance(r.get("status"), int)
        and 200 <= r["status"] < 400
    )
    json_count = sum(
        1 for r in records
        if isinstance(r, dict) and "json" in (r.get("content_type") or "").lower()
    )
    diagnosis = {
        "har_total_entries": len(records),
        "har_json_responses": json_count,
        "har_authenticated_responses": auth_count,
        "candidate_var_names_found": sorted(matched_var_names)[:30],
        "var_names_unmatched": sorted(known - matched_var_names)[:30],
        "known_envvar_names_count": len(known),
        "matched_count": len(matched_var_names),
    }

    return {
        "envvar_values": envvar_values,
        "endpoints": list(endpoints.values()),
        "shape_catalog": shape_catalog,
        "multi_value_warnings": multi_value,
        "noise_skipped": noise_skipped,
        "client_origin_count": client_origin,
        # R45.4 — structured diagnosis surfaces to discovery_executor's
        # caller (R45.3 update_auth_state) so the modal can show
        # operator-actionable detail when harvest is empty despite
        # valid auth.
        "_diagnosis": diagnosis,
    }


def clear_cache(project_id: str | None = None) -> None:
    """Clear cached endpoints (called on Re-sync)."""
    if project_id:
        _ENDPOINT_CACHE.pop(project_id, None)
    else:
        _ENDPOINT_CACHE.clear()


def format_endpoints_for_prompt(endpoints: list[dict], project_vars: dict | None = None,
                                max_entries: int = 50) -> str:
    """Format discovered endpoints as a compact string for LLM prompts."""
    if not endpoints:
        return ""

    lines = ["ACTUAL API ENDPOINTS (discovered from application — use these EXACT paths):"]

    # Group by service
    services: dict[str, list[dict]] = {}
    for ep in endpoints[:max_entries]:
        svc = ep.get("service", "default")
        services.setdefault(svc, []).append(ep)

    for svc, svc_endpoints in services.items():
        if svc != "default":
            lines.append(f"\n  [{svc}]:")
        for ep in svc_endpoints:
            line = f"    {ep['method']} {ep['path']}"
            if ep.get("summary"):
                line += f" — {ep['summary']}"
            if ep.get("params"):
                line += f" (params: {', '.join(ep['params'])})"
            if ep.get("request_body") and ep["request_body"].get("properties"):
                line += f" body: {{{', '.join(ep['request_body']['properties'])}}}"
            if not ep.get("auth_required"):
                line += " [NO AUTH]"
            if ep.get("response_type") and ep["response_type"] != "json":
                line += f" [{ep['response_type'].upper()}]"
            # R330 P3 — surface non-REST protocol so gen picks the right call
            # convention (graphql POST body, SSE/WS helpers) instead of a
            # plain REST request that can never succeed.
            if ep.get("protocol") and ep["protocol"] != "rest":
                line += f" [protocol: {ep['protocol']}]"
            lines.append(line)

    if len(endpoints) > max_entries:
        lines.append(f"  ... and {len(endpoints) - max_entries} more endpoints")

    # Include auth flow context
    if project_vars:
        lines.append("")
        lines.append("ENVIRONMENT VARIABLES (available for tests):")
        for k, v in project_vars.items():
            lines.append(f"  {k} = {v}")

    # Include detected auth flow
    project_id = project_vars.get("_project_id", "") if project_vars else ""
    auth_flow = _AUTH_FLOW_CACHE.get(project_id, {})
    if auth_flow:
        lines.append("")
        lines.append("AUTH FLOW (auto-detected from codebase):")
        if auth_flow.get("token_exchange"):
            lines.append("  - Token exchange detected: obtain service/agent token before API calls")
        if auth_flow.get("bearer"):
            lines.append("  - Use Bearer token in Authorization header")
        if auth_flow.get("cookie"):
            lines.append("  - Cookie-based auth: use {{auth_token}} cookie")
        if auth_flow.get("mcp"):
            lines.append("  - MCP protocol detected: use MCP client for these endpoints (NOT Newman)")
        if auth_flow.get("streaming"):
            lines.append("  - Streaming responses detected: use EventSource or SSE client (NOT Newman)")

    lines.append("")
    lines.append("IMPORTANT: Use these exact endpoint paths. Do NOT guess or invent paths.")
    lines.append("For MCP/SSE endpoints, generate Playwright tests (not Newman collections).")
    return "\n".join(lines)


# ── Fix II: DOM grounding for Playwright prompts ────────────────────────────
# The Playwright LLM consistently hallucinates `data-testid` attributes that
# don't exist on the SUT (verified: 190 of 190 Playwright FAILs in run-6d6274
# prompt in REAL selectors scraped from the SUT reduces hallucination to
# near-zero. Cost: one HTML fetch + parse per generation (~2s).

async def fetch_sut_selectors(base_url: str, paths: list[str] | None = None) -> dict:
    """Fetch and parse HTML from the SUT to extract real selectors.

    Returns a dict of:
      - data_testids: set of values seen on `data-testid` attributes
      - aria_labels: set of values seen on `aria-label` attributes
      - form_names: set of values seen on `name` attributes (for forms)
      - button_text: set of visible button text
      - role_attrs: set of `role=` attribute values

    Empty fetch failures degrade gracefully — the LLM falls back to its
    prior heuristics. Bounded total time: 10s across all paths.

    Used by atdd_designer.py to inject AVAILABLE_SELECTORS context into
    the Playwright generation prompt.
    """
    import asyncio as _aio
    import httpx as _httpx
    paths = paths or ["/", "/login", "/dashboard"]
    results = {
        "data_testids": set(),
        "aria_labels": set(),
        "form_names": set(),
        "button_text": set(),
        "role_attrs": set(),
    }
    try:
        async with _httpx.AsyncClient(
            timeout=3.0, verify=False, follow_redirects=True,
        ) as client:
            async def _fetch_one(p: str):
                try:
                    r = await client.get(f"{base_url.rstrip('/')}{p}")
                    if r.status_code != 200:
                        return ""
                    return r.text
                except Exception:
                    return ""
            htmls = await _aio.gather(*(_fetch_one(p) for p in paths))
    except Exception:
        return {k: list(v) for k, v in results.items()}

    # Parse with regex (BeautifulSoup is heavy and adds an import; the
    # regex catch-all is good enough for selector enumeration).
    import re as _re
    for html in htmls:
        if not html:
            continue
        for m in _re.finditer(r'data-testid\s*=\s*["\']([^"\']+)["\']', html):
            results["data_testids"].add(m.group(1))
        for m in _re.finditer(r'aria-label\s*=\s*["\']([^"\']+)["\']', html):
            results["aria_labels"].add(m.group(1))
        for m in _re.finditer(r'<input[^>]*\sname\s*=\s*["\']([^"\']+)["\']', html, _re.IGNORECASE):
            results["form_names"].add(m.group(1))
        for m in _re.finditer(r'<button[^>]*>([^<]{1,40})</button>', html, _re.IGNORECASE):
            t = m.group(1).strip()
            if t:
                results["button_text"].add(t)
        for m in _re.finditer(r'\srole\s*=\s*["\']([^"\']+)["\']', html):
            results["role_attrs"].add(m.group(1))

    return {k: sorted(v)[:50] for k, v in results.items()}


def format_selectors_for_prompt(selectors: dict) -> str:
    """Format the output of fetch_sut_selectors() into a prompt block.

    Atdd_designer.py prepends this to the Playwright generation prompt
    so the LLM is constrained to selectors that EXIST on the SUT.
    """
    if not selectors or not any(selectors.values()):
        return ""
    parts = ["AVAILABLE_SELECTORS (verified on the live SUT — use ONLY these for selectors):"]
    if selectors.get("data_testids"):
        parts.append("  data-testid:")
        for v in selectors["data_testids"][:30]:
            parts.append(f"    - [data-testid=\"{v}\"]")
    if selectors.get("aria_labels"):
        parts.append("  aria-label:")
        for v in selectors["aria_labels"][:20]:
            parts.append(f"    - [aria-label=\"{v}\"]")
    if selectors.get("form_names"):
        parts.append("  form input names:")
        for v in selectors["form_names"][:20]:
            parts.append(f"    - input[name=\"{v}\"]")
    if selectors.get("button_text"):
        parts.append("  visible button text:")
        for v in selectors["button_text"][:15]:
            parts.append(f"    - getByRole('button', {{ name: '{v}' }})")
    parts.append("")
    parts.append("IMPORTANT: Do NOT invent data-testid values not in the list above. "
                 "If a needed control is absent, use getByRole / getByLabel / getByText.")
    return "\n".join(parts)


# ── Fix KK: SUT-probe path-param values ─────────────────────────────────────
# When a Newman test uses `{{collection_id}}` etc., the runner substitutes
# from project env vars. Most projects have these set to placeholder
# strings ("REPLACE_ME") — every Newman item then 404s. For any path with
# `{x_id}`, there's almost always a sibling LIST endpoint that returns
# real IDs. Probe each list endpoint with the auth cookie, take the first
# result's `id`, and persist back to the project's env vars. One probe
# unlocks ~1500 Newman items per run on a ~166-endpoint API surface.

def _r213i_jsonpath_get(body, jp):
    """R213.I — evaluate a `_walk_leaves`-style jsonpath (``$.a.b[0].c``,
    ``$[0].id``, or the cm sentinel ``$.__cm_collection_key__``) against a
    REAL response body. Returns the leaf value, or None. SUT-agnostic — the
    jsonpaths come from the discovered provides/consumes graph, not hardcoded.
    """
    if not jp or body is None:
        return None
    if jp == "$.__cm_collection_key__":
        # cm list response is {"Collection":…, "<uuid-key>": [...]} — the
        # resource id is the UUID-shaped KEY, not a field (mirrors call_chain
        # _walk_leaves R215). Return the first uuid-shaped key whose value is a list.
        if isinstance(body, dict):
            for k, v in body.items():
                if isinstance(v, list) and re.fullmatch(r"[0-9a-fA-F-]{8,}", str(k)):
                    return k
        return None
    cur = body
    for key, idx in re.findall(r"\.([^.\[]+)|\[(\d+)\]", jp):
        if cur is None:
            return None
        if key:
            cur = cur.get(key) if isinstance(cur, dict) else None
        elif idx != "":
            i = int(idx)
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
    return cur if isinstance(cur, (str, int, float)) else None


def _r213i_build_provider_index(project_id: str) -> dict:
    """R213.I — build {var_name_lower: (provider_path_template, jsonpath)} from
    the discovered provides/consumes graph (call_chain). For each `{x_id}` a
    test consumes, this says WHICH endpoint's RESPONSE provides it + WHERE in
    that response the value lives — regardless of the provider's URL shape
    (covers nested-id / name-tail APIs the path-truncation heuristic misses).
    Observed-traffic-derived; zero per-SUT hardcoding.
    """
    idx: dict[str, tuple[str, str]] = {}
    try:
        for ch in load_chains(project_id):
            for n in (ch.get("nodes") or []):
                pt = n.get("path_template")
                if not pt:
                    continue
                for var, jp in (n.get("provides") or {}).items():
                    if var and jp and var.lower() not in idx:
                        idx[var.lower()] = (pt, jp)
    except Exception as _exc:
        log.debug("R213.I: provider index build failed: %s", _exc)
    return idx


def _r213i_host_and_auth(path, raw_chain, host_map, auth_chain, auth_for_path_fn, tokens,
                         default_base, default_headers, default_cookies):
    """R213.I/J — resolve the per-path (host, headers, cookies) for a probe GET
    by reusing the project auth chain. The host AND auth come from ONE match —
    `auth_for_path` selects the MOST-SPECIFIC rule (R213.J specificity, not
    declaration order) and returns its `host` family + the resolved credential
    (composite Bearer / agent_token / plain session_token / cookie — all config-driven).
    `host_map[host]` → base URL. Falls back to the legacy single base_url +
    Bearer when no rule matches. SUT-agnostic; no per-SUT code, no hand-ordering.
    """
    base = default_base
    headers = dict(default_headers or {})
    cookies = dict(default_cookies or {})
    if auth_chain is not None and auth_for_path_fn is not None:
        try:
            a = auth_for_path_fn(path, chain=auth_chain, tokens=tokens)
            # host from the SAME matched rule (R213.J) → host_map family lookup
            _host_key = a.get("host")
            if _host_key and isinstance(host_map, dict) and host_map.get(_host_key):
                base = host_map[_host_key].rstrip("/")
            if a.get("header_value"):
                headers[a.get("header_name") or "Authorization"] = a["header_value"]
            if a.get("cookie_name") and a.get("cookie_value"):
                cookies[a["cookie_name"]] = a["cookie_value"]
        except Exception as _exc:
            log.debug("R213.I/J: auth_for_path failed for %s: %s", path, _exc)
    return base, headers, cookies


async def probe_path_param_values(
    project: dict,
    endpoints: list[dict],
    auth_state_path: str | None = None,
    agent_token: str | None = None,
) -> dict[str, str]:
    """Fix KK + YY — fetch real path-param values from the live SUT.

    Returns a dict like {"collection_id": "abc-123", "schema_id": "def-456"}.
    Caller is responsible for persisting via the bulk-add API.

    Algorithm:
    1. Group endpoints by base path. For each `{name_id}` path-param token
       seen on a `GET /…/{name_id}` endpoint, find a sibling LIST endpoint
       (no path param, same prefix).
    2. GET the list endpoint with auth (Bearer agent_token + session-token
       cookie) + already-resolved parent path params from the project env.
    3. Parse common envelopes — top-level array, `data[]`, `items[]`,
       `results[]` — and take `[0].id` (or `_id`, `uuid`, `name`).
    4. Skip any param we can't resolve (logged WARNING).

    Fix YY (Phase F) extension: BEFORE the Fix QQ probe loop, attempt to
    bootstrap `schema_id` if it's still REPLACE_ME. The the example SUT API
    requires a schema_id for every collection-level call; without it the
    other probes 400. Bootstrap by listing schemas (or creating one).

    Bounded: max 50 probe calls per invocation to avoid hammering a SUT.
    Each call has a 5-second timeout.
    """
    import json as _json
    base_url = ""
    cookies: dict[str, str] = {}
    _staging = (project.get("environments", {}) or {}).get("staging", {}) or {}
    # R213.I — param VALUES + the per-path auth chain live under
    # `staging.variables` / `staging.auth`. The pre-R213.I code set
    # project_env_vars=`staging` (the whole env dict) → param lookups
    # (`project_env_vars.get("collection_id")`) read the WRONG level (None,
    # not the real value/REPLACE_ME) AND parent ids (account_id, …) were never
    # in `_resolved`, so templated provider URLs never substituted → the probe
    # harvested almost nothing. Read the variables map (flat-config fallback).
    project_env_vars = _staging.get("variables") if isinstance(_staging.get("variables"), dict) else _staging
    base_url = _staging.get("base_url") or project_env_vars.get("base_url") or (project.get("integrations", {}) or {}).get("base_url", "")
    if not base_url:
        return {}
    # ARTA_SANDBOX_BACKEND_HEURISTIC_DISABLE=1 (proper home = env auth.host_map).
    backend_url = base_url
    if "//sandbox." in base_url and os.environ.get("ARTA_SANDBOX_BACKEND_HEURISTIC") == "1":
        backend_url = base_url.replace("//sandbox.", "//backend.sandbox.", 1)
        # not silent (charter: no silent SUT-specific fallbacks). Other SUTs should
        # carry their host map via onboarding_config, not this heuristic.
        log.warning("A2: applied opt-in host heuristic sandbox.*→backend.sandbox.* "
                    "(%s → %s) — a deployment-specific guess, not a discovered rule",
                    base_url, backend_url)

    # Load auth cookies from the storage state file the operator stored.
    if auth_state_path:
        try:
            _data = _json.loads(Path(auth_state_path).read_text())
            for c in (_data.get("cookies") or []):
                if isinstance(c, dict) and c.get("name") and c.get("value"):
                    cookies[c["name"]] = c["value"]
        except Exception as _exc:
            log.debug("KK: failed to read auth state at %s: %s", auth_state_path, _exc)

    # Fix YY (Phase F): build auth headers. Bearer agent_token preferred for
    # service-to-service inner APIs; cookies cover UI-facing endpoints.
    auth_headers: dict[str, str] = {}
    if agent_token:
        auth_headers["Authorization"] = f"Bearer {agent_token}"

    # endpoints all 400 with "schema_id Missing" until a schema exists.
    # Try listing schemas; if empty, create one. Idempotent on existing
    # schema with matching name. Best-effort — failures degrade to the
    # existing SKIP-not-FAIL sentinel pattern.
    probed: dict[str, str] = {}

    # Phase G refactor: prefer URLs discovered by the SUT Onboarding
    # Agent (Fix GGG). `onboarding_config.list_endpoints[<param>].best`
    # holds the project-specific list URL when the onboarding agent
    _onboarding = (project.get("integrations", {}) or {}).get("onboarding_config") or {}
    _list_eps = (_onboarding.get("list_endpoints") or {}) if isinstance(_onboarding, dict) else {}
    _onboarding_schema_url = ""
    if isinstance(_list_eps.get("schema_id"), dict):
        _onboarding_schema_url = _list_eps["schema_id"].get("best", "")

    _account_id = project_env_vars.get("variables", {}).get("account_id") or project_env_vars.get("account_id")
    _subscriber_id = project_env_vars.get("variables", {}).get("subscriber_id") or project_env_vars.get("subscriber_id")
    _subscription_id = project_env_vars.get("variables", {}).get("subscription_id") or project_env_vars.get("subscription_id")
    _existing_schema = project_env_vars.get("variables", {}).get("schema_id", "") or project_env_vars.get("schema_id", "")
    if _existing_schema in ("", "REPLACE_ME") and _account_id and _subscriber_id and _subscription_id:
        # Build candidate list-URLs. Onboarding-agent suggestion first
        # (project-specific, high confidence), then legacy heuristics.
        _candidate_list_urls: list[str] = []
        if _onboarding_schema_url:
            _resolved_url = _onboarding_schema_url
            for k, v in (project_env_vars.get("variables", {}) or {}).items():
                if v and v != "REPLACE_ME":
                    _resolved_url = _resolved_url.replace("{" + k + "}", str(v))
            if _resolved_url.startswith("/"):
                _resolved_url = backend_url.rstrip("/") + _resolved_url
            _candidate_list_urls.append(_resolved_url)
        # R213.I — derive the service namespaces from the DISCOVERED endpoints
        # the legacy list only when discovery yields none. Killswitch
        # ARTA_PROBE_GENERIC_PROVIDER_DISABLE=1 reverts to the hardcoded set.
        _yy_services: list[str] = []
        if os.environ.get("ARTA_PROBE_GENERIC_PROVIDER_DISABLE") != "1":
            for _ep in endpoints:
                _mm = re.search(r"/api/([a-z][a-z_]+)/", _ep.get("path") or "")
                if _mm and _mm.group(1) not in _yy_services and _mm.group(1) not in ("v1", "v2", "v3"):
                    _yy_services.append(_mm.group(1))
        _yy_services = _yy_services or ["example_sut", "composite_svc", "extraction"]
        # Try listing schemas via the derived service namespaces as fallback.
        async with httpx.AsyncClient(
            timeout=10.0, verify=False, follow_redirects=True,
            cookies=cookies, headers=auth_headers,
        ) as boot_client:
            # First try the onboarding-suggested URL(s).
            for _u in _candidate_list_urls:
                try:
                    r = await boot_client.get(_u)
                    if r.status_code == 200:
                        body = r.json()
                        items = body if isinstance(body, list) else (
                            next((body.get(k) for k in ("data", "items", "results", "schemas")
                                  if isinstance(body.get(k), list) and body.get(k)), [])
                        )
                        if items and isinstance(items[0], dict):
                            for id_key in ("schema_id", "id", "_id", "uuid"):
                                if items[0].get(id_key):
                                    probed["schema_id"] = str(items[0][id_key])
                                    log.info("Fix YY (onboarding-driven): schema_id=%s from %s",
                                             probed["schema_id"], _u)
                                    break
                            if "schema_id" in probed:
                                break
                except Exception as _exc:
                    log.debug("YY onboarding-url %s → %s", _u, _exc)
            for service in _yy_services:
                if "schema_id" in probed:
                    break
                _list_url = f"{backend_url.rstrip('/')}/api/{service}/{_account_id}/{_subscriber_id}/{_subscription_id}/"
                try:
                    r = await boot_client.get(_list_url)
                    if r.status_code != 200:
                        log.debug("YY: schema-list %s → %d", _list_url, r.status_code)
                        continue
                    body = r.json()
                    items: list = []
                    if isinstance(body, list):
                        items = body
                    elif isinstance(body, dict):
                        for k in ("data", "items", "results", "schemas", "records"):
                            v = body.get(k)
                            if isinstance(v, list) and v:
                                items = v
                                break
                    if not items:
                        log.debug("YY: schema-list %s returned 200 but no items", _list_url)
                        continue
                    first = items[0] if isinstance(items[0], dict) else None
                    if not first:
                        continue
                    for id_key in ("schema_id", "id", "_id", "uuid"):
                        if first.get(id_key):
                            probed["schema_id"] = str(first[id_key])
                            log.info(
                                "Fix YY: bootstrapped schema_id=%s from %s (service=%s)",
                                probed["schema_id"], _list_url, service,
                            )
                            break
                    if "schema_id" in probed:
                        break
                except Exception as _yy_exc:
                    log.debug("YY: schema-list %s → error %s", _list_url, _yy_exc)

            # If still unresolved, attempt to CREATE a bootstrap schema.
            if "schema_id" not in probed:
                for service in _yy_services[:2]:
                    _create_url = f"{backend_url.rstrip('/')}/api/{service}/{_account_id}/{_subscriber_id}/{_subscription_id}/"
                    try:
                        r = await boot_client.post(
                            _create_url,
                            json={
                                "name": "ARTA-bootstrap-schema",
                                "description": "Created by ARTA Phase F bootstrap probe",
                                "schema_name": "ARTA-bootstrap-schema",
                            },
                        )
                        if r.status_code in (200, 201):
                            try:
                                body = r.json()
                            except Exception:
                                continue
                            if isinstance(body, dict):
                                for id_key in ("schema_id", "id", "_id", "uuid"):
                                    if body.get(id_key):
                                        probed["schema_id"] = str(body[id_key])
                                        log.info(
                                            "Fix YY: created bootstrap schema; schema_id=%s",
                                            probed["schema_id"],
                                        )
                                        break
                            if "schema_id" in probed:
                                break
                    except Exception as _yy_create_exc:
                        log.debug("YY: schema-create %s → error %s", _create_url, _yy_create_exc)

            if "schema_id" not in probed:
                log.warning(
                    "Fix YY: could not bootstrap schema_id (list/create both failed). "
                    "Newman items using {schema_id} will continue to SKIP."
                )

    # Fix QQ (KK v2): index ALL GET endpoints (with and without path params).
    # `/api/{account_id}/{subscriber_id}/.../collections/{collection_id}`
    # — the `list endpoint` here is `/api/{account_id}/.../collections`,
    # which itself has path params. The v1 index missed these because it
    # required clean (no-`{`) URLs.
    get_paths: set[str] = {
        ep.get("path", "").rstrip("/")
        for ep in endpoints
        if (ep.get("method") or "").upper() == "GET" and ep.get("path")
    }

    def _unresolved(pn: str) -> bool:
        ev = project_env_vars.get(pn, "")
        return (not (ev and ev != "REPLACE_ME" and not str(ev).startswith("__ARTA_UNSET"))
                and pn not in probed)

    # R213.I — build the provider TARGETS for each unresolved `{x_id}`/`{x_name}`:
    #   targets[param] = (provider_path_template, jsonpath_or_None, depth)
    # Priority: (1) the discovered provides/consumes graph (any URL shape — the
    # provider is whatever endpoint's RESPONSE yields the id, so nested-id /
    # name-tail APIs resolve, not just REST `/resource/{id}`); (2) onboarding-
    # agent-confirmed list endpoints; (3) the legacy path-truncation heuristic.
    # SUT-agnostic: every source is config/discovery-derived, no hardcoding.
    # Killswitch ARTA_PROBE_GENERIC_PROVIDER_DISABLE=1 → heuristic-only (legacy).
    _generic_on = os.environ.get("ARTA_PROBE_GENERIC_PROVIDER_DISABLE") != "1"
    targets: dict[str, tuple[str, "str | None", int]] = {}

    if _generic_on:
        _pidx = _r213i_build_provider_index(str(project.get("id") or ""))
        for ep in endpoints:
            _p = ep.get("path") or ""
            for _m in re.finditer(r"\{([a-z_]+_id|id|[a-z_]+_name|name)\}", _p):
                _pn = _m.group(1)
                if not _unresolved(_pn) or _pn in targets:
                    continue
                _prov = _pidx.get(_pn.lower())
                if _prov:
                    targets[_pn] = (_prov[0], _prov[1], (_prov[0] or "").count("{"))
        # (2) onboarding-discovered list endpoints
        for _pn, _cfg in (_list_eps.items() if isinstance(_list_eps, dict) else []):
            if (isinstance(_cfg, dict) and _cfg.get("best")
                    and _unresolved(_pn) and _pn not in targets):
                targets[_pn] = (_cfg["best"], None, str(_cfg["best"]).count("{"))

    # (3) legacy path-truncation heuristic (REST `/resource/{id}`) — always the
    # fallback so existing SUTs behave exactly as before.
    for ep in endpoints:
        path = ep.get("path") or ""
        m = re.search(r"\{([a-z_]+_id|id)\}", path)
        if not m:
            continue
        param_name = m.group(1)
        if not _unresolved(param_name) or param_name in targets:
            continue
        list_path = path[: m.start()].rstrip("/")
        if not list_path or list_path not in get_paths:
            continue
        targets[param_name] = (list_path, None, path.count("{"))

    if not targets:
        log.info("KK/R213.I: no candidate provider endpoints found (params already populated, or no discovered providers)")
        return probed

    # Resolve any parent path-params from env so we can substitute templated
    # parents (e.g. /api/{account_id}/collections needs account_id first).
    # Fix QQ: cumulative resolution — values harvested in earlier iterations
    # become available for substitution in later ones.
    _resolved: dict[str, str] = {
        k: v for k, v in project_env_vars.items()
        if v and v != "REPLACE_ME" and not str(v).startswith("__ARTA_UNSET")
    }

    def _substitute(path: str) -> str:
        out = path
        for var_name, var_value in _resolved.items():
            out = out.replace("{" + var_name + "}", str(var_value))
        return out

    # Note: `probed` was already initialized + may already contain
    # schema_id (Fix YY bootstrap). Don't reset.
    probe_count = 0
    if "schema_id" in probed:
        _resolved["schema_id"] = probed["schema_id"]

    # R213.I — per-path auth chain + host_map so each provider GET uses the
    # RIGHT host + RIGHT credential (composite Bearer / agent_token / plain
    # of one base_url + one Bearer for everything. Falls back to base_url +
    # auth_headers when no chain rule matches (legacy single-Bearer SUTs).
    _auth_block = _staging.get("auth") if isinstance(_staging.get("auth"), dict) else {}
    _raw_chain = _auth_block.get("chain") or []
    _host_map = _auth_block.get("host_map") or {}
    _auth_chain = None
    _auth_for_path_fn = None
    # NOTE: per-path host+auth is loaded whenever a chain is configured — it is
    # INDEPENDENT of the provider-discovery killswitch (`_generic_on`). How we
    # REACH a provider (right host + credential) must work even in legacy
    # provider mode. (The matcher's own behavior is gated by
    # ARTA_AUTH_SPECIFICITY_DISABLE inside best_rule.)
    if _raw_chain:
        try:
            from .auth_chain import AuthChain as _AC, auth_for_path as _AFP
            _auth_chain = _AC.from_config(_raw_chain)
            _auth_for_path_fn = _AFP
        except Exception as _ace:
            log.debug("R213.I: auth chain load failed: %s", _ace)
    _session_token = cookies.get("session-token") or cookies.get("cookie_value") or ""
    _tokens = {"session_token": _session_token, "cookie_value": _session_token, "agent_api_token": agent_token or ""}

    # Iterate shallowest provider path first so parent ids resolve before
    # children (e.g. collection_id before collection_item_id).
    for param_name, (prov_path, prov_jp, _depth) in sorted(
            targets.items(), key=lambda kv: kv[1][2])[:50]:
        if param_name in probed:
            continue
        probe_count += 1
        # refresh the token pool with any newly-harvested parent ids
        for _k in ("organization_id", "account_id", "root_account_id",
                   "subscriber_id", "subscription_id", "schema_id"):
            if _resolved.get(_k):
                _tokens[_k] = str(_resolved[_k])
        sub_path = _substitute(prov_path)
        if "{" in sub_path:
            log.debug("KK/R213.I: skip %s — unresolved parent in %s", param_name, sub_path)
            continue
        host, headers, ck = _r213i_host_and_auth(
            prov_path, _raw_chain, _host_map, _auth_chain, _auth_for_path_fn, _tokens,
            default_base=base_url, default_headers=auth_headers, default_cookies=cookies,
        )
        url = f"{host.rstrip('/')}{sub_path}"
        try:
            async with httpx.AsyncClient(
                timeout=5.0, verify=False, follow_redirects=True,
            ) as _cl:
                resp = await _cl.get(url, headers=headers, cookies=ck)
            if resp.status_code != 200:
                log.debug("KK/R213.I: %s %s → %d", param_name, url, resp.status_code)
                continue
            body = resp.json()
        except Exception as _e:
            log.debug("KK/R213.I: %s %s → error %s", param_name, url, _e)
            continue
        val = None
        # (a) provider jsonpath from the provides graph (generic id-location)
        if prov_jp:
            val = _r213i_jsonpath_get(body, prov_jp)
        # (b) fallback: common list envelope → first item's id-shaped field
        if val is None:
            items = body if isinstance(body, list) else None
            if items is None and isinstance(body, dict):
                for key in ("data", "items", "results", "records"):
                    v = body.get(key)
                    if isinstance(v, list) and v:
                        items = v
                        break
            if items:
                first = items[0]
                if isinstance(first, dict):
                    for id_key in ("id", "_id", "uuid", "guid"):
                        if first.get(id_key):
                            val = first[id_key]
                            break
        if val is None:
            continue
        probed[param_name] = str(val)
        # feed back so deeper child probes can substitute this parent
        _resolved[param_name] = str(val)
        log.info("KK/R213.I: harvested %s=%s from %s", param_name, str(val)[:40], url)

    log.info(
        "KK/R213.I: probed %d real path-param value(s) (%d providers inspected, generic=%s, base_url=%s)",
        len(probed), probe_count, _generic_on, base_url,
    )
    return probed


def _r313_d_collect_values(v, out: dict, depth: int = 0) -> None:
    """R313.D — Python port of the discovery probe's `_r313_collect` (single source
    of the enum-like predicate): recurse a decoded JSON body, collecting ENUM-LIKE
    scalar VALUES by field key into `out` (dict[str, set]). Enum-scoped — short
    (≤32 chars), no-whitespace tokens matching `^[A-Za-z0-9_.\\-]+$` — so free text,
    PII, and long ids are never captured. Identical semantics to the TS probe so the
    two value sources (client-XHR capture + this server-side sweep) are consistent."""
    if depth > 4 or v is None:
        return
    if isinstance(v, list):
        for x in v[:40]:
            _r313_d_collect_values(x, out, depth + 1)
        return
    if isinstance(v, dict):
        for k in list(v.keys())[:40]:
            cv = v[k]
            if isinstance(cv, (str, int, float, bool)):  # bool ⊂ int in Python
                s = str(cv)
                if 1 <= len(s) <= 32 and re.match(r"^[A-Za-z0-9_.\-]+$", s):
                    out.setdefault(k, set()).add(s)
            else:
                _r313_d_collect_values(cv, out, depth + 1)


def _r313_d_bearer_from_jwt_cookie(cookies: dict) -> str | None:
    """R313.D — SUT-agnostic BFF-auth fallback. Many SUTs front their API with a
    gateway (Kong/etc.) that wants `Authorization: Bearer <jwt>`, while the browser
    only ever holds the token in an httpOnly COOKIE — the BFF does the cookie→Bearer
    translation. A direct probe bypasses the BFF and gets 401 with the cookie alone
    (observed live on an SSR SUT: cookie→401, Bearer→200). So when no explicit Bearer is
    supplied, promote a JWT-SHAPED cookie value (three dot-separated base64url
    segments, non-trivial payload) to a Bearer. Pure JWT detection — no SUT or
    cookie-name literal. Returns the first JWT-shaped cookie value, or None."""
    for _cv in (cookies or {}).values():
        if not isinstance(_cv, str):
            continue
        _parts = _cv.split(".")
        if (len(_parts) == 3
                and all(re.match(r"^[A-Za-z0-9_\-]+$", p) for p in _parts)
                and len(_parts[1]) > 8):
            return _cv
    return None


def _probe_auth_headers(
    auth_state_path: str | None,
    agent_token: str | None,
    bearer_token: str | None,
) -> tuple[dict, dict]:
    """Shared read-only-probe auth builder (R313.D value-probe + list-id seeder).

    Returns (cookies, headers). Storage-state cookies + a Bearer (explicit
    agent_token/bearer_token, else a JWT-shaped cookie promoted to Bearer for
    BFF/gateway SUTs). SUT-agnostic — no host/route/cookie-name literal."""
    cookies: dict[str, str] = {}
    if auth_state_path:
        try:
            _data = json.loads(Path(auth_state_path).read_text())
            for c in (_data.get("cookies") or []):
                if isinstance(c, dict) and c.get("name") and c.get("value"):
                    cookies[c["name"]] = c["value"]
        except Exception as _exc:
            log.debug("probe-auth: failed to read auth state %s: %s", auth_state_path, _exc)
    headers: dict[str, str] = {}
    _bt = agent_token or bearer_token or _r313_d_bearer_from_jwt_cookie(cookies)
    if _bt:
        headers["Authorization"] = f"Bearer {_bt}"
    return cookies, headers


def _is_structural_list_path(path: str) -> bool:
    """SUT-AGNOSTIC structural test for a collection/list GET path — no SUT
    vocabulary. A list path has NO unresolved params and its last segment is a
    plural noun (ends in 's', not 'ss') OR a generic list marker. This is the
    replacement for keyword-gated capture: an entity is seeded because of its
    STRUCTURE (it returns a collection), not because its name matches a
    competitor SUT's dictionary."""
    p = (path or "").split("?")[0]
    if "{" in p or "}" in p or ":" in p:
        return False
    last = p.lower().rstrip("/").split("/")[-1]
    if not last:
        return False
    if any(m in last for m in ("list", "search")) or last.startswith("getall"):
        return True
    return last.endswith("s") and not last.endswith("ss")


async def seed_real_ids_from_list_endpoints(
    project_id: str,
    endpoints: list[dict],
    *,
    base_url: str,
    auth_state_path: str | None = None,
    agent_token: str | None = None,
    bearer_token: str | None = None,
    max_calls: int = 20,
    timeout: float = 6.0,
) -> int:
    """Proactively seed the real_id_store from ALL structurally list-shaped GET
    endpoints discovery found — SUT-AGNOSTIC, no keyword/placeholder gating.

    ROOT fix for the org-id chicken-and-egg: an entity whose ids were never
    captured (e.g. an SSR SUT `organization`, whose list `/v1/regions/global/
    organizations` returns 29 real orgs but was skipped by the keyword-gated
    probes) is absent from the R251 real-data block → the LLM fabricates an id →
    the placeholder-DRIVEN R230 probe never fires for it → the id stays
    uncaptured. Seeding structurally (a list endpoint = returns a collection)
    breaks the loop: real ids land in the store BEFORE gen, so R251/R336 surface
    them and gen uses them.

    Reuses the R313.D auth builder + `extract_real_ids`/`persist_real_ids`.
    GET-only → R154 non-mutation safe. Bounded + timeout-capped + exception-safe.
    Killswitch ARTA_LIST_ID_SEED_DISABLE=1. Returns count of id slots seeded."""
    if os.environ.get("ARTA_LIST_ID_SEED_DISABLE") == "1":
        return 0
    if not base_url or not endpoints:
        return 0
    from .real_id_store import extract_real_ids, persist_real_ids

    cookies, headers = _probe_auth_headers(auth_state_path, agent_token, bearer_token)

    # Structural list GETs, de-duped. Prefer real API lists (versioned/`/api/`)
    # then shallower paths, so `/v1/regions/.../organizations` beats a bare SPA
    # route. Cap so we never hammer the SUT.
    seen: set[str] = set()
    targets: list[str] = []
    for e in endpoints:
        if not isinstance(e, dict) or str(e.get("method", "GET")).upper() != "GET":
            continue
        path = str(e.get("path") or "")
        if not path or path in seen or not _is_structural_list_path(path):
            continue
        lp = path.lower()
        if ("/_next/" in lp or "/static/" in lp or "chunks" in lp
                or lp.endswith((".js", ".css", ".map", ".png", ".svg", ".ico",
                                ".woff", ".woff2", ".json"))):
            continue
        seen.add(path)
        targets.append(path)
    targets.sort(key=lambda c: (0 if re.search(r"/v\d+/|/api/", c) else 1, c.count("/")))
    targets = targets[:max_calls]
    if not targets:
        return 0

    records: list[dict] = []
    _base = base_url.rstrip("/")
    async with httpx.AsyncClient(
        timeout=timeout, verify=False, follow_redirects=True,
        cookies=cookies, headers=headers,
    ) as client:
        for path in targets:
            url = path if path.startswith("http") else _base + ("/" + path.lstrip("/"))
            try:
                r = await client.get(url)
            except Exception as _ex:
                log.debug("list_seed: GET %s failed: %s", url, _ex)
                continue
            if r.status_code // 100 != 2 or "json" not in (r.headers.get("content-type") or "").lower():
                continue
            try:
                body = r.json()
            except Exception:
                continue
            records.append({"method": "GET", "path": path, "status": r.status_code,
                            "response_body_sample": body, "_source": "list_seed"})
    if not records:
        return 0
    try:
        got = extract_real_ids(records)
    except Exception as _exc:
        log.debug("list_seed: extract_real_ids failed for %s: %s", project_id, _exc)
        return 0
    if not got:
        log.info("list_seed: no real ids found across %d list GET(s) for %s",
                 len(records), project_id)
        return 0
    persist_real_ids(project_id, got)
    log.info("list_seed: seeded %d real-id slot(s) for %s from %d list endpoint(s) "
             "— entities: %s", len(got), project_id, len(records),
             ", ".join(sorted({s.get("entity", "") for s in got.values()} - {""})) or "none")
    return len(got)


async def probe_response_value_samples(
    project_id: str,
    endpoints: list[dict],
    *,
    base_url: str,
    auth_state_path: str | None = None,
    agent_token: str | None = None,
    bearer_token: str | None = None,
    max_calls: int = 20,
    timeout: float = 6.0,
) -> int:
    """R313.D — SSR-compatible value-domain source. The discovery probe captures
    response VALUES from client-side XHRs (`_r313_collect`); server-rendered SUTs
    (SSR, e.g. an SSR SUT) fire few client XHRs → HAR/shape capture is empty → the
    R313 value-domain never populates and fabricated-enum assertions can't be
    validated. This is the complementary source: a BOUNDED, strictly read-only
    (GET-only — R154 non-mutation safe) sweep of the SUT's OWN concrete captured GET
    endpoints, extracting enum-like field values and writing them onto
    `discovered_endpoints/<pid>.json` as `response_value_samples` (the flat
    {field:[values]} shape `_r313_value_domains_from_captured` reads).

    Generic — no SUT host/route/cookie literal; auth is whatever the caller assembled
    (storage-state cookies + Bearer). Killswitch ARTA_R313_D_VALUE_PROBE_DISABLE=1.
    Returns the number of endpoints enriched with value samples (0 = no-op)."""
    if os.environ.get("ARTA_R313_D_VALUE_PROBE_DISABLE") == "1":
        return 0
    if not base_url or not endpoints:
        return 0

    # Auth: storage-state cookies + Bearer (shared with the list-id seeder).
    cookies, headers = _probe_auth_headers(auth_state_path, agent_token, bearer_token)

    # Select CONCRETE GET endpoints (no unresolved {param} tokens — those need
    # value resolution the value-probe deliberately doesn't do). Dedup by path,
    # cap at max_calls so we never hammer the SUT.
    seen_paths: set[str] = set()
    targets: list[str] = []
    for e in endpoints:
        if not isinstance(e, dict):
            continue
        if str(e.get("method", "GET")).upper() != "GET":
            continue
        path = e.get("path") or ""
        if not path or "{" in path or "}" in path or ":" in path.split("?")[0]:
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)
        targets.append(path)
        if len(targets) >= max_calls:
            break
    if not targets:
        return 0

    # Union enum-like values per endpoint path.
    vals_by_path: dict[str, dict] = {}
    _base = base_url.rstrip("/")
    async with httpx.AsyncClient(
        timeout=timeout, verify=False, follow_redirects=True,
        cookies=cookies, headers=headers,
    ) as client:
        for path in targets:
            url = path if path.startswith("http") else _base + ("/" + path.lstrip("/"))
            try:
                r = await client.get(url)
            except Exception as _ex:
                log.debug("R313.D: GET %s failed: %s", url, _ex)
                continue
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code < 200 or r.status_code >= 400 or "json" not in ct:
                continue
            try:
                body = r.json()
            except Exception:
                continue
            acc: dict = {}
            _r313_d_collect_values(body, acc)
            if acc:
                vals_by_path[path] = {k: sorted(v)[:12] for k, v in acc.items()}

    if not vals_by_path:
        log.info("R313.D: value probe found no enum-like values across %d GET(s) "
                 "for project %s", len(targets), project_id)
        return 0

    # Write value samples onto the matching discovered_endpoints entries + persist.
    enriched = 0
    for e in endpoints:
        if isinstance(e, dict) and e.get("path") in vals_by_path:
            _dst = e.setdefault("response_value_samples", {})
            for _f, _vv in vals_by_path[e["path"]].items():
                _cur = set(_dst.get(_f, [])); _cur.update(_vv); _dst[_f] = sorted(_cur)[:12]
            enriched += 1
    try:
        _fp = _CAPTURED_DIR / f"{project_id}.json"
        _fp.write_text(json.dumps(endpoints, indent=2))
    except Exception as _exc:
        log.warning("R313.D: failed to persist value samples for %s: %s", project_id, _exc)
    log.info("R313.D: value probe enriched %d endpoint(s) with enum-like value "
             "domains for project %s (from %d live GET response(s))",
             enriched, project_id, len(vals_by_path))
    return enriched


# Google OAuth third_party_token. Some inner service-to-service APIs
# endpoint which returns an internal "agent_token" used in
# `Authorization: Bearer <agent_token>`. Without this, ~177 Newman
# auth-4xx failures stay FAIL. ARTA already detects token-exchange
# patterns at line 410-424; this function performs the actual
# exchange and the caller persists the result to project env vars.

_R96_1_DEFAULT_CLAIM_MAP = {
    "account_id":      "root_account_id",
    "subscriber_id":   "subscriber_id",
    "subscription_id": "subscription_id",
    "schema_id":       "schema_id",
    "organization_id": "organizations[0]",
    "user_id":         "sub",
}


def _r96_1_resolve_template(
    jwt_payload: dict, tx_config: dict,
) -> tuple[str, dict]:
    """R96.1 — resolve URL placeholders + payload fields from a single
    value pool that merges JWT claims (auto-derived) + operator-supplied
    payload_template (tenant-scoped fixed values).

    Per-field resolution order:
      1. payload_template entry (operator wins for tenant-scoped IDs)
      2. JWT claim via jwt_claim_map (auto-derived per-user fields)
      3. None (raises ValueError if used in URL placeholder)

    The unified pool means URL placeholders like `{workspace_id}` can
    come from EITHER JWT (when the SUT puts it there) OR operator
    config (the example SUT pattern: workspace_id is tenant-scoped, not in JWT).
    """
    import re as _re_r96
    import time as _t_r96

    url_template = tx_config.get("url_template", "")
    payload_template = dict(tx_config.get("payload_template") or {})
    jwt_claim_map = tx_config.get("jwt_claim_map") or _R96_1_DEFAULT_CLAIM_MAP

    def _walk_jwt(path: str):
        """Resolve dotted/indexed JWT paths like 'organizations[0]'."""
        cur = jwt_payload
        for part in _re_r96.split(r"\.|\[|\]", path):
            if not part:
                continue
            if isinstance(cur, list) and part.isdigit():
                idx = int(part)
                if idx >= len(cur):
                    return None
                cur = cur[idx]
            elif isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
            if cur is None:
                return None
        return cur

    # Step 1: build unified value pool.
    pool: dict[str, object] = {}
    # R213.H.2 base layer — direct top-level scalar keys from `jwt_payload`.
    # The pool was previously built ONLY from `jwt_claim_map` paths +
    # `payload_template`, so a URL placeholder that is NEITHER a mapped claim
    # that lives in the project env_block) could never resolve → ValueError →
    # exchange returns None → no agent_token → the path-param probe fetches 0
    # ids → env_block resource ids stay REPLACE_ME → ~215 Newman items BLOCK.
    # R213.H enriches `jwt_payload` with the resolved env_block vars; this base
    # layer makes the resolver actually READ them by direct name. claim_map +
    # payload_template still override (applied after), so authoritative values
    # win. Killswitch ARTA_R213_H2_DIRECT_PAYLOAD_POOL_DISABLE=1.
    if os.environ.get("ARTA_R213_H2_DIRECT_PAYLOAD_POOL_DISABLE") != "1":
        for _k, _v in (jwt_payload or {}).items():
            if isinstance(_v, (str, int)) and str(_v).strip():
                pool[_k] = _v
    # ...JWT-derived via the claim map (overrides the base layer)...
    for field_name, claim_path in jwt_claim_map.items():
        v = _walk_jwt(claim_path)
        if v is not None:
            pool[field_name] = v
    # ...then operator overrides (skipping the exp_offset_seconds helper).
    for k, v in payload_template.items():
        if k == "exp_offset_seconds":
            continue
        if v is not None:
            pool[k] = v   # operator wins

    # Step 2: resolve URL placeholders from pool.
    resolved_url = url_template
    for placeholder in _re_r96.findall(
        r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", url_template,
    ):
        v = pool.get(placeholder)
        if v is None:
            raise ValueError(
                f"R96.1: URL placeholder {{{placeholder}}} has no value. "
                f"Either add it to payload_template (operator-supplied) "
                f"OR add a jwt_claim_map entry pointing to a JWT claim. "
                f"Pool currently has: {sorted(pool.keys())}"
            )
        resolved_url = resolved_url.replace("{" + placeholder + "}", str(v))

    # Step 3: build payload. Operator's payload_template fields PLUS
    # JWT-derived fields not in URL-only convention. Default behavior:
    # include all pool entries listed in payload_template + JWT-derived
    # organization_id/user_id by default.
    resolved_payload: dict = {}
    for k, v in pool.items():
        if k in payload_template:
            # Operator listed this field → include in payload
            resolved_payload[k] = v
    # JWT-derived organization_id / user_id are conventionally in
    for default_field in ("organization_id", "user_id"):
        if default_field in pool and default_field not in resolved_payload:
            resolved_payload[default_field] = pool[default_field]

    # Step 4: compute exp from offset.
    exp_offset = int(payload_template.get("exp_offset_seconds") or 3600)
    resolved_payload["exp"] = int(_t_r96.time()) + exp_offset

    return resolved_url, resolved_payload


def _a7_harvest_admin_token_id(storage_state: dict) -> str | None:
    """A7.1 — the SPA mints its analytics Bearer from the long-lived ADMIN
    `agent-user-token.tokenId` (NOT the analytics `agent-api-token`). Return that
    admin record id from the session's localStorage, or None. SUT-agnostic key set."""
    if not isinstance(storage_state, dict):
        return None
    _ADMIN_KEYS = ("agent-user-token", "agent_user_token", "agentUserToken")
    for o in storage_state.get("origins") or []:
        for it in o.get("localStorage") or []:
            if (it.get("name") or "") not in _ADMIN_KEYS:
                continue
            try:
                j = json.loads(it.get("value") or "")
            except Exception:
                j = {}
            if isinstance(j, dict):
                for k in ("tokenId", "token_id", "__auto_id__", "id"):
                    v = j.get(k)
                    if isinstance(v, str) and v:
                        return v
    return None


async def _a7_mint_bound_agent_token(
    project: dict, session_token_claims: dict, storage_state: dict
) -> str | None:
    """A7.1 (R218 KEYSTONE) — mint a properly resource-bound analytics token the
    way the SUT's SPA does, via `auth_chain.mint_agent_user_token`
    (`POST .../schema/{schema_id}/agent_user_token` with the session ADMIN token id
    + user_id). The server binds `engine_type=analytics_tool`; that token validates
    (live 422 = past-auth). Returns the bound token_id, or None to fall back.

    Root cause this addresses: the `create_agent_api_token` template path mints an
    UNBOUND token (engine_type=='') → analytics 400 "Cant Connect to Authourization
    Server". `mint_agent_user_token` is implemented + live-proven but was never
    wired in (zero call sites) — this is the wiring."""
    admin_id = _a7_harvest_admin_token_id(storage_state or {})
    if not admin_id:
        log.info("A7: no admin agent-user-token in storage → cannot mint a bound "
                 "token; falling back to create_agent_api_token template")
        return None
    account_id = session_token_claims.get("root_account_id") or session_token_claims.get("account_id")
    sub = session_token_claims.get("subscriber_id")
    subn = session_token_claims.get("subscription_id")
    schema = session_token_claims.get("schema_id")
    user_id = session_token_claims.get("user_id") or session_token_claims.get("sub")
    if not all([account_id, sub, subn, schema, user_id]):
        log.info("A7: session_token missing ids for mint (acct=%s sub=%s subn=%s schema=%s "
                 "user=%s) → fallback", bool(account_id), bool(sub), bool(subn),
                 bool(schema), bool(user_id))
        return None
    # Mint host = the backend host (same as the token_exchange url_template host),
    # NOT the analytics api host. Derive from config; env override wins.
    integrations = project.get("integrations") or {}
    tmpl = ((integrations.get("token_exchange") or {}).get("url_template")
            or integrations.get("base_url") or "")
    api_base = os.environ.get("TARGET_AUTH_MINT_BASE") or ""
    if not api_base and tmpl:
        from urllib.parse import urlsplit
        sp = urlsplit(tmpl)
        if sp.scheme and sp.netloc:
            api_base = f"{sp.scheme}://{sp.netloc}"
    if not api_base:
        log.info("A7: no mint base url resolvable → fallback")
        return None
    try:
        from .auth_chain import mint_agent_user_token
    except Exception:
        from auth_chain import mint_agent_user_token  # type: ignore
    # A7.5 — prefer the SOURCE-DISCOVERED mint route (env_block.auth.mint, learned
    # by github_context.discover_auth_chain_from_source) so this is SUT-agnostic;
    _mint_cfg: dict = {}
    for _envb in (project.get("environments") or {}).values():
        _m = ((_envb or {}).get("auth") or {}).get("mint") if isinstance(_envb, dict) else None
        if isinstance(_m, dict) and _m.get("endpoint_template"):
            _mint_cfg = _m
            break
    import asyncio as _aio
    # mint_agent_user_token is synchronous httpx → run off the event loop.
    return await _aio.to_thread(
        mint_agent_user_token, api_base=api_base, account_id=account_id,
        subscriber_id=sub, subscription_id=subn, schema_id=schema,
        admin_token_id=admin_id, user_id=user_id,
        endpoint_template=_mint_cfg.get("endpoint_template"),
        body_key=(_mint_cfg.get("body_key") or "agen_api_token"))


async def exchange_session_for_agent_token(
    project: dict, session_token: str, storage_state: dict | None = None
) -> str | None:
    """Exchange the wrapped session-token for an the example SUT agent token.

    Returns the agent token, or None if no exchange endpoint is
    discoverable / configured. Caller is responsible for persisting
    the result via the bulk-add env-var API.

    A7.2 (R218 KEYSTONE): when `storage_state` is provided, FIRST try the SUT's
    own `agent_user_token` mint (the flow the SPA uses → a token bound to
    `engine_type=analytics_tool`). Only if that yields nothing do we fall back to
    the `create_agent_api_token` template — which mints an UNBOUND token (empty
    resource binding) that the analytics authz layer rejects with 400 "Cant
    Connect to Authourization Server" (the root cause this fixes).
    """
    import base64 as _b64
    import json as _json2
    parts = session_token.split(".")
    if len(parts) != 3:
        log.debug("EEE: session_token is not a JWT (parts=%d); skipping exchange", len(parts))
        return None
    try:
        _seg = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = _json2.loads(_b64.urlsafe_b64decode(_seg))
    except Exception as _exc:
        log.debug("EEE: JWT payload decode failed: %s", _exc)
        return None

    # A7.1/A7.2 — prefer the SPA's agent_user_token mint (bound token).
    if storage_state is not None and os.environ.get("ARTA_A7_MINT_DISABLE") != "1":
        _bound = await _a7_mint_bound_agent_token(project, payload, storage_state)
        if _bound:
            log.info("A7: minted resource-BOUND agent token via agent_user_token "
                     "(the SPA flow) — len=%d", len(_bound))
            return _bound

    third_party = payload.get("third_party_token") or payload.get("id_token")
    if not third_party:
        log.debug("EEE: JWT has no third_party_token / id_token claim; skipping")
        return None

    integrations = project.get("integrations") or {}
    base_url = integrations.get("base_url") or ""

    # R213.H — enrich the R96.1 resolution pool with the project's RESOLVED
    # token-exchange url_template is
    # `.../schema_id/{schema_id}/create_agent_api_token`; `schema_id` is a
    # tenant constant in env_block, NOT a JWT claim, so without this the
    # resolver raised "placeholder {schema_id} has no value" → exchange
    # returned None → no agent_token → `probe_path_param_values` fetched 0
    # real ids → env_block resource ids (collection_id/container_name/…)
    # stayed REPLACE_ME → ~215 Newman items BLOCKED on unresolved path params.
    # Merge real (non-placeholder) values only; NEVER overwrite a JWT claim.
    # Killswitch ARTA_R213_H_EXCHANGE_ENVBLOCK_POOL_DISABLE=1.
    if os.environ.get("ARTA_R213_H_EXCHANGE_ENVBLOCK_POOL_DISABLE") != "1":
        try:
            def _r213h_empty(x) -> bool:
                # A JWT claim present but NULL/empty/placeholder must NOT block
                # claim with value None → the pre-fix `_k not in payload` guard
                # treated it as "present" and skipped env_block's real
                return (x is None or (isinstance(x, str)
                        and (not x.strip() or x.strip() == "REPLACE_ME"
                             or x.startswith("__ARTA"))))
            for _envb in (project.get("environments") or {}).values():
                _vars = (_envb or {}).get("variables") or {}
                _items = _vars.items() if hasattr(_vars, "items") else (
                    (d.get("name"), d.get("value")) for d in _vars if isinstance(d, dict))
                for _k, _v in _items:
                    if (_k and isinstance(_v, (str, int)) and str(_v).strip()
                            and str(_v).strip() != "REPLACE_ME"
                            and not str(_v).startswith("__ARTA")
                            and _r213h_empty(payload.get(_k))):
                        payload[_k] = _v
        except Exception as _r213h_exc:
            log.debug("R213.H: env_block pool-merge skipped: %s", _r213h_exc)

    # R96.1 KEYSTONE — Priority 0: template-based exchange config.
    # Operator declares `integrations.token_exchange.url_template` +
    # `payload_template` (+ optional `jwt_claim_map`) in projects.json.
    # JWT claims auto-substitute into URL placeholders + payload fields.
    # detect fallback can't find the exchange endpoint. Live evidence
    # (run-2f077d → run-f50786 transition): R95.1's wiring is correct
    # but `agent_token` stayed empty without this Priority-0 path.
    tx_template = integrations.get("token_exchange") or {}
    if isinstance(tx_template, dict) and tx_template.get("url_template"):
        try:
            url, body = _r96_1_resolve_template(payload, tx_template)
            log.info(
                "R96.1: resolved template URL for project=%s: %s",
                project.get("id", "?"), url,
            )
            async with httpx.AsyncClient(
                timeout=15, verify=False,
                cookies={"session-token": session_token},
            ) as client:
                resp = await client.post(url, json=body)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        data = None
                    if isinstance(data, dict):
                        # R218 — PREFER the RECORD ID (`__auto_id__`/`token_id`) over
                        # the JWT. Live-grounded (A5 + AN live probe): the SUT
                        # analytics/extraction family accepts `Bearer <tokenId>` (the
                        # record id the SPA sends — 422/past-auth) and REJECTS the
                        # agent_api_token JWT (400 "Cant Connect to Authourization
                        # Server"). create_agent_api_token returns BOTH
                        # {agent_api_token: <jwt>, __auto_id__: <uuid record id>};
                        # the SPA stores __auto_id__ as the Bearer. Returning the JWT
                        # (pre-fix) unauthenticated every analytics call when no
                        # stored tokenId was available. Killswitch
                        # ARTA_EXCHANGE_PREFER_JWT=1 reverts to the JWT.
                        _keys = ("__auto_id__", "token_id", "tokenId",
                                 "agent_api_token", "agent_token", "access_token",
                                 "token", "bearerToken", "example_sut_token")
                        if os.environ.get("ARTA_EXCHANGE_PREFER_JWT") == "1":
                            _keys = _keys[3:]  # legacy: JWT-first
                        for k in _keys:
                            v = data.get(k)
                            if isinstance(v, str) and len(v) > 20:
                                log.info(
                                    "R96.1: exchanged session token → agent_token via "
                                    "template (key=%s, len=%d)",
                                    k, len(v),
                                )
                                return v
                log.warning(
                    "R96.1: template exchange returned status=%d "
                    "body_sample=%r — falling through to legacy candidates",
                    resp.status_code, (resp.text or "")[:200],
                )
        except ValueError as _r96_1_val_exc:
            # Resolver-side error: missing required placeholder. Surface
            # to operator with actionable message.
            log.warning("R96.1: %s", _r96_1_val_exc)
        except Exception as _r96_1_exc:
            log.warning(
                "R96.1: template exchange failed: %s — falling through",
                _r96_1_exc,
            )

    candidates: list[str] = []

    # Phase G refactor: prefer the URL discovered by the SUT Onboarding
    # Agent (Fix GGG) over the legacy hardcoded guesses. The
    # onboarding_config is project-specific and stored at
    # `integrations.onboarding_config.auth.token_exchange_endpoint`.
    onboarding = integrations.get("onboarding_config") or {}
    if isinstance(onboarding, dict):
        oc_auth = onboarding.get("auth") or {}
        oc_url = oc_auth.get("token_exchange_endpoint")
        if oc_url:
            candidates.append(oc_url if oc_url.startswith("http") else (base_url.rstrip("/") + oc_url))

    cfg_url = integrations.get("token_exchange_endpoint")
    if cfg_url and cfg_url not in candidates:
        candidates.append(cfg_url)

    if base_url:
        # Legacy fallback heuristics — kept as a safety net when no
        # onboarding_config is available yet (first run on a fresh
        # project). Generic shapes only; per-project specifics live
        # in onboarding_config.
        b = base_url.rstrip("/")
        backend_b = (b.replace("//sandbox.", "//backend.sandbox.")
                     if "//sandbox." in b
                     and os.environ.get("ARTA_SANDBOX_BACKEND_HEURISTIC") == "1"
                     else b)
        for u in (
            f"{backend_b}/api/auth/agent-token",
            f"{backend_b}/api/auth/exchange",
            f"{backend_b}/api/token/exchange",
            f"{backend_b}/api/auth/agent",
            f"{b}/api/auth/agent-token",
            f"{b}/api/auth/exchange",
            f"{b}/api/token/exchange",
        ):
            if u not in candidates:
                candidates.append(u)

    async with httpx.AsyncClient(
        timeout=10, verify=False,
        cookies={"session-token": session_token},
    ) as client:
        for url in candidates:
            if not url:
                continue
            for body in (
                {"third_party_token": third_party},
                {"token": third_party},
                {"id_token": third_party},
            ):
                try:
                    resp = await client.post(url, json=body)
                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                        except Exception:
                            continue
                        if not isinstance(data, dict):
                            continue
                        for k in ("agent_token", "access_token", "token", "bearerToken", "example_sut_token"):
                            v = data.get(k)
                            if isinstance(v, str) and len(v) > 20:
                                log.info(
                                    "EEE: exchanged session-token → agent_token via %s (key=%s, len=%d)",
                                    url, k, len(v),
                                )
                                return v
                except Exception as _exc:
                    log.debug("EEE: %s body=%s failed: %s", url, list(body.keys()), _exc)
    log.warning(
        "EEE: no agent_token endpoint discovered; "
        "Newman items needing Bearer auth will fall back to session-token cookie. "
        "Set integrations.token_exchange_endpoint in projects.json to override."
    )
    return None
