"""R18a — Per-project OpenAPI spec cache with TTL.

Phase R18 motivation: investigation against a pilot SUT's swagger surfaced
that 175× Newman 415 errors come from **parameter placement**, not
Content-Type mismatches. Endpoints declare params as `in: query`,
but LLM-generated Newman items put them in the body. The SUT rejects
"JSON body when expecting query string" with 415.

This module is the data layer for the post-LLM Newman parameter
placement transformer (R18b) and validator (R18c). It:

  - Fetches the OpenAPI spec from canonical paths (best-effort).
  - Caches the parsed spec under `.arta/openapi/<project_id>.json`.
  - Provides `lookup_endpoint(spec, method, url_path)` that matches
    a Newman item's URL to an OpenAPI path entry, including templated
    paths (`/api/users/{id}` matches `/api/users/123`).

TTL: 24h. Refreshed lazily on next call after expiry. Operators can
force a refresh by deleting the cache file.

The `_try_openapi` helper in api_discovery.py already fetches a similar
list of canonical paths for endpoint extraction; this module persists
the FULL spec (not just endpoint metadata) because the placement
transformer needs `parameters[].in` per operation.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

_CACHE_DIR = Path(".arta/openapi")
_TTL_SEC = 86400  # 24h

# Mirrors _try_openapi() in api_discovery.py + a couple of extra paths.
CANONICAL_SWAGGER_PATHS = [
    "/swagger.json",
    "/openapi.json",
    "/v3/api-docs",
    "/api-docs",
    "/api/swagger.json",
    "/api/openapi.json",
    "/v1/openapi.json",
    "/docs/openapi.json",
]


async def fetch_openapi(api_base_url: str, project_id: str) -> dict | None:
    """Return the parsed OpenAPI dict for `project_id`, fetching + caching
    if absent or stale.

    Returns None when:
      - `api_base_url` is empty/None
      - No canonical path responds with a valid spec
      - Network error on every probe

    Best-effort — callers should treat None as "no signal, leave Newman
    unchanged" (the placement transformer is idempotent without spec).
    """
    if not api_base_url:
        return None
    cached = _read_cache(project_id)
    if cached is not None:
        return cached

    base = api_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, verify=False) as c:
        for path in CANONICAL_SWAGGER_PATHS:
            url = base + path
            try:
                r = await c.get(url)
            except Exception as exc:
                log.debug("R18a: %s probe failed: %s", url, exc)
                continue
            if r.status_code != 200:
                continue
            ctype = r.headers.get("content-type", "")
            if "json" not in ctype and not r.text.lstrip().startswith("{"):
                continue
            try:
                spec = r.json()
            except Exception:
                continue
            if not isinstance(spec, dict) or "paths" not in spec:
                continue
            _write_cache(project_id, spec)
            log.info(
                "R18a: cached OpenAPI spec for %s from %s (%d paths)",
                project_id, url, len(spec.get("paths") or {}),
            )
            return spec

    log.info("R18a: no OpenAPI spec found at %s for project %s", base, project_id)
    return None


def lookup_endpoint(spec: dict | None, method: str, url_path: str) -> dict | None:
    """Match a Newman item's URL to an OpenAPI operation entry.

    Handles two cases:
      1. Direct match — `/api/users/{id}` template == `/api/users/{id}` path.
      2. Templated match — concrete `/api/users/123` matches template
         `/api/users/{id}` when path-param placeholders align.

    Returns the operation dict (`{parameters: [...], requestBody: {...}, ...}`)
    or None when no match. Method is case-insensitive.
    """
    if not isinstance(spec, dict) or not url_path:
        return None
    method = (method or "").lower()
    if not method:
        return None
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return None

    # Direct match first (cheapest).
    direct = paths.get(url_path)
    if isinstance(direct, dict) and method in direct:
        op = direct.get(method)
        return op if isinstance(op, dict) else None

    # Templated match: walk all paths whose segment count matches.
    url_segments = [s for s in url_path.split("/") if s]
    for tmpl_path, ops in paths.items():
        if not isinstance(ops, dict) or method not in ops:
            continue
        op = ops.get(method)
        if not isinstance(op, dict):
            continue
        tmpl_segments = [s for s in tmpl_path.split("/") if s]
        if len(tmpl_segments) != len(url_segments):
            continue
        match = True
        for u, t in zip(url_segments, tmpl_segments):
            if t.startswith("{") and t.endswith("}"):
                continue  # path-param placeholder — accept anything
            if u != t:
                match = False
                break
        if match:
            return op
    return None


def get_parameter_placement(operation: dict | None) -> dict[str, str]:
    """Return `{param_name: in}` map for an OpenAPI operation.

    `in` is one of "query", "header", "path", "cookie", "body".
    OpenAPI 3.x uses `parameters[]` for query/header/path/cookie and
    `requestBody` for body. We collapse both into one map for the
    placement transformer's lookup.
    """
    out: dict[str, str] = {}
    if not isinstance(operation, dict):
        return out
    for p in operation.get("parameters") or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        loc = p.get("in")
        if isinstance(name, str) and isinstance(loc, str):
            out[name] = loc
    # requestBody implicitly = body. We don't enumerate body schema
    # properties — the LLM may legitimately put any of them in the body.
    return out


def get_request_content_type(operation: dict | None) -> str | None:
    """R86.2a — Return the canonical Content-Type for an OpenAPI operation's
    requestBody, or None if the op has no body.

    OpenAPI 3.x shape:
        operation.requestBody.content = {
            "application/json": { "schema": {...} },
            "multipart/form-data": { "schema": {...} },
            ...
        }

    Selection priority when multiple media types are declared:
      1. `application/json` (most common, safest)
      2. `multipart/form-data` (file uploads — Postman needs special body mode)
      3. `application/x-www-form-urlencoded`
      4. `text/plain`
      5. The first key in content (deterministic fallback).

    Returns None when:
      - operation isn't a dict
      - requestBody is missing OR `required=false` and content is empty
      - content is missing or not a dict

    Used by R86.2a (gen-time) to inject `Content-Type` headers + set the
    Postman body mode correctly. The SUT rejects POSTs without a Content-Type
    with 415 (Unsupported Media Type); having the OpenAPI spec tell us the
    right value lets us eliminate this class of test_gen_bug at the source.
    """
    if not isinstance(operation, dict):
        return None
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return None
    content = request_body.get("content")
    if not isinstance(content, dict) or not content:
        return None
    # Priority list
    _PRIORITY = (
        "application/json",
        "application/vnd.api+json",
        "multipart/form-data",
        "application/x-www-form-urlencoded",
        "text/plain",
    )
    for mt in _PRIORITY:
        if mt in content:
            return mt
    # Deterministic fallback: first key in insertion order
    first_key = next(iter(content.keys()), None)
    return first_key if isinstance(first_key, str) else None


def _cache_path(project_id: str) -> Path:
    return _CACHE_DIR / f"{project_id}.json"


def _read_cache(project_id: str) -> dict | None:
    p = _cache_path(project_id)
    if not p.is_file():
        return None
    try:
        if time.time() - p.stat().st_mtime > _TTL_SEC:
            return None
        spec = json.loads(p.read_text())
        # R330 P2b — a doc whose ops are ALL arta-merged stubs (persist_openapi_doc)
        # is NOT a fetched spec: it carries zero param constraints. Treating it as
        # fresh starved param grounding for the full TTL. All-stub → cache miss.
        paths = (spec or {}).get("paths")
        if isinstance(paths, dict) and paths:
            ops = [op for ms in paths.values() if isinstance(ms, dict)
                   for op in ms.values() if isinstance(op, dict)]
            if ops and all(op.get("x-arta-stub") or op.get("summary") == "arta-openapi-ingested"
                           for op in ops):
                return None
        return spec
    except Exception as exc:
        log.debug("R18a: cache read failed for %s: %s", project_id, exc)
        return None


def _write_cache(project_id: str, spec: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(project_id).write_text(json.dumps(spec))
    except Exception as exc:
        log.warning("R18a: cache write failed for %s: %s", project_id, exc)
