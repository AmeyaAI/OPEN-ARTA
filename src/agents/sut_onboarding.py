"""SUT Onboarding Agent (Phase G — Fix GGG).

Generic, project-agnostic onboarding for any system-under-test. Replaces
the project-specific hardcoded URL guesses scattered across Fix EEE
(token exchange) and Fix YY (schema bootstrap) with a single LLM-assisted
pipeline that:

1. Decodes the JWT and harvests UUID-shaped claims (account_id,
   subscription_id, etc.) into env vars.
2. Walks the OpenAPI spec for path-param dependencies.
3. Ingests Playwright trace.zip HAR (if available) for runtime-validated
   list endpoints.
4. Calls Haiku to disambiguate when multiple endpoints match.
5. Live-probes candidate endpoints with auth.
6. Surfaces unresolved fields with operator-friendly suggestions.

The output dict (`onboarding_config`) is persisted to
`project.integrations.onboarding_config`. Subsequent test generation +
execution read from it instead of guessing per-project.

For the operator UI: returns a `needs_operator` list with
`{field, reason, suggestion}` rows so the frontend can surface a form
with pre-filled defaults.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("arta.sut_onboarding")


# Common path-param fallback heuristics. Keys are param names that
# appear across many projects; values are sensible defaults that
# typically pass SUT validation as enum-shaped strings.
_DEFAULT_FALLBACKS: dict[str, list[str]] = {
    "entry_type": ["default", "string", "text", "json"],
    "data_type": ["string", "number", "boolean", "json"],
    "access_type": ["read", "write", "admin", "public"],
    "version": ["v1", "1", "latest"],
    "container_name": ["default"],
    "context": ["default", "primary"],
    "folder_path": ["/", "default"],
}


def _decode_jwt_payload(token: str) -> dict | None:
    """Best-effort JWT payload decode. Returns None on malformed input."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return None


def _harvest_uuid_claims(payload: dict) -> dict[str, str]:
    """Walk a JWT payload, harvest every UUID-shaped value with a key
    that ends in `_id` or matches well-known tenant fields. Returns
    a dict of {var_name: value} ready for env-var injection."""
    if not isinstance(payload, dict):
        return {}
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    harvested: dict[str, str] = {}

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and uuid_re.match(v):
                    if k.endswith("_id") or k in ("sub", "user", "account", "tenant", "organization"):
                        canon = k if k.endswith("_id") else f"{k}_id"
                        harvested.setdefault(canon, v)
                else:
                    _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(payload)
    return harvested


def _find_list_endpoint_candidates(
    endpoints: list[dict], param_name: str
) -> list[dict]:
    """For a path param `{X_id}`, return GET endpoints whose path
    plausibly lists those entities. Heuristics:
    - path ends with `/{X_id}` (drop tail; remainder is list URL)
    - path noun matches the param prefix (e.g. `schema_id` ↔ `/schemas`)
    """
    candidates = []
    noun_singular = param_name[:-3] if param_name.endswith("_id") else param_name
    noun_variants = {noun_singular, noun_singular + "s", noun_singular.replace("_", "/")}

    for ep in endpoints:
        if (ep.get("method") or "").upper() != "GET":
            continue
        path = (ep.get("path") or "").rstrip("/")
        if not path:
            continue
        # Pattern A: ends with /{param_name}
        if path.endswith("/{" + param_name + "}"):
            candidates.append({
                "path": path[: -len("/{" + param_name + "}")],
                "match": "trailing-id",
                "score": 0.9,
            })
        # Pattern B: ends with the resource noun
        last_segment = path.rsplit("/", 1)[-1].strip("{}")
        if last_segment in noun_variants and "{" not in last_segment:
            candidates.append({
                "path": path,
                "match": "noun-match",
                "score": 0.85,
            })
    # Deduplicate by path, keep highest score
    by_path: dict[str, dict] = {}
    for c in candidates:
        prior = by_path.get(c["path"])
        if not prior or c["score"] > prior["score"]:
            by_path[c["path"]] = c
    return sorted(by_path.values(), key=lambda x: -x["score"])


def _find_token_exchange_candidates(endpoints: list[dict]) -> list[dict]:
    """Find POST endpoints that look like token-exchange. Match on
    path/summary keywords: `auth`, `exchange`, `agent`, `token`."""
    keywords = ("exchange", "agent", "issue", "swap")  # avoid `token` alone (too noisy)
    out = []
    for ep in endpoints:
        if (ep.get("method") or "").upper() != "POST":
            continue
        path = (ep.get("path") or "").lower()
        summary = (ep.get("summary") or "").lower()
        if "auth" in path or "auth" in summary:
            if any(kw in path or kw in summary for kw in keywords):
                out.append({"path": ep.get("path"), "summary": ep.get("summary", "")})
    return out


def _infer_shape(value: Any, depth: int = 0) -> dict:
    """Phase B3: JSON-Schema-lite shape inference for chain extraction.

    Bounded depth (5) and child-count (20) — large nested payloads stay tractable.
    Used by `_ingest_har` to stamp request/response shapes onto each record so
    Phase C dataflow inference + Phase D chain-aware Newman generation can run
    without re-fetching the SUT.
    """
    if depth >= 5:
        return {"type": "any"}
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        _s: dict = {"type": "string"}
        # R313 — value-domain grounding: additively record ENUM-LIKE scalar values
        # (short, no whitespace: status/state/type/role enums) so the value-domain
        # extractor can union them across records into the field's real allowed set.
        # This is the root fix for fabricated enum assertions (the LLM guessed
        # `validStates=['RUNNING',...]` but the SUT returns 'registered'). Scoped to
        # enum-like tokens to avoid capturing free-text / PII / long ids. OPT-IN
        # (default OFF → byte-identical to the pre-R313 shape, so every existing
        # response_body_shape consumer is unaffected until a value-domain re-ingest
        # is explicitly requested): set ARTA_R313_VALUE_CAPTURE=1 to enable.
        import os as _os_r313
        if (_os_r313.environ.get("ARTA_R313_VALUE_CAPTURE") == "1"
                and value and len(value) <= 32
                and re.fullmatch(r"[A-Za-z0-9_.\-]+", value)):
            _s["value"] = value
        return _s
    if isinstance(value, list):
        items = _infer_shape(value[0], depth + 1) if value else {"type": "any"}
        return {"type": "array", "items": items, "len": len(value)}
    if isinstance(value, dict):
        # Cap child count so 200-key blobs don't blow up the shape catalog
        keys = list(value.keys())[:20]
        return {
            "type": "object",
            "properties": {k: _infer_shape(value[k], depth + 1) for k in keys},
            "extra_keys": max(0, len(value) - len(keys)),
        }
    return {"type": "any"}


def _parse_iso_or_none(text: str | None) -> str | None:
    """HAR entries use ISO 8601. Pass through if present, None otherwise."""
    if not text or not isinstance(text, str):
        return None
    return text


def _ingest_har(
    har_path: Path,
    *,
    project_settings: dict | None = None,
) -> list[dict]:
    """Parse a Playwright HAR file into ordered, shape-annotated records.

    Phase B3 extension to the original Phase G/H parser. Each record now
    carries:
      - sequence_index, started_at, duration_ms — preserves call ordering
        for Phase C chain extraction (the original loop dropped this).
      - path / query_params / headers — split out for downstream resolution.
      - request_body_shape / response_body_shape — JSON-Schema-lite snapshots
        used by Phase D Newman generation to emit accurate request bodies.
      - request_body_sample / response_body_sample — first concrete payload
        used by Phase C dataflow inference (which value provides which env var).
      - cookies / set_cookies — extracted for Phase C as a parallel data-flow
        lane (auth tokens often flow via Set-Cookie).

    Phase I1: HAR is loaded via `load_har_with_caps` (file-size cap) and
    redacted via `redact_har` BEFORE entries are returned. Callers never see
    raw token values; chain extraction works against shape-preserving
    sentinels like `<<REDACTED_HEADER_VALUE>>`. Per-project size + redaction
    overrides are read from `project_settings`.

    Phase I2: per-record body cap is enforced inside `load_har_with_caps`.
    Truncated bodies stay shape-inferable from their prefix.
    """
    if not har_path.is_file():
        return []
    if har_path.suffix != ".har":
        log.debug("HAR ingest skipped (not .har): %s", har_path)
        return []

    # Resolve caps + redaction patterns from project settings (Phase I7).
    try:
        from . import discovery_settings as ds
        from .har_redactor import (
            DEFAULT_MAX_BODY_BYTES,
            DEFAULT_MAX_FILE_BYTES,
            HARTooLarge,
            load_har_with_caps,
            redact_har,
        )
    except ImportError as exc:  # pragma: no cover — defensive against partial deploys
        log.warning("HAR ingest: redactor unavailable (%s); refusing to parse", exc)
        return []

    max_file = ds.har_max_size_bytes(project_settings) if project_settings else DEFAULT_MAX_FILE_BYTES
    max_body = ds.har_body_max_bytes(project_settings) if project_settings else DEFAULT_MAX_BODY_BYTES
    extra_pats = ds.redaction_extra_patterns(project_settings) if project_settings else []

    try:
        raw_har = load_har_with_caps(har_path, max_file_bytes=max_file, max_body_bytes=max_body)
    except HARTooLarge as exc:
        log.warning("HAR oversized: %s — orchestrator should retry with mode=minimal", exc)
        return []
    except Exception as exc:
        log.debug("HAR ingest skipped (%s): %s", har_path, exc)
        return []

    # Phase I1 — redact BEFORE we return anything to callers.
    redacted, hits = redact_har(raw_har, extra_patterns=extra_pats)
    if hits:
        log.info("HAR redaction: %s hits=%s", har_path.name, hits)

    entries = (redacted.get("log") or {}).get("entries") or []
    out: list[dict] = []
    for idx, e in enumerate(entries[:500]):
        req = e.get("request") or {}
        resp = e.get("response") or {}
        url = req.get("url", "") or ""

        # Path + query split — the parser in api_discovery does the same so
        # downstream consumers don't have to URL-parse twice.
        path = url
        query_params: list[dict] = req.get("queryString") or []
        if "?" in url:
            path = url.split("?", 1)[0]

        # Body sample: parse JSON when content-type is JSON-ish so shape
        # inference works. Non-JSON bodies are stored as text snippets.
        def _maybe_json(body_dict: dict) -> Any | None:
            text = (body_dict or {}).get("text")
            if not isinstance(text, str) or not text:
                return None
            mime = (body_dict.get("mimeType") or "").lower()
            if "json" in mime:
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    pass
            return None

        post_data = req.get("postData") or {}
        request_sample = _maybe_json(post_data)
        response_sample = _maybe_json(resp.get("content") or {})

        out.append({
            "sequence_index": idx,
            "method": (req.get("method") or "GET").upper(),
            "url": url,
            "path": path,
            "query_params": [
                {"name": q.get("name"), "value": q.get("value")}
                for q in query_params
                if isinstance(q, dict)
            ],
            "headers": [
                {"name": h.get("name"), "value": h.get("value")}
                for h in (req.get("headers") or [])
                if isinstance(h, dict)
            ],
            "cookies": [
                {"name": c.get("name"), "value": c.get("value")}
                for c in (req.get("cookies") or [])
                if isinstance(c, dict)
            ],
            "set_cookies": [
                {"name": c.get("name"), "value": c.get("value")}
                for c in (resp.get("cookies") or [])
                if isinstance(c, dict)
            ],
            "status": resp.get("status", 0),
            "content_type": (resp.get("content") or {}).get("mimeType", ""),
            "started_at": _parse_iso_or_none(e.get("startedDateTime")),
            "duration_ms": int(e.get("time") or 0),
            "request_body_shape": _infer_shape(request_sample) if request_sample is not None else None,
            "response_body_shape": _infer_shape(response_sample) if response_sample is not None else None,
            "request_body_sample": request_sample,
            "response_body_sample": response_sample,
        })
    return out


async def _live_probe(
    url: str, cookies: dict[str, str], headers: dict[str, str]
) -> dict | None:
    """GET the URL with auth. Return parsed JSON if 200 + array body,
    else None."""
    try:
        async with httpx.AsyncClient(
            timeout=5.0, verify=False, follow_redirects=True,
            cookies=cookies, headers=headers,
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            body = resp.json()
        items: list = []
        if isinstance(body, list):
            items = body
        elif isinstance(body, dict):
            for key in ("data", "items", "results", "records"):
                v = body.get(key)
                if isinstance(v, list) and v:
                    items = v
                    break
        if not items:
            return None
        first = items[0]
        if not isinstance(first, dict):
            return None
        for id_key in ("id", "_id", "uuid", "guid"):
            if first.get(id_key):
                return {"url": url, "first_id": str(first[id_key]), "raw_first": first}
        return {"url": url, "first_id": None, "raw_first": first}
    except Exception:
        return None


async def discover(
    project: dict,
    session_token: str | None = None,
    har_path: str | None = None,
    endpoints: list[dict] | None = None,
) -> dict:
    """Run the SUT Onboarding Agent end-to-end.

    Returns a dict matching the `onboarding_config` shape documented
    in the Phase G plan. Caller persists to
    `project.integrations.onboarding_config` and re-reads on next run.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    integrations = project.get("integrations", {}) or {}
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    base_url = integrations.get("base_url", "")
    # literal in the onboarding flow (which runs for EVERY new SUT). Config-first;
    # imposed on a new SUT.
    _auth_cookie_name = (integrations.get("auth_cookie_name")
                         or integrations.get("cookie_name") or "session-token")

    config: dict[str, Any] = {
        "version": 1,
        "discovered_at": now_iso,
        "auth": {
            "method": "cookie",  # default; updated below if exchange found
            "cookie_name": _auth_cookie_name,
            "auth_state_path": None,
        },
        "harvested_claims": {},
        "list_endpoints": {},
        "create_endpoints": {},
        "har_observations": [],
        "needs_operator": [],
    }

    # ── Step 1: JWT decode + claim harvest ─────────────────────────────
    if session_token:
        payload = _decode_jwt_payload(session_token)
        if payload:
            harvested = _harvest_uuid_claims(payload)
            config["harvested_claims"] = harvested
            log.info(
                "GGG: harvested %d UUID claims from JWT: %s",
                len(harvested), sorted(harvested.keys()),
            )
            # Auto-detect nested third_party_token for token-exchange flow.
            if payload.get("third_party_token") or payload.get("id_token"):
                config["auth"]["method"] = "exchange"

    # Endpoints input — caller may pass already-discovered endpoints
    # to avoid a re-fetch; otherwise fall back to project's stored
    # discovered_endpoints.
    if not endpoints:
        from .api_discovery import _load_captured_endpoints
        endpoints = _load_captured_endpoints(project.get("id", ""))

    # ── Step 2: token-exchange detection ───────────────────────────────
    cfg_url = integrations.get("token_exchange_endpoint")
    if cfg_url:
        config["auth"]["token_exchange_endpoint"] = cfg_url
        config["auth"]["method"] = "exchange"
    elif endpoints:
        candidates = _find_token_exchange_candidates(endpoints)
        if len(candidates) == 1:
            config["auth"]["token_exchange_endpoint"] = candidates[0]["path"]
            config["auth"]["method"] = "exchange"
            log.info("GGG: auto-detected token exchange endpoint: %s", candidates[0]["path"])
        elif len(candidates) > 1:
            config["needs_operator"].append({
                "field": "token_exchange_endpoint",
                "reason": "multiple_candidates",
                "candidates": [c["path"] for c in candidates],
                "suggestion": candidates[0]["path"],
            })

    # ── Step 3: HAR ingestion (best-effort) ────────────────────────────
    if har_path:
        observations = _ingest_har(Path(har_path))
        config["har_observations"] = observations[:50]
        if observations:
            log.info("GGG: ingested %d HAR entries", len(observations))

    # ── Step 4: list-endpoint discovery per path-param ─────────────────
    # Walk endpoints, collect every {x_id} param, find candidate list URLs.
    seen_params: set[str] = set()
    for ep in (endpoints or []):
        for m in re.finditer(r"\{([a-z_]+)\}", ep.get("path", "")):
            seen_params.add(m.group(1))

    project_env_vars = (project.get("environments", {}) or {}).get("staging", {}) or {}
    if hasattr(project_env_vars, "model_dump"):
        project_env_vars = project_env_vars.model_dump()
    existing_vars = project_env_vars.get("variables", {}) or {}

    for param in sorted(seen_params):
        # Skip already-resolved params.
        cur = existing_vars.get(param, "")
        if cur and cur != "REPLACE_ME" and not str(cur).startswith("__ARTA_UNSET"):
            continue
        # Skip params resolved via JWT.
        if param in config["harvested_claims"]:
            continue
        candidates = _find_list_endpoint_candidates(endpoints or [], param)
        if candidates:
            config["list_endpoints"][param] = {
                "candidates": [c["path"] for c in candidates[:5]],
                "best": candidates[0]["path"],
                "score": candidates[0]["score"],
            }
        elif param in _DEFAULT_FALLBACKS:
            config["needs_operator"].append({
                "field": param,
                "reason": "enum_fallback",
                "suggestion": _DEFAULT_FALLBACKS[param][0],
                "alternatives": _DEFAULT_FALLBACKS[param],
            })
        else:
            config["needs_operator"].append({
                "field": param,
                "reason": "no_list_endpoint",
                "suggestion": "REPLACE_ME",
            })

    # ── Step 5: live-probe candidate endpoints with auth ───────────────
    if session_token and base_url and config["list_endpoints"]:
        cookies = {_auth_cookie_name: session_token}   # R293 — per-SUT cookie name
        headers: dict[str, str] = {}
        # Build resolved-parents map from harvested claims + existing vars
        resolved = {**config["harvested_claims"]}
        for k, v in existing_vars.items():
            if v and v != "REPLACE_ME" and not str(v).startswith("__ARTA_UNSET"):
                resolved[k] = v

        def _substitute(p: str) -> str:
            out = p
            for k, v in resolved.items():
                out = out.replace("{" + k + "}", str(v))
            return out

        # Try backend.* prefix variant for sandbox-style URLs. A5 platform/SUT-
        # it is ADDITIVE (the original is still tried) so it never breaks another SUT,
        # ARTA_SANDBOX_BACKEND_HEURISTIC_DISABLE=1 (proper home = env_block.auth.host_map).
        import os as _os_a5
        base_variants = [base_url]
        if "//sandbox." in base_url and _os_a5.environ.get("ARTA_SANDBOX_BACKEND_HEURISTIC") == "1":
            base_variants.insert(0, base_url.replace("//sandbox.", "//backend.sandbox.", 1))
            log.warning("A2: trying opt-in host heuristic sandbox.*→backend.sandbox.* for "
                        "%s (deployment-specific guess; prefer onboarding_config host_map)",
                        base_url)

        for param, info in list(config["list_endpoints"].items()):
            for base in base_variants:
                test_url = base.rstrip("/") + _substitute(info["best"])
                if "{" in test_url.split("://", 1)[-1]:
                    # Parents not resolved; skip live probe.
                    info["probe_status"] = "unresolved_parents"
                    break
                result = await _live_probe(test_url, cookies, headers)
                if result and result.get("first_id"):
                    info["probed_url"] = result["url"]
                    info["probed_first_id"] = result["first_id"]
                    resolved[param] = result["first_id"]
                    log.info("GGG: live-probed %s=%s from %s", param, result["first_id"], result["url"])
                    break
            else:
                info["probe_status"] = "no_200"

    # ── Step 6: needs_operator finalisation ────────────────────────────
    # Anything still in list_endpoints without a probed_first_id stays
    # unresolved; surface to operator with the best candidate as
    # suggestion.
    for param, info in config["list_endpoints"].items():
        if not info.get("probed_first_id"):
            config["needs_operator"].append({
                "field": param,
                "reason": "probe_failed_or_unresolved_parents",
                "suggestion": info.get("best", ""),
                "candidates": info.get("candidates", []),
            })

    # ── Step 7 (Fix NNN): static frontend route + selector extraction ──
    # Best-effort: only runs when the project has GitHub repos configured.
    # The routes feed Playwright generation prompts so the LLM uses real
    # paths (and `data-testid`s) instead of guessing.
    try:
        from .github_context import extract_frontend_routes
        routes = await extract_frontend_routes(project)
        if routes:
            config["frontend_routes"] = routes
            log.info("GGG+NNN: persisted %d frontend routes into onboarding_config", len(routes))
    except Exception as exc:
        log.debug("Fix NNN: extract_frontend_routes skipped: %s", exc)

    log.info(
        "GGG: onboarding complete — %d harvested, %d list_endpoints, %d needs_operator",
        len(config["harvested_claims"]),
        len(config["list_endpoints"]),
        len(config["needs_operator"]),
    )
    return config
