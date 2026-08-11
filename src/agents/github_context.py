"""
GitHub Code Context via MCP — Uses the public GitHub MCP server to let agents
fetch source code on-demand from project repositories.

Architecture:
  ARTA Agent → github_context.call_tool() → GitHub MCP Server (stdio) → GitHub API

The GitHub MCP server (github-mcp-server npm package) provides tools:
  - search_code: Search for code patterns across repos
  - get_file_contents: Read specific files
  - list_directory: Browse directory structure
  - search_repositories: Find repos

This module wraps the MCP server as a Python async client, providing a simple
interface for ARTA agents to query project source code.

Setup:
  npm install -g github-mcp-server
  Set GITHUB_TOKEN env var or configure per-project in Settings → Integrations
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("arta.github_context")


def _gh_repo_cap() -> int:
    """R196 — max SUT repos to walk per extraction. Default 15 (covers a
    14-repo platform). Pre-R196 every walk was hardcoded to repos[:3],
    so a multi-repo SUT had only its first 3 repos read — ARTA literally wasn't
    'going through all the code', missing the business-logic repos (pipeline-api,
    analytics, monitoring-*, subscriptions) where the dataset→app→
    agent-token→launch workflow + its data-dependencies live, so R193/R195 had
    nothing to chain. Env-overridable: ARTA_GITHUB_MAX_REPOS."""
    try:
        return max(1, int(os.environ.get("ARTA_GITHUB_MAX_REPOS", "15")))
    except (TypeError, ValueError):
        return 15

# Cache to avoid repeated MCP calls for the same queries
_CACHE: dict[str, str] = {}

# ── R105.A — Disk-backed cache layer with 24h TTL ────────────────────────
#
# Pre-R105.A every github_context.py helper hit the GitHub REST API every
# call → 19 PW reqs × ~189 API calls = 3,591 calls per regen batch →
# 1000/h rate limit blew immediately → R104.B's SUT source-context block
# stayed empty → "0 R104.B: injected SUT source context" log events in
# run-de1031 despite the agent-owned MCP scaffold being wired correctly.
#
# Post-R105.A: keyed by (repo, branch, op[, path]) with 24h TTL on disk
# AND in-memory. First gen call for a repo fills the cache; subsequent
# 18 reqs in the same batch are zero-API-call. Operator can force-flush
# via POST /api/admin/clear-github-cache.

import time as _time_r105
from pathlib import Path as _Path_r105

_R105_CACHE_DIR = _Path_r105(".arta/github_cache")
_R105_CACHE_TTL_SECONDS = 24 * 60 * 60   # 24 hours


def _r105_a_cache_key(*parts: str) -> str:
    """Stable hash key from (repo, branch, op, *args). Filesystem-safe."""
    import hashlib
    joined = ":".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode()).hexdigest()[:24]


def _r105_a_cache_get(key: str) -> str | None:
    """Return cached value if fresh (< 24h old); else None.

    Two-tier: in-memory dict first, then disk. Disk hit warms the
    in-memory cache for subsequent reads in the same process.
    """
    if key in _CACHE:
        return _CACHE[key]
    _path = _R105_CACHE_DIR / f"{key}.json"
    if not _path.exists():
        return None
    try:
        _age = _time_r105.time() - _path.stat().st_mtime
        if _age > _R105_CACHE_TTL_SECONDS:
            return None
        _content = _path.read_text(encoding="utf-8")
        _CACHE[key] = _content   # warm in-memory for subsequent reads
        return _content
    except Exception as exc:
        log.debug("R105.A: cache read failed for %s: %s", key, exc)
        return None


def _r105_a_cache_set(key: str, value: str) -> None:
    """Write to in-memory + disk. Graceful on disk write failure."""
    _CACHE[key] = value
    try:
        _R105_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_R105_CACHE_DIR / f"{key}.json").write_text(value, encoding="utf-8")
    except Exception as exc:
        log.debug("R105.A: cache write failed for %s: %s", key, exc)


def _r105_a_cache_clear(project_id: str | None = None) -> int:
    """Clear cache entries. Returns count of entries cleared.

    project_id is informational (key hash doesn't directly encode it);
    callers pass it for the admin-endpoint response. Without filter,
    clears everything (in-memory + disk).
    """
    cleared = len(_CACHE)
    _CACHE.clear()
    if _R105_CACHE_DIR.exists():
        for _f in _R105_CACHE_DIR.glob("*.json"):
            try:
                _f.unlink()
                cleared += 1
            except Exception:
                pass
    return cleared


class GitHubMCPClient:
    """Async client that spawns the GitHub MCP server and calls its tools via stdio JSON-RPC."""

    def __init__(self, github_token: str):
        self._token = github_token
        self._proc: asyncio.subprocess.Process | None = None
        self._request_id = 0

    async def start(self) -> None:
        """Start the GitHub MCP server subprocess."""
        import shutil
        mcp_cmd = shutil.which("github-mcp-server")
        if not mcp_cmd:
            # Try npx fallback
            npx = shutil.which("npx")
            if npx:
                mcp_cmd = npx
                args = [mcp_cmd, "github-mcp-server"]
            else:
                raise RuntimeError("github-mcp-server not found. Install: npm install -g github-mcp-server")
        else:
            args = [mcp_cmd]

        env = {**os.environ, "GITHUB_TOKEN": self._token}
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Initialize MCP connection
        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "arta", "version": "1.0"},
        })
        # Send initialized notification
        await self._send_notification("notifications/initialized", {})

    async def stop(self) -> None:
        """Stop the MCP server subprocess."""
        if self._proc:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
            self._proc = None

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call an MCP tool and return the text result."""
        if not self._proc or self._proc.returncode is not None:
            try:
                await self.start()
            except Exception as e:
                log.warning("MCP server start failed: %s", e)
                return ""  # Return empty — caller should fall back to direct API

        try:
            result = await self._send_request("tools/call", {
                "name": tool_name,
                "arguments": arguments,
            })

            # Extract text content from MCP response
            content = result.get("content", [])
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return "\n".join(texts)
        except Exception as e:
            log.warning("MCP tool call %s failed: %s", tool_name, e)
            return ""

    async def _send_request(self, method: str, params: dict) -> dict:
        """Send JSON-RPC request to MCP server and wait for response.

        Uses Content-Length framing per MCP/LSP spec:
          Content-Length: <N>\r\n\r\n<JSON payload>
        """
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }

        if not self._proc or not self._proc.stdin or not self._proc.stdout:
            raise RuntimeError("MCP server not started")

        await self._write_message(request)

        # Read response (may need to skip notifications)
        deadline = asyncio.get_event_loop().time() + 30
        while asyncio.get_event_loop().time() < deadline:
            response = await asyncio.wait_for(self._read_message(), timeout=30)
            if response.get("id") == self._request_id:
                if "error" in response:
                    raise RuntimeError(f"MCP error: {response['error']}")
                return response.get("result", {})
            # Skip notifications (no id field)

        raise RuntimeError(f"MCP response timeout for {method}")

    async def _send_notification(self, method: str, params: dict) -> None:
        """Send JSON-RPC notification (no response expected)."""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._write_message(notification)

    async def _write_message(self, msg: dict) -> None:
        """Write a JSON-RPC message. Supports both newline-delimited and Content-Length framing."""
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("MCP server not started")
        body = json.dumps(msg)
        # Use newline-delimited JSON (most npm MCP servers use this, not LSP Content-Length)
        self._proc.stdin.write((body + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_message(self) -> dict:
        """Read a JSON-RPC message. Auto-detects framing and skips non-JSON lines.

        MCP servers may emit startup banners, warnings, or logging to stdout
        before the JSON-RPC stream begins. We skip those gracefully.
        """
        if not self._proc or not self._proc.stdout:
            raise RuntimeError("MCP server not started")

        max_skip = 50  # Safety limit to avoid infinite loops
        for _ in range(max_skip):
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=30)
            if not line:
                raise RuntimeError("MCP server closed connection")

            text = line.decode("utf-8").strip()

            # Skip empty lines
            if not text:
                continue

            # Content-Length framing (LSP style)
            if text.lower().startswith("content-length:"):
                content_length = int(text.split(":", 1)[1].strip())
                await self._proc.stdout.readline()  # blank separator
                body = await asyncio.wait_for(self._proc.stdout.readexactly(content_length), timeout=30)
                return json.loads(body.decode("utf-8"))

            # Try parsing as JSON — skip non-JSON lines (startup banners, logs)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                log.debug("MCP: skipping non-JSON line: %s", text[:100])
                continue

        raise RuntimeError("MCP: no valid JSON-RPC message after skipping startup output")


# ── High-level functions for agents ──────────────────────────────────────────

async def fetch_code_context(project: dict) -> str:
    """Fetch relevant source code from project's GitHub repos via MCP.

    Called during test generation to provide code context to LLM agents.
    Uses the GitHub MCP server's search_code and get_file_contents tools.

    Returns formatted code context string, or empty string if unavailable.
    """
    project_id = project.get("id", "")
    cache_key = f"context:{project_id}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    integrations = project.get("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()

    token = integrations.get("github_token", "") or os.environ.get("GITHUB_TOKEN", "")
    repos = integrations.get("repositories", [])

    if not repos or not token:
        return ""

    # Use direct GitHub REST API (the npm github-mcp-server package is a Git CLI tool,
    # NOT the GitHub API MCP server — it doesn't support search_code/get_file_contents).
    # TODO: Switch to the official GitHub MCP server (Go binary) when available in Docker.
    result = await _fetch_direct_api(project)
    _CACHE[cache_key] = result  # Cache even empty results to skip retries
    return result


def _gh_token(project: dict) -> str:
    integrations = project.get("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    return integrations.get("github_token", "") or os.environ.get("GITHUB_TOKEN", "")


def _gh_org(project: dict) -> str:
    """Org/owner from the first configured repo (`owner/name` → `owner`)."""
    integrations = project.get("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    for r in (integrations.get("repositories", []) or []):
        full = r.get("repo") if isinstance(r, dict) else r
        if full and "/" in str(full):
            return str(full).split("/", 1)[0]
    return ""


async def get_file(project: dict, repo: str, file_path: str) -> str:
    """Read a specific file from a project repo, returning its raw text.

    R297.A: prefers the direct GitHub REST contents API (works everywhere the
    token reaches api.github.com — no MCP-server binary dependency). The npm
    `github-mcp-server` is a Git-CLI wrapper that does NOT implement
    get_file_contents, so the MCP path returns "MCP server closed connection" in
    every deployment that lacks the (nonexistent) API MCP binary — the same reason
    `fetch_code_context` already routes through the REST API. MCP stays as a
    fallback. Killswitch ARTA_GH_DIRECT_API_DISABLE=1 forces MCP-only.
    """
    token = _gh_token(project)
    if not token:
        return ""

    cache_key = f"file:{repo}:{file_path}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    if os.environ.get("ARTA_GH_DIRECT_API_DISABLE") != "1":
        try:
            import base64
            import httpx
            headers = {"Authorization": f"token {token}",
                       "Accept": "application/vnd.github.v3+json"}
            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/contents/{file_path.lstrip('/')}")
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("content", "") if isinstance(data, dict) else ""
                if content and data.get("encoding") == "base64":
                    text = base64.b64decode(content).decode("utf-8", "replace")
                else:
                    text = content or ""
                _CACHE[cache_key] = text
                return text
            log.debug("R297.A: REST get_file %s/%s -> HTTP %s", repo, file_path,
                      resp.status_code)
        except Exception as e:
            log.debug("R297.A: REST get_file %s/%s failed: %s — MCP fallback",
                      repo, file_path, e)

    client = GitHubMCPClient(token)
    try:
        await client.start()
        owner, name = repo.split("/", 1) if "/" in repo else ("", repo)
        result = await client.call_tool("get_file_contents", {
            "owner": owner,
            "repo": name,
            "path": file_path,
        })
        _CACHE[cache_key] = result
        return result
    except Exception as e:
        log.warning("Failed to read %s/%s via MCP: %s", repo, file_path, e)
        return ""
    finally:
        await client.stop()


async def search_code(project: dict, query: str, repo: str | None = None) -> str:
    """Search for code across a project's repos, returning the GitHub search/code
    JSON text (`{"items":[{"repository":{"full_name":..},"path":..}]}`).

    R297.A: prefers the direct GitHub REST search API. Scopes to a single `repo`
    when given, else to the configured ORG (`org:<owner>`) so a class/DTO in ANY
    of the SUT's repos is found — the old MCP path only searched `repos[0]`, so a
    cross-repo lookup silently missed. MCP stays as a fallback; killswitch
    ARTA_GH_DIRECT_API_DISABLE=1 forces MCP-only.
    """
    token = _gh_token(project)
    if not token:
        return ""

    integrations = project.get("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    repos = integrations.get("repositories", [])

    if repo:
        scope = f"repo:{repo}"
    else:
        org = _gh_org(project)
        if org:
            scope = f"org:{org}"
        elif repos:
            scope = f"repo:{(repos[0].get('repo', '') if isinstance(repos[0], dict) else repos[0])}"
        else:
            return ""
    search_query = f"{query} {scope}".strip()

    if os.environ.get("ARTA_GH_DIRECT_API_DISABLE") != "1":
        try:
            import httpx
            headers = {"Authorization": f"token {token}",
                       "Accept": "application/vnd.github.v3+json"}
            async with httpx.AsyncClient(timeout=20, headers=headers) as client:
                resp = await client.get(
                    "https://api.github.com/search/code",
                    params={"q": search_query, "per_page": 10})
            if resp.status_code == 200:
                return resp.text
            log.debug("R297.A: REST search_code(%s) -> HTTP %s", search_query,
                      resp.status_code)
        except Exception as e:
            log.debug("R297.A: REST search_code(%s) failed: %s — MCP fallback",
                      search_query, e)

    client = GitHubMCPClient(token)
    try:
        await client.start()
        result = await client.call_tool("search_code", {
            "query": search_query,
            "per_page": 10,
        })
        return result
    except Exception as e:
        log.warning("Code search failed via MCP: %s", e)
        return ""
    finally:
        await client.stop()


# ── Fallback: direct GitHub API (when MCP server not available) ──────────────

async def _fetch_direct_api(project: dict) -> str:
    """Fallback: fetch code via direct GitHub REST API (no MCP dependency)."""
    import httpx

    integrations = project.get("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    token = integrations.get("github_token", "") or os.environ.get("GITHUB_TOKEN", "")
    repos = integrations.get("repositories", [])

    if not repos or not token:
        return ""

    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    snippets: list[str] = []

    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        for repo_entry in repos[:_gh_repo_cap()]:  # R196 — env-configurable (was 3)
            repo = repo_entry.get("repo", "")
            if not repo:
                continue
            try:
                # Get repo tree to find key files
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/git/trees/{repo_entry.get('branch', 'main')}",
                    params={"recursive": "1"},
                )
                if resp.status_code != 200:
                    continue

                tree = resp.json().get("tree", [])
                key_files = [
                    item["path"] for item in tree
                    if item.get("type") == "blob"
                    and any(p in item["path"].lower() for p in ["route", "api", "model", "schema", "controller"])
                    and item["path"].endswith((".py", ".ts", ".js"))
                    and "node_modules" not in item["path"]
                    and "test" not in item["path"].lower()
                ][:10]

                for fpath in key_files:
                    try:
                        import base64
                        fresp = await client.get(f"https://api.github.com/repos/{repo}/contents/{fpath}")
                        if fresp.status_code == 200:
                            content = base64.b64decode(fresp.json().get("content", "")).decode("utf-8", errors="replace")
                            snippets.append(f"## {repo}/{fpath}\n```\n{content[:4000]}\n```")
                    except Exception:
                        pass
            except Exception as e:
                log.debug("Direct API fetch failed for %s: %s", repo, e)

    return "\n\n".join(snippets)


def clear_cache(project_id: str | None = None) -> None:
    """Clear cached code context. Called on 'Re-sync from GitHub'."""
    if project_id:
        keys_to_remove = [k for k in _CACHE if project_id in k]
        for k in keys_to_remove:
            del _CACHE[k]
    else:
        _CACHE.clear()


# ── Fix NNN: Frontend route + selector extraction ────────────────────────────
# Static-source extraction for Next.js / React projects. Complements the
# live-DOM probe (Fix II) — NNN runs offline against GitHub source so it works
# before the SUT is reachable; the routes feed the Playwright generation prompt.

import re as _re_nnn


def _path_to_route(file_path: str) -> str | None:
    """Map a Next.js page file to its URL route.

    app router:    app/foo/[id]/page.tsx → /foo/:id
    pages router:  pages/foo/[id].tsx    → /foo/:id
    Group folders ((marketing)) are stripped. Returns None for non-page files.
    """
    parts = file_path.split("/")
    if not parts:
        return None
    if parts[0] == "app":
        if not parts[-1].startswith("page."):
            return None
        segs = parts[1:-1]
    elif parts[0] == "pages":
        last = parts[-1]
        if last in ("_app.tsx", "_document.tsx", "_error.tsx", "_app.js", "_document.js"):
            return None
        if last.endswith((".tsx", ".jsx", ".ts", ".js")):
            stem = last.rsplit(".", 1)[0]
            segs = parts[1:-1] + ([] if stem == "index" else [stem])
        else:
            return None
    else:
        return None

    cleaned: list[str] = []
    for s in segs:
        if s.startswith("(") and s.endswith(")"):
            continue
        if s.startswith("[") and s.endswith("]"):
            cleaned.append(":" + s.strip("[]."))
        else:
            cleaned.append(s)
    return "/" + "/".join(cleaned) if cleaned else "/"


_TESTID_RE = _re_nnn.compile(r'data-testid=["\']([^"\']+)["\']')
_BUTTON_RE = _re_nnn.compile(r"<button[^>]*>([^<]{1,80})</button>", _re_nnn.IGNORECASE)
_NAME_RE = _re_nnn.compile(r'\bname=["\']([a-zA-Z0-9_\-]+)["\']')


def _r105_b_extract_react_router_paths(content: str) -> list[dict]:
    """R105.B — parse React Router 6+ `<Route path="..." element={<X />} />`
    patterns from a Routes.js / Routes.jsx / Routes.tsx file.

    Pre-R105.B one SUT's CRA + React Router 6 web repo returned 0 routes
    from `extract_frontend_routes` because the function only walks Next.js
    `app/*.tsx` / `pages/*.tsx` paths. R105.B adds the React Router parser
    alongside the Next.js logic so non-Next.js SUTs yield FE routes.

    Returns list of {route, component} dicts. Empty list on parse failure.
    """
    import re as _re_r105_b
    routes: list[dict] = []
    # Match both single-quote and double-quote variants; allow whitespace
    # around `=` and inside the element. The element tag is the component
    # name (first identifier inside `<...`).
    pattern = _re_r105_b.compile(
        r'<Route\s+[^>]*?\bpath\s*=\s*["\']([^"\']+)["\']'
        r'[^>]*?\belement\s*=\s*\{\s*<\s*(\w+)',
        _re_r105_b.IGNORECASE | _re_r105_b.DOTALL,
    )
    for m in pattern.finditer(content):
        path, component = m.group(1), m.group(2)
        if not path:
            continue
        routes.append({"route": path, "component": component})
    # Also support `<Route path="..." component={X} />` (React Router 5)
    pattern_v5 = _re_r105_b.compile(
        r'<Route\s+[^>]*?\bpath\s*=\s*["\']([^"\']+)["\']'
        r'[^>]*?\bcomponent\s*=\s*\{\s*(\w+)',
        _re_r105_b.IGNORECASE | _re_r105_b.DOTALL,
    )
    seen = {r["route"] for r in routes}
    for m in pattern_v5.finditer(content):
        path, component = m.group(1), m.group(2)
        if path and path not in seen:
            routes.append({"route": path, "component": component})
            seen.add(path)

    # R107.A — React Router 6 data-router CONFIG-OBJECT syntax used by
    # createBrowserRouter/createRoutesFromElements:
    #   { path: '/dashboard', element: <Dashboard /> }
    # shape; pre-R107.A the JSX-only parser found 0 routes from the
    # empty → PW LLM kept hallucinating page.goto paths.
    pattern_obj = _re_r105_b.compile(
        r"\bpath\s*:\s*['\"]([^'\"]+)['\"]"
        r"\s*,\s*element\s*:\s*<\s*(\w+)",
        _re_r105_b.IGNORECASE | _re_r105_b.DOTALL,
    )
    for m in pattern_obj.finditer(content):
        path, component = m.group(1), m.group(2)
        if path and path not in seen:
            routes.append({"route": path, "component": component})
            seen.add(path)
    # Also catch `{ path: '/x', component: SomeComponent }` (React Router 5
    pattern_obj_v5 = _re_r105_b.compile(
        r"\bpath\s*:\s*['\"]([^'\"]+)['\"]"
        r"\s*,\s*component\s*:\s*(\w+)",
        _re_r105_b.IGNORECASE | _re_r105_b.DOTALL,
    )
    for m in pattern_obj_v5.finditer(content):
        path, component = m.group(1), m.group(2)
        # Skip when the captured component name is a string literal value
        # Real React Router component refs are bare identifiers.
        if path and path not in seen and component[0].isupper():
            routes.append({"route": path, "component": component})
            seen.add(path)
    return routes


# ── B1 — Module-Federation remote-route extraction ───────────────────────────
# A micro-frontend shell (Webpack Module Federation) mounts each remote through a
# SINGLE catch-all `<Route path=".../:param">`; the concrete param resolves at
# runtime (via a remote registry's `localRoute`) to a remote bundle. Remotes that
# INVISIBLE to the static `<Route>` parsers above, so their routes were never
# discovered → gen hallucinated their DOM. This recovers them SUT-agnostically:
# the shell's `RemoteAppHandler` matches the URL substring against `localRoute`,
# so `<base>/<param>/<localRoute-token>` navigates the remote.


def _r298_b1_extract_mf_remotes(content: str) -> list[dict]:
    """B1 — parse a Module-Federation remote registry (`remoteModuleUrls` w/
    `localRoute`) for its mounted remotes. Returns `[{local_route, remote_name,
    module}]` (one entry per localRoute token, since a remote may map several).
    SUT-agnostic; empty when the file isn't an MF registry."""
    import re as _re_mf
    if not content or "localRoute" not in content:
        return []
    _lr = _re_mf.compile(r"""["']?localRoute["']?\s*:\s*\[([^\]]*)\]""", _re_mf.I)
    _tok = _re_mf.compile(r"""["']([A-Za-z0-9_\-./]+)["']""")
    _rn = _re_mf.compile(r"""["']?remoteName["']?\s*:\s*["']([A-Za-z0-9_\-]+)["']""", _re_mf.I)
    _mod = _re_mf.compile(r"""["']?moduleToLoad["']?\s*:\s*["']([^"']+)["']""", _re_mf.I)
    out: list[dict] = []
    seen: set[str] = set()
    for m in _lr.finditer(content):
        tokens = _tok.findall(m.group(1) or "")
        if not tokens:
            continue
        # remoteName / moduleToLoad sit just ABOVE localRoute in the same object;
        # take the NEAREST-preceding match (last within a small back-window) so
        # adjacent registry objects don't cross-contaminate.
        _back = content[max(0, m.start() - 400): m.start()]
        _rn_all = _rn.findall(_back)
        _mod_all = _mod.findall(_back)
        _remote_name = _rn_all[-1] if _rn_all else ""
        _module = _mod_all[-1] if _mod_all else ""
        for tok in tokens:
            if not tok or tok in seen:
                continue
            seen.add(tok)
            out.append({"local_route": tok, "remote_name": _remote_name,
                        "module": _module})
    return out


def _r298_b1_synthesize_mf_routes(routes: list[dict], mf_remotes: list[dict]) -> list[dict]:
    """B1 — synthesize concrete MF remote routes by substituting each remote's
    `localRoute` token into the shell's catch-all `<Route path=".../:param">`
    (already present in `routes`). Returns the NEW route dicts (not yet in
    `routes`). Pure + testable. No catch-all or no remotes → []."""
    import re as _re_b1s
    if not mf_remotes:
        return []
    _existing = {r.get("route") for r in routes if isinstance(r, dict)}
    # The remote-mount catch-all is the `<Route path=".../:param">` whose COMPONENT
    # is the federated-remote LOADER (e.g. RemoteAppHandler) — NOT every `:param`
    # route. Matching on the component name avoids synthesizing bogus routes into
    # unrelated params like `/Security/activateUser/:guid` (activateUser),
    # `/portal/xss/:id` (XSSComponent), or an iframe wrapper. SUT-agnostic.
    _loader_re = _re_b1s.compile(r"remote|federat|mfe|microfrontend", _re_b1s.I)
    _catchalls = sorted({
        r["route"] for r in routes
        if isinstance(r, dict) and isinstance(r.get("route"), str)
        and _re_b1s.search(r"/:[A-Za-z_][A-Za-z0-9_]*$", r["route"])
        and _loader_re.search(str(r.get("component") or ""))
    })
    new: list[dict] = []
    for _ca in _catchalls:
        _base = _ca.rsplit("/:", 1)[0]
        for _rm in mf_remotes:
            _tok = _rm.get("local_route")
            if not _tok:
                continue
            _sr = f"{_base}/{_tok}"
            if _sr in _existing:
                continue
            _existing.add(_sr)
            new.append({
                "route": _sr,
                "file": _rm.get("file", "(module-federation)"),
                "repo": _rm.get("repo", ""),
                "component": _rm.get("remote_name", ""),
                "source": "module_federation",
                "relevance_terms": [t for t in (
                    _rm.get("remote_name"), _rm.get("module"),
                    _rm.get("local_route")) if t],
            })
    return new


_R105_C_API_CALL_PATTERNS = None   # lazy compile

def _r105_c_extract_api_calls_from_component(content: str) -> list[str]:
    """R105.C — extract API call URLs from a React component file.

    Detects the dominant React data-fetching idioms:
      - `fetch('/api/...')`
      - `axios.{get,post,...}('/api/...')` / `apiClient.X(...)` / `api.X(...)`
      - `useQuery({ ..., queryFn: () => fetch('/api/...') })`
      - Custom hooks: `useFetchX('/api/...')`, `useDatasets('/api/...')`
      - Generic `.{get,post,put,patch,delete}('/api/...')` chain calls

    Returns deduped sorted list of path strings (capped at 10 per file).
    Pre-R105.C the PW prompt's FRONTEND ROUTES block listed route + component
    but NOT the API calls each route triggers, so the LLM hallucinated
    `await page.waitForResponse(...'/v1/seeds')` instead of the real
    `/api/v1/datasets` that the dashboard component invokes.
    """
    import re as _re_r105_c
    global _R105_C_API_CALL_PATTERNS
    if _R105_C_API_CALL_PATTERNS is None:
        _R105_C_API_CALL_PATTERNS = [
            # fetch('/api/...')
            _re_r105_c.compile(r'\bfetch\(\s*[`\'"]([^`\'"]+)[`\'"]'),
            # axios.X('/api/...') / apiClient.X / api.X / client.X / http.X
            _re_r105_c.compile(
                r'\b(?:axios|apiClient|api|client|http)\.'
                r'(?:get|post|put|patch|delete|head|options)\s*\('
                r'\s*[`\'"]([^`\'"]+)[`\'"]'
            ),
            # Custom hooks: useFetchX('/api/...'), useDatasets('/api/...'), etc.
            _re_r105_c.compile(
                r'\buse(?:Fetch|Get|Post|Query|Mutation|Api)\w*\('
                r'\s*[`\'"]([^`\'"]+)[`\'"]'
            ),
            # Generic chained call: `.get('/api/...')` (after axios.create or
            # similar). Anchor on the leading dot + HTTP verb to reduce noise.
            _re_r105_c.compile(
                r'(?<=\.)(?:get|post|put|patch|delete)\s*\('
                r'\s*[`\'"](\/[^`\'"]+)[`\'"]'
            ),
        ]
    calls: set[str] = set()
    for pat in _R105_C_API_CALL_PATTERNS:
        for m in pat.finditer(content):
            url = m.group(1)
            # Keep only path-like URLs (start with `/` or `${...}` template var)
            if url.startswith("/") or url.startswith("${"):
                calls.add(url[:120])
    return sorted(calls)[:10]


def _extract_selectors_from_source(source: str) -> dict:
    """Pull data-testid, button text, and form field names from a tsx source."""
    return {
        "test_ids": sorted(set(_TESTID_RE.findall(source)))[:50],
        "buttons": sorted({b.strip() for b in _BUTTON_RE.findall(source) if b.strip()})[:30],
        "form_fields": sorted(set(_NAME_RE.findall(source)))[:50],
    }


async def extract_frontend_routes(project: dict) -> list[dict]:
    """Fix NNN: walk Next.js source repos and extract routes + selectors.

    Returns a list of route dicts persistable to
    `project.integrations.frontend_routes`. Each entry looks like:
        {"route": "/admin/users/:id", "file": "app/admin/users/[id]/page.tsx",
         "test_ids": [...], "buttons": [...], "form_fields": [...]}

    Best-effort: any per-repo / per-file failure is swallowed; a missing
    GitHub token returns []. Caller is responsible for persisting the result.
    """
    import httpx
    import base64

    integrations = project.get("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    token = integrations.get("github_token", "") or os.environ.get("GITHUB_TOKEN", "")
    repos = integrations.get("repositories", [])
    if not repos or not token:
        return []

    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    routes: list[dict] = []
    mf_remotes: list[dict] = []   # B1 — Module-Federation remotes across all repos

    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        # R196 MISS (fixed) — this walk was left at the hardcoded `repos[:3]`
        # when R196 converted the other three walks (:411, :887, :1134) to the
        # env-configurable cap. R196's own docstring names the bug: "a multi-repo
        # SUT had only its first 3 repos read — ARTA literally wasn't going
        # extraction saw 3 → 3 routes with empty selectors → the probe had no
        # real SPA routes to walk → shell DOM catalog. Same cap, same env var
        # (ARTA_GITHUB_MAX_REPOS), single source of truth.
        for repo_entry in repos[:_gh_repo_cap()]:
            repo = repo_entry.get("repo", "")
            branch = repo_entry.get("branch", "main")
            if not repo:
                continue
            try:
                # R105.A — cache the git/trees fetch (large response; one per repo+branch)
                _tree_key = _r105_a_cache_key(repo, branch, "tree")
                _tree_cached = _r105_a_cache_get(_tree_key)
                if _tree_cached:
                    tree = json.loads(_tree_cached).get("tree", [])
                else:
                    resp = await client.get(
                        f"https://api.github.com/repos/{repo}/git/trees/{branch}",
                        params={"recursive": "1"},
                    )
                    if resp.status_code != 200:
                        log.debug("R105.A: tree fetch %s returned %d", repo, resp.status_code)
                        continue
                    _r105_a_cache_set(_tree_key, resp.text)
                    tree = resp.json().get("tree", [])
                # R105.B — also include React Router route files (CRA / Vite / etc.)
                # `src/Routes/Routes.js` (React Router 6); without this, FE-routes
                page_files = [
                    item["path"] for item in tree
                    if item.get("type") == "blob"
                    and (
                        (item["path"].startswith("app/") and (item["path"].endswith("page.tsx") or item["path"].endswith("page.jsx")))
                        or (item["path"].startswith("pages/") and item["path"].endswith((".tsx", ".jsx")))
                    )
                    and "node_modules" not in item["path"]
                ][:60]
                react_router_files = [
                    item["path"] for item in tree
                    if item.get("type") == "blob"
                    and any(
                        item["path"].endswith(f"Routes{ext}")
                        for ext in (".js", ".jsx", ".ts", ".tsx")
                    )
                    and "node_modules" not in item["path"]
                    and "/test" not in item["path"].lower()
                ][:8]
                # B1 — Module-Federation remote-registry files (path heuristic;
                # content is confirmed at parse time). Catches RemoteModuleUrlHandler,
                # moduleFederation.config, remoteApp handlers — the files that carry
                # the `localRoute` registry for remotes with no internal *Routes.*.
                mf_files = [
                    item["path"] for item in tree
                    if item.get("type") == "blob"
                    and item["path"].endswith((".js", ".jsx", ".ts", ".tsx"))
                    and "node_modules" not in item["path"]
                    and "/test" not in item["path"].lower()
                    and any(_k in item["path"].lower() for _k in (
                        "modulefederation.config", "remotemoduleurl", "remotemodule",
                        "remoteapp", "federatedcomponent", "remoteroutes",
                        "microfrontend"))
                ][:8]
                for fpath in page_files:
                    route = _path_to_route(fpath)
                    if route is None:
                        continue
                    try:
                        # R105.A — cache each file's content (Next.js page files)
                        _file_key = _r105_a_cache_key(repo, branch, "content", fpath)
                        _file_cached = _r105_a_cache_get(_file_key)
                        if _file_cached:
                            content = _file_cached
                        else:
                            fresp = await client.get(f"https://api.github.com/repos/{repo}/contents/{fpath}",
                                                     params={"ref": branch})
                            if fresp.status_code != 200:
                                routes.append({"route": route, "file": fpath, "repo": repo})
                                continue
                            content = base64.b64decode(fresp.json().get("content", "")).decode("utf-8", errors="replace")
                            _r105_a_cache_set(_file_key, content)
                        sel = _extract_selectors_from_source(content)
                        # R105.C — extract API calls invoked by this component
                        api_calls = _r105_c_extract_api_calls_from_component(content)
                        _route_entry = {
                            "route": route,
                            "file": fpath,
                            "repo": repo,
                            **sel,
                        }
                        if api_calls:
                            _route_entry["api_calls"] = api_calls
                        routes.append(_route_entry)
                    except Exception:
                        routes.append({"route": route, "file": fpath, "repo": repo})
                # R105.B — parse React Router files for <Route path=... element=...>
                for fpath in react_router_files:
                    try:
                        _rr_key = _r105_a_cache_key(repo, branch, "content", fpath)
                        _rr_cached = _r105_a_cache_get(_rr_key)
                        if _rr_cached:
                            rr_content = _rr_cached
                        else:
                            fresp = await client.get(
                                f"https://api.github.com/repos/{repo}/contents/{fpath}",
                                params={"ref": branch},
                            )
                            if fresp.status_code != 200:
                                continue
                            rr_content = base64.b64decode(
                                fresp.json().get("content", "")
                            ).decode("utf-8", errors="replace")
                            _r105_a_cache_set(_rr_key, rr_content)
                        rr_routes = _r105_b_extract_react_router_paths(rr_content)
                        # R105.C — extract api_calls from the React Router file too
                        rr_api_calls = _r105_c_extract_api_calls_from_component(rr_content)
                        for rr in rr_routes:
                            _route_entry = {
                                "route": rr["route"],
                                "file": fpath,
                                "repo": repo,
                                "component": rr.get("component", ""),
                            }
                            if rr_api_calls:
                                _route_entry["api_calls"] = rr_api_calls
                            routes.append(_route_entry)
                    except Exception as exc:
                        log.debug("R105.B: React Router parse failed for %s/%s: %s", repo, fpath, exc)
                # B1 — parse Module-Federation remote registries for remotes that
                # ship no internal <Route> (invisible to the static parsers above).
                if os.environ.get("ARTA_B1_MF_ROUTE_EXTRACT_DISABLE") != "1":
                    for fpath in mf_files:
                        try:
                            _mf_key = _r105_a_cache_key(repo, branch, "content", fpath)
                            _mf_cached = _r105_a_cache_get(_mf_key)
                            if _mf_cached:
                                mf_content = _mf_cached
                            else:
                                fresp = await client.get(
                                    f"https://api.github.com/repos/{repo}/contents/{fpath}",
                                    params={"ref": branch},
                                )
                                if fresp.status_code != 200:
                                    continue
                                mf_content = base64.b64decode(
                                    fresp.json().get("content", "")
                                ).decode("utf-8", errors="replace")
                                _r105_a_cache_set(_mf_key, mf_content)
                            for _rm in _r298_b1_extract_mf_remotes(mf_content):
                                _rm["repo"] = repo
                                _rm["file"] = fpath
                                mf_remotes.append(_rm)
                        except Exception as _mf_exc:
                            log.debug("B1: MF registry parse failed for %s/%s: %s",
                                      repo, fpath, _mf_exc)
            except Exception as exc:
                log.debug("Fix NNN: route extraction failed for %s: %s", repo, exc)

    # B1 — synthesize Module-Federation remote routes. The shell mounts each remote
    # via a catch-all `<Route path=".../:param">` (parsed above); the param resolves
    # (per the registry's `localRoute`) to a remote bundle. The remotes declare no
    # `<Route>` of their own, so substitute each localRoute token into the catch-all
    # param → a concrete, probe-navigable route (the shell's handler matches the URL
    # substring against localRoute). SUT-agnostic; killswitch handled at parse time.
    if mf_remotes and os.environ.get("ARTA_B1_MF_ROUTE_EXTRACT_DISABLE") != "1":
        _mf_new = _r298_b1_synthesize_mf_routes(routes, mf_remotes)
        if _mf_new:
            routes.extend(_mf_new)
            log.info("B1: synthesized %d Module-Federation remote route(s) from %d "
                     "remote(s): %s", len(_mf_new), len(mf_remotes),
                     ", ".join(r["route"] for r in _mf_new[:10]))

    log.info("Fix NNN: extracted %d frontend routes from %d repo(s)", len(routes), len(repos))
    return routes


# ── Fix SSS-1: Commit window listing for defect attribution ──────────────────

async def list_commits_in_window(
    repo: str,
    token: str,
    since: str,
    until: str,
    max_commits: int = 30,
) -> list[dict]:
    """Fix SSS-1: list commits in a [since, until) ISO-8601 window.

    Used by defect_intel to attribute PASS→FAIL transitions to the smallest
    set of commits that landed between the two run timestamps. Best-effort:
    rate-limit, auth, and transport failures degrade to an empty list — defect
    classification must not block on commit history.

    `repo` is `owner/name`. Returns a list of:
        {"sha": "...", "author": "...", "message": "...", "html_url": "..."}
    """
    import httpx
    if not repo or not token or not since or not until:
        return []
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    out: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/commits",
                params={"since": since, "until": until, "per_page": max_commits},
            )
            if resp.status_code != 200:
                log.debug("Fix SSS-1: GitHub commits %s returned %d", repo, resp.status_code)
                return []
            for c in (resp.json() or [])[:max_commits]:
                commit = c.get("commit", {}) or {}
                author = (commit.get("author") or {}).get("name") or (c.get("author") or {}).get("login") or ""
                message = (commit.get("message") or "").splitlines()[0][:160]
                out.append({
                    "sha": c.get("sha", ""),
                    "author": author,
                    "message": message,
                    "html_url": c.get("html_url", ""),
                })
    except Exception as exc:
        log.debug("Fix SSS-1: commit window query failed for %s: %s", repo, exc)
        return []
    return out


# ── R104.B — Agent-owned MCP helpers (accept persistent client) ──────────


async def extract_backend_routes_with_client(
    *,
    client,
    project: dict,
    gherkin_text: str = "",
    max_per_repo: int = 25,
) -> list[dict]:
    """R104.B — extract backend route declarations from the SUT's primary
    repos using an agent-owned MCP client (no per-call spawn).

    Searches repo files for common route-declaration patterns:
      - FastAPI:    `@app.{get,post,put,patch,delete,...}("/path"`
      - Express:    `app.{verb}("/path"`, `router.{verb}("/path"`
      - Django:     `path('/path', ...)`, `re_path('...', ...)`
      - Flask:      `@app.route("/path", methods=[...])`

    Returns list of `{method, path, file}` dicts. Cap to top-N by Gherkin
    keyword overlap (cheap relevance filter).

    Pre-R104.B this didn't exist — Newman + PW gen relied on
    captured_endpoints (runtime probe) which misses internal routes
    that haven't been called yet. Post-R104.B the LLM sees the SUT's
    code-level truth alongside captured_endpoints.

    Graceful: returns [] on any failure so the caller falls back to
    pre-R104 grounding paths.
    """
    if client is None:
        return []
    integrations = project.get("integrations") or {}
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    repos = integrations.get("repositories") or []
    if not repos:
        return []
    # Search patterns — keep loose so we catch many frameworks.
    patterns = [
        # FastAPI / Starlette: `@app.post("/...")`, `@router.get("/...")`
        (r'@(?:app|router)\.(get|post|put|patch|delete|head|options)\(\s*[\'"]([^\'"]+)[\'"]', "fastapi"),
        # Express / Hono: `app.post('/...')`, `router.get(...)`
        (r'(?:app|router)\.(get|post|put|patch|delete|head|options)\(\s*[\'"]([^\'"]+)[\'"]', "express"),
        # Flask: `@app.route('/...', methods=['POST'])` — extract path only; method optional
        (r'@(?:app|bp|blueprint)\.route\(\s*[\'"]([^\'"]+)[\'"]', "flask"),
    ]
    import re as _re_r104_b
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    # Limit to top 3 repos to bound runtime
    for repo in repos[:_gh_repo_cap()]:  # R196 — env-configurable (was 3)
        repo_full = repo.get("repo") if isinstance(repo, dict) else repo
        if not repo_full or "/" not in repo_full:
            continue
        owner, name = repo_full.split("/", 1)
        # Use search_code for each pattern. The MCP server's `search_code`
        # returns matches across the repo's default branch.
        for pat, _flavor in patterns:
            try:
                # Use a substring marker that's likely to hit in the target repo
                # (search_code wants a keyword query, not a regex). We search
                # for the prefix; client-side regex extracts (method, path).
                _query_keyword = (
                    "@app." if _flavor == "fastapi"
                    else "app.route(" if _flavor == "flask"
                    else "app.post("   # Express coarse-keyword
                )
                _search_result = await client.call_tool("search_code", {
                    "q": f"{_query_keyword} repo:{repo_full}",
                    "per_page": 30,
                })
            except Exception as exc:
                log.debug("R104.B: search_code(%s, %s) failed: %s", repo_full, _flavor, exc)
                continue
            if not _search_result:
                continue
            # The search result is a text blob with file paths + match contexts.
            # Apply our regex client-side to extract (method, path).
            for _match in _re_r104_b.finditer(pat, _search_result, _re_r104_b.IGNORECASE):
                _groups = _match.groups()
                if len(_groups) == 2:
                    _method, _path = _groups[0].upper(), _groups[1]
                else:   # Flask pattern only captures path
                    _method, _path = "GET", _groups[0]
                if not _path.startswith("/"):
                    continue
                key = (_method, _path)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "method": _method,
                    "path": _path,
                    "file": "",     # parsed from search result if needed
                    "_flavor": _flavor,
                })
            if len(out) >= max_per_repo * 3:   # hard ceiling across repos
                break
        if len(out) >= max_per_repo * 3:
            break

    # Relevance-filter by Gherkin keyword overlap (cheap heuristic).
    if gherkin_text and out:
        _gherkin_words = {
            w.lower() for w in _re_r104_b.findall(r"\w+", gherkin_text)
            if len(w) >= 4
        }
        def _score(r: dict) -> int:
            path_words = set(_re_r104_b.findall(r"\w+", r.get("path") or ""))
            return sum(1 for w in path_words if w.lower() in _gherkin_words)
        out.sort(key=_score, reverse=True)
    return out[:max_per_repo]


# ── R157 — SUT-agnostic auth-refresh endpoint discovery from source ──────────

def _gather_identifier_names(project: dict, env_block: dict | None = None) -> set[str]:
    """Collect the identifier names ARTA can FILL — the names a refresh route's
    path params are matched against. Two sources:
      1. the env-block's declared `variables` keys, and
      2. the expired session cookie's JWT claim keys (the dead token still
         names the identifiers needed to mint its successor).
    Always includes `refresh_token` (sourced from storage-state localStorage).
    """
    names: set[str] = set()
    eb = env_block or {}
    for k in (eb.get("variables") or {}):
        names.add(k)
    try:
        from .auth_refresher import (
            _find_storage_state_path,
            _read_storage_state,
            _decode_jwt_payload,
            _select_env_block,
        )
        env_name = None
        if env_block is None and isinstance(project, dict):
            env_name, eb2 = _select_env_block(project, None)
            for k in (eb2.get("variables") or {}):
                names.add(k)
        sp = _find_storage_state_path(env_name)
        if sp:
            storage = _read_storage_state(sp) or {}
            for c in storage.get("cookies") or []:
                if "token" in (c.get("name") or "").lower():
                    claims = _decode_jwt_payload(c.get("value") or "") or {}
                    names.update(str(k) for k in claims)
                    break
    except Exception as exc:  # pragma: no cover — best-effort
        log.debug("R157: identifier-name gather (claims) skipped: %s", exc)
    names.add("refresh_token")
    return names


async def _discover_refresh_routes_rest(
    project: dict,
    *,
    max_repos: int = 8,
    max_files_per_repo: int = 25,
) -> list[dict]:
    """REST-direct, BRANCH-AWARE scan for token-refresh route declarations.

    Walks each repo's git tree on its CONFIGURED branch (GitHub's code-search
    API only indexes the default branch — these SUT repos live on release
    branches — and the in-container MCP server is unreliable), fetches
    source files whose path hints at auth/refresh, and regexes route decls.
    Reuses the R105.A cache + the same httpx pattern as extract_frontend_routes.
    Returns `[{method, path, file}]`. Best-effort: [] on any failure.
    """
    import httpx
    import base64
    import re as _re

    integrations = project.get("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    token = integrations.get("github_token", "") or os.environ.get("GITHUB_TOKEN", "")
    repos = integrations.get("repositories", [])
    if not token or not repos:
        return []
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    route_pats = [
        _re.compile(r'@(?:app|router|api|auth_router|router_v1)\.(get|post|put|patch)\(\s*[\'"]([^\'"]+)[\'"]', _re.I),
        _re.compile(r'(?:app|router|api)\.(get|post|put|patch)\(\s*[\'"]([^\'"]+)[\'"]', _re.I),
        _re.compile(r'@(?:app|bp|blueprint)\.route\(\s*[\'"]([^\'"]+)[\'"]', _re.I),
    ]
    path_hint = _re.compile(r'(auth|refresh|token|login|session|oauth|account)', _re.I)
    src_ext = (".py", ".js", ".ts", ".go", ".java", ".rb")
    out: list[dict] = []
    seen: set[tuple] = set()
    async with httpx.AsyncClient(timeout=25, headers=headers) as client:
        for repo_entry in repos[:max_repos]:
            repo = repo_entry.get("repo", "") if isinstance(repo_entry, dict) else repo_entry
            branch = repo_entry.get("branch", "main") if isinstance(repo_entry, dict) else "main"
            if not repo:
                continue
            try:
                tkey = _r105_a_cache_key(repo, branch, "tree")
                tc = _r105_a_cache_get(tkey)
                if tc:
                    tree = json.loads(tc).get("tree", [])
                else:
                    r = await client.get(
                        f"https://api.github.com/repos/{repo}/git/trees/{branch}",
                        params={"recursive": "1"},
                    )
                    if r.status_code != 200:
                        log.debug("R157 REST: tree %s@%s -> %d", repo, branch, r.status_code)
                        continue
                    _r105_a_cache_set(tkey, r.text)
                    tree = r.json().get("tree", [])
            except Exception as e:
                log.debug("R157 REST: tree fetch %s failed: %s", repo, e)
                continue
            cands = [
                it["path"] for it in tree
                if it.get("type") == "blob"
                and it["path"].endswith(src_ext)
                and path_hint.search(it["path"])
                and "node_modules" not in it["path"]
                and "/test" not in it["path"].lower()
            ][:max_files_per_repo]
            for fp in cands:
                try:
                    fkey = _r105_a_cache_key(repo, branch, "content", fp)
                    content = _r105_a_cache_get(fkey)
                    if content is None:
                        fr = await client.get(
                            f"https://api.github.com/repos/{repo}/contents/{fp}",
                            params={"ref": branch},
                        )
                        if fr.status_code != 200:
                            continue
                        content = base64.b64decode(fr.json().get("content", "")).decode("utf-8", "replace")
                        _r105_a_cache_set(fkey, content)
                except Exception:
                    continue
                if "refresh" not in content.lower():
                    continue
                for pat in route_pats:
                    for m in pat.finditer(content):
                        g = m.groups()
                        method, path = (g[0].upper(), g[1]) if len(g) == 2 else ("GET", g[0])
                        if not path.startswith("/") or "refresh" not in path.lower():
                            continue
                        key = (method, path)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append({"method": method, "path": path, "file": f"{repo}:{fp}"})
    return out


async def discover_auth_refresh_config(
    project: dict,
    *,
    env_block: dict | None = None,
    client=None,
) -> dict | None:
    """R157 KEYSTONE — autonomously derive an `auth.refresh` config from the
    SUT's OWN backend source, eliminating per-project operator hand-wiring.

    Searches the project's repos for a token-refresh route declaration, then
    `protocol_discovery.derive_auth_refresh_config` templates its path params
    from the identifiers ARTA already knows (the expired cookie's JWT claims).
    The route's param NAMES match the claim names, so the connection is
    automatic — ARTA reads the SUT's code-level truth instead of guessing.

    `client`: an optional agent-owned MCP client (R104.B). When None, falls
    back to the module-level `search_code` helper. Returns the config or None
    (graceful — caller proceeds without auto-refresh).
    """
    from .protocol_discovery import derive_auth_refresh_config
    integrations = project.get("integrations") or {}
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    repos = integrations.get("repositories") or []
    token = integrations.get("github_token") or os.environ.get("GITHUB_TOKEN", "")
    if not repos or not token:
        log.info("R157: auth-refresh source discovery skipped (repos=%d, token=%s).",
                 len(repos), bool(token))
        return None

    ids = _gather_identifier_names(project, env_block)
    api_base = (env_block or {}).get("api_base_url") if env_block else None

    # PRIMARY: branch-aware REST tree-walk (works on non-default branches +
    # no MCP dependency). FALLBACK: MCP/search_code (default-branch only).
    routes: list[dict] = await _discover_refresh_routes_rest(project)
    if not routes:
        import re as _re
        patterns = [
            _re.compile(r'@(?:app|router)\.(get|post|put|patch)\(\s*[\'"]([^\'"]+)[\'"]', _re.I),
            _re.compile(r'(?:app|router)\.(get|post|put|patch)\(\s*[\'"]([^\'"]+)[\'"]', _re.I),
            _re.compile(r'@(?:app|bp|blueprint)\.route\(\s*[\'"]([^\'"]+)[\'"]', _re.I),
        ]
        seen: set[tuple] = set()
        for repo in repos[:_gh_repo_cap()]:  # R196 — env-configurable (was 5)
            repo_full = repo.get("repo") if isinstance(repo, dict) else repo
            if not repo_full or "/" not in repo_full:
                continue
            try:
                if client is not None:
                    blob = await client.call_tool("search_code", {
                        "q": f"refresh repo:{repo_full}", "per_page": 20,
                    })
                else:
                    blob = await search_code(project, "refresh", repo_full)
            except Exception as exc:
                log.debug("R157: search_code(%s) failed: %s", repo_full, exc)
                continue
            if not blob:
                continue
            for pat in patterns:
                for m in pat.finditer(blob):
                    g = m.groups()
                    method, path = (g[0].upper(), g[1]) if len(g) == 2 else ("GET", g[0])
                    if not path.startswith("/") or "refresh" not in path.lower():
                        continue
                    key = (method, path)
                    if key in seen:
                        continue
                    seen.add(key)
                    routes.append({"method": method, "path": path, "file": repo_full})

    cfg = derive_auth_refresh_config(routes, ids, api_base_url=api_base)
    if cfg:
        log.info(
            "R157: auto-derived auth.refresh from SUT source: %s %s "
            "(from %d candidate refresh route(s), %d identifiers)",
            cfg.get("method"), cfg.get("endpoint"), len(routes), len(ids),
        )
    else:
        log.info("R157: no fillable refresh route found in source "
                 "(%d candidate route(s) scanned).", len(routes))
    return cfg


# ── A2 (R218) — discover the per-endpoint AUTH CHAIN from the SUT's frontend
#    api-client source. SUT-agnostic: the auth is LEARNED from that SUT's own
#    client code (axios interceptors / api clients), never hand-coded. Replaces
#    constant stays as a safety-net fallback). ────────────────────────────────

# Path heuristics + scores for files most likely to construct per-endpoint auth.
_A2_FILE_SCORES = (
    ("interceptor", 10), ("axios", 9), ("apiclient", 9), ("httpclient", 8),
    ("authservice", 8), ("/auth", 6), ("api-client", 9), ("apiconfig", 6),
    ("/services/", 4), ("/api/", 4), ("token", 4), ("request", 3),
)
# Canonical token placeholders ARTA resolves at runtime (auth_chain tokens dict).
# `auth_token` (the default bearer/session token) is FIRST + SUT-agnostic — its
# omission (C3 root cause) forced the LLM to map a bearer SUT's token onto the
_A2_TOKEN_NAMES = ("auth_token", "session_token", "agent_api_token", "organization_id",
                   "cookie_value", "subscriber_id", "subscription_id",
                   "account_id", "schema_id")


def _a2_score_path(path: str) -> int:
    p = (path or "").lower()
    if "node_modules" in p or "/test" in p or ".spec." in p or ".test." in p:
        return -1
    if not p.endswith((".ts", ".tsx", ".js", ".jsx")):
        return -1
    return sum(w for tok, w in _A2_FILE_SCORES if tok in p)


async def _a2_fetch_auth_client_source(
    project: dict, *, max_files: int = 10, max_chars: int = 5000, max_repos: int = 5,
    scorer=None,
) -> list[dict]:
    """A2.1 — fetch frontend api-client source files (best-effort). Reuses the
    R105.A cache + the same GitHub REST tree/contents path as
    `extract_frontend_routes`. `scorer(path)->int` selects + ranks files (default
    `_a2_score_path` = auth/interceptor files; AN3.0 passes `_an_score_path` for
    the data-connection/dataset/upload files). Returns [{repo, file, content}]."""
    _score = scorer or _a2_score_path
    import base64
    import httpx
    integrations = project.get("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    token = integrations.get("github_token", "") or os.environ.get("GITHUB_TOKEN", "")
    repos = integrations.get("repositories", []) or []
    if not repos or not token:
        return []
    # Prioritise frontend/client/analytics repos (where the api-client lives).
    def _repo_rank(r):
        n = (str(r.get("repo", "")) + " " + str(r.get("name", ""))).lower()
        return -sum(k in n for k in ("web", "frontend", "app", "ui", "client", "analytics"))
    repos = sorted(repos, key=_repo_rank)[:max_repos]
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    candidates: list[tuple[int, str, str, str]] = []  # (score, repo, branch, path)
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        for repo_entry in repos:
            repo, branch = repo_entry.get("repo", ""), repo_entry.get("branch", "main")
            if not repo:
                continue
            try:
                _tk = _r105_a_cache_key(repo, branch, "tree")
                _tc = _r105_a_cache_get(_tk)
                if _tc:
                    tree = json.loads(_tc).get("tree", [])
                else:
                    resp = await client.get(
                        f"https://api.github.com/repos/{repo}/git/trees/{branch}",
                        params={"recursive": "1"})
                    if resp.status_code != 200:
                        continue
                    _r105_a_cache_set(_tk, resp.text)
                    tree = resp.json().get("tree", [])
            except Exception:
                continue
            for item in tree:
                if item.get("type") != "blob":
                    continue
                sc = _score(item.get("path", ""))
                if sc > 0:
                    candidates.append((sc, repo, branch, item["path"]))
        candidates.sort(key=lambda t: -t[0])
        snippets: list[dict] = []
        async with httpx.AsyncClient(timeout=20, headers=headers) as fc:
            for _sc, repo, branch, fpath in candidates[:max_files]:
                try:
                    _fk = _r105_a_cache_key(repo, branch, "content", fpath)
                    content = _r105_a_cache_get(_fk)
                    if content is None:
                        fr = await fc.get(
                            f"https://api.github.com/repos/{repo}/contents/{fpath}",
                            params={"ref": branch})
                        if fr.status_code != 200:
                            continue
                        content = base64.b64decode(
                            fr.json().get("content", "")).decode("utf-8", errors="replace")
                        _r105_a_cache_set(_fk, content)
                    snippets.append({"repo": repo, "file": fpath, "content": content[:max_chars]})
                except Exception:
                    continue
    return snippets


# ── Fix 2 (P8) — multi-language backend ROUTE extractor from GitHub source ──────
#    The runtime probe only captures endpoints it TRIGGERS; hallucinated paths 404
#    (136 PW + 61 Newman in run-43b4b8). No live extractor parses Java Spring /
#    invisible. Direct GitHub REST (no MCP); routes persisted into captured_endpoints
#    with source="github" (authoritative, R206 keep-rule). SUT-agnostic. Killswitch
#    ARTA_GH_ROUTE_EXTRACT_DISABLE.
def _is_backend_route_file(path: str) -> bool:
    p = (path or "").lower()
    if "/test" in p or "test/" in p or "/target/" in p or "node_modules" in p or ".spec." in p:
        return False
    if p.endswith(".java") or p.endswith(".cs"):
        return any(k in p for k in ("controller", "resource", "endpoint", "restapi", "/api/", "route"))
    if p.endswith(".service.ts"):
        return True
    return False


def _extract_routes_multilang(code: str, file_path: str) -> list[dict]:
    """Pure route extractor for Java Spring / .NET / Angular / Express. Returns
    [{method, path, source:'github', file}]. Composes class-level base paths with
    method-level sub-paths (Spring `@RequestMapping` + `@*Mapping`, .NET `[Route]` +
    `[Http*]`)."""
    import re as _r
    p = (file_path or "").lower()
    raw: list[tuple[str, str, str]] = []  # (verb, base, sub)

    if p.endswith(".java"):
        # class-level base = the @RequestMapping BEFORE the first method mapping
        _first_method = _r.search(
            r"@(?:Get|Post|Put|Delete|Patch)Mapping|@RequestMapping\([^)]*method\s*=", code)
        head = code[:_first_method.start()] if _first_method else code
        _bm = _r.search(r'@RequestMapping\(\s*(?:value\s*=\s*)?[\{]?\s*["\']([^"\']+)["\']', head)
        base = _bm.group(1) if _bm else ""
        for mm in _r.finditer(
                r'@(Get|Post|Put|Delete|Patch)Mapping\(\s*(?:value\s*=\s*|path\s*=\s*)?'
                r'[\{]?\s*["\']([^"\']*)["\']', code):
            raw.append((mm.group(1).upper(), base, mm.group(2)))
        # bare @GetMapping (no path arg) → base itself
        for mm in _r.finditer(r'@(Get|Post|Put|Delete|Patch)Mapping\s*(?:\(\s*\))?\s*(?:public|private|protected|\n)', code):
            raw.append((mm.group(1).upper(), base, ""))
        # @RequestMapping(method=RequestMethod.X, value="/y")
        for mm in _r.finditer(r'@RequestMapping\(([^)]*method\s*=[^)]*)\)', code):
            args = mm.group(1)
            vm = _r.search(r'method\s*=\s*(?:\{\s*)?RequestMethod\.(\w+)', args)
            pm = _r.search(r'(?:value|path)\s*=\s*[\{]?\s*["\']([^"\']+)["\']', args)
            if vm:
                raw.append((vm.group(1).upper(), base, pm.group(1) if pm else ""))
    elif p.endswith(".cs"):
        _bm = _r.search(r'\[Route\(\s*["\']([^"\']+)["\']', code)
        base = (_bm.group(1) if _bm else "").replace("[controller]", "").replace("[action]", "")
        for mm in _r.finditer(r'\[Http(Get|Post|Put|Delete|Patch)\(\s*["\']([^"\']*)["\']', code):
            raw.append((mm.group(1).upper(), base, mm.group(2)))
        for mm in _r.finditer(r'\[Http(Get|Post|Put|Delete|Patch)\]\s', code):
            raw.append((mm.group(1).upper(), base, ""))
    elif p.endswith(".service.ts"):
        for mm in _r.finditer(
                r'\.(get|post|put|delete|patch)\s*(?:<[^>]*>)?\s*\(\s*[`\'\"]([^`\'\"]*)[`\'\"]', code):
            url = mm.group(2)
            if "/" in url:
                raw.append((mm.group(1).upper(), "", url))
    else:  # Express / NestJS (loose)
        for mm in _r.finditer(
                r'(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*[`\'\"]([^`\'\"]+)[`\'\"]', code):
            raw.append((mm.group(1).upper(), "", mm.group(2)))

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for verb, base, sub in raw:
        path = (base or "") + ("/" + sub.lstrip("/") if sub else "")
        if not path:
            continue
        if path.startswith("http"):
            path = "/" + path.split("/", 3)[-1] if path.count("/") >= 3 else path
        if not path.startswith("/"):
            path = "/" + path
        path = _r.sub(r"/{2,}", "/", path).rstrip("/") or "/"
        # E1 (R263) — Angular base-URL cleanup. The old blanket `${...}→{id}`
        # rewrote EVERY interpolation, including `${this.baseUrl}`/`${apiUrl}`,
        # producing garbage like `/{id}/GetLeaseAssetsByOwnerAccountId/{id}`
        # where the leading `/{id}` is a mangled base-URL, not a path param.
        # Map base-URL-ish interpolations to empty; only genuine path params
        # become `{id}`. Conservative: anything ambiguous stays `{id}` (fails
        # toward the old behavior). Killswitch ARTA_GH_ANGULAR_BASEURL_FIX_DISABLE.
        if os.environ.get("ARTA_GH_ANGULAR_BASEURL_FIX_DISABLE") != "1":
            def _r263_sub(m):
                ident = m.group(1) or ""
                # a base-URL var: the FIRST path token, or an identifier naming
                # a host/root (base/api/url/host/env/endpoint).
                if _r.search(r"base|api|url|host|env|endpoint|root|server", ident, _r.I):
                    return ""
                return "{id}"
            # strip a leading `/${baseUrl}` → "" first (first-token base var)
            path = _r.sub(r"^/\$\{[^}]*(?:base|api|url|host|env|endpoint|root|server)[^}]*\}",
                          "", path, flags=_r.I)
            path = _r.sub(r"\$\{([^}]*)\}", _r263_sub, path)
            path = _r.sub(r"/{2,}", "/", path)
            if not path.startswith("/"):
                path = "/" + path
        else:
            path = _r.sub(r"\$\{[^}]+\}", "{id}", path)   # Angular ${x} → {id} (pre-E1)
        path = _r.sub(r":\w+", "{id}", path)           # Express :id → {id}
        if len(path) < 2 or not _r.match(r"^/[\w{}/.\-]+$", path):
            continue
        key = (verb, path.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"method": verb, "path": path, "source": "github", "file": file_path})
    return out


async def extract_backend_routes_from_source(
    project: dict, *, max_repos: int = 8, max_files_per_repo: int = 40, max_chars: int = 60000,
    cap: int = 400,
) -> list[dict]:
    """Fix 2 (P8) — fetch backend controller/service source from the project's repos
    and extract REAL routes (Java Spring / .NET / Angular / Express). SUT-agnostic,
    direct GitHub REST + R105.A cache. Returns [{method, path, source:'github', file}]
    (deduped, capped). Killswitch ARTA_GH_ROUTE_EXTRACT_DISABLE."""
    if os.environ.get("ARTA_GH_ROUTE_EXTRACT_DISABLE") == "1":
        return []
    import base64 as _b64
    import httpx
    integrations = project.get("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    token = integrations.get("github_token", "") or os.environ.get("GITHUB_TOKEN", "")
    repos = integrations.get("repositories", []) or []
    if not repos or not token:
        return []
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    all_routes: list[dict] = []
    seen: set[tuple[str, str]] = set()
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        for repo_entry in repos[:max_repos]:
            repo, branch = repo_entry.get("repo", ""), repo_entry.get("branch", "main")
            if not repo:
                continue
            try:
                _tk = _r105_a_cache_key(repo, branch, "tree")
                _tc = _r105_a_cache_get(_tk)
                if _tc:
                    tree = json.loads(_tc).get("tree", [])
                else:
                    resp = await client.get(
                        f"https://api.github.com/repos/{repo}/git/trees/{branch}",
                        params={"recursive": "1"})
                    if resp.status_code != 200:
                        continue
                    _r105_a_cache_set(_tk, resp.text)
                    tree = resp.json().get("tree", [])
            except Exception:
                continue
            _files = [it["path"] for it in tree
                      if it.get("type") == "blob" and _is_backend_route_file(it.get("path", ""))]
            for fpath in _files[:max_files_per_repo]:
                try:
                    _fk = _r105_a_cache_key(repo, branch, "content", fpath)
                    content = _r105_a_cache_get(_fk)
                    if content is None:
                        fr = await client.get(
                            f"https://api.github.com/repos/{repo}/contents/{fpath}",
                            params={"ref": branch})
                        if fr.status_code != 200:
                            continue
                        content = _b64.b64decode(
                            fr.json().get("content", "")).decode("utf-8", errors="replace")
                        _r105_a_cache_set(_fk, content)
                    for r in _extract_routes_multilang(content[:max_chars], fpath):
                        k = (r["method"], r["path"].lower())
                        if k not in seen:
                            seen.add(k)
                            all_routes.append(r)
                            if len(all_routes) >= cap:
                                break
                except Exception:
                    continue
                if len(all_routes) >= cap:
                    break
            if len(all_routes) >= cap:
                break
    log.info("Fix 2 (P8): extracted %d backend route(s) from %d repo(s) source "
             "(Java Spring / .NET / Angular / Express)", len(all_routes), min(len(repos), max_repos))
    return all_routes


def _a2_get_llm_client(injected=None):
    """Return ARTA's configured LLM client. ARTA runs on the **Claude Code CLI**
    (no Anthropic API key) — so build via the canonical `create_llm_client` factory
    mirroring `ARTA_LLM_PROVIDER` (default `claude_code` → `ClaudeCLIClient`, which
    exposes the Anthropic-SDK-compatible `.messages.create(...)`). Prefers an
    injected client (the caller's app.state client) when provided — single source
    of truth, no second client built."""
    if injected is not None:
        return injected
    from .llm_client import create_llm_client
    from ..models.llm_config import LLMConfig, LLMProvider
    prov = os.environ.get("ARTA_LLM_PROVIDER", "claude_code").lower()
    try:
        cfg = LLMConfig(provider=LLMProvider(prov),
                        model=(os.environ.get("ARTA_LLM_MODEL") or None))
    except Exception:
        cfg = LLMConfig(provider=LLMProvider.CLAUDE_CODE)
    return create_llm_client(cfg)


async def _a2_llm_extract(snippets: list[dict], *, client=None) -> dict | None:
    """A2.2 — ask the LLM (ARTA's configured provider — claude_code CLI by default)
    to extract per-endpoint auth-chain rules (+ host_map + analytics body manifest)
    from the fetched api-client source. Returns the raw extraction dict or None.
    Isolated so unit tests mock it with a recorded extraction (no LLM call)."""
    if not snippets:
        return None
    try:
        llm = _a2_get_llm_client(client)
    except Exception as exc:
        log.warning("A2.2: no LLM client available (%s)", exc)
        return None
    src = "\n\n".join(f"// FILE: {s['repo']}/{s['file']}\n{s['content']}" for s in snippets)[:24000]
    prompt = (
        "You are reverse-engineering a web app's API client to recover its PER-ENDPOINT auth.\n"
        "From the source below (axios interceptors / api clients), output STRICT JSON:\n"
        '{"chain":[{"match":"<url substring or /path-prefix>","scheme":"bearer|cookie|none",'
        '"value_template":"<template>","cookies":[{"name":"<cookie>","value_template":"<tmpl>"}],'
        '"host":"<family key or null>"}],"host_map":{"<family>":"<base-url>"},'
        '"body_manifest":{"<url substring>":["<body field names>"]},'
        '"mint_manifest":{"endpoint_template":"<route the client POSTs to MINT/create '
        'its per-session API/agent token, with {api_base}/{account_id}/{subscriber_id}/'
        '{subscription_id}/{schema_id} placeholders>","body_key":"<request-body field '
        'name carrying the existing admin token id>"},'
        '"refresh_manifest":{"endpoint_template":"<route the client GETs/POSTs to REFRESH '
        'its EXPIRED SESSION token/cookie using the refresh_token, with {api_base}/'
        '{subscription_id}/{provider}/{schema_id}/{user_id} placeholders + '
        '?refresh_token={refresh_token}>","method":"GET|POST","token_path":"<JSON path of the '
        'FRESH session token in the response, e.g. token or data.token>"}}\n\n'
        "RULES:\n"
        "- One chain rule per distinct auth treatment the client applies to a URL group.\n"
        "- SCHEME per group: use \"bearer\" when the client sends the token in an "
        "`Authorization: Bearer <...>` header; \"cookie\" when it sets a Cookie header / "
        "document.cookie; \"none\" for unauthenticated groups. Decide from THIS SUT's own "
        "code — do NOT assume cookie/session-token (many SUTs are pure bearer/Auth0).\n"
        "- value_template uses ONLY these canonical placeholders (map the code's token "
        f"variables to them): {', '.join('{%s}' % t for t in _A2_TOKEN_NAMES)}. "
        "The DEFAULT bearer/session token maps to {auth_token} — e.g. a call sending "
        '`Authorization: Bearer <accessToken>` -> scheme "bearer", value_template '
        '"{auth_token}". Map a DISTINCT service token to its own placeholder: '
        "`Bearer <agentApiToken.tokenId>` -> \"{agent_api_token}\"; a composite "
        '`svc_<org>_<session>` -> "svc_{organization_id}_{session_token}".\n'
        "- Include `cookies` ONLY when the client ALSO sets a cookie for that group.\n"
        "- `match` should be the most specific URL fragment that selects the group "
        "(e.g. \"/analytics\", \"/billing\"). Order does not matter.\n"
        "- body_manifest: for any analytics/query endpoint, list the REQUEST BODY field "
        "names the client sends (e.g. user_query, app_id, dataset_type, dataset_id, dataset_name).\n"
        "- mint_manifest: if the client MINTS a per-session API/agent token before calling "
        "analytics (e.g. a POST to a route ending `agent_user_token`/`create_*_token` that "
        "returns a token_id the client then sends as Bearer), give that route as "
        "`endpoint_template` (parameterize ids as the placeholders above) and the body field "
        "that carries the existing admin token as `body_key`. Omit/empty if no such mint exists.\n"
        "- refresh_manifest: the route the client calls to REFRESH its expired SESSION token/"
        "cookie with the stored refresh_token (e.g. `getRefreshToken`/`refreshAccessToken` → a "
        "route like `/refresh/{subscription_id}/{provider}/{schema_id}/{user_id}?refresh_token=…` "
        "that returns a FRESH session token written back to the cookie). Prefer the route that "
        "returns the SESSION token, NOT a third-party (Google) token refresh. Give "
        "`endpoint_template` + `method` + `token_path` (where the fresh token is in the response). "
        "Omit/empty if the client has no session-refresh flow.\n"
        "- Output ONLY the JSON object, no prose.\n\n"
        f"SOURCE:\n{src}"
    )
    try:
        _model = (os.environ.get("ARTA_A2_MODEL")
                  or getattr(llm, "model", None) or "claude-sonnet-4-6")
        msg = await llm.messages.create(
            model=_model, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(getattr(b, "text", "") for b in (getattr(msg, "content", None) or []))
        text = text.strip()
        if "```" in text:  # strip code fences if present
            text = text.split("```")[1].lstrip("json").strip() if text.count("```") >= 2 else text
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            return None
        return json.loads(text[start:end + 1])
    except Exception as exc:
        log.warning("A2.2: LLM auth-chain extraction failed: %s", exc)
        return None


async def discover_auth_chain_from_source(
    project: dict, *, env_block: dict | None = None, client=None,
) -> dict | None:
    """A2 (R218) KEYSTONE — learn the per-endpoint auth chain from the SUT's
    frontend api-client SOURCE (SUT-agnostic). Returns
    {chain:[rule-dicts], host_map:{...}, body_manifest:{...}} or None (graceful →
    caller falls back to the HAR-derived chain, then the hardcoded template).
    Killswitch ARTA_AUTH_CHAIN_SOURCE_DERIVE_DISABLE=1."""
    if os.environ.get("ARTA_AUTH_CHAIN_SOURCE_DERIVE_DISABLE") == "1":
        return None
    snippets = await _a2_fetch_auth_client_source(project)
    if not snippets:
        log.info("A2: no api-client source files found for auth-chain discovery")
        return None
    extracted = await _a2_llm_extract(snippets, client=client)
    if not isinstance(extracted, dict) or not extracted.get("chain"):
        return None
    # Validate via the canonical loader (drops malformed rules; preserves cookies).
    from .auth_chain import AuthChain
    chain_obj = AuthChain.from_config({"chain": extracted.get("chain")})
    if chain_obj.is_empty():
        log.info("A2: source extraction produced no valid auth rules")
        return None
    valid_rules = []
    for r in chain_obj.rules:
        d = {"match": r.match, "scheme": r.scheme, "value_template": r.value_template,
             "cookie_name": r.cookie_name}
        if r.host:
            d["host"] = r.host
        if r.cookies:
            d["cookies"] = r.cookies
        valid_rules.append(d)
    # Filter host_map to REAL URLs only. Frontend source typically references
    # service URLs via env vars (e.g. `process.env.REACT_APP_ANALYTICS_BASE_URL`),
    # so the LLM returns env-var NAMES, not literal hosts — installing those would
    # pollute the real host_map (the hook `.update()`s it). Keep only http(s)
    # entries; a rule whose `host` family then has no URL safely falls back to the
    # base_url at request time. (The chain RULES — the auth itself — are the win.)
    _hm = {k: v for k, v in (extracted.get("host_map") or {}).items()
           if isinstance(v, str) and v.startswith("http")}
    log.info("A2: discovered %d auth rule(s) + %d resolved host(s) from %d source "
             "file(s) (SUT-agnostic, learned from frontend api-client)",
             len(valid_rules), len(_hm), len(snippets))
    # A7.5 (R218) — the token-MINT route the SPA uses to obtain its (bound)
    # analytics Bearer. Keep only a well-formed manifest with a parameterized route;
    # consumed by api_discovery._a7_mint_bound_agent_token (SUT-agnostic mint).
    _mint = extracted.get("mint_manifest")
    if not (isinstance(_mint, dict) and isinstance(_mint.get("endpoint_template"), str)
            and "{" in _mint.get("endpoint_template", "")):
        _mint = {}
    # frontend's actual refresh call is ground truth; a backend source-grep finds the
    # WRONG route (the Google-token refresh). Keep only a parameterized route.
    _refresh = extracted.get("refresh_manifest")
    if not (isinstance(_refresh, dict) and isinstance(_refresh.get("endpoint_template"), str)
            and "{" in _refresh.get("endpoint_template", "")):
        _refresh = {}
    return {
        "chain": valid_rules,
        "host_map": _hm,
        "body_manifest": extracted.get("body_manifest") or {},
        "mint_manifest": _mint,
        "refresh_manifest": _refresh,
    }


# ── R244 (R218 sibling) — discover the SUT's LOGIN FLOW(s) from the frontend
#    authentication source, SUT-agnostically. The auth-flow reqs (login / session /
#    logout) need the SUT's REAL login REQUEST — endpoint + method + body-field
#    plus a {refreshToken,...} refresh). Pre-R244 the LLM GUESSED the login request
#    0/4). Learn it from the SUT's own login source instead of hand-coding. Mirrors
#    A2's fetch+LLM pattern; isolated `_login_llm_extract` so tests mock it.
_LOGIN_FILE_SCORES = (
    ("login", 10), ("signin", 9), ("sign-in", 9), ("authentication", 8),
    ("/auth", 6), ("authservice", 8), ("auth.service", 8), ("sso", 7),
    ("session", 4), ("logout", 4), ("credential", 5), ("/services/", 3),
)


def _login_score_path(path: str) -> int:
    p = (path or "").lower()
    if "node_modules" in p or ".spec." in p or ".test." in p or "__tests__" in p:
        return -1
    if not p.endswith((".ts", ".tsx", ".js", ".jsx")):
        return -1
    return sum(w for tok, w in _LOGIN_FILE_SCORES if tok in p)


async def _login_llm_extract(snippets: list[dict], *, client=None) -> dict | None:
    """R244.2 — ask ARTA's LLM to recover the SUT's LOGIN REQUEST contract(s) from
    the frontend auth source. Returns the raw extraction dict or None. Isolated so
    unit tests mock it with a recorded extraction (no LLM call)."""
    if not snippets:
        return None
    try:
        llm = _a2_get_llm_client(client)
    except Exception as exc:
        log.warning("R244.2: no LLM client available (%s)", exc)
        return None
    src = "\n\n".join(f"// FILE: {s['repo']}/{s['file']}\n{s['content']}" for s in snippets)[:24000]
    prompt = (
        "You are reverse-engineering a web app's AUTHENTICATION module to recover the\n"
        "EXACT login/session HTTP request(s) it sends, so an automated test can replay\n"
        "them without guessing. From the source below, output STRICT JSON:\n"
        '{"login_flows":[{"name":"<short id e.g. email-login|password-login|refresh|logout>",'
        '"purpose":"<one line>","method":"POST|GET|PUT",'
        '"endpoint":"<the REAL request path if resolvable; else the service/action pair the '
        'client abstraction uses, e.g. \\"userManagement:email-login\\">",'
        '"body_fields":{"<field name the client SENDS>":"<source: '
        'credential_email|credential_username|credential_password|refresh_token|literal:<value>|'
        'runtime:<expr>|unknown>"}}]}\n\n'
        "RULES:\n"
        "- Cover EVERY distinct auth request the module makes: the primary login, any\n"
        "  email/username pre-check, the password/credential submit, token refresh, logout.\n"
        "- Preserve the EXACT body field NAMES the client sends (e.g. userName vs username,\n"
        "  isFrontEnd, hostName, email, refreshToken) — a test must match them verbatim.\n"
        "- Tag each field's source: credential_* = filled from the tester's login creds;\n"
        "  literal:<v> = a constant the client hardcodes (e.g. isFrontEnd -> literal:true);\n"
        "  runtime:<expr> = a value read at runtime (e.g. hostName from a branding config ->\n"
        "  runtime:OPBrandingConfig.name); refresh_token = the stored refresh token.\n"
        "- endpoint: give the literal path if the code has one; if the client uses a\n"
        "  service/action abstraction (e.g. sendJsonRequest(\"userManagement\",\"email-login\")),\n"
        "  return \"<service>:<action>\" so the caller can resolve it against captured endpoints.\n"
        "- Output ONLY the JSON object, no prose.\n\n"
        f"SOURCE:\n{src}"
    )
    try:
        _model = (os.environ.get("ARTA_R244_MODEL")
                  or getattr(llm, "model", None) or "claude-sonnet-4-6")
        msg = await llm.messages.create(
            model=_model, max_tokens=1800,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(getattr(b, "text", "") for b in (getattr(msg, "content", None) or []))
        text = text.strip()
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip() if text.count("```") >= 2 else text
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            return None
        return json.loads(text[start:end + 1])
    except Exception as exc:
        log.warning("R244.2: LLM login-flow extraction failed: %s", exc)
        return None


async def discover_login_flow_from_source(
    project: dict, *, client=None,
) -> dict | None:
    """R244 (R218 sibling) KEYSTONE — learn the SUT's LOGIN REQUEST contract(s) from
    its frontend authentication SOURCE (SUT-agnostic). Returns
    {login_flows:[{name, purpose, method, endpoint, body_fields:{name:source}}]} or
    None (graceful → gen falls back to guessing, as pre-R244). Killswitch
    ARTA_R244_LOGIN_DISCOVER_DISABLE=1."""
    if os.environ.get("ARTA_R244_LOGIN_DISCOVER_DISABLE") == "1":
        return None
    snippets = await _a2_fetch_auth_client_source(project, scorer=_login_score_path)
    if not snippets:
        log.info("R244: no authentication source files found for login-flow discovery")
        return None
    extracted = await _login_llm_extract(snippets, client=client)
    if not isinstance(extracted, dict):
        return None
    flows = extracted.get("login_flows")
    if not isinstance(flows, list) or not flows:
        return None
    # Keep only well-formed flows (name + method + at least one body field OR endpoint).
    valid = []
    for f in flows:
        if not isinstance(f, dict):
            continue
        if not (f.get("name") and f.get("method")):
            continue
        if not (f.get("endpoint") or isinstance(f.get("body_fields"), dict)):
            continue
        valid.append({
            "name": str(f.get("name"))[:40],
            "purpose": str(f.get("purpose") or "")[:120],
            "method": str(f.get("method")).upper()[:6],
            "endpoint": str(f.get("endpoint") or "")[:160],
            "body_fields": {str(k)[:40]: str(v)[:60]
                            for k, v in (f.get("body_fields") or {}).items()
                            if isinstance(f.get("body_fields"), dict)},
        })
    if not valid:
        return None
    log.info("R244: discovered %d login flow(s) from %d auth source file(s) "
             "(SUT-agnostic, learned from frontend auth module)",
             len(valid), len(snippets))
    return {"login_flows": valid}


# ── AN3.0 (R218) — discover the SUT's data-INGESTION flow from the frontend
#    source, SUT-agnostically (the operator chose "discover, don't guess"). The
#    request shapes for file-upload / create-data-set are uncaptured; the SUT's
#    own data-connection/dataset client is the ground truth. ────────────────────

# Path heuristics for the data-connection / dataset / file-upload client files.
_AN_FILE_SCORES = (
    ("dataset", 10), ("filesconnection", 10), ("connection", 7), ("fileupload", 9),
    ("file-upload", 9), ("upload", 6), ("datasource", 7), ("ingest", 6),
    ("dataprocessing", 6), ("data-processing", 6), ("/services/", 3), ("/api/", 3),
    # A8 — the APP layer the analytics query is scoped to (often the same
    # user-management client as datasets, but score app-specific files too).
    ("create-app", 7), ("appservice", 6), ("app-service", 6), ("/apps", 5),
    ("workspace", 4),
)


def _an_score_path(path: str) -> int:
    p = (path or "").lower()
    if "node_modules" in p or "/test" in p or ".spec." in p or ".test." in p:
        return -1
    if not p.endswith((".ts", ".tsx", ".js", ".jsx")):
        return -1
    return sum(w for tok, w in _AN_FILE_SCORES if tok in p)


async def discover_ingestion_manifest(project: dict, *, client=None) -> dict | None:
    """AN3.0 (R218) — learn the SUT's dataset-INGESTION flow from its frontend
    data-connection/dataset client source (SUT-agnostic). Returns
    {steps:[{name, method, path, request_body, returns}], dataset_type_values}
    or None. `steps` is the ordered connect→upload→register→status→delete flow;
    `returns` names the field(s) a step yields (e.g. dataset_id). Mirrors A2's
    fetch+LLM pattern; isolated `_an_llm_extract` so tests mock it. Killswitch
    ARTA_AN_INGESTION_DISCOVER_DISABLE=1."""
    if os.environ.get("ARTA_AN_INGESTION_DISCOVER_DISABLE") == "1":
        return None
    snippets = await _a2_fetch_auth_client_source(project, scorer=_an_score_path)
    if not snippets:
        log.info("AN3.0: no data-connection/dataset source files found")
        return None
    extracted = await _an_llm_extract(snippets, client=client)
    if not isinstance(extracted, dict) or not extracted.get("steps"):
        return None
    _app = extracted.get("app_manifest") or {}
    log.info("AN3.0: discovered %d-step ingestion flow%s from %d source file(s) "
             "(SUT-agnostic, learned from frontend dataset client)",
             len(extracted["steps"]),
             " + APP layer (query app-scoped)" if _app.get("app_id_field") else "",
             len(snippets))
    return extracted


async def _an_llm_extract(snippets: list[dict], *, client=None) -> dict | None:
    """AN3.0 — LLM-extract the ingestion flow manifest from the dataset/connection
    source (ARTA's claude_code CLI provider). Isolated for unit-test mocking."""
    if not snippets:
        return None
    try:
        llm = _a2_get_llm_client(client)
    except Exception as exc:
        log.warning("AN3.0: no LLM client available (%s)", exc)
        return None
    src = "\n\n".join(f"// FILE: {s['repo']}/{s['file']}\n{s['content']}" for s in snippets)[:24000]
    prompt = (
        "You are reverse-engineering a web app's DATA-INGESTION client (how it uploads a "
        "file/dataset to the analytics backend, then queries it). From the source below, "
        "output STRICT JSON describing the ORDERED flow:\n"
        '{"steps":[{"name":"upload|create_dataset|register_connection|status|delete|...",'
        '"method":"POST|GET|PUT|DELETE","path":"<url path or fragment, e.g. '
        '.../data-processing/event/file-upload>","request_body":{"<field>":"<type/desc>"},'
        '"returns":["<field the response yields, e.g. dataset_id|file_id|connection_id>"]}],'
        '"dataset_type_values":["structured","document","mongo","files"],'
        '"app_manifest":{"list_endpoint":"<path/fragment the client GETs to LIST apps, '
        'e.g. .../get-list-of-apps>","create_endpoint":"<path it POSTs to CREATE an app>",'
        '"app_id_field":"<the response field that carries the app id, e.g. app_id|analytics_app_id>",'
        '"query_scoped_by_app":true}}\n\n'
        "RULES:\n"
        "- Order steps as the client actually calls them (e.g. file-upload -> create-data-set "
        "-> poll status -> [query] -> delete-data-set).\n"
        "- request_body: the field NAMES the client sends per step (name, dataset_type, "
        "file, connection_id, dataset_id, …).\n"
        "- returns: which field of each response the client reads + reuses downstream "
        "(esp. the dataset_id used by the query engine).\n"
        "- dataset_type_values: the allowed dataset_type strings the code uses.\n"
        "- app_manifest: the analytics query is often scoped to an APP. If the client "
        "lists/creates an 'app' (or analytics application/workspace) and sends its id "
        "(app_id) on the query, give the list/create endpoints + the app-id field name. "
        "Set query_scoped_by_app true iff the query body carries app_id. Omit/empty if "
        "the SUT has no app concept.\n"
        "- Output ONLY the JSON object, no prose.\n\n"
        f"SOURCE:\n{src}"
    )
    try:
        _model = (os.environ.get("ARTA_A2_MODEL")
                  or getattr(llm, "model", None) or "claude-sonnet-4-6")
        msg = await llm.messages.create(
            model=_model, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(getattr(b, "text", "") for b in (getattr(msg, "content", None) or [])).strip()
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip() if text.count("```") >= 2 else text
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            return None
        return json.loads(text[start:end + 1])
    except Exception as exc:
        log.warning("AN3.0: LLM ingestion-flow extraction failed: %s", exc)
        return None


# ── G1 (R218) — discover the FULL analytics WORKFLOW MANIFEST from the SUT source,
#    SUT-agnostically. This is the autonomy layer over analytics_manifest.py: instead
#    of a hand-authored default, ARTA reads any SUT's dataset/upload/query/monitoring
#    client + backend schemas and emits the manifest (the operator's directive:
#    "ARTA should go through the SUT code and understand — generic"). Mirrors A2. ─────
_AN_WORKFLOW_SCORES = (
    ("dataset", 10), ("analyticstool", 9), ("create-app", 8), ("createapp", 8),
    ("addintegrations", 8), ("fileupload", 8), ("file-upload", 8), ("upload", 6),
    ("datasource", 7), ("job-create", 10), ("jobcreate", 10), ("job_create", 10),
    ("monitoring", 6), ("query-engine", 7), ("querychat", 6), ("query_router", 9),
    ("queue_mapping", 9), ("api_schema", 6), ("user_managment", 7), ("user_management", 7),
    ("connection", 6), ("chatbot", 4), ("source", 4), ("/apps", 5),
)


def _an_workflow_score_path(path: str) -> int:
    """G1 file selector — like `_an_score_path` but ALSO accepts backend `.py`
    (routers/schemas carry the dataset_type→engine routing + the job-create body)."""
    p = (path or "").lower()
    if "node_modules" in p or "/test" in p or ".spec." in p or ".test." in p:
        return -1
    if not p.endswith((".ts", ".tsx", ".js", ".jsx", ".py")):
        return -1
    return sum(w for tok, w in _AN_WORKFLOW_SCORES if tok in p)


def _validate_workflow_manifest(extracted: dict) -> dict:
    """Keep only well-formed manifest pieces from the LLM extraction (drops env-var
    'hosts', modes without an id_prefix, endpoints without a real path). Returns a
    PARTIAL manifest that the runtime `analytics_manifest.load_manifest` deep-merges
    over the DEFAULT — so a good extraction refines the default, a poor one is inert."""
    out: dict = {}
    modes = {}
    for name, spec in (extracted.get("dataset_modes") or {}).items():
        if not (isinstance(spec, dict) and isinstance(spec.get("id_prefix"), str) and spec["id_prefix"]):
            continue
        m = {"id_prefix": spec["id_prefix"]}
        for k in ("dataset_type", "engine"):
            if isinstance(spec.get(k), str) and spec[k]:
                m[k] = spec[k]
        if isinstance(spec.get("is_excel"), bool):
            m["is_excel"] = spec["is_excel"]
        if isinstance(spec.get("verifies"), list):
            m["verifies"] = [v for v in spec["verifies"] if isinstance(v, str)]
        modes[str(name)] = m
    if modes:
        out["dataset_modes"] = modes
    eps = {}
    for eid, spec in (extracted.get("endpoints") or {}).items():
        if isinstance(spec, dict) and isinstance(spec.get("path"), str) and "/" in spec["path"]:
            e = {"path": spec["path"], "method": str(spec.get("method", "GET")).upper()}
            if isinstance(spec.get("host"), str):
                e["host"] = spec["host"]
            eps[str(eid)] = e
    if eps:
        out["endpoints"] = eps
    rr = {c: mode for c, mode in (extracted.get("routing_rules") or {}).items()
          if isinstance(mode, str) and mode in modes}
    if rr:
        out["routing_rules"] = rr
    # hosts: keep only literal http(s) bases (env-var NAMES → dropped, like A2 does).
    hosts = {}
    for hid, val in (extracted.get("hosts") or {}).items():
        base = val.get("base") if isinstance(val, dict) else val
        if isinstance(base, str) and base.startswith("http"):
            hosts[str(hid)] = {"default": base}
    if hosts:
        out["hosts"] = hosts
    return out


async def _an_workflow_llm_extract(snippets: list[dict], *, client=None) -> dict | None:
    """G1 — LLM-extract the analytics WORKFLOW MANIFEST (dataset modes + engines,
    endpoint templates, routing, hosts) from the SUT source. Isolated for test mocking."""
    if not snippets:
        return None
    try:
        llm = _a2_get_llm_client(client)
    except Exception as exc:
        log.warning("G1: no LLM client available (%s)", exc)
        return None
    src = "\n\n".join(f"// FILE: {s['repo']}/{s['file']}\n{s['content']}" for s in snippets)[:26000]
    prompt = (
        "You are reverse-engineering a web app + backend to recover its ANALYTICS WORKFLOW so a "
        "generic test tool can drive ANY dataset type. From the source below, output STRICT JSON:\n"
        '{"dataset_modes":{"<mode e.g. files|excel|mongo|source>":{"id_prefix":"<dataset_id prefix '
        'e.g. files_>","dataset_type":"<type string>","engine":"document_rag|tabular|other",'
        '"is_excel":true|false,"verifies":["count","aggregation","content"]}},'
        '"endpoints":{"<step id: presign_upload|register_file|create_dataset|trigger_job|'
        'dataset_status|create_app|query>":{"host":"backend|analytics|monitor","method":"POST|GET",'
        '"path":"<url path TEMPLATE with {account}/{subscriber}/{subscription}/{schema}/{project} '
        'placeholders>"}},"routing_rules":{"count":"<mode>","aggregation":"<mode>","content":"<mode>"},'
        '"hosts":{"backend":"<literal base url or env-var name>","analytics":"...","monitor":"..."}}\n\n'
        "RULES:\n"
        "- dataset_modes: the DISTINCT ways to make a queryable dataset (upload files → document "
        "RAG; upload excel/spreadsheet → tabular; connect a mongo/extract source → tabular). "
        "id_prefix drives dataset_type = dataset_id.split('_')[0]. engine = which engine ANSWERS it: "
        "'tabular' (row counts / sums / group-by on tables/spreadsheets) vs 'document_rag' (content "
        "Q&A over documents). is_excel true ONLY for the excel/spreadsheet mode. verifies = the "
        "assertion classes that engine can answer (tabular→count+aggregation+content; document_rag→content).\n"
        "- endpoints: the path TEMPLATE per step. trigger_job = the route that ENQUEUES an uploaded "
        "file for the ingest CONSUMER to parse/index (e.g. a 'monitoring/job-create'); without it "
        "the dataset never indexes. Look in backend routers (queue_mapping) for the engine routing.\n"
        "- routing_rules: which mode a count/aggregation check should use vs a content check.\n"
        "- hosts: literal http(s) base per host family if present, else the env-var name.\n"
        "- Output ONLY the JSON object, no prose.\n\n"
        f"SOURCE:\n{src}"
    )
    try:
        _model = (os.environ.get("ARTA_A2_MODEL")
                  or getattr(llm, "model", None) or "claude-sonnet-4-6")
        msg = await llm.messages.create(
            model=_model, max_tokens=2200,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(getattr(b, "text", "") for b in (getattr(msg, "content", None) or [])).strip()
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip() if text.count("```") >= 2 else text
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            return None
        return json.loads(text[start:end + 1])
    except Exception as exc:
        log.warning("G1: LLM workflow extraction failed: %s", exc)
        return None


async def discover_analytics_workflow_from_source(project: dict, *, client=None) -> dict | None:
    """G1 (R218) KEYSTONE — learn the FULL analytics WORKFLOW MANIFEST from the SUT
    source (SUT-agnostic): the dataset-creation MODES + their engine routing, the
    ingestion TRIGGER, endpoint templates, hosts. Returns a PARTIAL manifest dict
    (the runtime `analytics_manifest.load_manifest` deep-merges it over the DEFAULT via
    `ARTA_AN_WORKFLOW_MANIFEST` / `env_block.analytics`) or None (graceful → the
    source-grounded default stands). Mirrors A2/AN3.0. Killswitch
    ARTA_AN_WORKFLOW_DISCOVER_DISABLE=1."""
    if os.environ.get("ARTA_AN_WORKFLOW_DISCOVER_DISABLE") == "1":
        return None
    snippets = await _a2_fetch_auth_client_source(
        project, scorer=_an_workflow_score_path, max_files=14, max_repos=8)
    if not snippets:
        log.info("G1: no analytics-workflow source files found")
        return None
    extracted = await _an_workflow_llm_extract(snippets, client=client)
    if not isinstance(extracted, dict):
        return None
    manifest = _validate_workflow_manifest(extracted)
    if not manifest.get("dataset_modes"):
        log.info("G1: source extraction produced no valid dataset modes")
        return None
    log.info("G1: discovered analytics workflow manifest — %d mode(s), %d endpoint(s), "
             "%d routing rule(s), %d resolved host(s) from %d source file(s) (SUT-agnostic)",
             len(manifest.get("dataset_modes", {})), len(manifest.get("endpoints", {})),
             len(manifest.get("routing_rules", {})), len(manifest.get("hosts", {})), len(snippets))
    return manifest
