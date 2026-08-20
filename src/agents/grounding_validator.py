"""R42.1 — per-tool grounding validator.

The user's bar: ≥95% RAW pass rate per tool, zero skips. The single
biggest pass-rate lift is eliminating *hallucinated symbols* at gen
time — endpoints absent from the captured store, testids absent from
the DOM catalog, env vars never declared. Pre-R42.1 the LLM was free
to invent these; R37.3 / R29.0 caught a subset; the rest landed as
runtime 415s, 404s, timeouts, BLOCKED rows.

This module provides one entry point per tool:

    validate_newman_grounded(parsed, project_id, env_vars, captured_endpoints) -> Violations
    validate_playwright_grounded(content, project_id, dom_catalog) -> Violations
    validate_k6_grounded(content, env_vars) -> Violations

Each returns a `Violations` list (possibly empty). Callers (each
`automation_engineer._generate_<tool>`) treat a non-empty list as
"regenerate with these violations as hints" — up to 3 retries — and
stamp the test as `_grounding_failed=True` on persistent failure so
it's excluded from dispatch (NOT a skip; not part of the denominator).
"""
from __future__ import annotations

import ast
import builtins as _builtins
import json
import logging
import os         # R140.A — killswitch env var
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("arta.grounding")

_PLACEHOLDER_VALUES = {"REPLACE_ME", "REPLACE-ME", "***", "REDACTED", "TODO", ""}


@dataclass
class GroundingViolation:
    """One grounding error. Tool-specific kind + symbol + remediation hint."""
    tool: str
    kind: str            # e.g. "unknown_endpoint", "hallucinated_testid", "unset_env_var"
    symbol: str          # the offending symbol verbatim
    location: str        # item name or line number for operator context
    hint: str            # human-readable + LLM-friendly retry hint

    def __post_init__(self) -> None:
        # Tier-1 telemetry: violation KIND + tool only (closed enums — the
        # symbol/location/hint never leave the machine). Fail-silent.
        try:
            from ..telemetry import emit as _tel_emit
            from ..telemetry.schema import TIER1_EVENTS as _tel_ev
            _kinds = _tel_ev["validator.violation"]["violation_kind"]
            _tel_emit("validator.violation", {
                "violation_kind": self.kind if self.kind in _kinds else "other",
                "runtime": self.tool,
            })
        except Exception:
            pass


def _is_set(v: Any) -> bool:
    return isinstance(v, str) and v.strip() and v.strip() not in _PLACEHOLDER_VALUES


# ────────────────────────────────────────────────────────────────────────────
# Recipe↔test column consistency (T4 gen-time == R264 dispatch — single source)
# ────────────────────────────────────────────────────────────────────────────

_DATA_COL_RE = re.compile(r'data\[["\']([A-Za-z_]\w*)["\']\]')


def extract_asserted_columns(test_code: str) -> set[str]:
    """The fixture column names an analytics test READS via `data["col"]`. Used by both
    the gen-time guard (T4) and the dispatch guard (R264) so the two never drift."""
    return set(_DATA_COL_RE.findall(test_code or ""))


def columns_asserted_not_in_recipe(test_code: str, recipe_columns) -> list[str]:
    """T4/R264 SSoT — the sorted list of `data["col"]` names the test asserts on that the
    recipe does NOT produce. Fail-OPEN: returns [] when the recipe has no columns (can't
    judge) or the test references none. A non-empty result means the recipe + test
    diverged at gen time → the test would KeyError at runtime (live 12-vs-5 case)."""
    rcols = {
        (c.get("name") if isinstance(c, dict) else c)
        for c in (recipe_columns or [])
    }
    rcols.discard(None)
    if not rcols:
        return []
    refs = extract_asserted_columns(test_code)
    return sorted(refs - rcols)


# R74.4 — single source of truth lives in src.shared.env_var_patterns.
# Pre-R74.4 this predicate was duplicated between execution.py (R43)
# and grounding_validator.py (R72.2) with a "must stay in sync" comment.
# Re-export the canonical version so existing call sites still work.
from src.shared.env_var_patterns import is_r43_substitutable_name  # noqa: F401


# ────────────────────────────────────────────────────────────────────────────
# Newman
# ────────────────────────────────────────────────────────────────────────────

_VAR_REF_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")


def validate_newman_grounded(
    parsed: dict,
    *,
    project_id: str,
    env_vars: dict | None = None,
    captured_endpoints: list[dict] | None = None,
    openapi_spec: dict | None = None,
) -> list[GroundingViolation]:
    """Walk a parsed Newman collection, surface hallucinated symbols.

    Four checks (3 + R95.4):
      1. Every `{{var}}` reference (URL/header/body) must exist in
         project env_vars (filled or not — operator declares the set).
      2. Every endpoint URL's templated form must be in
         `captured_endpoints` (R37.3 strict-mode rule applied per-item
         here — the gen-time filter operates on textual endpoints, this
         operates post-LLM after the LLM may have re-emitted dropped
         endpoints).
      3. Every item must have at least one `pm.test()` block. Bare
         "send + don't assert" items are useless.
      4. R95.4 — Every JSON body field must appear in the OpenAPI
         `requestBody.content.application/json.schema.properties` for
         the resolved endpoint. Pre-R95.4 the LLM invented body fields
         (135 × HTTP 400 in run-2f077d). Skipped when `openapi_spec`
         is absent (cold-start project — no signal to enforce).

    Skipped silently when captured_endpoints is empty (cold-start
    project — no signal to enforce).
    """
    out: list[GroundingViolation] = []
    if not isinstance(parsed, dict):
        return out
    top_items = parsed.get("item") or []
    if not isinstance(top_items, list):
        return out
    # R305 — FLATTEN folder-nested items. Real Postman collections group requests
    # into folders (item[].item[]...); pre-R305 this loop only saw top-level items
    # and `continue`d past every folder (no `request`), so NONE of the grounding
    # checks (endpoint / var / assertion / G1 value / G2 shape) ran on a
    # folder-structured collection. Recurse to the leaf request items.
    def _r305_flatten(nodes, _depth=0):
        flat = []
        if not isinstance(nodes, list) or _depth > 8:
            return flat
        for n in nodes:
            if not isinstance(n, dict):
                continue
            if isinstance(n.get("item"), list):      # folder → recurse
                flat.extend(_r305_flatten(n["item"], _depth + 1))
            elif isinstance(n.get("request"), dict):  # leaf request
                flat.append(n)
        return flat
    items = _r305_flatten(top_items)

    declared_vars = set((env_vars or {}).keys())
    captured_keys: set[tuple[str, str]] = set()
    if captured_endpoints:
        for e in captured_endpoints:
            if isinstance(e, dict):
                m = (e.get("method") or "").upper()
                p = e.get("path") or ""
                if m and p:
                    captured_keys.add((m, p))

    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or "<unnamed>"
        request = item.get("request")
        if not isinstance(request, dict):
            continue

        # 1. Var refs
        url = request.get("url")
        url_str = ""
        if isinstance(url, str):
            url_str = url
        elif isinstance(url, dict):
            url_str = url.get("raw") or ""
        body = request.get("body")
        body_raw = body.get("raw") if isinstance(body, dict) else ""
        headers = request.get("header") or []
        header_blob = " ".join(
            (h.get("key", "") + "=" + str(h.get("value", "")))
            for h in headers if isinstance(h, dict)
        )
        for blob in (url_str, body_raw or "", header_blob):
            for match in _VAR_REF_RE.finditer(str(blob)):
                var = match.group(1)
                if var in ("baseUrl", "base_url"):
                    continue
                # R72.2 — vars matching R43 substitution patterns are
                # "implicitly declared" — ARTA produces synthetic values
                # at dispatch time. Don't penalise the LLM for using
                # `{{user_id}}` / `{{version}}` / `{{folder_path}}` /
                # `{{cookie_value}}` etc.
                if is_r43_substitutable_name(var):
                    continue
                if declared_vars and var not in declared_vars:
                    out.append(GroundingViolation(
                        tool="newman",
                        kind="undeclared_var",
                        symbol=var,
                        location=name,
                        hint=(
                            f"Item '{name}' references {{{{ {var} }}}} but the "
                            "project's env vars don't declare it AND it doesn't "
                            "match any R43 substitution pattern (`*_id`, "
                            "`*_name`, `*_count`, `*_path`, `*_type`, "
                            "`*_version`, `context|scope|tenant|namespace`, "
                            "`*cookie*`). Use only vars from: "
                            f"{sorted(declared_vars)[:8]}..."
                        ),
                    ))
                elif not declared_vars:
                    # R72.2 cold-start visibility — log what WOULD have
                    # been flagged if signal were present. Operators see
                    # which vars discovery needs to harvest.
                    log.warning(
                        "R72.2: newman cold-start grounding — would flag "
                        "undeclared var `%s` in item '%s' (discovery hasn't "
                        "harvested project env vars yet)",
                        var, name,
                    )

        # 2. Endpoint match (templated)
        method = (request.get("method") or "GET").upper()
        path = _extract_path_from_url(url) if url else ""
        _shape_ok = not (path and captured_keys) or _path_matches_captured(method, path, captured_keys)
        # 2b. G1 (R305) — path-parameter VALUE grounding. The shape check above
        # wildcards every `{param}` slot, so a wrong enum value (region=global on
        # a us-texas-1-only resource → 404) shape-matches and passes. Ground the
        # concrete VALUE against the captured value-set for enum-like slots. Skip
        # for negative tests (they deliberately use invalid values — R252.N).
        if (_shape_ok and path and captured_endpoints
                and not _R252_NEGATIVE_CTX_RE.search(name or "")):
            _pv = _r305_param_value_violation(method, path, captured_endpoints)
            if _pv is not None:
                _bad_val, _seg_i, _allowed = _pv
                out.append(GroundingViolation(
                    tool="newman",
                    kind="unknown_param_value",
                    symbol=f"{method} {path}",
                    location=name,
                    hint=(
                        f"Item '{name}' uses path-parameter value `{_bad_val}` "
                        f"(segment {_seg_i}) which the SUT was NEVER observed to "
                        f"serve for this resource — captured values are {_allowed}. "
                        f"This is a wrong enum/region value, not a new id (runtime 404).\n\n"
                        f"AFTER — use a captured value:\n"
                        f"  {path.replace('/' + _bad_val + '/', '/' + _allowed[0] + '/')}"
                    ),
                ))
        if path and captured_keys and not _path_matches_captured(method, path, captured_keys):
            # R111.F — surface VALID ALTERNATIVES at violation construction so the
            # LLM sees them on retry-1 (not deferred until format_violations_as_hint
            # is called downstream). Mirrors R93.B + R104.B alternatives idiom.
            _alt_prefix_parts = [s for s in path.split("/") if s][:2]
            _alt_prefix = "/" + "/".join(_alt_prefix_parts) if _alt_prefix_parts else ""
            _captured_alts = [
                f"{m} {p}" for (m, p) in captured_keys
                if m == method and (not _alt_prefix or p.startswith(_alt_prefix))
            ][:5]
            _first_alt = _captured_alts[0].split(" ", 1)[1] if _captured_alts else path
            out.append(GroundingViolation(
                tool="newman",
                kind="unknown_endpoint",
                symbol=f"{method} {path}",
                location=name,
                hint=(
                    f"Item '{name}' targets `{method} {path}` which is NOT in "
                    f"the SUT's captured-endpoint store (R45.3 runtime discovery).\n\n"
                    f"BEFORE (BROKEN — runtime 404):\n"
                    f"  \"url\": {{\"raw\": \"{{{{base_url}}}}{path}\", ...}}\n\n"
                    f"AFTER — use a captured endpoint:\n"
                    f"  // Captured {method} endpoints under {_alt_prefix or '/'}: {_captured_alts}\n"
                    f"  \"url\": {{\"raw\": \"{{{{base_url}}}}{_first_alt}\", ...}}\n\n"
                    f"If no captured path matches the scenario, the SUT likely "
                    f"lacks this API. Use the gherkin to drive a different "
                    f"scenario OR mark the requirement as no-API-surface (R97.C)."
                ),
            ))

        # 3. Has pm.test()
        events = item.get("event") or []
        test_event = next(
            (e for e in events
             if isinstance(e, dict) and (e.get("listen") == "test")),
            None,
        )
        script_lines = []
        if isinstance(test_event, dict):
            script = test_event.get("script") or {}
            exec_block = script.get("exec") if isinstance(script, dict) else None
            if isinstance(exec_block, list):
                script_lines = [str(l) for l in exec_block]
            elif isinstance(exec_block, str):
                script_lines = [exec_block]
        if not any("pm.test" in l for l in script_lines):
            out.append(GroundingViolation(
                tool="newman",
                kind="no_assertion",
                symbol=name,
                location=name,
                hint=(
                    f"Item '{name}' has no pm.test() block. Every Newman "
                    "item must assert at least one expectation."
                ),
            ))

        # 3b. G2 (R305) — response-shape assertion grounding. The SUT's captured
        # response for this endpoint is a WRAPPER OBJECT ({servers:[...]}), but the
        # item asserts the TOP-LEVEL body is an array → `expected {servers:[...]} to
        # be an array` (3 of the 16 gen bugs). Deterministic backstop to the Pass-2
        # shape prompt. Killswitch ARTA_R305_SHAPE_GROUNDING_DISABLE.
        if (script_lines and captured_endpoints and path
                and os.environ.get("ARTA_R305_SHAPE_GROUNDING_DISABLE") != "1"
                and _r305_response_root(method, path, captured_endpoints) == "object"):
            _script_blob = "\n".join(script_lines)
            if _R305_BARE_ARRAY_ASSERT_RE.search(_script_blob):
                out.append(GroundingViolation(
                    tool="newman",
                    kind="response_shape_mismatch",
                    symbol=f"{method} {path}",
                    location=name,
                    hint=(
                        f"Item '{name}' asserts the TOP-LEVEL response is an array, "
                        f"but the SUT's captured response for `{method} {path}` is a "
                        f"WRAPPER OBJECT. Assert the list under its key instead:\n"
                        f"  const body = pm.response.json();\n"
                        f"  pm.expect(body.<listKey>).to.be.an('array');  // e.g. body.servers\n"
                        f"NOT pm.expect(body).to.be.an('array')."
                    ),
                ))

        # 3c. G2 (R305) — response FIELD grounding. A `to.have.property('<field>')`
        # existence assertion for a field the SUT's captured response does NOT
        # contain (e.g. asserting `name` when the object has `displayName`). Only
        # fires against a COMPLETE captured object shape (>= 8 keys) so a sparse
        # sample never false-flags. Non-destructive: reports so R57.1 retry feeds
        # the real field names. Killswitch ARTA_R305_SHAPE_GROUNDING_DISABLE.
        if (script_lines and captured_endpoints and path
                and os.environ.get("ARTA_R305_SHAPE_GROUNDING_DISABLE") != "1"):
            _keys = _r305_response_keys(method, path, captured_endpoints)
            if _keys:
                _blob3c = "\n".join(script_lines)
                _bad_fields = sorted({
                    f for f in _R305_PROPERTY_ASSERT_RE.findall(_blob3c)
                    if f not in _keys
                })
                for _bf in _bad_fields:
                    out.append(GroundingViolation(
                        tool="newman",
                        kind="response_shape_mismatch",
                        symbol=f"{method} {path}",
                        location=name,
                        hint=(
                            f"Item '{name}' asserts the response has property "
                            f"`{_bf}`, but the SUT's captured response for "
                            f"`{method} {path}` has no such field. Real fields: "
                            f"{sorted(_keys)[:12]}. Use one of those (e.g. a rename "
                            f"like `displayName` instead of `name`)."
                        ),
                    ))

        # 4. R95.4 — body fields must exist in OpenAPI requestBody schema
        # for the resolved endpoint. Catches LLM-invented body field names
        # (135 × HTTP 400 in run-2f077d).
        #
        # R143.C — extended to ALSO check captured response_body_shape
        # as fallback when OpenAPI is absent OR declares no properties.
        # Live target: Iter 2 (run-f80567) had 61 × HTTP 500 on
        # at ARTA-discovery time). The captured shape is the only signal
        # available; R143.C uses conservative thresholds (≥5 shape keys
        # + ≥50% body fields unknown) to avoid false positives.
        if path and body_raw:
            try:
                _r95_4_body_violations = _r95_4_validate_body_fields(
                    name=name,
                    method=method,
                    path=path,
                    body_raw=body_raw,
                    openapi_spec=openapi_spec,
                    captured_endpoints=captured_endpoints,  # R143.C
                )
                out.extend(_r95_4_body_violations)
            except Exception as _r95_4_exc:
                log.debug(
                    "R95.4: body-field grounding skipped for '%s': %s",
                    name, _r95_4_exc,
                )

        # 5. R111.G — assertion fields must exist in OpenAPI response schema.
        # Live evidence (run-99dbcf): 363 × Newman 200-marked-FAIL because the
        # LLM-generated `pm.test` cited fields not in the SUT response shape
        # (e.g. `json.insight.metric` when the real response is `{data: {...}}`).
        # Pre-R111.G these FAILs flowed to R34.1 as `sut_regression` (HTTP was
        # 200 but assertion failed → defect classifier doesn't know it's an
        # ARTA-side gen bug). R111.G surfaces them at gen-time so the LLM can
        # correct on retry + R102.C BLOCKs the residuals at dispatch.
        if openapi_spec and path and script_lines:
            try:
                _r111_g_violations = _r111_g_validate_assertion_fields(
                    name=name,
                    method=method,
                    path=path,
                    script_lines=script_lines,
                    openapi_spec=openapi_spec,
                )
                out.extend(_r111_g_violations)
            except Exception as _r111_g_exc:
                log.debug(
                    "R111.G: assertion-field grounding skipped for '%s': %s",
                    name, _r111_g_exc,
                )

    return out


def _r113_k_extract_schema_properties(resp_obj: dict, openapi_spec: dict) -> set[str]:
    """R113.K helper — extract top-level property names from one response
    object's JSON schema. Resolves `$ref` one level. Returns empty set
    when schema is absent / lacks properties / is additionalProperties.
    """
    if not isinstance(resp_obj, dict):
        return set()
    content = resp_obj.get("content") or {}
    json_schema = (
        content.get("application/json") or content.get("application/*+json") or {}
    ).get("schema") or {}
    if not isinstance(json_schema, dict):
        return set()
    if "$ref" in json_schema:
        ref = json_schema["$ref"]
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            schema_name = ref.rsplit("/", 1)[-1]
            components = openapi_spec.get("components") or {}
            schemas = components.get("schemas") or {}
            json_schema = schemas.get(schema_name) or json_schema
    properties = json_schema.get("properties") or {}
    if not isinstance(properties, dict) or not properties:
        return set()
    return set(properties.keys())


def _r150_g_extract_nested_paths(
    resp_obj: dict, openapi_spec: dict, max_depth: int = 4,
) -> set[str]:
    """R150.G — recursive variant of _r113_k_extract_schema_properties.
    Returns set of dotted paths reachable in the schema (e.g.,
    {'insight', 'insight.metric', 'insight.org_id', 'meta.total_count'}).

    Iter 9 evidence (run-8b552c): 38 × Newman HTTP-200-marked-FAIL
    crashed with `Cannot read properties of undefined (reading 'metric')`
    on assertions like `json.insight.metric`. R111.G's top-level-only
    check accepted `insight` as valid (it's a top-level prop) but the
    nested `.metric` access wasn't grounded against `insight`'s sub-schema.
    R150.G adds full path traversal so `insight.metric` is validated end-
    to-end.

    Resolves `$ref` recursively (bounded to 5 levels to avoid cycles).
    Descends into `array.items` schemas as well. Max depth bounded at 4
    to prevent unbounded traversal on deeply-nested SUT schemas.

    Returns empty set when schema absent. Caller composes nested_paths
    via union across all response codes (2xx + 4xx + 5xx) per R113.K
    pattern. Killswitch: `ARTA_R150_G_NESTED_PATH_DISABLE=1` at the
    caller skips the helper entirely.
    """
    paths: set[str] = set()
    if not isinstance(resp_obj, dict):
        return paths
    content = resp_obj.get("content") or {}
    json_schema = (
        content.get("application/json") or content.get("application/*+json") or {}
    ).get("schema") or {}
    if not isinstance(json_schema, dict):
        return paths

    components = (openapi_spec.get("components") or {}) if isinstance(openapi_spec, dict) else {}
    components_schemas = components.get("schemas") or {} if isinstance(components, dict) else {}

    def _resolve_ref(schema: dict, ref_depth: int = 0) -> dict:
        if ref_depth > 5 or not isinstance(schema, dict):
            return schema
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[-1]
            target = components_schemas.get(name)
            if isinstance(target, dict):
                return _resolve_ref(target, ref_depth + 1)
        return schema

    def _walk(schema: dict, prefix: str = "", depth: int = 0) -> None:
        if depth > max_depth or not isinstance(schema, dict):
            return
        schema = _resolve_ref(schema)
        if not isinstance(schema, dict):
            return
        # Array: descend into items with same prefix (since pm.X[0].Y == pm.X.Y in access regex)
        items = schema.get("items")
        if isinstance(items, dict):
            _walk(items, prefix, depth + 1)
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict):
            return
        for prop_name, prop_schema in properties.items():
            if not isinstance(prop_name, str):
                continue
            full = f"{prefix}.{prop_name}" if prefix else prop_name
            paths.add(full)
            if isinstance(prop_schema, dict):
                _walk(prop_schema, full, depth + 1)

    _walk(json_schema)
    return paths


def validate_newman_endpoint_shape(
    parsed: dict,
    *,
    captured_endpoints: list[dict] | None = None,
) -> list[GroundingViolation]:
    """R115.A.2 — Newman endpoint-shape validator.

    Catches 3 distinct LLM-hallucinated path patterns that aren't
    surfaced by `validate_newman_grounded`'s capture-set check (since
    a hallucinated path may not even be wrong-prefix; it's wrong-shape):

    1. **Static-asset paths** — `/static/`, `*.js`, `*.css`, `*.png`,
       `/_next/`, `/assets/` patterns are NEVER valid API endpoints.
       Live evidence (run-8da91d): `/static/js/bundle.js` emitted.

    2. **Frontend-route patterns** — well-known SPA route shapes that
       map to UI pages, not API endpoints. Examples: `/login`,
       `/signup`, `/settings`, `/dashboard`, `/profile` when NOT
       prefixed with `/api/`. Live evidence: `/settings` emitted.

    3. **Likely typos** — small edit distance (1-2 chars) between
       generated path segment + nearest captured_endpoints segment.
       Example: `/organizationss` (extra 's') vs captured `/organization`.
       Live evidence: `/.../organizationss` emitted.

    Each match emits `GroundingViolation(kind="endpoint_shape_mismatch",
    symbol=<path>, hint=<BEFORE/AFTER with nearest valid path>)`.

    Mission: -200 of the 238 × 404 cluster prevented at gen time
    (R115.A.2 mission contract). Pillar 1.
    """
    out: list[GroundingViolation] = []
    if not isinstance(parsed, dict):
        return out
    items = parsed.get("item") or []
    if not isinstance(items, list):
        return out

    # Pattern 1: static-asset regex
    _static_asset_re = re.compile(
        r"^/?(?:static/|_next/|assets/|public/)|\.(?:js|css|png|jpg|jpeg|gif|svg|ico|woff2?|ttf|map)(?:[?#]|$)",
        re.IGNORECASE,
    )

    # Pattern 2: SPA frontend-route shapes (UI pages, not API endpoints)
    _FRONTEND_ROUTES = {
        "/login", "/signup", "/signin", "/register",
        "/logout", "/forgot", "/reset-password",
        "/settings", "/profile", "/account",
        "/dashboard", "/home", "/welcome",
    }

    # Build captured endpoint segments for typo detection
    captured_segs: dict[str, str] = {}  # last-segment → full path
    if captured_endpoints:
        for ep in captured_endpoints:
            if not isinstance(ep, dict):
                continue
            ep_path = ep.get("path") or ""
            if not ep_path:
                continue
            # Index by last non-empty segment
            segs = [s for s in ep_path.split("/") if s and "{" not in s]
            if segs:
                captured_segs.setdefault(segs[-1].lower(), ep_path)

    def _walk(items_list: list) -> None:
        if not isinstance(items_list, list):
            return
        for item in items_list:
            if not isinstance(item, dict):
                continue
            sub = item.get("item")
            if isinstance(sub, list):
                _walk(sub)
                continue
            request = item.get("request")
            if not isinstance(request, dict):
                continue
            # R115.A.2 — extract method for frontend-route gating. POST /login
            # is a legitimate API auth call; GET /login is a frontend route.
            method = (request.get("method") or "GET").upper()
            url = request.get("url")
            url_path = ""
            if isinstance(url, str):
                # Strip protocol+host if present
                if "://" in url:
                    _split = url.split("://", 1)[1]
                    url_path = "/" + _split.split("/", 1)[1] if "/" in _split else "/"
                else:
                    url_path = url
            elif isinstance(url, dict):
                raw = url.get("raw") or ""
                if "://" in raw:
                    _split = raw.split("://", 1)[1]
                    url_path = "/" + _split.split("/", 1)[1] if "/" in _split else "/"
                else:
                    url_path = raw
            if not url_path:
                continue
            # Strip query/fragment + {{base_url}}
            url_path = url_path.split("?")[0].split("#")[0]
            url_path = url_path.replace("{{base_url}}", "").strip()
            if not url_path.startswith("/"):
                url_path = "/" + url_path
            url_lower = url_path.lower()
            item_name = item.get("name") or "<unnamed>"

            # Pattern 1: static asset
            if _static_asset_re.search(url_path):
                out.append(GroundingViolation(
                    tool="newman",
                    kind="endpoint_shape_mismatch",
                    symbol=f"static_asset:{url_path}",
                    location=item_name,
                    hint=(
                        f"R115.A.2: '{url_path}' looks like a static-asset path "
                        f"(.js/.css/.png/static/) — NOT a valid API endpoint. "
                        f"Static assets are served by the SPA's CDN/web server, "
                        f"not the SUT's API. ARTA generates tests for API "
                        f"endpoints only.\n\n"
                        f"BEFORE (BROKEN — run-8da91d evidence):\n"
                        f"  GET {url_path}  // 404 — not an API endpoint\n\n"
                        f"AFTER — remove this item OR replace with a real API "
                        f"endpoint from the OpenAPI spec / captured endpoints."
                    ),
                ))
                continue

            # Pattern 2: frontend route.
            # R115.A.2 method-gating: ONLY flag for GET (HTML page fetch).
            # POST /login + body is a valid API auth call; flagging it would
            # be a false positive for auth-test fixtures.
            if method == "GET" and not url_path.startswith("/api/"):
                if url_lower in _FRONTEND_ROUTES or any(
                    url_lower == fr or url_lower.startswith(fr + "/")
                    for fr in _FRONTEND_ROUTES
                ):
                    out.append(GroundingViolation(
                        tool="newman",
                        kind="endpoint_shape_mismatch",
                        symbol=f"frontend_route:{url_path}",
                        location=item_name,
                        hint=(
                            f"R115.A.2: '{url_path}' is a SPA frontend route, "
                            f"NOT an API endpoint. Frontend routes render UI "
                            f"pages; API endpoints handle data. ARTA Newman "
                            f"tests should target /api/* paths only.\n\n"
                            f"BEFORE (BROKEN — run-8da91d evidence):\n"
                            f"  GET {url_path}  // returns HTML 200 (SPA route)\n\n"
                            f"AFTER — use the API endpoint that the frontend "
                            f"page CALLS, not the route itself. Check captured_endpoints."
                        ),
                    ))
                    continue

            # Pattern 3: typo detection (edit distance 1-2 from captured)
            if captured_segs:
                segs = [s for s in url_path.split("/") if s and "{" not in s]
                if segs:
                    last_seg = segs[-1].lower()
                    if last_seg not in captured_segs:
                        # Find nearest match by Levenshtein-ish heuristic
                        import difflib
                        for cap_seg, cap_path in captured_segs.items():
                            # Skip very short segments (false-positive prone)
                            if len(cap_seg) < 5 or len(last_seg) < 5:
                                continue
                            # Quick filter: similar length (1-2 char diff)
                            if abs(len(cap_seg) - len(last_seg)) > 2:
                                continue
                            # Authoritative check: edit distance via difflib
                            # (ratio ≥ 0.85 means ~1-2 char edit on 10-char string)
                            ratio = difflib.SequenceMatcher(None, cap_seg, last_seg).ratio()
                            if 0.85 <= ratio < 1.0:
                                out.append(GroundingViolation(
                                    tool="newman",
                                    kind="endpoint_shape_mismatch",
                                    symbol=f"typo:{url_path}",
                                    location=item_name,
                                    hint=(
                                        f"R115.A.2: '{url_path}' looks like a typo "
                                        f"of '{cap_path}' (segment '{segs[-1]}' vs "
                                        f"captured '{cap_seg}', edit-distance ~ 1-2).\n\n"
                                        f"BEFORE (BROKEN — likely typo):\n"
                                        f"  GET {url_path}  // 404\n\n"
                                        f"AFTER — use the captured endpoint:\n"
                                        f"  GET {cap_path}"
                                    ),
                                ))
                                break

    _walk(items)
    return out


# ────────────────────────────────────────────────────────────────────────────
# R124.N — Newman pm.test API misuse detector
# ────────────────────────────────────────────────────────────────────────────


# Postman runtime APIs that DO NOT exist. The LLM frequently hallucinates
# these by analogy with other frameworks (`assertEqual` from xUnit,
# `assertNotNull` from JUnit, etc.). Valid Postman APIs use `pm.test()` +
# `pm.expect()`.
_R124_N_INVALID_PM_APIS = (
    "pm.assertEqual",
    "pm.assertNotEqual",
    "pm.assertTrue",
    "pm.assertFalse",
    "pm.assertNotNull",
    "pm.assertNull",
    "pm.assertResponseHasStatus",
    "pm.assertResponseHasBody",
    "pm.assertStatus",
    "pm.assertContains",
    "pm.assert",
)


def validate_newman_pm_api_usage(content: str) -> list["GroundingViolation"]:
    """R124.N — flag non-existent pm.* APIs in Newman pm.test scripts.

    Live evidence (run-d52a8c): 33 Newman FAILs with `null/undefined`
    runtime errors traced to LLM-emitted `pm.assertEqual(...)`,
    `pm.assertNotNull(...)`, etc. These APIs don't exist in Postman's
    pm runtime — only `pm.test()` + `pm.expect()` + assertion-style
    `pm.response.to.have.status()` are valid.

    Parallel to R95.3 PW API misuse: catches the bad pattern at gen
    time + R57.1 retry-with-hint shows the LLM the BEFORE/AFTER fix.
    """
    out: list[GroundingViolation] = []
    if not content:
        return out
    seen: set[str] = set()
    for invalid in _R124_N_INVALID_PM_APIS:
        # Match `pm.assertX(` with optional whitespace
        pattern = re.compile(r"\b" + re.escape(invalid) + r"\s*\(")
        for m in pattern.finditer(content):
            if invalid in seen:
                continue
            seen.add(invalid)
            line_no = content[:m.start()].count("\n") + 1
            out.append(GroundingViolation(
                tool="newman",
                kind="bad_pm_api",
                symbol=invalid,
                location=f"line {line_no}",
                hint=(
                    f"`{invalid}(...)` is NOT a valid Postman runtime API "
                    f"(run-d52a8c surfaced 33 such gen-bugs).\n"
                    f"Valid Postman APIs: pm.test, pm.expect, pm.response, "
                    f"pm.environment, pm.collectionVariables, pm.sendRequest.\n\n"
                    f"BEFORE (BROKEN — TypeError at runtime: '{invalid} is not a function'):\n"
                    f"  {invalid}(actual, expected, 'label');\n\n"
                    f"AFTER 1 — equality assertion:\n"
                    f"  pm.test('label', function () {{\n"
                    f"      pm.expect(actual).to.eql(expected);\n"
                    f"  }});\n\n"
                    f"AFTER 2 — status check:\n"
                    f"  pm.test('status is 200', function () {{\n"
                    f"      pm.response.to.have.status(200);\n"
                    f"  }});\n\n"
                    f"AFTER 3 — null check:\n"
                    f"  pm.test('value present', function () {{\n"
                    f"      pm.expect(pm.response.json().field).to.not.be.null;\n"
                    f"  }});"
                ),
            ))
            break  # one violation per invalid API name (avoid duplicates)
    return out


def _r111_g_validate_assertion_fields(
    *, name: str, method: str, path: str, script_lines: list[str], openapi_spec: dict,
) -> list[GroundingViolation]:
    """R111.G + R113.K — verify Newman item's `pm.test` assertions reference
    fields present in the OpenAPI response schema for the resolved endpoint.

    Detects two access patterns:
      - `pm.response.json().<field>` (direct chain)
      - `let X = pm.response.json(); X.<field>` (via variable)

    Top-level field references only (no nested-path traversal yet).

    R113.K extension: union the property names across ALL declared response
    schemas (2xx + 4xx + 5xx). Pre-R113.K: only 2xx checked. Live evidence
    (run-78bb3d): 40 × items marked HTTP-200-FAIL because LLM-generated
    `pm.response.json().metric` assertions crashed with `Cannot read
    properties of undefined (reading 'metric')` when SUT returned 401 with
    body `{"error": "API key missing"}`. The LLM's assertion targeted the
    2xx shape; R111.G correctly grounded it against 2xx schema but didn't
    catch the 4xx-side gap. R113.K unions both: assertions valid in EITHER
    success or error path are accepted; assertions valid in NEITHER are
    flagged. If only 2xx is declared (no error schema in OpenAPI),
    conservatively skips to avoid false positives — operator action is to
    request the SUT team declare error schemas.

    Skips silently when no response schema is declared / additionalProperties.
    """
    if not script_lines:
        return []
    try:
        from .openapi_cache import lookup_endpoint
        op = lookup_endpoint(openapi_spec, method, path)
    except Exception:
        return []
    if not isinstance(op, dict):
        return []
    responses = op.get("responses") or {}
    if not isinstance(responses, dict):
        return []

    # R113.K — collect properties from ALL declared response schemas
    # (2xx + 4xx + 5xx + default). Field grounded in ANY declared
    # response is considered valid; only fields grounded in NONE flag.
    # R150.G — additionally collect NESTED dotted paths so assertions
    # like `json.insight.metric` are validated end-to-end (not just the
    # `insight` top-level segment).
    import os as _os_r150g
    _r150g_disabled = _os_r150g.environ.get("ARTA_R150_G_NESTED_PATH_DISABLE") == "1"
    success_fields: set[str] = set()
    error_fields: set[str] = set()
    success_nested: set[str] = set()
    error_nested: set[str] = set()
    for code, resp_obj in responses.items():
        props = _r113_k_extract_schema_properties(resp_obj, openapi_spec)
        nested = (
            _r150_g_extract_nested_paths(resp_obj, openapi_spec)
            if not _r150g_disabled else set()
        )
        if not props and not nested:
            continue
        # Classify by status-code prefix
        code_str = str(code).strip()
        if code_str.startswith("2") or code_str == "default":
            success_fields |= props
            success_nested |= nested
        elif code_str.startswith("4") or code_str.startswith("5"):
            error_fields |= props
            error_nested |= nested

    # If no success schema declared, skip (legacy R111.G behavior)
    if not success_fields and not success_nested:
        return []

    valid_fields = success_fields | error_fields
    valid_nested = success_nested | error_nested  # R150.G — all reachable dotted paths
    has_error_schema_declared = bool(error_fields) or bool(error_nested)
    out: list[GroundingViolation] = []

    # Concat all script lines for regex scan
    script_text = "\n".join(script_lines)
    # Step 1 — find any var bound to pm.response.json()
    _bind_re = re.compile(
        r"(?:const|let|var)\s+(\w+)\s*=\s*(?:pm\.response\.json|responseJson|response\.json)\s*\(\s*\)"
    )
    json_var_names = {"json", "responseData", "resp", "data"}   # common LLM names
    for m in _bind_re.finditer(script_text):
        json_var_names.add(m.group(1))
    # Step 2 — find direct chain accesses + var-bound accesses.
    # R150.G extension: capture FULL dotted path (not just first segment).
    # Pattern: <var>.<path> OR pm.response.json().<path>
    # where <path> = `[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*` (dotted access chain)
    _access_re = re.compile(
        r"(?:pm\.response\.json\(\s*\)|\b(" + "|".join(re.escape(v) for v in json_var_names) + r")\b)"
        r"\.([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)"
    )
    seen_fields: set[str] = set()
    # Postman builtins + utility methods (legacy R111.G allowlist)
    _PM_BUILTINS = {
        "to", "be", "have", "expect", "test", "include", "equal",
        "deep", "length", "match", "status", "json", "text",
        "headers", "code",
    }
    for m in _access_re.finditer(script_text):
        full_path = m.group(2)
        first_segment = full_path.split(".", 1)[0]
        if full_path in seen_fields:
            continue
        seen_fields.add(full_path)
        # Allow common Postman builtins / utility methods on FIRST segment
        if first_segment in _PM_BUILTINS:
            continue
        # R150.G — when nested-path is enabled AND the access is dotted,
        # check the FULL path against nested set. When not dotted (single
        # segment) OR killswitch is set, fall back to legacy top-level
        # check against `valid_fields`.
        is_dotted = "." in full_path
        if not _r150g_disabled and is_dotted:
            path_valid = (
                full_path in valid_nested
                # Also accept if any longer-path prefix exists (e.g.,
                # `insight.metric` valid when schema declares
                # `insight.metric.value`). This handles the case where
                # the LLM accesses an intermediate path.
                or any(p.startswith(full_path + ".") for p in valid_nested)
            )
        else:
            path_valid = first_segment in valid_fields

        if not path_valid:
            field = full_path  # surface full dotted path in hint
            _avail_success = sorted(success_fields)[:6]
            _avail_error = sorted(error_fields)[:4] if error_fields else []
            _first_field = _avail_success[0] if _avail_success else "<field>"
            # R113.K — surface BOTH success + error schema fields in hint
            _schema_note = (
                "OpenAPI's response schemas for `{m} {p}` (success 2xx + "
                "error 4xx/5xx unioned per R113.K) do NOT declare this field."
                .format(m=method, p=path)
                if has_error_schema_declared
                else
                "OpenAPI's 2xx response schema for `{m} {p}` does NOT declare "
                "this field. (Note: error 4xx/5xx schemas not declared → "
                "assertion may also crash on error responses; ask SUT team "
                "to declare error schemas.)"
                .format(m=method, p=path)
            )
            _avail_hint = (
                f"  // 2xx fields: {_avail_success}\n"
                + (f"  // 4xx/5xx fields: {_avail_error}\n" if _avail_error else "")
            )
            out.append(GroundingViolation(
                tool="newman",
                kind="unknown_response_field",
                symbol=f"{method} {path} → .{field}",
                location=name,
                hint=(
                    f"Item '{name}' asserts `<response>.{field}` but {_schema_note}\n\n"
                    f"BEFORE (BROKEN — assertion may fail at runtime; e.g. "
                    f"received `401` with body `{{\"error\": \"...\"}}` → "
                    f"`json.{field}` is undefined → TypeError):\n"
                    f"  pm.test('check', () => {{\n"
                    f"    const json = pm.response.json();\n"
                    f"    pm.expect(json.{field}).to.equal('...');\n"
                    f"  }});\n\n"
                    f"AFTER — assert against a real response field + handle "
                    f"error path:\n"
                    f"{_avail_hint}"
                    f"  pm.test('check', () => {{\n"
                    f"    if (pm.response.code >= 400) return;  // skip on error\n"
                    f"    const json = pm.response.json();\n"
                    f"    pm.expect(json.{_first_field}).to.exist;\n"
                    f"  }});"
                ),
            ))

    return out


def _r95_4_validate_body_fields(
    *, name: str, method: str, path: str, body_raw: str,
    openapi_spec: dict | None = None,
    captured_endpoints: list[dict] | None = None,
) -> list[GroundingViolation]:
    """R95.4 — verify Newman item's JSON body fields exist in OpenAPI's
    requestBody schema OR (R143.C fallback) captured response_body_shape.

    For POST/PUT/PATCH items with a JSON body, look up the OpenAPI
    operation for (method, path), extract the `requestBody.content.
    application/json.schema.properties` keys, and flag any top-level
    body field that's NOT in the schema.

    R143.C extension: when OpenAPI declares no properties (e.g., a
    `/api/collection/*` domain that lacks an OpenAPI spec at
    ARTA-discovery time), fall back to captured `response_body_shape`
    for the same path — request/response often share field names; the
    captured shape is the only signal available for non-OpenAPI domains.

    R143.C fires the captured-shape check only when shape is well-
    populated (≥5 distinct field names) AND the body-vs-shape unknown
    ratio is ≥50% — conservative threshold to avoid false positives.

    Skips non-JSON bodies (form-data, urlencoded, binary uploads) +
    non-mutating methods (GET/DELETE/HEAD/OPTIONS).
    """
    if method not in ("POST", "PUT", "PATCH"):
        return []
    if not body_raw or not isinstance(body_raw, str):
        return []
    # Try to parse JSON body; bail on non-JSON
    try:
        body_obj = json.loads(body_raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(body_obj, dict):
        return []
    body_fields = set(body_obj.keys())
    if not body_fields:
        return []

    # ── Tier 1 — OpenAPI request schema check (existing behavior) ──
    if openapi_spec:
        try:
            from .openapi_cache import lookup_endpoint
            op = lookup_endpoint(openapi_spec, method, path)
        except Exception:
            op = None
        if isinstance(op, dict):
            req_body = op.get("requestBody") or {}
            content = (req_body.get("content") or {})
            json_schema = (
                content.get("application/json") or content.get("application/*+json") or {}
            ).get("schema") or {}
            if isinstance(json_schema, dict):
                # Resolve $ref if present (one level deep)
                if "$ref" in json_schema:
                    ref = json_schema["$ref"]
                    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                        schema_name = ref.rsplit("/", 1)[-1]
                        components = openapi_spec.get("components") or {}
                        schemas = components.get("schemas") or {}
                        json_schema = schemas.get(schema_name) or json_schema
                properties = json_schema.get("properties") or {}
                if isinstance(properties, dict) and properties:
                    # OpenAPI has properties → enforce
                    valid_fields = set(properties.keys())
                    unknown = body_fields - valid_fields
                    if unknown:
                        return [GroundingViolation(
                            tool="newman",
                            kind="unknown_request_field",
                            symbol=", ".join(sorted(unknown)[:5]),
                            location=name,
                            hint=(
                                f"Item '{name}' body has field(s) {sorted(unknown)[:5]} not in "
                                f"OpenAPI schema for {method} {path}. Valid fields per "
                                f"OpenAPI: {sorted(valid_fields)[:10]}. Remove invalid fields "
                                "OR rename to match the schema."
                            ),
                        )]
                    return []  # OpenAPI verified; no violations
                # Schema has no properties → fall through to Tier 2

    # ── Tier 2 — R143.C captured response_body_shape fallback ──
    # Live evidence: Iter 2 (run-f80567) showed 61 × HTTP 500 on
    # WITHOUT an OpenAPI spec at ARTA-discovery time. R143.C uses the
    # captured shape as a weak grounding signal: when ≥5 fields and ≥50%
    # of body fields are unknown, flag (conservative threshold).
    if not captured_endpoints:
        return []
    try:
        shape_fields: set[str] = set()
        method_norm = method.upper()
        path_norm = path.rstrip("/") or "/"
        for ep in captured_endpoints:
            if not isinstance(ep, dict):
                continue
            ep_method = (ep.get("method") or "GET").upper()
            ep_path = (ep.get("path") or "").rstrip("/") or "/"
            if ep_method != method_norm:
                continue
            if ep_path != path_norm:
                continue
            shape = ep.get("response_body_shape") or {}
            if isinstance(shape, dict):
                shape_fields.update(k for k in shape.keys() if isinstance(k, str))
        # Conservative gate: require ≥5 distinct keys in shape so single-field
        # responses don't false-positive.
        if len(shape_fields) < 5:
            return []
        unknown = body_fields - shape_fields
        # Require ≥50% of body fields unknown → high-confidence hallucination
        if len(unknown) / max(len(body_fields), 1) < 0.5:
            return []
        return [GroundingViolation(
            tool="newman",
            kind="unknown_request_field",
            symbol=", ".join(sorted(unknown)[:5]),
            location=name,
            hint=(
                f"R143.C: Item '{name}' body has field(s) {sorted(unknown)[:5]} "
                f"that don't appear in the SUT's captured response shape for "
                f"{method} {path} (captured shape has {len(shape_fields)} fields; "
                f"{int(100*len(unknown)/max(len(body_fields),1))}% of body fields "
                f"unknown). OpenAPI spec unavailable for this path; using captured "
                f"response_body_shape as the grounding signal. Valid fields per "
                f"captured shape (sample): {sorted(shape_fields)[:10]}. Either "
                f"rename body fields to match OR confirm the SUT accepts these "
                f"field names (and update the SUT discovery)."
            ),
        )]
    except Exception:
        return []


def _extract_path_from_url(url: Any) -> str:
    if isinstance(url, str):
        if url.startswith("{{base_url}}"):
            return url[len("{{base_url}}"):].split("?", 1)[0]
        if "://" in url:
            try:
                from urllib.parse import urlparse
                return urlparse(url).path
            except Exception:
                return ""
        return url.split("?", 1)[0]
    if isinstance(url, dict):
        path_segs = url.get("path")
        if isinstance(path_segs, list):
            return "/" + "/".join(str(s) for s in path_segs)
        raw = url.get("raw") or ""
        return _extract_path_from_url(raw)
    return ""


# G1 (R305) — an OPAQUE resource id (hex run / uuid / long-numeric). Distinct
# from a low-cardinality enum VALUE like a region (`us-texas-1`) or status
# (`active`): those are word-like and reused across many resources, ids are not.
# Used to EXEMPT genuinely-new resource ids from path-parameter-value grounding
# so only enum-like slots (region/status) are enforced.
_R305_OPAQUE_ID_RE = re.compile(
    r"[0-9a-f]{6,}|^\d{3,}$|[0-9a-f]{8}-[0-9a-f]{4}", re.IGNORECASE)


def _r305_param_value_violation(
    method: str, path: str, captured_endpoints: list[dict] | None,
) -> tuple[str, int, list[str]] | None:
    """G1 — flag a concrete path-parameter VALUE the SUT was never observed to
    serve for that slot (e.g. `region=global` on a resource only ever served
    under `us-texas-1`). Returns (bad_value, seg_index, allowed_values) or None.

    Conservative — fires ONLY when ALL hold, so genuinely-new resource ids never
    trip it:
      - a captured concrete value-set exists for the slot (paths agreeing on all
        OTHER concrete positions), and
      - that set is LOW-cardinality (<= 3 distinct → an enum like region/status,
        not a high-cardinality id slot), and
      - none of the captured values (nor the emitted one) is opaque-id-shaped, and
      - the emitted value is not in the set.
    Killswitch ARTA_R305_PARAM_VALUE_GROUNDING_DISABLE=1. SUT-agnostic."""
    if os.environ.get("ARTA_R305_PARAM_VALUE_GROUNDING_DISABLE") == "1":
        return None
    segs = [s for s in path.split("/") if s]
    cands: list[list[str]] = []
    for e in captured_endpoints or []:
        if not isinstance(e, dict) or (e.get("method") or "").upper() != method:
            continue
        cs = [s for s in str(e.get("path") or "").split("/") if s]
        if len(cs) == len(segs):
            cands.append(cs)
    if not cands:
        return None
    for i, u in enumerate(segs):
        if (u.startswith("{") and u.endswith("}")) or u.startswith("{{") or u.startswith(":"):
            continue
        if _R305_OPAQUE_ID_RE.search(u):
            continue  # opaque id — never value-ground (new ids must pass)
        value_set: set[str] = set()
        for cs in cands:
            ok = True
            for j, (pu, ct) in enumerate(zip(segs, cs)):
                if j == i:
                    continue
                if (ct.startswith("{") and ct.endswith("}")):
                    continue
                if (pu.startswith("{") and pu.endswith("}")) or pu.startswith("{{"):
                    continue
                if pu != ct:
                    ok = False
                    break
            if not ok:
                continue
            ci = cs[i]
            if not (ci.startswith("{") and ci.endswith("}")):
                value_set.add(ci)
        # enum-like slot only: small set, no opaque-id members
        if (value_set and len(value_set) <= 3
                and not any(_R305_OPAQUE_ID_RE.search(v) for v in value_set)
                and u not in value_set):
            return (u, i, sorted(value_set)[:6])
    return None


def _r305_response_keys(
    method: str, path: str, captured_endpoints: list[dict] | None,
) -> set[str] | None:
    """G2 — the FULL set of top-level keys of the captured object response for
    (method, path), or None if no COMPLETE object shape is captured. Only returns
    a set when the shape is well-populated (>= 8 keys) so property-existence
    grounding never false-flags against a sparse/partial sample. Reuses the same
    wildcard path-matching as _r305_response_root."""
    isegs = [s for s in path.split("/") if s]

    def _wild(s: str) -> bool:
        return ((s.startswith("{") and s.endswith("}"))
                or bool(_R305_OPAQUE_ID_RE.search(s)) or s.isdigit())
    for e in captured_endpoints or []:
        if not isinstance(e, dict) or (e.get("method") or "GET").upper() != method:
            continue
        sh = e.get("response_body_shape")
        if not isinstance(sh, dict):
            continue
        cs = [s for s in str(e.get("path") or "").split("/") if s]
        if len(cs) != len(isegs):
            continue
        if all(_wild(x) or _wild(y) or x == y for x, y in zip(isegs, cs)):
            if sh.get("type") == "object" and isinstance(sh.get("properties"), dict):
                keys = set(sh["properties"].keys())
            elif sh.get("type"):
                return None  # array / scalar — no object keys
            else:
                keys = set(sh.keys())  # raw sample
            return keys if len(keys) >= 8 else None
    return None


# G2 — a `to.have.property('<field>')` existence assertion (also `.a.property`).
_R305_PROPERTY_ASSERT_RE = re.compile(
    r"\.to\.have(?:\.a|\.an)?\.property\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]")


def _r305_response_root(
    method: str, path: str, captured_endpoints: list[dict] | None,
) -> str | None:
    """G2 — 'object' / 'array' / None for the captured response_body_shape of the
    endpoint matching (method, path). Wildcards `{var}`/opaque-id/numeric segments
    (region literals stay distinct so list vs detail don't collide)."""
    isegs = [s for s in path.split("/") if s]

    def _wild(s: str) -> bool:
        return ((s.startswith("{") and s.endswith("}"))
                or bool(_R305_OPAQUE_ID_RE.search(s)) or s.isdigit())
    for e in captured_endpoints or []:
        if not isinstance(e, dict) or (e.get("method") or "GET").upper() != method:
            continue
        sh = e.get("response_body_shape")
        if not sh:
            continue
        cs = [s for s in str(e.get("path") or "").split("/") if s]
        if len(cs) != len(isegs):
            continue
        if all(_wild(x) or _wild(y) or x == y for x, y in zip(isegs, cs)):
            if isinstance(sh, list) or (isinstance(sh, dict) and sh.get("type") == "array"):
                return "array"
            if isinstance(sh, dict):
                return "object"
    return None


# G2 — a bare top-level array assertion on the JSON response body, in any of the
# syntax variants the LLM cycles through (to.be.an('array') / Array.isArray).
_R305_BODYVAR = r"(?:pm\.response\.json\(\)|body|jsonData|responseJson|data)"
_R305_BARE_ARRAY_ASSERT_RE = re.compile(
    rf"pm\.expect\(\s*{_R305_BODYVAR}\s*\)\s*\.to\.be\.an\(\s*['\"]array['\"]"
    rf"|Array\.isArray\(\s*{_R305_BODYVAR}\s*\)",
    re.IGNORECASE,
)


def _path_matches_captured(
    method: str, path: str, captured_keys: set[tuple[str, str]],
) -> bool:
    """True when (method, path) matches any captured endpoint, treating
    `{var}` placeholders as wildcards on either side."""
    if (method, path) in captured_keys:
        return True
    path_segs = [s for s in path.split("/") if s]
    for cap_m, cap_p in captured_keys:
        if cap_m != method:
            continue
        cap_segs = [s for s in cap_p.split("/") if s]
        if len(cap_segs) != len(path_segs):
            continue
        match = True
        for u, t in zip(path_segs, cap_segs):
            u_var = (u.startswith("{") and u.endswith("}"))
            t_var = (t.startswith("{") and t.endswith("}")) or u.startswith(":")
            if u_var or t_var:
                continue
            # Postman variable substitution like `{{coach_id}}`
            if u.startswith("{{") and u.endswith("}}"):
                continue
            if u != t:
                match = False
                break
        if match:
            return True
    return False


# R252 (WS1c) — an id-shaped path LITERAL.
#
# Deliberately NOT _R190_ID_SHAPED_RE: that regex is owned by two callers (a
# path normalizer at ~:3325 and an orphan-segment detector at ~:3383) which
# both need its exact current semantics, and it does not match business-shaped
# ids like `ACC-OTP-57291` / `ASSET-VEH-2468` — precisely the shapes the LLM
# invented most. Kept as a sibling so R190's callers are untouched.
_R252_ID_SHAPED_RE = re.compile(
    r"^(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"   # UUID
    r"|[0-9a-f]{12,}"                                                   # long hex / mongo oid
    r"|\d{2,}"                                                          # numeric id
    r"|[A-Za-z]{2,}[-_][A-Za-z0-9-]*\d[A-Za-z0-9-]*"                    # ACC-OTP-57291
    r")$",
    re.IGNORECASE,
)


# R252.N — markers that the enclosing test DELIBERATELY uses an invalid id.
# A negative test's id is supposed to be unreal; flagging it would punish
# correct test design (and is the false-positive class R255 already hit).
_R252_NEGATIVE_CTX_RE = re.compile(
    r"returns?\s+(an?\s+)?error|invalid|not\s+found|non[\s-]?existent|nonexistent|"
    r"unknown|malformed|reject|negative|bad\s+request|does\s+not\s+exist|"
    r"tobe\(4\d\d\)|tobe\(5\d\d\)|status\(\)\)\.tobe\(4|expect\(\[?4\d\d|"
    r"@negative|very\s+large|out\s+of\s+range|boundary",
    re.IGNORECASE,
)


def validate_path_literal_ids(
    method: str,
    path: str,
    captured_keys: set[tuple[str, str]],
    *,
    real_ids: set[str] | None = None,
    session_slots: set[str] | None = None,
    tool: str = "playwright",
    location: str = "",
) -> list[GroundingViolation]:
    """R252 (WS1c) — reject FABRICATED id literals in request paths.

    This is the hole every other defense left open. `_path_matches_captured`
    treats a captured `{account_id}` slot as a wildcard (`if u_var or t_var:
    continue`), so `/accounts/ACC-OTP-57291` "matches" and passes grounding.
    `_R190_ID_SHAPED_RE`'s callers then WHITELIST id-shaped segments. And
    because no `{{var}}` survives to dispatch, R43/R169/R217/R230 never probe
    and R170 never BLOCKs. Net effect: R170's no-fabrication principle was
    enforced against ARTA's own synthesizer but NOT against the LLM's.

    An id-shaped literal sitting in a captured `{var}` slot must be a
    KNOWN-REAL id. Otherwise it is fabricated -> kind='fabricated_id', and the
    fix is to emit `{{<slot>}}` so the existing dispatch stack re-arms.

    A parallel walker by design: `_path_matches_captured` has many callers and
    changing its semantics in place is a real regression risk.

    Exemptions:
      • `real_ids` — verified real (R250 store).
      • `session_slots` — R186 substitutes org/account ids from storage-state;
        those literals are real by construction.
      • `{var}` / `{{var}}` — already the desired shape.

    Empty `real_ids` (store not yet populated) still flags: an id ARTA cannot
    verify is one it should not be asserting against. Callers choose the
    consequence via ARTA_R252_FABRICATED_ID_MODE (flag|block|off).
    """
    real_ids = real_ids or set()
    session_slots = session_slots or set()
    if not path or not captured_keys:
        return []

    path_segs = [s for s in path.split("/") if s]
    for cap_m, cap_p in captured_keys:
        if cap_m != method:
            continue
        cap_segs = [s for s in cap_p.split("/") if s]
        if len(cap_segs) != len(path_segs):
            continue
        suspects: list[tuple[str, str]] = []
        match = True
        for u, t in zip(path_segs, cap_segs):
            u_var = (u.startswith("{") and u.endswith("}"))
            t_var = (t.startswith("{") and t.endswith("}")) or u.startswith(":")
            if u_var:
                continue          # spec already parameterized — the goal state
            if t_var:
                # THE fabricated position: captured says "param here", the spec
                # hardcoded a literal.
                if _R252_ID_SHAPED_RE.match(u):
                    suspects.append((u, t))
                continue
            if u != t:
                match = False
                break
        if not match:
            continue
        out: list[GroundingViolation] = []
        for literal, slot in suspects:
            if literal in real_ids:
                continue
            slot_name = slot.strip("{}").strip(":").lower()
            if slot_name in session_slots:
                continue
            out.append(GroundingViolation(
                tool=tool,
                kind="fabricated_id",
                symbol=literal,
                location=location or f"{method} {path}",
                hint=(
                    f"'{literal}' is an invented id — the SUT never served it, "
                    f"so this request is a guaranteed 404 that says nothing "
                    f"about SUT quality. Use a real id from [HARD CONSTRAINT — "
                    f"REAL TEST DATA], or emit {{{{{slot_name}}}}} so ARTA "
                    f"resolves it at dispatch (or BLOCKs the test truthfully)."
                ),
            ))
        return out          # first structurally-matching template wins
    return []


# ────────────────────────────────────────────────────────────────────────────
# Playwright
# ────────────────────────────────────────────────────────────────────────────

# R190 — an id-shaped path segment is a RESOLVED path-param value (R180/R186
# substitute `:org_id` → the literal session id). It corresponds to a templated
# `{var}` in captured paths and must NOT be treated as an orphan/hallucinated
# segment. Matches UUIDs, long hex (≥12), all-numeric, and mongo-style 24-hex ids.
_R190_ID_SHAPED_RE = re.compile(
    r"^(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # UUID
    r"|[0-9a-f]{12,}"                                                  # long hex (incl 24-hex mongo)
    r"|\d{2,}"                                                         # numeric id
    r")$",
    re.IGNORECASE,
)
_TESTID_REF_RE = re.compile(r"getByTestId\(\s*['\"`]([^'\"`]+)['\"`]\s*\)")
# R78.6 — `getByRole('button', { name: 'Submit' })` matcher. The role
# captures group 1; the name captures group 2. JS allows single/double
# quotes for both arguments and the `name` key.
_ROLE_NAME_REF_RE = re.compile(
    r"getByRole\(\s*['\"`]([^'\"`]+)['\"`]\s*,\s*\{[^}]*?name\s*:\s*"
    r"['\"`]([^'\"`]+)['\"`]",
    re.IGNORECASE,
)
# R140.A — getByLabel('X') matcher (aria-label form binding). LLM has
# been observed hallucinating labels like 'Insight Metric' that aren't
# in the SUT's DOM catalog. Validator emits `catalog_aria_label_unknown`
# for the rejected text.
_LABEL_REF_RE = re.compile(r"getByLabel\(\s*['\"`]([^'\"`]+)['\"`]\s*\)")
# R140.A — getByText('X') matcher. Playwright matches by SUBSTRING by
# default, so the validator accepts when ANY catalog text contains the
# spec's text (not just exact match). Emits `catalog_text_unknown` only
# when zero catalog texts contain it as substring.
_TEXT_REF_RE = re.compile(r"getByText\(\s*['\"`]([^'\"`]+)['\"`]\s*\)")


def validate_playwright_syntax(content: str) -> list[GroundingViolation]:
    """R113.I.2 — deterministic post-LLM TypeScript syntax validator for PW.

    Pre-R113.I.2 evidence (run-78bb3d artifacts at data/artifacts/pw-run-
    78bb3d-req_am_*.spec.json): the LLM reproduced three concrete bad
    patterns that R95.3's API-misuse lint doesn't catch because they're
    SYNTAX errors not API misuse:

      1. Duplicate import name from same module:
         `import { test, expect, chromium } from '@playwright/test';
          import { chromium, ... } from '@playwright/test';`
         → `SyntaxError: Identifier 'chromium' has already been declared.`
         (run-78bb3d req_am_014:2)

      2. `export const test = test.extend({...})` shadow-conflict:
         The imported `test` is in scope; redeclaring + exporting under
         the same name creates a `TypeError: Duplicate declaration "test"`.
         (run-78bb3d req_am_016:28)

      3. Unquoted string concatenation in a template literal or string:
         `query: "Why did sales drop?" when they rose`
         → `SyntaxError: Unexpected token, expected ","`
         (run-78bb3d req_am_013:490)

    Each match returns a GroundingViolation with a BEFORE/AFTER hint so
    R57.1's retry-with-hint loop can fix on attempt 2. Mirrors the proven
    R110.B / R102.B / R93.B BEFORE/AFTER idiom that's eliminated similar
    classes of LLM regressions.
    """
    import re as _re_r113_i
    violations: list[GroundingViolation] = []

    # Pattern 1: Duplicate import names from the SAME module
    # Match all `import { ... } from '<path>'` lines and check for
    # repeated identifiers across the union of imports per module.
    _import_re = _re_r113_i.compile(
        r"^\s*import\s*\{\s*([^}]+)\s*\}\s*from\s*['\"]([^'\"]+)['\"];?\s*$",
        _re_r113_i.MULTILINE,
    )
    _module_imports: dict[str, list[str]] = {}
    for m in _import_re.finditer(content):
        imports_block = m.group(1)
        module = m.group(2)
        names = [
            n.strip().split(" as ")[0].strip()
            for n in imports_block.split(",")
            if n.strip()
        ]
        _module_imports.setdefault(module, []).extend(names)
    for module, names in _module_imports.items():
        dupes = {n for n in names if names.count(n) > 1}
        for dupe in dupes:
            violations.append(GroundingViolation(
                tool="playwright",
                kind="pw_syntax_error",
                symbol=f"duplicate_import:{dupe}@{module}",
                location="playwright_spec_imports",
                hint=(
                    f"R113.I.2: identifier '{dupe}' is imported TWICE from "
                    f"'{module}'. TypeScript will fail to compile with "
                    f"'Identifier already declared'.\n\n"
                    f"BEFORE (BROKEN):\n"
                    f"  import {{ test, expect, {dupe} }} from '{module}';\n"
                    f"  import {{ {dupe}, ... }} from '{module}';\n\n"
                    f"AFTER (CORRECT) — consolidate into ONE import OR rename:\n"
                    f"  import {{ test, expect, {dupe}, ... }} from '{module}';\n"
                    f"  // OR if you need both for different purposes:\n"
                    f"  import {{ {dupe} }} from '{module}';\n"
                    f"  import {{ {dupe} as {dupe}Alt }} from '{module}';"
                ),
            ))

    # Pattern 2: `export const test = test.extend(...)` self-shadow
    # The imported `test` is in scope; redeclaring under same name fails.
    _test_shadow_re = _re_r113_i.compile(
        r"^\s*export\s+const\s+test\s*=\s*test\.extend\b",
        _re_r113_i.MULTILINE,
    )
    if _test_shadow_re.search(content):
        violations.append(GroundingViolation(
            tool="playwright",
            kind="pw_syntax_error",
            symbol="duplicate_declaration:test",
            location="playwright_spec_fixture",
            hint=(
                "R113.I.2: `export const test = test.extend({...})` shadows "
                "the imported `test`. TypeScript reports 'Duplicate "
                "declaration test'.\n\n"
                "BEFORE (BROKEN):\n"
                "  import { test } from '@playwright/test';\n"
                "  export const test = test.extend({...});\n\n"
                "AFTER (CORRECT) — rename the extended fixture:\n"
                "  import { test as baseTest } from '@playwright/test';\n"
                "  export const test = baseTest.extend({...});\n"
                "  // OR keep the import name and rename the extended local:\n"
                "  import { test } from '@playwright/test';\n"
                "  const authFixture = test.extend({...});\n"
                "  // Then use `authFixture('foo', ...)` in test bodies."
            ),
        ))

    # Pattern 3: Unquoted string concatenation — heuristic for unmatched
    # closing quote followed by alphanumeric content on the same line.
    # Detects: `query: "Why?" when they rose` → invalid string concat.
    # Conservative regex: require a value-position context (colon or `=`)
    # followed by `"..."` followed by whitespace + alphanumeric (not a JS
    # operator/keyword). Avoids false-positives on lines like `if (x)`.
    _bad_concat_re = _re_r113_i.compile(
        # value-context: `: "..."` OR `= "..."` (config / assignment)
        r"(?:^|[:=])\s*\"[^\"\n]*\"\s+(?!(?:and|or|in|of|as|is|\+|\-|\*|=|<|>|/|,|\)|\]|\}|;))[A-Za-z][A-Za-z0-9_]*",
        _re_r113_i.MULTILINE,
    )
    for m in _bad_concat_re.finditer(content):
        snippet = m.group(0)[:80]
        violations.append(GroundingViolation(
            tool="playwright",
            kind="pw_syntax_error",
            symbol=f"unquoted_concat:{snippet}",
            location="playwright_spec_string",
            hint=(
                "R113.I.2: detected unquoted string concatenation. A string "
                "literal is followed by alphanumeric content WITHOUT a "
                "concatenation operator or closing punctuation. This is a "
                "TypeScript SyntaxError.\n\n"
                "BEFORE (BROKEN):\n"
                "  query: \"Why did sales drop?\" when they rose\n\n"
                "AFTER 1 (CORRECT) — wrap entire string in backticks:\n"
                "  query: `Why did sales drop? when they rose`\n\n"
                "AFTER 2 (CORRECT) — explicit concatenation:\n"
                "  query: \"Why did sales drop?\" + \" when they rose\"\n\n"
                "AFTER 3 (CORRECT) — fold into one string literal:\n"
                "  query: \"Why did sales drop? when they rose\""
            ),
        ))

    # R114.A.1a — Missing hook import. Detect bare `beforeEach(...)` calls
    # (NOT `test.beforeEach(...)`) when the identifier isn't in the import
    # list from `@playwright/test`. Live evidence (run-0179d0 req_am_005):
    # `import { test, expect, Page, chromium }` then bare `beforeEach(...)`
    # at line 11 → `ReferenceError: beforeEach is not defined`, 0 tests
    # executed. R102.E doesn't catch this — its regex only matches `test.<hook>`.
    _BARE_HOOKS = ("beforeEach", "afterEach", "beforeAll", "afterAll")
    # Build the set of identifiers imported FROM @playwright/test only.
    _pw_imports: set[str] = set()
    for m in _import_re.finditer(content):
        if "@playwright/test" not in m.group(2):
            continue
        for name in m.group(1).split(","):
            name = name.strip().split(" as ")[0].strip()
            if name:
                _pw_imports.add(name)
    for _hook in _BARE_HOOKS:
        # Bare call: `beforeEach(` NOT preceded by `.` (excludes
        # `test.beforeEach`, `t.beforeEach`, etc.). Use negative-lookbehind.
        _bare_re = _re_r113_i.compile(
            rf"(?<![\w.]){_hook}\s*\(",
            _re_r113_i.MULTILINE,
        )
        if _bare_re.search(content) and _hook not in _pw_imports:
            violations.append(GroundingViolation(
                tool="playwright",
                kind="pw_syntax_error",
                symbol=f"missing_import:{_hook}",
                location="playwright_spec_imports",
                hint=(
                    f"R114.A.1a: bare `{_hook}(...)` is called but `{_hook}` "
                    f"is NOT imported from '@playwright/test'. Node will "
                    f"throw `ReferenceError: {_hook} is not defined` and "
                    f"the spec compiles to 0 executed tests.\n\n"
                    f"BEFORE (BROKEN — run-0179d0 req_am_005 pattern):\n"
                    f"  import {{ test, expect, Page }} from '@playwright/test';\n"
                    f"  {_hook}(async ({{ page }}) => {{ ... }});\n\n"
                    f"AFTER (CORRECT) — qualify with `test.`:\n"
                    f"  import {{ test, expect, Page }} from '@playwright/test';\n"
                    f"  test.{_hook}(async ({{ page }}) => {{ ... }});"
                ),
            ))

    # R114.A.1b — Hook-fixture-misuse. The fixture signature `(arg, use) =>`
    # is ONLY valid inside `test.extend({ fixtureName: async (arg, use) => })`.
    # Used inside a lifecycle hook it yields `TypeError: use is not a function`.
    # Live evidence (run-0179d0 req_am_019: 6 of 12 tests fail). R102.E
    # rewrites this pattern at gen time (R114.B regex extended) but R114.A.1b
    # is the safety net for cases R102.E can't rewrite (unbalanced braces).
    _hook_fixture_re = _re_r113_i.compile(
        r"(?:test\.)?(beforeEach|afterEach|beforeAll|afterAll)\s*\(\s*async\s*"
        r"\(\s*\{[^}]*\}\s*,\s*use\s*\)",
        _re_r113_i.MULTILINE,
    )
    _seen_hook_misuse: set[str] = set()
    for m in _hook_fixture_re.finditer(content):
        hook_name = m.group(1)
        if hook_name in _seen_hook_misuse:
            continue
        _seen_hook_misuse.add(hook_name)
        violations.append(GroundingViolation(
            tool="playwright",
            kind="pw_syntax_error",
            symbol=f"hook_fixture_misuse:{hook_name}",
            location="playwright_spec_hook",
            hint=(
                f"R114.A.1b: `{hook_name}(async ({{...}}, use) => ...)` uses "
                f"the FIXTURE-extend signature inside a lifecycle hook. "
                f"`use` is not a function in hook scope and the test fails "
                f"at runtime with `TypeError: use is not a function`.\n\n"
                f"BEFORE (BROKEN — run-0179d0 req_am_019 pattern):\n"
                f"  test.{hook_name}(async ({{ page }}, use) => {{\n"
                f"    await seedData(page);\n"
                f"    await use(page);\n"
                f"    await cleanupData(page);\n"
                f"  }});\n\n"
                f"AFTER 1 (CORRECT) — pair before/after hooks:\n"
                f"  test.{hook_name}(async ({{ page }}) => {{\n"
                f"    await seedData(page);\n"
                f"  }});\n"
                f"  test.{hook_name.replace('before','after')}(async ({{ page }}) => {{\n"
                f"    await cleanupData(page);\n"
                f"  }});\n\n"
                f"AFTER 2 (CORRECT) — promote to a fixture via test.extend:\n"
                f"  const test = baseTest.extend({{\n"
                f"    seededPage: async ({{ page }}, use) => {{\n"
                f"      await seedData(page);\n"
                f"      await use(page);\n"
                f"      await cleanupData(page);\n"
                f"    }},\n"
                f"  }});"
            ),
        ))

    # R114.A.1c — Broken-fixture-pattern consolidation. Promotes Fix A from
    # automation_engineer._validate_response (line ~6026) into the structured
    # validator channel so the hint follows R110.B/R113.I.2 BEFORE/AFTER idiom
    # AND R57.1 retry-with-hint loop sees it via format_violations_as_hint.
    # Live evidence: 3 specs on disk (req_am_004, 005, 019 per run-0179d0)
    # still carry the pattern despite the validator → indicates the existing
    # check raised on attempt-1 but R57.1 hint format wasn't catching the LLM
    # eye. R114.A.1c funnels through the proven BEFORE/AFTER channel.
    _let_page_re = _re_r113_i.compile(
        r"^\s*let\s+page\s*(?::\s*[\w<>\[\]|]+)?\s*(?:=\s*[a-zA-Z_$][\w$]*)?\s*;",
        _re_r113_i.MULTILINE,
    )
    if _let_page_re.search(content):
        _cb_re = _re_r113_i.compile(
            r"(?:test|test\.(?:before|after)(?:Each|All)|(?<![\w.])(?:before|after)(?:Each|All))"
            r"\s*\(\s*(?:'[^']*'\s*,\s*)?async\s*\(\s*([^)]*)\s*\)\s*=>\s*\{",
        )
        for cb in _cb_re.finditer(content):
            args = cb.group(1)
            # Balanced-brace walk to find body end
            _start = cb.end()
            _depth = 1
            _i = _start
            while _i < len(content) and _depth > 0:
                _c = content[_i]
                if _c == "{":
                    _depth += 1
                elif _c == "}":
                    _depth -= 1
                _i += 1
            if _depth != 0:
                continue  # unbalanced; bail this callback
            _body = content[_start:_i - 1]
            if "page." in _body and "page" not in args:
                violations.append(GroundingViolation(
                    tool="playwright",
                    kind="pw_syntax_error",
                    symbol="broken_fixture_pattern:let_page",
                    location="playwright_spec_fixture",
                    hint=(
                        "R114.A.1c: `let page` declared at describe scope + "
                        "callback uses `page.X` WITHOUT destructuring "
                        "`{ page }` from the fixture argument. `page` is "
                        "declared but never assigned → runtime "
                        "`TypeError: Cannot read properties of undefined`.\n\n"
                        "BEFORE (BROKEN — run-170e18: 16 tests crashed):\n"
                        "  test.describe('x', () => {\n"
                        "    let page: any;\n"
                        "    test.beforeEach(async ({}) => {\n"
                        "      await page.request.post(...);  // page is undefined!\n"
                        "    });\n"
                        "  });\n\n"
                        "AFTER (CORRECT) — destructure on every callback:\n"
                        "  test.describe('x', () => {\n"
                        "    test.beforeEach(async ({ page }) => {\n"
                        "      await page.request.post(...);\n"
                        "    });\n"
                        "    test('case', async ({ page }) => {\n"
                        "      await page.goto('/x');\n"
                        "    });\n"
                        "  });"
                    ),
                ))
                break  # one violation per file is enough

    # R123.A KEYSTONE — undefined function/symbol detector. Pre-R123.A
    # evidence (run-b5e3e4 req_am_004): 30 of 42 PW FAILs (71%) crashed
    # with `ReferenceError: setupPage is not defined` because the LLM
    # emitted `await setupPage(...)` but never declared the function
    # anywhere in the file. R113.I.2 catches duplicate IMPORTS;
    # R114.A.1c catches broken fixture pattern; neither catches the
    # `function-called-but-never-declared` shape. R123.A closes the
    # gap by building a DECLARED set (imports + local defs + globals
    # + playwright fixtures) and emitting a violation for any call
    # site whose name is not in the set.
    #
    # Conservative guards (minimize false positives):
    #  - skip method-chain calls (`page.X(...)` — `X` is a method, not function)
    #  - skip language keywords (`if`, `for`, `await`, `async`, etc.)
    #  - skip common globals (`console`, `Math`, `JSON`, `Promise`, ...)
    #  - skip names ≤ 2 chars (likely loop indexes)
    _R123_A_KEYWORDS = {
        "if", "for", "while", "switch", "return", "typeof", "instanceof",
        "new", "async", "await", "function", "class", "try", "catch",
        "throw", "import", "export", "do", "in", "of", "as", "from",
        "delete", "void", "yield", "let", "const", "var", "extends",
        "super", "this", "true", "false", "null", "undefined", "case",
        "default", "break", "continue", "finally", "static", "public",
        "private", "protected", "readonly", "interface", "type", "enum",
        "namespace", "declare", "abstract", "implements", "module",
    }
    _R123_A_GLOBALS = {
        "console", "Math", "Array", "Object", "JSON", "Date", "Promise",
        "String", "Number", "Boolean", "RegExp", "Error", "Symbol", "Map",
        "Set", "WeakMap", "WeakSet", "globalThis", "window", "document",
        "localStorage", "sessionStorage", "navigator", "process", "Buffer",
        "require", "module", "exports", "__dirname", "__filename", "fetch",
        "URL", "URLSearchParams", "FormData", "Headers", "Request", "Response",
        "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
        "decodeURIComponent", "encodeURI", "decodeURI", "setTimeout",
        "setInterval", "clearTimeout", "clearInterval", "crypto",
        # R290 — DOM/web-platform globals the LLM legitimately uses (esp. event
        # simulation in token-refresh / storage tests). Constructors are already
        # covered by the `new X(` skip above; these also cover bare references.
        "Event", "CustomEvent", "StorageEvent", "MessageEvent", "Blob", "File",
        "FormData", "AbortController", "AbortSignal", "TextEncoder",
        "TextDecoder", "atob", "btoa", "structuredClone", "queueMicrotask",
        "BigInt", "Proxy", "Reflect", "Intl", "WebSocket", "EventSource",
    }
    _R123_A_PW_FIXTURES = {
        # Names available as Playwright test fixtures or directly bound by
        # the test runner. The LLM emits `await test.step(...)` etc.
        "test", "expect", "page", "request", "context", "browser",
        "browserName", "Page", "BrowserContext", "Browser", "APIRequest",
        "APIRequestContext", "APIResponse", "Locator", "ElementHandle",
        "Frame", "Worker", "Dialog", "Download", "Mouse", "Keyboard",
        "Touchscreen", "ConsoleMessage", "FileChooser", "Route", "Request",
        "Response", "Selectors", "Tracing", "Video", "WebSocket",
        # Common shared helpers the LLM imports from ../common/
        "waitForSPAReady", "skipIfAuthStale", "isAuthLoggedIn",
        "findByVision", "visionClickFallback", "smartLocate", "smartVisible",
        "smartClick",
    }

    # Build DECLARED set
    _declared: set[str] = set(_R123_A_KEYWORDS | _R123_A_GLOBALS | _R123_A_PW_FIXTURES)

    # Add imported names (reuse R113.I.2's _module_imports walk results)
    for _names in _module_imports.values():
        for _n in _names:
            _declared.add(_n.strip())

    # Add local declarations: function X(, const X =, let X =, var X =,
    # async function X(, export function X(, export const X =, etc.
    _decl_patterns = (
        r"\b(?:async\s+)?function\s+([a-zA-Z_$][\w$]*)\s*\(",
        r"\b(?:const|let|var)\s+([a-zA-Z_$][\w$]*)\s*[=:]",
        r"\b(?:export\s+)?(?:async\s+)?function\s+([a-zA-Z_$][\w$]*)\s*\(",
        r"\b(?:export\s+)?(?:const|let|var)\s+([a-zA-Z_$][\w$]*)\s*[=:]",
        r"\bclass\s+([a-zA-Z_$][\w$]*)\s*[\{e]",
        # destructured: const { a, b } = ... — names inside braces
        r"\b(?:const|let|var)\s*\{\s*([^}]+)\s*\}\s*=",
    )
    for _pat in _decl_patterns:
        for _m in _re_r113_i.finditer(_pat, content):
            _decl = _m.group(1).strip()
            # For destructured groups, split by comma + colon (renames)
            if "," in _decl or ":" in _decl:
                for _piece in _decl.split(","):
                    _name = _piece.split(":")[0].strip()
                    if _name and _name[0].isalpha() or _name.startswith("_") or _name.startswith("$"):
                        _declared.add(_name)
            else:
                _declared.add(_decl)

    # R179 — scan for call sites on a COMMENT/STRING-STRIPPED copy. Pre-R179 the
    # call-site regex ran on raw content, so a word followed by `(` inside a
    # comment or string (e.g. `// Inject token after OAuth flow (simulate ...)`,
    # `*  ...the JWT (default: authToken)`, a template literal) was wrongly
    # flagged as an "undefined symbol called" (false `pw_syntax_error`). That
    # wasted R57.1 retries + could R102.A-BLOCK syntactically-valid specs
    # The strip replaces comment/string spans with EQUAL-LENGTH blanks but keeps
    # newlines, so character offsets + line numbers are preserved exactly (even
    # through multi-line block comments). DECLARED-set build above stays on raw
    # `content` so real declarations are still seen. True positives are intact:
    # a genuine `setupPage()` call in real code survives the strip and is flagged.
    def _r179_blank(_m_strip):
        return _re_r113_i.sub(r"[^\n]", " ", _m_strip.group(0))
    _scan = _re_r113_i.sub(
        r"'[^'\n]*'|\"[^\"\n]*\"|`[^`]*`|//[^\n]*|/\*.*?\*/",
        _r179_blank, content, flags=_re_r113_i.DOTALL,
    )

    # Find all call sites: `<name>(` where prior char is NOT `.` or alphanumeric
    # (i.e., it's start-of-line, whitespace, `;`, `{`, `(`, `,`, `=`, etc.)
    _call_re = _re_r113_i.compile(
        r"(?:^|[\s;{(,=!&|?+\-*/%<>:])([a-zA-Z_$][\w$]*)\s*\(",
        _re_r113_i.MULTILINE,
    )
    _undefined_calls: set[str] = set()
    for _m in _call_re.finditer(_scan):
        _name = _m.group(1)
        if len(_name) <= 2:
            continue  # skip short names (loop indices, shorthand)
        if _name in _declared:
            continue
        # R288 — the comment above says "prior char is NOT `.`", but `\s` is in
        # the preceding-char class and the regex never looks PAST the whitespace
        # to a `.`. So a method call chained across a newline —
        #     expect(x).
        #       toContain('y')          // valid JS method continuation
        # — has whitespace immediately before `toContain`, gets matched as a
        # BARE call, and is flagged `undefined_symbol:toContain`. It is a real
        # exactly this (all attempts rejected `undefined_symbol:toContain`, then
        # R121 kept the old spec) while its on-disk spec uses `.toContain(`
        # correctly 6 times. Walk back over whitespace: if the real preceding
        # token is `.`, it is a method continuation — skip.
        _back = _m.start(1) - 1
        while _back >= 0 and _scan[_back] in " \t\r\n":
            _back -= 1
        if _back >= 0 and _scan[_back] == ".":
            continue  # method call across whitespace/newline — not a bare call
        # R290 — `new X(` is a CONSTRUCTOR, not a bare function call. R123.A's
        # own comment says it skips these, but the code never did: `new URL(`
        # only passed because URL happened to be allowlisted, while every other
        # cleared toContain, died on `undefined_symbol:StorageEvent`
        # (`window.dispatchEvent(new StorageEvent('storage', …))` — a real DOM
        # constructor the token-refresh test needs). The undefined-symbol check
        # is about undefined FUNCTION calls; a `new` target that does not exist
        # is a different, rarer error TypeScript catches at compile. Skip any
        # name whose preceding token is the `new` keyword — generic across
        # StorageEvent/CustomEvent/Event/Blob/AbortController/etc.
        if _re_r113_i.search(r"\bnew\s*$", _scan[:_m.start(1)]):
            continue
        # Skip names starting with uppercase IF they're being called as a
        # constructor (`new X(`) — those are already handled by `new` keyword.
        # Also skip if surrounded by `new ` (we don't want to flag `new SomeClass()`).
        _undefined_calls.add(_name)

    if _undefined_calls:
        # Emit ONE violation per undefined name (operator-debuggable)
        for _name in sorted(_undefined_calls):
            # Find the first line where this name appears as a call site (scan
            # the stripped copy so a comment occurrence isn't reported; offsets
            # match raw content since the strip is length-preserving).
            _first_match = _re_r113_i.search(
                rf"(?:^|[\s;{{(,=!&|?+\-*/%<>:]){_re_r113_i.escape(_name)}\s*\(",
                _scan, _re_r113_i.MULTILINE,
            )
            _line = content[:_first_match.start()].count("\n") + 1 if _first_match else 0
            violations.append(GroundingViolation(
                tool="playwright",
                kind="pw_syntax_error",
                symbol=f"undefined_symbol:{_name}",
                location=f"playwright_spec_line_{_line}",
                hint=(
                    f"R123.A: `{_name}` is called at line {_line} but NEVER "
                    f"declared in this file (not imported, not defined as "
                    f"function/const, not a Playwright fixture, not a global).\n\n"
                    f"BEFORE (BROKEN — run-b5e3e4 req_am_004: 30 tests crashed):\n"
                    f"  test('foo', async ({{ page }}) => {{\n"
                    f"    await {_name}();  // ← {_name} was never defined!\n"
                    f"    await page.goto('/');\n"
                    f"  }});\n\n"
                    f"AFTER (CORRECT) — option 1: define the helper inline:\n"
                    f"  async function {_name}(): Promise<void> {{\n"
                    f"    // setup logic here\n"
                    f"  }}\n"
                    f"  test('foo', async ({{ page }}) => {{\n"
                    f"    await {_name}();\n"
                    f"    await page.goto('/');\n"
                    f"  }});\n\n"
                    f"AFTER (CORRECT) — option 2: import from shared module:\n"
                    f"  import {{ {_name} }} from '../common/sub_flows';"
                ),
            ))

    # R213 V2.3 — TDZ (temporal dead zone) use-before-init detector.
    # A `const`/`let X` referenced TEXTUALLY BEFORE its declaration in the SAME
    # scope throws `Cannot access 'X' before initialization` at runtime
    # (run-3ad7dc: 24 PW FAILs, e.g. `Cannot access 'subscriberId' before
    # initialization`). R123.A catches truly-undefined symbols but NOT a symbol
    # that IS declared, just later. Conservative to avoid false positives:
    #   • only `const`/`let` (function decls are hoisted — legal before-use);
    #   • the use must be at the SAME brace depth as the declaration (a use
    #     nested inside a function/arrow body may be a deferred closure that
    #     runs AFTER init → NOT flagged);
    #   • the brace depth must never drop BELOW the declaration's depth between
    #     the use and the decl (so a sibling-scope reuse of the name is NOT
    #     flagged — they're different `X`s).
    # Runs on the comment/string-stripped `_scan` so offsets/lines are exact.
    # Killswitch ARTA_R213_TDZ_DISABLE=1.
    if os.environ.get("ARTA_R213_TDZ_DISABLE", "").lower() not in ("1", "true"):
        # prefix brace-depth array: _depth[i] = unmatched `{` before char i
        _depth = [0] * (len(_scan) + 1)
        _d = 0
        for _i, _ch in enumerate(_scan):
            _depth[_i] = _d
            if _ch == "{":
                _d += 1
            elif _ch == "}":
                _d = max(0, _d - 1)
        _depth[len(_scan)] = _d
        _tdz_seen: set[str] = set()
        for _dm in _re_r113_i.finditer(r"\b(?:const|let)\s+([a-zA-Z_$][\w$]*)\s*=", _scan):
            _nm = _dm.group(1)
            if _nm in _tdz_seen or len(_nm) <= 2:
                continue
            _decl_pos = _dm.start(1)
            _decl_depth = _depth[_decl_pos]
            # earliest bare-identifier use NOT preceded by `.` and not a decl
            _use_pos = None
            for _um in _re_r113_i.finditer(
                    rf"(?<![.\w$]){_re_r113_i.escape(_nm)}\b", _scan):
                _p = _um.start()
                if _p >= _decl_pos:
                    break  # all later uses are fine
                # skip if this occurrence is itself a `const/let NAME` decl
                _pre = _scan[max(0, _p - 12):_p]
                if _re_r113_i.search(r"\b(?:const|let|var|function)\s+$", _pre):
                    continue
                _use_pos = _p
                break
            if _use_pos is None:
                continue
            # same scope: use at the SAME depth as decl AND depth never drops
            # below the decl depth between them (no scope exit/re-enter).
            if _depth[_use_pos] != _decl_depth:
                continue
            if min(_depth[_use_pos:_decl_pos] or [_decl_depth]) < _decl_depth:
                continue
            _tdz_seen.add(_nm)
            _uline = content[:_use_pos].count("\n") + 1
            _dline = content[:_decl_pos].count("\n") + 1
            violations.append(GroundingViolation(
                tool="playwright",
                kind="pw_syntax_error",
                symbol=f"tdz_use_before_init:{_nm}",
                location=f"playwright_spec_line_{_uline}",
                hint=(
                    f"R213 V2.3: `{_nm}` is USED at line {_uline} but its "
                    f"const/let declaration is at line {_dline} (AFTER the use, "
                    f"same scope) → at runtime this throws `Cannot access "
                    f"'{_nm}' before initialization` (temporal dead zone).\n\n"
                    f"BEFORE (BROKEN):\n"
                    f"  const total = {_nm} + 1;   // ← used here, line {_uline}\n"
                    f"  const {_nm} = await getValue();  // ← declared later, line {_dline}\n\n"
                    f"AFTER (CORRECT) — declare `{_nm}` BEFORE its first use:\n"
                    f"  const {_nm} = await getValue();\n"
                    f"  const total = {_nm} + 1;"
                ),
            ))

    # R123.B — import-vs-local-def collision detector. Pre-R123.B
    # evidence (run-b5e3e4 req_am_014): `smartVisible` is imported from
    # `../common/smart_locator` at line 21 AND redeclared as a local
    # `function smartVisible(...)` at lines 24-37. TypeScript fails to
    # compile with `TS2451: Cannot redeclare block-scoped variable`,
    # producing `tests=0 returncode=1` (silent failure pre-R113.C
    # per-spec logs).
    _import_names_set: set[str] = set()
    for _names in _module_imports.values():
        for _n in _names:
            _import_names_set.add(_n.strip())
    # Collect local function/const/let/var top-level declarations
    _local_def_re = _re_r113_i.compile(
        r"\b(?:async\s+)?function\s+([a-zA-Z_$][\w$]*)\s*\("
        r"|\b(?:const|let|var)\s+([a-zA-Z_$][\w$]*)\s*[=:]",
    )
    _local_names: dict[str, int] = {}
    for _m in _local_def_re.finditer(content):
        _n = (_m.group(1) or _m.group(2) or "").strip()
        if _n:
            _local_names[_n] = content[:_m.start()].count("\n") + 1
    _collisions = _import_names_set & set(_local_names.keys())
    for _collision in sorted(_collisions):
        _line = _local_names.get(_collision, 0)
        violations.append(GroundingViolation(
            tool="playwright",
            kind="pw_syntax_error",
            symbol=f"import_local_def_collision:{_collision}",
            location=f"playwright_spec_line_{_line}",
            hint=(
                f"R123.B: `{_collision}` is imported AND redeclared "
                f"locally at line {_line}. TypeScript fails to compile "
                f"with `TS2451: Cannot redeclare block-scoped variable`.\n\n"
                f"BEFORE (BROKEN — run-b5e3e4 req_am_014: tests=0 returncode=1):\n"
                f"  import {{ {_collision} }} from '../common/smart_locator';\n"
                f"  // ... later ...\n"
                f"  async function {_collision}(page: Page, ...) {{\n"
                f"    // local override\n"
                f"  }}\n\n"
                f"AFTER (CORRECT) — pick ONE:\n"
                f"  // Option 1: use the imported version, delete the local\n"
                f"  import {{ {_collision} }} from '../common/smart_locator';\n"
                f"\n"
                f"  // Option 2: rename the local to avoid collision\n"
                f"  import {{ {_collision} }} from '../common/smart_locator';\n"
                f"  async function {_collision}Local(...) {{ ... }}"
            ),
        ))

    # R125.B — Gherkin-translation completeness gate. When LLM truncates
    # mid-translation OR auth-fails mid-stream, raw Gherkin (Scenario:/Given/
    # When/Then) lands on disk with NO test() blocks → Playwright finds 0
    # tests at dispatch. Live evidence: req_am_002.spec.ts (5 lines of raw
    # Gherkin only). R125.B flags this so R57.1 retry-with-hint kicks in.
    violations.extend(_r125_b_validate_gherkin_translation(content))

    return violations


# R125.B — Gherkin-translation completeness gate constants + helper.
# ── R211 B2 — gen-time test-CASE quality (measurable / AC-grounded / fail-first) ──
# The ATDD prompt ASKS for measurable Then-steps (MEASURABLE THEN rule) but
# nothing VALIDATES it — so vague, un-failable assertions ship and pollute the
# SUT-quality verdict. This validator enforces fail-first at gen time, feeding
# the existing retry-with-hint loop.
_R211_THEN_RE = re.compile(r"^\s*(Then|And|But)\s+(.*)$", re.IGNORECASE | re.MULTILINE)
_R211_MEASURABLE_PATTERNS = (
    re.compile(r"\bstatus\b.*\b([1-5]\d\d)\b", re.I),          # exact HTTP status
    re.compile(r"\b([1-5]\d\d)\b.*\bstatus\b", re.I),
    re.compile(r"\b(under|below|less than|within|<=?|>=?|at most|at least)\b.*\b\d", re.I),  # num+comparator
    re.compile(r"\b\d+(\.\d+)?\s*(ms|s|sec|seconds|%|percent|kb|mb|requests|items|rows|records)\b", re.I),
    re.compile(r"\b(equals?|equal to|==|is exactly|matches)\b", re.I),   # exact value
    re.compile(r"\bnon-?empty\b|\bis not empty\b|\bcontains?\b.*\b(field|token|id|value|key)\b", re.I),
    re.compile(r"\bfield\b.*\b(is|equals?|contains?)\b", re.I),
    re.compile(r"\bheader\b.*\b(is|equals?|present|contains?)\b", re.I),
)
_R211_VAGUE_PATTERNS = (
    re.compile(r"\bit works\b", re.I),
    re.compile(r"\bis (logged in|successful|displayed|shown|visible)\b", re.I),
    re.compile(r"\bloads? (gracefully|correctly|properly|fine)\b", re.I),
    re.compile(r"\b(errors?|exceptions?) (are|is) handled\b", re.I),
    re.compile(r"\b(works|behaves|responds) (as expected|correctly|properly)\b", re.I),
    re.compile(r"\bsuccessfully\b", re.I),
    re.compile(r"\bthe (user|system|page|app|response) (is|works|responds)\b\s*$", re.I),
)
# generic 2xx with no value/field assertion is NOT measurable on its own
_R211_BARE_OK_RE = re.compile(r"^\s*(Then|And)\s+(the )?(response|request|call|it)\s+"
                              r"(is|returns?|succeeds?|is ok|is successful)\b", re.I)


def _r211_then_is_measurable(line: str) -> bool:
    if any(p.search(line) for p in _R211_MEASURABLE_PATTERNS):
        return True
    return False


def scenario_budget_for_risk(priority: str = "", risk_score: int = 0) -> set[str]:
    """R211 B2 — scenario-type budget by risk (BMAD TEA Layer 2): high-risk gets
    the full battery, low-risk gets the essentials. Coverage matches risk
    instead of 8-scenarios-regardless."""
    p = (priority or "").upper()
    if not p:
        p = "P0" if risk_score >= 8 else "P1" if risk_score >= 6 else "P2" if risk_score >= 4 else "P3"
    base = {"happy_path", "negative"}
    if p in ("P0", "P1", "P2"):
        base |= {"boundary", "security"}
    if p in ("P0", "P1"):
        base |= {"performance", "idempotency", "concurrency", "accessibility"}
    return base


_SCENARIO_HEAD_RE = re.compile(
    r"^[ \t]*(Scenario Outline|Scenario Template|Scenario|Example)[ \t]*:(.*)$",
    re.MULTILINE,
)
_TAG_LINE_RE = re.compile(r"^[ \t]*@[^\n]*$")
_AC_TAG_RE = re.compile(r"@ac[:\-]([A-Za-z0-9_.\-]+)", re.IGNORECASE)


def split_feature_scenarios(feature_text: str) -> list[dict]:
    """R213 (WS2a) — split a `.feature` file into its individual scenarios.

    Why this exists: `atdd_designer` appends ONE WHOLE `.feature` FILE per
    requirement (`all_gherkin.append(result["feature_file"])`, returned as
    `gherkin_scenarios`), but `tests.py` iterated that list as if each element
    were a scenario. For a single-requirement generate that means n=1, so:
      • `measurable_pct` reads 100% if ANY Then anywhere in the file is
        measurable — one good assertion masked a whole file of vague ones;
      • `grounded_pct` was a per-FILE verdict wearing a per-scenario name;
      • `_ac = _acs[_i]` paired AC[0] with "the entire file" and nothing else.
    Every R213 number was measuring the wrong unit, which is why the gate could
    never be trusted enough to enable.

    Returns [{"name", "text", "line", "tags"}] in file order. Leading `@tags`
    attach to the scenario they precede. Background/Feature preamble is
    excluded — it is not a scenario and must not be graded as one.
    """
    if not feature_text or not feature_text.strip():
        return []
    lines = feature_text.splitlines()
    heads: list[tuple[int, str, str]] = []   # (line_idx, keyword, name)
    for m in _SCENARIO_HEAD_RE.finditer(feature_text):
        line_idx = feature_text[:m.start()].count("\n")
        heads.append((line_idx, m.group(1), (m.group(2) or "").strip()))
    if not heads:
        return []

    out: list[dict] = []
    for i, (line_idx, _kw, name) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(lines)
        # Trailing @tags belong to the NEXT scenario, not this one's body.
        body_end = end
        while body_end - 1 > line_idx and _TAG_LINE_RE.match(lines[body_end - 1] or ""):
            body_end -= 1
        # Leading @tags immediately above the header describe THIS scenario.
        tag_start = line_idx
        while tag_start - 1 >= 0 and _TAG_LINE_RE.match(lines[tag_start - 1] or ""):
            tag_start -= 1
        tags: list[str] = []
        for t_line in lines[tag_start:line_idx]:
            tags.extend(re.findall(r"@[A-Za-z0-9_.:\-]+", t_line))
        out.append({
            "name": name,
            "text": "\n".join(lines[line_idx:body_end]).strip("\n"),
            "line": line_idx + 1,
            "tags": tags,
        })
    return out


def ac_for_scenario(scenario: dict, acs: list[dict] | None) -> dict | None:
    """R213 (WS2a) — pair a scenario with its AC by IDENTITY, never position.

    The old `_acs[_i]` pairing was wrong under ANY unit: scenario order is the
    LLM's choice and has no relationship to AC order, so a "vague assertion"
    could be reported against an AC that had nothing to do with it. Match on an
    explicit `@ac:<id>` tag first, then on the AC id/title appearing in the
    scenario name. No match → None (ungraded against an AC) rather than a
    confidently wrong one.
    """
    if not acs:
        return None
    by_id = {
        str(a.get("id") or a.get("ac_id") or "").lower(): a
        for a in acs if isinstance(a, dict)
    }
    for tag in scenario.get("tags") or []:
        m = _AC_TAG_RE.match(tag)
        if m:
            hit = by_id.get(m.group(1).lower())
            if hit:
                return hit
    name = (scenario.get("name") or "").lower()
    for ac_id, ac in by_id.items():
        if ac_id and ac_id in name:
            return ac
    for a in acs:
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "").strip().lower()
        if title and title in name:
            return a
    return None


# R255 (WS2c) — a scenario whose NAME declares a negative-auth intent.
_R255_NEG_AUTH_NAME_RE = re.compile(
    r"\b(unauthenticated|unauthorized|without\s+(a\s+)?(auth|token|credential|login)|"
    r"no\s+(auth|token|credential)|invalid\s+(token|credential|auth)|"
    r"expired\s+token|logged[\s-]?out|anonymous|not\s+logged\s+in)\b",
    re.IGNORECASE,
)
# The Given/setup ESTABLISHES the negative precondition. Deliberately broad:
# a false NEGATIVE here costs one unflagged scenario, while a false POSITIVE
# tells the operator a correct test is broken. `unauthorized`/`forbidden`/
# `lacks permission` were missed by the first draft and flagged a perfectly
# good real scenario ("Given a user is unauthorized").
_R255_NEG_SETUP_RE = re.compile(
    r"\b(no\s+(auth|token|credential|session|permission)|"
    r"without\s+(a\s+)?(auth|token|credential|header|permission)|"
    r"invalid\s+(token|credential|auth)|expired\s+(token|session)|logged\s+out|"
    r"not\s+(logged\s+in|authenticated|authorized|permitted)|"
    r"un(authenticated|authorized)|anonymous|forbidden|"
    r"lacks?\s+(permission|access|the\s+role)|"
    r"(read[\s-]?only|guest|viewer)\s+(user|role))\b",
    re.IGNORECASE,
)
# The Given AUTHENTICATES normally — the contradiction.
_R255_POS_AUTH_SETUP_RE = re.compile(
    r"\b(logged\s+in|authenticated|valid\s+(token|credential|session)|"
    r"signed\s+in|with\s+(a\s+)?valid\s+(token|auth|credential))\b",
    re.IGNORECASE,
)


def validate_negative_assertion_class_gherkin(
    scenario_text: str, *, ac: dict | None = None,
) -> list[GroundingViolation]:
    """R255 (WS2c) — a negative-auth scenario must SET UP the negative case.

    R247 fixed the SCRIPT (specs titled "Unauthenticated ..." were sending the
    real token); the CASE that specifies them is still wrong. Worse, those
    mislabeled tests FALSELY PASSED against a stale token: the 401 they
    asserted came from expiry, not from the SUT enforcing auth. They tested
    nothing and reported success — the most expensive kind of failure, because
    it is invisible.

    `validate_negative_assertion_class` (scripts) compares title-intent against
    the asserted STATUS only, so "Unauthenticated user cannot X" + a Given that
    logs in + `Then 401` passes it clean. This checks the SETUP instead.

    Violation kind: `negative_setup_mismatch`.
    """
    if not scenario_text:
        return []
    lines = scenario_text.splitlines()
    name = ""
    for ln in lines:
        m = _SCENARIO_HEAD_RE.match(ln)
        if m:
            name = (m.group(2) or "").strip()
            break
    if not name or not _R255_NEG_AUTH_NAME_RE.search(name):
        return []

    # Steps before the first Then are the setup.
    setup: list[str] = []
    for ln in lines:
        if re.match(r"^\s*Then\b", ln, re.IGNORECASE):
            break
        if re.match(r"^\s*(Given|When|And|But)\b", ln, re.IGNORECASE):
            setup.append(ln.strip())
    setup_text = " ".join(setup)
    if _R255_NEG_SETUP_RE.search(setup_text):
        return []          # negative precondition established — correct

    # Flag ONLY a provable CONTRADICTION: the setup positively authenticates.
    #
    # An earlier draft also flagged any negative-titled scenario whose setup
    # merely failed to match the regex above — and it immediately mis-flagged a
    # correct real scenario ("Given a user is unauthorized") because the pattern
    # did not yet know that word. Absence of a phrase I recognize is not
    # evidence of a defect; claiming otherwise sends the operator to fix a test
    # that was already right. Same rule as R258 branch 4: abstain unless it can
    # be proven.
    if not _R255_POS_AUTH_SETUP_RE.search(setup_text):
        return []
    return [GroundingViolation(
        tool="gherkin",
        kind="negative_setup_mismatch",
        symbol=name,
        location=f"Scenario: {name}",
        hint=(
            f"'{name}' declares a NEGATIVE-auth intent but its setup "
            f"authenticates normally. A test that sends a VALID token and "
            f"asserts 401/403 does not test auth enforcement: it passes only "
            f"while the token happens to be stale, and reports success while "
            f"testing nothing. Establish the negative precondition in the "
            f"Given (no token / an invalid token / logged out), or retitle the "
            f"scenario to match what it actually tests."
        ),
    )]


def validate_test_case_quality(
    scenario_text: str,
    *,
    ac: dict | None = None,
    mapped_endpoints: list[dict] | None = None,
    is_api_typed: bool = False,
) -> list[GroundingViolation]:
    """R211 B2 — gen-time test-CASE quality for ONE Gherkin scenario.

    Emits violations (fed to the ATDD retry-with-hint loop) when:
      • `vague_assertion` — no Then/And step asserts a CONCRETE, fail-first
        outcome (exact status / number+unit / exact value / non-empty field).
        A test that cannot fail when the behavior is absent is worthless to a
        SUT-quality verdict.
      • `endpoint_ungrounded` — an API-typed scenario references no endpoint in
        the requirement's mapped real surface (set-membership via
        traceability_gate.path_matches_template).
    Killswitch: ARTA_R211_TESTCASE_QUALITY_DISABLE=1 (caller-side).
    """
    out: list[GroundingViolation] = []
    text = scenario_text or ""
    then_lines = [m.group(2).strip() for m in _R211_THEN_RE.finditer(text) if m.group(2).strip()]

    if then_lines:
        measurable = any(_r211_then_is_measurable(l) for l in then_lines)
        # fail-first: at least ONE Then/And must be concretely verifiable.
        if not measurable:
            sample = then_lines[0][:80]
            out.append(GroundingViolation(
                tool="gherkin", kind="vague_assertion", symbol=sample,
                location=(ac or {}).get("id", "scenario"),
                hint=("Then/And must assert a CONCRETE, fail-first outcome — an exact "
                      "HTTP status ('status is 200'), a number+unit ('under 2000ms'), an "
                      "exact value ('the status field equals \"active\"'), or a non-empty "
                      "named field. Replace vague outcomes ('it works', 'is logged in', "
                      "'loads gracefully', 'successfully')."),
            ))

    if is_api_typed and mapped_endpoints:
        try:
            from .traceability_gate import extract_test_endpoints, path_matches_template
        except Exception:
            extract_test_endpoints = path_matches_template = None  # type: ignore
        if extract_test_endpoints and path_matches_template:
            tmpls = [(e.get("path") if isinstance(e, dict) else e) for e in mapped_endpoints]
            tmpls = [t for t in tmpls if t]
            # pull any /api-ish path tokens the scenario references
            refs = set()
            for m in re.finditer(r"(/(?:api|v1|v2|rest|graphql)/[^\s'\"`)]+)", text, re.I):
                refs.add(m.group(1))
            if refs and not any(path_matches_template(r, t) for r in refs for t in tmpls):
                out.append(GroundingViolation(
                    tool="gherkin", kind="endpoint_ungrounded",
                    symbol=sorted(refs)[0][:80],
                    location=(ac or {}).get("id", "scenario"),
                    hint=("This API scenario references an endpoint outside the "
                          "requirement's mapped real surface. Target one of: "
                          + ", ".join(tmpls[:5])),
                ))
    return out


# ─── WS1: negative/security-test assertion-CLASS correctness ────────────────
_NEG_INTENT_RE = re.compile(
    r"\b(invalid|unauthenti\w*|unauthori\w*|without\s+auth|no[-\s]?auth\b|"
    r"missing\s+(auth|token|cred\w*|param\w*|header)|injection|sql[-\s]?inject\w*|"
    r"\bxss\b|tamper\w*|malformed|expired|revoked|forbidden|\bnegative\b|enumerat\w*|"
    r"rejected|denied|bad\s+(token|cred\w*|request|input)|no\s+token|"
    r"empty\s+(token|cred\w*|param\w*))",
    re.I,
)
# Fix 3 — SECURITY/SANITIZATION intent (injection/XSS/attack payloads). A well-built
# SUT SANITIZES the payload and returns 200 (safe handling), so these must NOT be
# forced to must-4xx like an auth-rejection test — a 4xx-only assertion FALSE-FAILS
# when the SUT correctly sanitizes and returns 200. The REAL injection/XSS signal is
# ZAP's job (dedicated tool); PW/Newman security tests are smoke ("didn't 5xx/crash").
_SEC_INTENT_RE = re.compile(
    r"\b(inject\w*|sql[-\s]?inject\w*|\bxss\b|cross[-\s]?site|malicious|adversari\w*|"
    r"payload|\battack\w*|tamper\w*|script\s*>|drop\s+table)\b",
    re.I,
)
_STATUS_ASSERT_RE = re.compile(
    r"(?:toBeOneOf|oneOf|toContain|to\.include|to\.contain|to\.have\.members)\s*\(\s*\[[^\]]*\]"
    r"|expect\s*\(\s*\[[^\]]*\]\s*\)\s*\.\s*(?:toContain|to\.include|to\.contain)"  # expect([...]).to(Contain|.include)
    r"|\.status\(\)\s*\)\s*\.\s*toBe\s*\(\s*\d{3}"              # expect(r.status()).toBe(NNN)
    r"|to\.have\.status\s*\(\s*\d{3}"                            # pm chai .to.have.status(NNN)
    r"|\bcode\b\s*(?:===|==|to\.equal\s*\(|toBe\s*\()\s*\d{3}",  # response code === NNN
    re.I,
)
_STATUS_NUM_RE = re.compile(r"\b([1-5]\d\d)\b")


def validate_negative_assertion_class(content: str, tool: str = "playwright") -> list[GroundingViolation]:
    """WS1 — a NEGATIVE/security/adversarial test MUST assert a 4xx (rejection)
    status, not 2xx.

    Catches the inversion the current gate misses: `_r211_then_is_measurable`
    accepts ANY `[1-5]\\d\\d`, so a scenario named "invalid token returns 401"
    that asserts `oneOf([200,201,204])` is "measurable" and ships — then
    FALSE-FAILS when the SUT CORRECTLY rejects with 4xx (live: ~45 such
    fails + the whole "SUT is right, test is wrong" class). Heuristic: read the
    per-test intent from the `test('<title>')` (PW) or `"name"` (Newman) label;
    if it signals a negative/rejection case AND the test's status assertion is
    2xx-ONLY, emit a violation → R57.1 retry-with-hint fixes it at gen.

    Conservative: only fires when the title is clearly negative-intent AND a
    concrete 2xx-only status assertion is present (no 4xx anywhere in the
    block). Positive tests and 4xx-tolerant assertions are untouched.
    Killswitch: ARTA_WS1_NEG_ASSERT_DISABLE=1 (caller-side).
    """
    out: list[GroundingViolation] = []
    if not content:
        return out
    # test/item labels: PW test('...'), or Newman collection "name": "..."
    label_re = (re.compile(r"test\s*\(\s*['\"`]([^'\"`]+)['\"`]") if tool != "newman"
                else re.compile(r"\"name\"\s*:\s*\"([^\"]+)\""))
    matches = list(label_re.finditer(content))
    for i, m in enumerate(matches):
        title = m.group(1)
        if not _NEG_INTENT_RE.search(title):
            continue
        # Fix 3 — security/sanitization tests (injection/XSS/payload) may legitimately
        # get a 200 (SUT sanitized). Do NOT force must-4xx; accept 4xx ∪ 200 → skip.
        if _SEC_INTENT_RE.search(title):
            continue
        body = content[m.end():(matches[i + 1].start() if i + 1 < len(matches) else len(content))]
        asserts = _STATUS_ASSERT_RE.findall(body)
        if not asserts:
            continue
        nums = {int(n) for a in asserts for n in _STATUS_NUM_RE.findall(a)}
        if not nums:
            continue
        has_4xx = any(400 <= n < 500 for n in nums)
        only_2xx = all(200 <= n < 300 for n in nums)
        if only_2xx and not has_4xx:
            out.append(GroundingViolation(
                tool=tool, kind="negative_test_asserts_2xx",
                symbol=title[:60], location=title[:80],
                hint=(f"NEGATIVE/security test \"{title[:50]}\" asserts a 2xx status "
                      f"{sorted(nums)} but its intent is to verify the SUT REJECTS the "
                      f"request — assert a 4xx class instead, e.g. "
                      f"expect([400,401,403,404,422]).toContain(response.status()). A "
                      f"2xx assertion FALSE-FAILS here when the SUT correctly rejects "
                      f"(the SUT is right; the test is wrong)."),
            ))
    return out


_R125_B_GHERKIN_TOKENS = ("Scenario:", "Scenario Outline:", "Background:", "Feature:")
_R125_B_GHERKIN_STEP_RE = re.compile(r"^\s*(Given|When|Then|And|But)\s+", re.MULTILINE)
_R125_B_TEST_BLOCK_RE = re.compile(r"\btest\s*(?:\.\w+)?\s*\(\s*['\"`]", re.MULTILINE)
_R125_B_COMMENT_LINE_RE = re.compile(r"^\s*(//|\*|/\*)")


def _r125_b_validate_gherkin_translation(content: str) -> list[GroundingViolation]:
    """R125.B — detect half-translated specs (raw Gherkin + no test() blocks).

    A spec with `Scenario:` / `Given:` / `When:` / `Then:` keywords OUTSIDE
    of comments BUT with zero `test(...)` blocks means the LLM truncated
    mid-translation (req_am_002 evidence). At dispatch, Playwright will
    find 0 tests + report `tests=0 returncode=1`.

    To avoid false positives on PW specs that legitimately quote Gherkin
    inside string literals (test names) or comments, we only count
    occurrences that:
      - Are at the start of a line (after optional whitespace)
      - Are NOT inside `//` or `/* */` comments
      - Are NOT inside string literals (heuristic: line has no quote
        before the keyword)
    """
    if not content or not content.strip():
        return []

    # Scan line-by-line, ignore comments + lines where Gherkin keyword
    # appears INSIDE a string (quoted before the keyword)
    has_unquoted_gherkin = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or _R125_B_COMMENT_LINE_RE.match(stripped):
            continue
        # Skip lines where keyword is inside a string literal:
        # `test('Given user logs in', ...)` should NOT count.
        for tok in _R125_B_GHERKIN_TOKENS:
            if tok in stripped and not _is_inside_quote_r125_b(stripped, tok):
                has_unquoted_gherkin = True
                break
        if has_unquoted_gherkin:
            break
        if _R125_B_GHERKIN_STEP_RE.match(stripped):
            # Step keyword at start of line, not inside test() body string
            if not _is_inside_quote_r125_b(stripped, stripped.split()[0]):
                has_unquoted_gherkin = True
                break

    has_test_blocks = bool(_R125_B_TEST_BLOCK_RE.search(content))

    if has_unquoted_gherkin and not has_test_blocks:
        return [GroundingViolation(
            tool="playwright",
            kind="incomplete_gherkin_translation",
            symbol="<spec_file>",
            location="line 1",
            hint=(
                "R125.B — Spec contains raw Gherkin keywords (Scenario:/Given/"
                "When/Then) but ZERO `test(...)` blocks. Playwright will find "
                "0 tests at dispatch (`tests=0 returncode=1`).\n\n"
                "The Gherkin scenarios MUST be TRANSLATED into TypeScript "
                "`test('name', async ({page}) => {...})` blocks. Each "
                "Given/When/Then becomes a sequence of page interactions + "
                "`expect()` assertions.\n\n"
                "BEFORE (BROKEN — verbatim Gherkin in .spec.ts):\n"
                "  Scenario: User creates organization\n"
                "    Given the user is logged in\n"
                "    When the user navigates to /organizations\n"
                "    Then the page title contains 'Organizations'\n\n"
                "AFTER (CORRECT — translated to Playwright test()):\n"
                "  import { test, expect } from '@playwright/test';\n"
                "  test('User creates organization', async ({ page }) => {\n"
                "    // Given the user is logged in (storage state preset)\n"
                "    await page.goto('/organizations');\n"
                "    // When the user navigates ...\n"
                "    // Then the page title contains 'Organizations'\n"
                "    await expect(page).toHaveTitle(/Organizations/);\n"
                "  });\n\n"
                "If you cannot translate, OMIT the Gherkin from the .spec.ts "
                "file entirely — keep it in the Gherkin/.feature file. Raw "
                "Gherkin in a TypeScript file is a syntax error."
            ),
        )]
    return []


def _is_inside_quote_r125_b(line: str, token: str) -> bool:
    """Return True if `token` appears inside a quoted string in `line`.

    Heuristic: count single + double quotes BEFORE the token's position.
    Odd count → token is inside a string literal.
    """
    idx = line.find(token)
    if idx < 0:
        return False
    prefix = line[:idx]
    # Strip escaped quotes
    prefix = prefix.replace("\\'", "").replace('\\"', "")
    return (prefix.count("'") % 2 == 1) or (prefix.count('"') % 2 == 1)


# ── R331 — vacuous-assertion blocker (a test must actually VERIFY the SUT) ────
# A test whose ONLY assertion is a tautology (`expect(true).toBeTruthy()`,
# `expect(1).toBe(1)`) PASSES trivially and verifies NOTHING — the antithesis of
# falls back to mock-the-endpoint (`page.route`) + a vacuous `expect(true)`,
# producing a guaranteed fake PASS. Killswitch ARTA_R331_ASSERTION_QUALITY_DISABLE=1.
_R331_LITERAL = r"true|false|null|undefined|-?\d+(?:\.\d+)?|['\"`][^'\"`]*['\"`]"
# expect(<literal>) . [not.] (no-arg-truthy-matcher | constant-matcher(<literal>))
_R331_TAUTOLOGY_RE = re.compile(
    r"expect\(\s*(?:" + _R331_LITERAL + r")\s*\)\s*\.\s*(?:not\s*\.\s*)?(?:"
    r"toBeTruthy\(\s*\)|toBeFalsy\(\s*\)|toBeDefined\(\s*\)|toBeUndefined\(\s*\)|toBeNull\(\s*\)|"
    r"toBe\(\s*(?:" + _R331_LITERAL + r")\s*\)|"
    r"toEqual\(\s*(?:" + _R331_LITERAL + r")\s*\)"
    r")"
)
_R331_EXPECT_RE = re.compile(r"\bexpect\s*\(")


def _iter_pw_test_blocks(content: str):
    """Yield (test_name, body_text) for each `test('name', ... => { body })`,
    brace-matched from the arrow body. Approximate (does not model strings/
    comments containing braces) but sufficient for per-test assertion heuristics."""
    for m in re.finditer(r"\btest(?:\.\w+)?\s*\(\s*(['\"`])(.*?)\1", content):
        name = m.group(2)
        arrow = content.find("=>", m.end())
        if arrow == -1:
            continue
        brace = content.find("{", arrow)
        if brace == -1:
            continue
        depth, i, n = 0, brace, len(content)
        while i < n:
            c = content[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        yield name, content[brace:i + 1]


def validate_playwright_assertion_quality(content: str) -> list[GroundingViolation]:
    """R331 — reject a PW test whose assertions are ALL tautologies (verifies
    nothing). Conservative: only flags when EVERY `expect(...)` in the test is a
    constant-vs-constant tautology (a test that mixes a real assertion with a
    vacuous one is left alone). Feeds R57.1 retry-with-hint → R102.A/C BLOCK."""
    import os as _os_r331
    if _os_r331.environ.get("ARTA_R331_ASSERTION_QUALITY_DISABLE") == "1" or not content:
        return []
    violations: list[GroundingViolation] = []
    for name, body in _iter_pw_test_blocks(content):
        total = len(_R331_EXPECT_RE.findall(body))
        if total == 0:
            continue  # no-assertion tests are handled by other validators
        tauto = len(_R331_TAUTOLOGY_RE.findall(body))
        if tauto >= 1 and tauto == total:
            mocks = "page.route(" in body
            violations.append(GroundingViolation(
                tool="playwright",
                kind="vacuous_assertion",
                symbol="expect(true).toBeTruthy()",
                location=name[:70],
                hint=(
                    "This test's ONLY assertion is a tautology that always passes and "
                    "verifies NOTHING about the SUT. Replace it with a REAL assertion on "
                    "the SUT's actual behavior: call the endpoint with `request.get(...)` "
                    "and `expect(resp.status()).toBe(200)` + assert on `await resp.json()`."
                    + (" You set up a `page.route` MOCK but never navigated to trigger it "
                       "or asserted the rendered UI — either drive the page (`page.goto` + "
                       "assert the DOM reflects the data) or drop the mock and hit the real "
                       "endpoint. Do NOT mock the SUT's API and assert `expect(true)`."
                       if mocks else " Never assert `expect(true).toBeTruthy()`.")
                ),
            ))
    return violations


def validate_playwright_api_usage(content: str) -> list[GroundingViolation]:
    """R95.3 — deterministic lint for Playwright API misuse patterns.

    Pre-R95.3 the LLM emitted Playwright code with invalid API calls
    that R42.1's selector grounding doesn't catch:
      - `_test.test.info(...).fixture` — `.info()` returns TestInfo
        which has no `.fixture` method (LLM hallucinated this 2x in
        run-2f077d)
      - `expect(<page-locator>).toBeOK()` — `toBeOK` only valid on
        `APIResponse` instances; page locators don't have it
      - `await page.fixture(...)` — `page` has no `.fixture` method

    Each match returns a GroundingViolation so the R42.1 retry loop
    fires + the LLM corrects on attempt 2.

    Returns empty list when no misuse found.
    """
    import re as _re_r95_3
    violations: list[GroundingViolation] = []

    # Pattern 1: _test.test.info(...).fixture or testInfo.fixture
    _info_fixture_re = _re_r95_3.compile(
        r"(?:_test\.test\.info\([^)]*\)|test\.info\([^)]*\)|testInfo)\.fixture\b"
    )
    for m in _info_fixture_re.finditer(content):
        violations.append(GroundingViolation(
            tool="playwright",
            kind="bad_playwright_api",
            symbol=m.group(0),
            location="playwright_spec",
            hint=(
                "TestInfo has no `.fixture` method.\n\n"
                "BEFORE (BROKEN — runtime TypeError):\n"
                "  const testInfo = _test.test.info();\n"
                "  const data = testInfo.fixture;   // TypeError: .fixture undefined\n\n"
                "AFTER 1 — inject fixture via test signature:\n"
                "  test('mytest', async ({ myFixture }) => {\n"
                "    expect(myFixture).toBeDefined();\n"
                "  });\n\n"
                "AFTER 2 — define a custom fixture via test.extend:\n"
                "  const test = baseTest.extend({\n"
                "    myFixture: async ({}, use) => { await use(...); },\n"
                "  });\n"
                "  test('mytest', async ({ myFixture }) => { ... });"
            ),
        ))

    # Pattern 2: expect(<page-side>).toBeOK() — toBeOK only valid for APIResponse.
    # Heuristic: scan all `.toBeOK()` calls; check the expect()'s receiver text
    # for page-locator patterns. Using a token-level scan instead of a single
    # regex so nested parens in page.locator('btn') don't trip us up.
    for _to_be_ok_match in _re_r95_3.finditer(r"\.toBeOK\(\s*\)", content):
        # Walk backwards to find the matching `expect(` opener
        _idx = _to_be_ok_match.start()
        _expect_idx = content.rfind("expect(", max(0, _idx - 400), _idx)
        if _expect_idx < 0:
            continue
        # Balanced-paren scan to find the closing `)` of expect(...)
        _depth = 0
        _in_str = False
        _str_ch = ""
        _close_idx = -1
        for _i in range(_expect_idx + len("expect"), _idx):
            _c = content[_i]
            if _in_str:
                if _c == _str_ch and content[_i-1] != "\\":
                    _in_str = False
                continue
            if _c in ("'", '"', "`"):
                _in_str = True
                _str_ch = _c
                continue
            if _c == "(":
                _depth += 1
            elif _c == ")":
                _depth -= 1
                if _depth == 0:
                    _close_idx = _i
                    break
        if _close_idx <= _expect_idx:
            continue
        # Receiver is content between `expect(` and matching `)`
        _receiver = content[_expect_idx + len("expect("):_close_idx].strip()
        # Page-locator patterns: `page.X`, `pageLocator`, `<x>Locator`,
        # `.getBy*(...)` on page, or assignments naming `locator` var.
        _is_page_side = (
            _receiver.startswith("page.")
            or _re_r95_3.search(r"\b\w*[Ll]ocator\b", _receiver) is not None
            or _receiver.startswith("page ")
        )
        # Common APIResponse-named vars (allow): response, resp, res, apiResponse
        _is_api_resp = bool(_re_r95_3.match(
            r"^(response|resp|res|apiResponse|api_resp)\b", _receiver,
        ))
        if _is_page_side and not _is_api_resp:
            _full = content[_expect_idx:_idx + len(_to_be_ok_match.group(0))]
            violations.append(GroundingViolation(
                tool="playwright",
                kind="bad_playwright_api",
                symbol=_full[:80],
                location="playwright_spec",
                hint=(
                    "`.toBeOK()` is valid only on APIResponse (from "
                    "`page.request.X(...)` or `request.X(...)` calls).\n\n"
                    "BEFORE (BROKEN — TypeError: toBeOK not defined on Locator):\n"
                    "  const btn = page.getByRole('button', { name: 'Save' });\n"
                    "  await expect(btn).toBeOK();\n\n"
                    "AFTER 1 — for APIResponse status:\n"
                    "  const resp = await page.request.post(`${apiBase}/api/save`);\n"
                    "  await expect(resp).toBeOK();\n\n"
                    "AFTER 2 — for page element visibility:\n"
                    "  const btn = page.getByRole('button', { name: 'Save' });\n"
                    "  await expect(btn).toBeVisible();\n\n"
                    "AFTER 3 — for page navigation status:\n"
                    "  await expect(page).toHaveURL(/\\/success/);"
                ),
            ))

    # Pattern 3: await page.fixture(...) — page has no `.fixture` method.
    _page_fixture_re = _re_r95_3.compile(r"(?:await\s+)?page\.fixture\b")
    for m in _page_fixture_re.finditer(content):
        violations.append(GroundingViolation(
            tool="playwright",
            kind="bad_playwright_api",
            symbol=m.group(0),
            location="playwright_spec",
            hint=(
                "`page.fixture()` does NOT exist on the Page object.\n\n"
                "BEFORE (BROKEN — TypeError: page.fixture is not a function):\n"
                "  test('mytest', async ({ page }) => {\n"
                "    const data = await page.fixture('userData');\n"
                "  });\n\n"
                "AFTER — inject the fixture via test signature destructure:\n"
                "  test('mytest', async ({ page, userData }) => {\n"
                "    // userData is now the fixture value\n"
                "  });\n\n"
                "If userData needs custom setup, define it via:\n"
                "  const test = baseTest.extend({\n"
                "    userData: async ({}, use) => { await use({ id: 1 }); },\n"
                "  });"
            ),
        ))

    # R101.C Pattern 4: `request.<httpVerb>(...)` or `request.newPage(...)` at
    # module scope — the LLM imports `request` from '@playwright/test' and
    # calls it like a fixture, but module-scoped `request` is an
    # APIRequest *factory* (only `.newContext()` is valid). HTTP verbs
    # require an APIRequestContext from `request.newContext()` OR the
    # `{ request }` fixture destructured in the test function arg.
    #
    # Heuristic: match `request.<verb>(` NOT preceded by `await` followed
    # by `page.` (which would be `await page.request.post(...)` — the
    # CORRECT pattern, since page.request is an APIRequestContext) AND
    # NOT preceded by `apiContext` / `ctx` / `apiRequestContext` /
    # `_ctx`. Pre-R101.C run-8c03c9 had 11 PW FAILs from this misuse:
    #   `await request.post(url, opts)` → "TypeError: request.post is not a function"
    #   `await request.newPage()` → same shape on req_am_001.
    _bad_request_re = _re_r95_3.compile(
        r"(?<![.])\brequest\.(?:newPage|get|post|put|patch|delete|head|options|fetch)\s*\("
    )
    # R101.C false-positive guard: if the file has at least one test
    # signature destructuring `{ request }` as a fixture, `request.X(...)`
    # calls inside the file are likely correct fixture usage. Suppress
    # the Pattern 4 check entirely for such files. The under-flag is
    # intentional: catching the misuse only when no `{ request }`
    # destructure exists is safe because the module-scope `request` (the
    # APIRequest factory) is the ONLY thing in scope without destructure.
    _file_uses_request_fixture = bool(_re_r95_3.search(
        r"async\s*\(\s*\{[^}]*\brequest\b[^}]*\}", content,
    ))
    for m in _bad_request_re.finditer(content):
        if _file_uses_request_fixture:
            break   # entire pattern suppressed for fixture-style files
        # Skip if `page.request.<verb>(` (correct usage)
        _ctx_start = max(0, m.start() - 10)
        _ctx = content[_ctx_start:m.start()]
        if _ctx.rstrip().endswith("page.") or _ctx.rstrip().endswith(".request"):
            continue
        violations.append(GroundingViolation(
            tool="playwright",
            kind="bad_playwright_api",
            symbol=m.group(0).rstrip("("),
            location="playwright_spec",
            hint=(
                "`request` imported from '@playwright/test' is an APIRequest "
                "FACTORY, not an HTTP client. Direct calls like "
                "`request.post(url)` crash at runtime with "
                "\"TypeError: _test.request.post is not a function\".\n\n"
                "BEFORE (BROKEN — runtime TypeError):\n"
                "  import { test, expect, request } from '@playwright/test';\n"
                "  test('foo', async ({ page }) => {\n"
                "    const resp = await request.post(`${base}/api`, "
                "{ data: {...} });\n"
                "  });\n\n"
                "AFTER 1 — REMOVE `request` import + use `page.request`:\n"
                "  import { test, expect } from '@playwright/test';\n"
                "  test('foo', async ({ page }) => {\n"
                "    const resp = await page.request.post(`${base}/api`, "
                "{ data: {...} });\n"
                "  });\n\n"
                "AFTER 2 — destructure `{ request }` fixture (Playwright "
                "injects an APIRequestContext):\n"
                "  import { test, expect } from '@playwright/test';\n"
                "  test('foo', async ({ page, request }) => {\n"
                "    const resp = await request.post(`${base}/api`, "
                "{ data: {...} });\n"
                "  });\n\n"
                "Note: `request.newPage()` does NOT exist. Use "
                "`browser.newPage()` if you need a fresh page context (rare; "
                "the `{ page }` fixture handles this)."
            ),
        ))

    # R115.B Pattern 4b: `request(url)` — invoking `request` AS A FUNCTION
    # (not as namespace `request.X(...)`). Babel compiles this to
    # `(0, _test.request)(url)` which fails at runtime with
    # `TypeError: (0, _test.request) is not a function` because the
    # imported `request` symbol from '@playwright/test' is an APIRequest
    # factory namespace, NOT a callable.
    #
    # Pre-R115.B: run-8da91d req_am_015 had 2 PW FAILs from this exact
    # pattern (TC-AM-015-AUTO001/002: `TypeError: (0, _test.request) is
    # not a function`). R101.C Pattern 4 only caught `request.<verb>(`,
    # not the bare `request(` invocation form.
    #
    # Heuristic: match `request\s*\(` where the next char is a URL-like
    # quote/backtick/template (not `.` which would be method access).
    # Also require `request` is imported from '@playwright/test'.
    _file_imports_request_from_playwright = bool(_re_r95_3.search(
        r"import\s*\{[^}]*\brequest\b[^}]*\}\s*from\s*['\"]@playwright/test['\"]",
        content,
    ))
    if _file_imports_request_from_playwright and not _file_uses_request_fixture:
        # Match: bare `request(` not preceded by `.` or `page.` or identifier.
        # Exclude `request.X(` (Pattern 4) by requiring NEXT char is quote/`/`/{.
        _bare_request_call_re = _re_r95_3.compile(
            r"(?<![.\w])request\s*\(\s*[`'\"\{]",
        )
        for m in _bare_request_call_re.finditer(content):
            violations.append(GroundingViolation(
                tool="playwright",
                kind="bad_playwright_api",
                symbol="bare_request_invocation:request(...)",
                location="playwright_spec",
                hint=(
                    "R115.B: `request(url)` invokes the APIRequest factory AS "
                    "A FUNCTION, which crashes at runtime with `TypeError: "
                    "(0, _test.request) is not a function`. The `request` "
                    "symbol from '@playwright/test' is a NAMESPACE, not a "
                    "callable. Use it via `page.request.X(...)` OR destructure "
                    "the `{ request }` fixture (which Playwright injects as "
                    "an APIRequestContext — callable via `.X(...)` methods).\n\n"
                    "BEFORE (BROKEN — run-8da91d req_am_015 TC-AM-015-AUTO001/2):\n"
                    "  import { test, request } from '@playwright/test';\n"
                    "  test('foo', async ({ page }) => {\n"
                    "    const resp = await request('/api/x');  // ← TypeError\n"
                    "  });\n\n"
                    "AFTER 1 (CORRECT) — use page.request namespace:\n"
                    "  import { test } from '@playwright/test';\n"
                    "  test('foo', async ({ page }) => {\n"
                    "    const resp = await page.request.get('/api/x');\n"
                    "  });\n\n"
                    "AFTER 2 (CORRECT) — destructure `{ request }` fixture "
                    "(Playwright injects an APIRequestContext):\n"
                    "  import { test } from '@playwright/test';\n"
                    "  test('foo', async ({ page, request }) => {\n"
                    "    const resp = await request.get('/api/x');\n"
                    "  });"
                ),
            ))

    # R101.C Pattern 5: `await use(...)` inside `test.beforeEach`/`afterEach`/
    # `test()` hook callbacks. `use` is ONLY a valid fixture-control argument
    # in fixture definitions inside `test.extend({...})`. LLM emits:
    #   test.beforeEach(async ({ browser }, use) => {
    #     page = await browser.newPage();
    #     await use(page);   // ← runtime TypeError: use is not a function
    #   });
    # Match: hook signature with `, use)` AND `await use(` body call.
    _hook_use_re = _re_r95_3.compile(
        r"(?:test\.before(?:Each|All)|test\.after(?:Each|All)|test\s*\(\s*['\"`][^'\"`]*['\"`]\s*,)"
        r"\s*\(?\s*async\s*\(\s*\{[^}]*\}\s*,\s*use\s*\)",
    )
    for m in _hook_use_re.finditer(content):
        violations.append(GroundingViolation(
            tool="playwright",
            kind="bad_playwright_api",
            symbol=m.group(0)[:80],
            location="playwright_spec",
            hint=(
                "`use` is only valid in fixture definitions, not in "
                "beforeEach/afterEach/test callbacks. Runtime crashes "
                "with `TypeError: use is not a function`.\n\n"
                "BEFORE (BROKEN):\n"
                "  test.beforeEach(async ({ page }, use) => {\n"
                "    await seedData(page);\n"
                "    await use();\n"
                "    await cleanupData(page);\n"
                "  });\n\n"
                "AFTER (CORRECT — split setup/teardown into paired hooks):\n"
                "  test.beforeEach(async ({ page }) => {\n"
                "    await seedData(page);\n"
                "  });\n"
                "  test.afterEach(async ({ page }) => {\n"
                "    await cleanupData(page);\n"
                "  });\n\n"
                "If you need a FIXTURE with lifecycle, use test.extend:\n"
                "  const test = baseTest.extend({\n"
                "    seededPage: async ({ page }, use) => {\n"
                "      await seedData(page); await use(page); await cleanupData(page);\n"
                "    }\n"
                "  });"
            ),
        ))

    # R101.D Pattern 6: `getByRole('role', { name: 'concatenated body text' })`
    # where the `name` contains smushed multi-word phrases (camelCase mid-string
    # OR multiple sentence fragments joined without space). The LLM saw multiple
    # DOM text nodes (e.g. heading + paragraph) and concatenated them into one
    # name string — but `getByRole` matches the ARIA accessible-name (single
    # attribute), not body text. Live evidence (run-8c03c9 TC-AM-006):
    #   → "Select an AI en" is the suffix of a different element's text.
    # Heuristic: flag a `name` string that contains a lowercase letter followed
    # by an uppercase letter mid-word (e.g. "CloudSelect") OR a sentence
    # fragment ending in lowercase mid-string ("AI en" + cut-off).
    _smushed_role_re = _re_r95_3.compile(
        r"getByRole\(\s*['\"`]([a-z]+)['\"`]\s*,\s*\{\s*name:\s*['\"`]([^'\"`]{20,})['\"`]\s*\}",
    )
    for m in _smushed_role_re.finditer(content):
        _name = m.group(2)
        # Sniff: lowercase-uppercase boundary mid-string (e.g. "cloudSelect")
        # OR cut-off mid-word (ends with a partial word like "AI en")
        _has_cc_boundary = bool(_re_r95_3.search(r"[a-z][A-Z]", _name))
        _looks_truncated = (
            _re_r95_3.search(r"\s+[a-z]{1,3}$", _name) is not None
            and not _name.endswith(("y", "s", "ed"))
        )
        if _has_cc_boundary or _looks_truncated:
            violations.append(GroundingViolation(
                tool="playwright",
                kind="bad_playwright_api",
                symbol=m.group(0)[:100],
                location="playwright_spec",
                hint=(
                    "`getByRole(role, { name })` matches a SINGLE ARIA "
                    "accessible-name, not concatenated body text. The name "
                    f"`{_name[:60]}...` looks smushed from multiple elements.\n\n"
                    "BEFORE (BROKEN — LLM concatenated heading + paragraph text):\n"
                    "  page.getByRole('main', { name: 'Welcome to Acme AI CloudSelect an AI en' })\n\n"
                    "AFTER (CORRECT — pick ONE catalog entry, OR use getByText for body text):\n"
                    "  page.getByRole('heading', { name: 'Welcome to Acme AI Cloud' })\n"
                    "  page.getByText('Select an AI engine')  // body text content\n\n"
                    "Rules:\n"
                    "1. Use the EXACT catalog role+name pair (one string from "
                    "the DOM catalog).\n"
                    "2. For non-role content, use getByText('<exact fragment>').\n"
                    "3. NEVER concatenate text across separate DOM elements."
                ),
            ))

    # R125.A — fixture-parameter validator. LLM emits non-PW fixtures in
    # `test(..., async ({page, ordersResp, usersResp}) => ...)` destructure;
    # Playwright raises `Test has unknown parameter "ordersResp"` at dispatch.
    # 45 such errors in pw-run-12764a-req_am_002.spec.json (run-d52a8c era).
    violations.extend(_r125_a_validate_fixture_params(content))

    return violations


# R125.A — fixture-parameter validator constants + helper.
# Single source of truth for the PW built-in fixture allow-list.
_R125_A_PW_BUILTIN_FIXTURES = frozenset({
    "page", "context", "request", "browser", "browserName",
    "storageState", "viewport", "userAgent", "testInfo",
    "playwright", "headless", "baseURL", "channel",
    "video", "trace", "screenshot",
})

_R125_A_FIXTURE_DESTRUCTURE_RE = re.compile(
    r"\b(?:test|test\.(?:before|after)(?:Each|All))\s*\("
    r"(?:\s*['\"][^'\"]*['\"]\s*,\s*)?"
    r"async\s*\(\s*\{\s*([^}]+?)\s*\}",
    re.MULTILINE | re.DOTALL,
)


def _r125_a_validate_fixture_params(content: str) -> list[GroundingViolation]:
    """R125.A — flag fixture destructures that reference names not in the PW
    built-in set nor declared via `test.extend({...})`.

    Pre-R125.A: LLM emitted `async ({page, ordersResp, usersResp})` in
    test() callbacks → Playwright runtime errors `Test has unknown
    parameter "ordersResp"` at dispatch (req_am_002 evidence, 45 errors
    in a single spec). The existing `validate_playwright_api_usage`
    checked `.toBeOK` / `request.X` / `testInfo.fixture` but NOT the
    test/hook signature fixture names.

    Returns one violation per distinct unknown fixture name (first
    occurrence only — deduped via `seen` set).
    """
    declared: set[str] = set(_R125_A_PW_BUILTIN_FIXTURES)
    # Collect operator-declared fixtures. The canonical shape is
    # `.extend({foo: async ({}, use) => ..., bar: ...})` but the body can
    # contain nested braces (callback bodies), so balanced-brace parsing
    # would be expensive. Use a permissive heuristic: every top-level
    # `<name>: async (` ANYWHERE in the file declares a fixture. False
    # positives (object literals with same shape) are rare in PW specs.
    for fix_match in re.finditer(r"(\w+)\s*:\s*async\s*\(", content):
        declared.add(fix_match.group(1))
    out: list[GroundingViolation] = []
    seen: set[str] = set()
    for m in _R125_A_FIXTURE_DESTRUCTURE_RE.finditer(content):
        raw = m.group(1)
        # Split on commas + whitespace; drop typing annotations after ':'
        for part in re.split(r"[,\s]+", raw.strip()):
            name = part.split(":")[0].strip()
            # Skip empty strings, rest-spread (...), and trailing commas
            if not name or name.startswith("..."):
                continue
            if name in declared or name in seen:
                continue
            seen.add(name)
            line_num = content[:m.start()].count("\n") + 1
            out.append(GroundingViolation(
                tool="playwright",
                kind="unknown_fixture_parameter",
                symbol=name,
                location=f"line {line_num}",
                hint=(
                    f"`{name}` is NOT a Playwright built-in fixture nor "
                    f"declared via `test.extend({{...}})`. Playwright will "
                    f"raise `Test has unknown parameter \"{name}\"` at "
                    f"dispatch.\n\n"
                    f"BEFORE (BROKEN — runtime fixture-resolution error):\n"
                    f"  test('foo', async ({{ page, {name} }}) => {{ ... }});\n\n"
                    f"AFTER 1 — use page.route() for response mocking instead:\n"
                    f"  test('foo', async ({{ page }}) => {{\n"
                    f"    await page.route('**/api/x', (route) => route.fulfill({{\n"
                    f"      json: {{ /* mock response */ }}\n"
                    f"    }}));\n"
                    f"  }});\n\n"
                    f"AFTER 2 — declare the fixture explicitly via test.extend():\n"
                    f"  const test = baseTest.extend({{\n"
                    f"    {name}: async ({{}}, use) => {{ await use(/* value */); }},\n"
                    f"  }});\n"
                    f"  test('foo', async ({{ page, {name} }}) => {{ ... }});\n\n"
                    f"Allowed built-ins: page, context, request, browser, "
                    f"browserName, storageState, viewport, testInfo, baseURL."
                ),
            ))
    return out


_PW_API_REQUEST_RE = re.compile(
    # Match `page.request.<verb>(...)` OR `request.<verb>(...)` (where the
    # `request` is the fixture, not the module-import — Pattern 4 above
    # catches the module-scope misuse). Capture (method, url-string).
    #
    # R291 — allow an OPTIONAL `apiUrlFor(` wrapper before the quoted path.
    # R207.B/R210 rewrote happy-path requests to `request.post(apiUrlFor('/x'))`
    # so each family gets the right token — but this regex required the URL to be
    # a quote IMMEDIATELY after the paren, so `apiUrlFor(` broke the match and
    # endpoint+method grounding was SILENTLY SKIPPED for the modern form. Effect
    # (live, run-771720): a spec POSTing a GET-only endpoint
    # (`GetAccountRelationshipHierarchy`) passed grounding and 405'd at runtime,
    # and fabricated `apiUrlFor` paths passed too. The `(?:page\.)?request.<verb>`
    # capture already carries the METHOD, and downstream only matches captured
    # endpoints of the SAME verb — so once the path is extracted, a wrong-method
    # call becomes an unknown_endpoint (there are no same-verb captures for it).
    r"(?:page\.)?request\.(get|post|put|patch|delete|head|options|fetch)\s*\(\s*"
    r"(?:apiUrlFor\s*\(\s*)?"          # R291 — optional apiUrlFor(...) wrapper
    r"[`'\"]([^`'\"]+)[`'\"]",
    re.IGNORECASE,
)


# R118.E.2 — when `getByRole('<role>', ...)` references a role fully
# absent from the SUT's DOM catalog (not just the specific name absent),
# offer a concrete alternative selector strategy. Pre-R118.E.2 the
# R78.6 hint said "use a role from the catalog" but didn't tell the LLM
# what to do when the desired role (e.g., 'textbox' for `<input>`)
# doesn't exist at all in the SUT. The LLM then defaults to re-emitting
# the same hallucinated role on retry. R118.E.2 maps common semantic
# roles → concrete fallback selector recipes so the LLM has a positive
# recovery path.
_R118_E_2_ROLE_TO_FALLBACK: dict[str, str] = {
    "textbox": "page.getByLabel('<form label text>')  // or page.locator('input[type=email]')",
    "cell": "page.getByText('<cell value>')  // or page.locator('td').nth(N)",
    "gridcell": "page.getByText('<cell value>')  // or page.locator('[role=gridcell]')",
    "row": "page.locator('tr').nth(N)  // or filter by row content via .filter({ hasText: '...' })",
    "table": "page.locator('table')  // or page.locator('[role=table]') if present",
    "listbox": "page.locator('ul').filter({ hasText: '...' })  // or page.locator('[role=listbox]')",
    "combobox": "page.getByLabel('<dropdown label>')  // or page.locator('select')",
    "dialog": "page.locator('[role=dialog]')  // or page.locator('.modal, [aria-modal=true]')",
    "menu": "page.locator('[role=menu]')  // or page.locator('nav, ul.menu')",
    "tab": "page.getByRole('button', { name: '<tab name>' })  // tabs often render as buttons",
    "tabpanel": "page.locator('[role=tabpanel]')  // or use the tab name + assert visible content",
    "checkbox": "page.locator('input[type=checkbox]')  // or page.getByLabel('<label>')",
    "radio": "page.locator('input[type=radio]')  // or page.getByLabel('<option label>')",
    "switch": "page.locator('[role=switch]')  // or page.locator('input[type=checkbox]')",
    "slider": "page.locator('input[type=range]')  // or page.locator('[role=slider]')",
    "spinbutton": "page.locator('input[type=number]')  // or page.getByLabel('<numeric field>')",
    "progressbar": "page.locator('[role=progressbar]')  // or page.locator('progress')",
    "tree": "page.locator('[role=tree]')  // or page.locator('ul.tree, [aria-multiselectable]')",
    "treeitem": "page.locator('[role=treeitem]')  // or page.getByText('<item label>')",
    "tooltip": "page.locator('[role=tooltip]')  // or trigger hover then assert visible text",
}


def _r117_b_split_smushed_name(name: str) -> list[str]:
    """R117.B — split a smushed `name` string into its likely
    single-element fragments.

    Smushing comes from the discovery probe capturing raw `textContent`
    of a multi-element subtree (e.g., `<main>` landmark containing a
    heading + paragraph + button → "Welcome to Acme AI CloudSelect an
    AI engine to get startedEXTRACT"). The LLM faithfully emits this
    as `getByRole('main', { name: '<smushed>' })`.

    Split strategy (priority order):
      1. camelCase boundary (`a-z` immediately followed by `A-Z`) —
         the dominant smushing signature (e.g., 'CloudSelect')
      2. Multi-sentence boundary (`. ` followed by capital)
      3. Multiple-whitespace boundary (rare)

    Returns a list of likely fragments. If the name has NO smushing
    signature, returns `[name]` (single-fragment passthrough). Caller
    typically uses the FIRST fragment as `getByText('<first-fragment>')`
    alternative.

    Examples:
      'Welcome to Acme AI CloudSelect an AI en'
        → ['Welcome to Acme AI Cloud', 'Select an AI en']
      'Click here. Then submit'
        → ['Click here', 'Then submit']
      'Submit'
        → ['Submit']
    """
    if not isinstance(name, str) or not name.strip():
        return []
    s = name.strip()
    # Split at camelCase boundaries (insert split-marker between a-z and A-Z)
    s_camel_split = re.sub(r"([a-z])([A-Z])", r"\1\n\2", s)
    if "\n" in s_camel_split:
        frags = [f.strip() for f in s_camel_split.split("\n") if f.strip()]
        return frags
    # Split at multi-sentence boundary
    if re.search(r"\.\s+[A-Z]", s):
        frags = [f.strip().rstrip(".") for f in re.split(r"\.\s+(?=[A-Z])", s) if f.strip()]
        return frags
    # No smushing signature → single fragment
    return [s]


# ────────────────────────────────────────────────────────────────────────────
# R150.E — Playwright response-assertion field grounding
# ────────────────────────────────────────────────────────────────────────────


# Match patterns like:
#   expect(json.insight.metric).toEqual(...)
#   expect(response.json().data.value).toBe(...)
#   expect(json.records[0].id).toMatchObject(...)
# Captures the dotted path AFTER the json/response anchor.
# R275 — JavaScript intrinsics that are NOT SUT response fields. Deliberately
# tiny: only members every JS array/string/Map carries, which therefore can
# never appear in a captured response shape. Anything broader would start
# suppressing real hallucinated-field detections.
_R275_JS_INTRINSICS = frozenset({"length", "size"})

_R150_E_PW_ASSERT_RE = re.compile(
    r"expect\(\s*"
    r"(?:json|responseData|resp|data|responseJson|response\.json\(\s*\))"
    r"\s*\.\s*"
    r"([a-zA-Z_][\w]*(?:(?:\.[a-zA-Z_][\w]*)|(?:\[\s*\d+\s*\]))*)"
    r"\s*\)\s*\."
    r"(?:toEqual|toBe|toMatchObject|toHaveProperty|toContain|toMatch|toBeNull|toBeUndefined|toBeDefined|toBeTruthy|toBeFalsy)\b"
)


def _r150_e_collect_grounded_paths(
    captured_endpoints: list[dict] | None,
    expected_outputs: dict | None,
) -> set[str]:
    """R150.E — union all dotted-path field references the SUT actually
    returns OR the recipe declares. Used as the grounding set for PW
    response-assertion validation.

    Sources:
      (a) every captured_endpoint.response_body_shape → expanded via
          `_r150_g_extract_nested_paths` (reused from R150.G).
      (b) recipe.expected_outputs keys (flat dict of column_name → value);
          each key is added as a top-level grounded path.

    Returns empty set when both sources empty (cold-start safe).
    """
    grounded: set[str] = set()
    for ep in captured_endpoints or []:
        if not isinstance(ep, dict):
            continue
        shape = ep.get("response_body_shape")
        if not shape:
            continue
        # Wrap shape into the {content: {application/json: {schema}}}
        # envelope that _r150_g_extract_nested_paths expects.
        synthetic_resp = {
            "content": {"application/json": {"schema": shape}}
        }
        grounded |= _r150_g_extract_nested_paths(synthetic_resp, {})

    if isinstance(expected_outputs, dict):
        for key in expected_outputs.keys():
            if isinstance(key, str):
                grounded.add(key)

    return grounded


def _r150_e_validate_pw_response_assertions(
    content: str,
    *,
    captured_endpoints: list[dict] | None,
    expected_outputs: dict | None = None,
) -> list[GroundingViolation]:
    """R150.E — flag PW `expect(json.<path>).toEqual(...)` assertions where
    the dotted path doesn't exist in any captured SUT response shape OR in
    the recipe's expected_outputs.

    Mission: closes Iter 9 dominant cluster (23 × waitForResponse timeout
    + ~41 × locator-timeout-on-invented-field): the LLM emitted PW
    assertions like `expect(json.insight.metric).toEqual('sales')` where
    the SUT's actual response shape uses different field paths → the
    assertion fails at runtime, often with cryptic timeout errors when
    the assertion is inside a waitForResponse callback.

    Parallel to R111.G (Newman pm.test field grounding). Both consume the
    same R150.A populated `response_body_shape` data.

    Killswitch: `ARTA_R150_E_PW_ASSERT_VALIDATOR_DISABLE=1`.

    Conservative skip: when grounded set is empty (cold-start, no
    response shapes captured yet), returns [] — never blocks a project
    that hasn't completed discovery refresh.
    """
    import os as _os_r150e
    if _os_r150e.environ.get("ARTA_R150_E_PW_ASSERT_VALIDATOR_DISABLE") == "1":
        return []
    if not content:
        return []

    grounded = _r150_e_collect_grounded_paths(captured_endpoints, expected_outputs)
    if not grounded:
        return []   # cold-start safe — never flag with empty signal

    # Sample alternatives for hint (deterministic order; top-6 by length)
    _alts = sorted(grounded, key=lambda p: (-len(p), p))[:6]

    violations: list[GroundingViolation] = []
    seen_paths: set[str] = set()
    for m in _R150_E_PW_ASSERT_RE.finditer(content):
        raw_path = m.group(1)
        # Normalize array-index references: `records[0].id` → `records.id`
        # to match the dotted-path convention from
        # `_r150_g_extract_nested_paths` which descends into array items
        # WITHOUT introducing index notation.
        norm_path = re.sub(r"\[\s*\d+\s*\]", "", raw_path)
        if norm_path in seen_paths:
            continue
        seen_paths.add(norm_path)

        # R275 — JS INTRINSICS are not SUT response fields.
        #
        # `expect(data.length).toBe(3)` on an array response is ordinary,
        # correct JavaScript, but this check treated `length` as a field path
        # and demanded it appear in a captured response shape — so it can never
        # trace=628bba4f): `pw_assertion_field_unknown: ['length']` was the LAST
        # blocker after the catalog work had cleared unknown_endpoint AND
        # semantic_intent_mismatch.
        #
        # ARTA contradicts itself here: R220.A exists to make exactly these
        # `.length` assertions null-safe (`?? []`), i.e. the platform EXPECTS
        # the pattern that this validator rejects.
        #
        # Suppression is safe: a SUT field genuinely NAMED `length` still passes
        # via the grounded check below, so this only removes the false positive.
        # Trailing-segment match, so a real nested field like `payload.length_cm`
        # is untouched. Killswitch ARTA_R275_JS_INTRINSIC_EXEMPT_DISABLE=1.
        if os.environ.get("ARTA_R275_JS_INTRINSIC_EXEMPT_DISABLE") != "1":
            if norm_path.split(".")[-1] in _R275_JS_INTRINSICS:
                continue

        # Accept the assertion if:
        #   (a) exact full-path match in grounded set, OR
        #   (b) any longer grounded path starts with this path + "."
        #       (intermediate path access — `records` when `records.id`
        #        is grounded). Mirrors R150.G's intermediate-prefix logic.
        is_grounded = (
            norm_path in grounded
            or any(g.startswith(norm_path + ".") for g in grounded)
        )
        if is_grounded:
            continue

        line_no = content[:m.start()].count("\n") + 1
        violations.append(GroundingViolation(
            tool="playwright",
            kind="pw_assertion_field_unknown",
            symbol=norm_path,
            location=f"line {line_no}",
            hint=(
                f"`expect(json.{norm_path}).toEqual(...)` references a field "
                f"path NOT in any captured SUT response shape OR the recipe's "
                f"expected_outputs.\n\n"
                f"BEFORE (BROKEN — assertion will fail at runtime; the SUT "
                f"does NOT return this field):\n"
                f"  expect(json.{norm_path}).toEqual('...');\n\n"
                f"AFTER 1 — use a path that exists in the SUT's captured "
                f"response shapes (R45.3 + R150.A populated):\n"
                f"  // Available grounded paths: {_alts}\n"
                f"  expect(json.{_alts[0] if _alts else '<grounded_path>'})"
                f".toEqual('...');\n\n"
                f"AFTER 2 — if the requirement's recipe.expected_outputs "
                f"declares this field, ensure the SUT actually returns it; "
                f"if not, the recipe needs regeneration (R150.A discovery "
                f"refresh + R150.B grounded sample-payload hint)."
            ),
        ))

    return violations


# ── R154.B — destructive-pattern blocker (Pillar 1+1b non-mutation guarantee)
# Mission: ATDD's *report SUT quality without affecting it* directive demands
# that generated test scripts NEVER include destructive operations against
# the SUT by default. R154.A guarded the probe phase; R154.B guards the gen
# phase; R154.C guards the dispatch phase. Together they form a 3-stage
# structural non-mutation defense.
#
# Patterns rejected (default-deny):
#   - Direct HTTP method calls: request.post/put/patch/delete + page.request.*
#   - Newman/Postman destructive methods: "POST"/"PUT"/"PATCH"/"DELETE" in spec
#   - Form submission / state mutation: page.fill, checkbox/radio selectors,
#     submit-type buttons
#   - Pytest destructive verbs: create_/delete_/update_/insert_ helper calls
#
# Opt-in marker: `@intentional-destructive` in spec header (first 5 lines)
# exempts the spec from this validator. R154.C dispatch-time gate further
# requires SUT_TEST_DATA_NAMESPACE env var when the marker is present.

_R154_B_OPT_IN_MARKER = "@intentional-destructive"

_R154_B_DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # (regex, kind, description)
    (
        re.compile(r"\brequest\.(post|put|patch|delete)\s*\("),
        "destructive_http_method",
        "request.{post,put,patch,delete}() — mutates SUT state",
    ),
    (
        re.compile(r"\bpage\.request\.(post|put|patch|delete)\s*\("),
        "destructive_http_method",
        "page.request.{post,put,patch,delete}() — mutates SUT state",
    ),
    (
        re.compile(r"\bpage\.fill\s*\("),
        "destructive_form_fill",
        "page.fill() populates form input — typically precedes form submit",
    ),
    (
        re.compile(r"\bgetByRole\s*\(\s*['\"]checkbox['\"]\s*"),
        "destructive_state_toggle",
        "getByRole('checkbox') click mutates app state",
    ),
    (
        re.compile(r"\bgetByRole\s*\(\s*['\"]radio['\"]\s*"),
        "destructive_state_toggle",
        "getByRole('radio') click mutates app state",
    ),
    (
        re.compile(r"\btype\s*=\s*['\"]submit['\"]"),
        "destructive_submit_button",
        "type=\"submit\" button submits form / mutates SUT",
    ),
    # Pytest destructive helpers (analytics tests)
    (
        re.compile(r"\b(create|delete|update|insert)_[a-z]"),
        "destructive_analytics_op",
        "destructive Pytest helper (create_/delete_/update_/insert_)",
    ),
]


def _r154_b_has_opt_in_marker(content: str) -> bool:
    """Check the first 5 lines of `content` for the opt-in marker
    `@intentional-destructive`. When present, the spec is exempt from
    R154.B destructive-pattern rejection (R154.C dispatch-time gate
    still requires SUT_TEST_DATA_NAMESPACE env var).
    """
    if not content:
        return False
    head = content.split("\n", 5)[:5]
    return any(_R154_B_OPT_IN_MARKER in line for line in head)


# R242 — auth-endpoint exemption for the R154 destructive-HTTP-method guard.
# Login / logout / token / refresh / SSO / OAuth POSTs are the MECHANISM of
# session-refresh — literally REQUIRE them), NOT business-data mutations. R154's
# non-mutation guarantee protects SUT *business state*; an auth handshake
# establishes / rotates / ends a session and is safe to exercise. Pre-R242 the
# guard matched ANY `request.post(` → every auth-flow spec was rejected at gen
# (escalation storm) AND blocked at dispatch (never ran) → auth reqs unmeasurable.
# R242 flags a destructive_http_method ONLY when a NON-auth mutation call exists.
_R154_AUTH_URL_RE = re.compile(
    r"(?:login|log-?in|logout|log-?out|signin|sign-?in|signout|sign-?out|"
    r"\bsso\b|oauth2?|/token|refresh|/auth\b|authenticate|authorize|"
    r"\bsession\b|connect/token)",
    re.IGNORECASE,
)
_R154_HTTP_MUTATION_CALL_RE = re.compile(
    r"(?:page\.)?request\.(?:post|put|patch|delete)\s*\(\s*"
    r"([`'\"][^`'\"]*[`'\"]|[^,)]+)"
)


def _r154_b_nonauth_mutation_match(content: str, read_post_allowlist=None):
    """R242 — return the first `request.{post,put,patch,delete}()` (or
    `page.request.*`) call site whose URL is NOT an auth endpoint (a genuine
    business-state mutation), or None when EVERY HTTP-mutation call targets an
    auth endpoint (login/token/refresh/sso/…) — in which case R154 must not
    flag `destructive_http_method`. Killswitch ARTA_R154_AUTH_EXEMPT_DISABLE=1
    reverts to pre-R242 behavior (any mutation call counts).

    R278 — `read_post_allowlist`: exact paths the OPERATOR has named as
    read-only POSTs. Same exemption logic as R242's auth carve-out and the same
    argument: this SUT exposes READS as POST, so a POST to a named read endpoint
    is not a mutation, it is the only way to read.

    Why the mechanism is needed at all: one live AC states
    `POST /Reefer/api/getReeferStatusData is called ... then HTTP 200 with data[]
    rows`. R154.B rejected `request.post` unconditionally, so that requirement
    was UNGENERABLE — the retry ladder could never produce a spec that both
    satisfies the AC and passes the validator. R266 solved this for the PROBE
    (network layer); gen had no equivalent.

    Deliberately NOT inferred from the verb or the path name: a `get*`-named
    POST can still mutate (`getOrCreateX`), so the list stays operator-named —
    consistent with the R266 decision. Empty/absent list → unchanged behaviour.
    """
    import os as _os_r242
    if _os_r242.environ.get("ARTA_R154_AUTH_EXEMPT_DISABLE") == "1":
        return _R154_HTTP_MUTATION_CALL_RE.search(content)
    _allow = [a for a in (read_post_allowlist or []) if isinstance(a, str) and a.startswith("/")]
    _r278_on = os.environ.get("ARTA_R278_GEN_READ_POST_EXEMPT_DISABLE") != "1"
    for m in _R154_HTTP_MUTATION_CALL_RE.finditer(content):
        url = m.group(1) or ""
        if _R154_AUTH_URL_RE.search(url):
            continue                       # R242 — auth endpoint
        if _r278_on and _allow:
            # Exempt ONLY the POST verb: a named read endpoint is still never a
            # legitimate target for PUT/PATCH/DELETE.
            _is_post = ".post(" in (m.group(0) or "").lower()
            if _is_post and any(a.rstrip("/") in url for a in _allow):
                continue                   # R278 — operator-named read-POST
        return m   # a non-auth, non-allowlisted mutation → genuinely destructive
    return None


def validate_playwright_destructive_patterns(
    content: str,
    read_post_allowlist=None,
) -> list[GroundingViolation]:
    """R154.B — reject LLM-generated PW specs that contain destructive
    operations against the SUT. Returns [] when:
      - opt-in marker `@intentional-destructive` present in spec header
      - no destructive pattern detected
      - killswitch ARTA_R154_B_DESTRUCTIVE_VALIDATOR_DISABLE=1 set

    Otherwise returns one GroundingViolation per (pattern × first match)
    so R57.1 retry-with-hint surfaces ALL destructive patterns in one
    pass (avoids whack-a-mole retry where LLM fixes one + re-emits another).

    Per-spec call site: validate_playwright_grounded retry chain at
    automation_engineer.py:_generate_playwright. After R57.1 budget
    exhausts, R102.A stamp → R102.C dispatch BLOCK with kind
    `destructive_test_pattern` (parent reason
    `playwright_grounding_violation`).
    """
    import os as _os_r154b
    if _os_r154b.environ.get("ARTA_R154_B_DESTRUCTIVE_VALIDATOR_DISABLE") == "1":
        return []
    if not content:
        return []
    # Opt-in marker exempts the entire spec
    if _r154_b_has_opt_in_marker(content):
        return []

    violations: list[GroundingViolation] = []
    seen_kinds: set[str] = set()
    for pattern, kind, description in _R154_B_DESTRUCTIVE_PATTERNS:
        # R242 — destructive_http_method: exempt auth-endpoint POSTs. Only flag
        # when a NON-auth business mutation call exists (login/token/refresh are
        # the mechanism of authenticated testing, not SUT-state mutations).
        if kind == "destructive_http_method":
            m = _r154_b_nonauth_mutation_match(content, read_post_allowlist)
        else:
            m = pattern.search(content)
        if not m:
            continue
        # Emit at most ONE violation per kind (avoid noise when a single
        # destructive pattern appears 10× in one spec — operator only needs
        # to see "this kind detected once" to act).
        if kind in seen_kinds:
            continue
        seen_kinds.add(kind)
        line_no = content[:m.start()].count("\n") + 1
        symbol_preview = m.group(0)[:80]
        violations.append(GroundingViolation(
            tool="playwright",
            kind="destructive_test_pattern",
            symbol=f"{kind}:{symbol_preview}",
            location=f"line {line_no}",
            hint=(
                f"R154.B — destructive operation detected ({description}).\n"
                f"ARTA's mission is to *report* SUT quality without mutating "
                f"SUT state. Generated test specs MUST use read-only patterns "
                f"unless the operator has explicitly opted in to destructive "
                f"testing.\n\n"
                f"BEFORE (BROKEN — mutates SUT):\n"
                f"  {symbol_preview}\n\n"
                f"AFTER 1 — convert to read-only assertion against the "
                f"SUT's current state:\n"
                f"  await expect(page.getByText('<existing-content>'))."
                f"toBeVisible();\n"
                f"  const resp = await page.request.get(`${{apiBase}}/...`);\n"
                f"  expect(resp.status()).toBe(200);\n\n"
                f"AFTER 2 — if this test MUST verify a destructive flow "
                f"(operator-approved bug-bash), the operator must:\n"
                f"  1. Set `ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1` AND\n"
                f"     `SUT_TEST_DATA_NAMESPACE=<sandbox-scope>` env vars.\n"
                f"  2. Add comment marker `// @intentional-destructive: "
                f"<reason>` within first 5 lines of the spec.\n"
                f"  3. Ensure all destructive operations are scoped to "
                f"the test sandbox namespace.\n"
                f"Without all three conditions, R154.C dispatch gate will "
                f"BLOCK this spec at execute time."
            ),
        ))

    return violations


def _r154_b_extract_destructive_patterns(content: str) -> list[str]:
    """Inspection helper for R154.C dispatch gate — returns list of
    destructive-pattern KINDS detected in `content`. Returns [] when
    opt-in marker present OR content has no destructive patterns.
    Used at dispatch time to decide whether to BLOCK or proceed.
    """
    if not content:
        return []
    if _r154_b_has_opt_in_marker(content):
        return []
    kinds: list[str] = []
    seen: set[str] = set()
    for pattern, kind, _desc in _R154_B_DESTRUCTIVE_PATTERNS:
        if kind in seen:
            continue
        # R242 — destructive_http_method: exempt auth-endpoint POSTs; only BLOCK
        # when a NON-auth business mutation exists (shared with the gen validator).
        if kind == "destructive_http_method":
            if _r154_b_nonauth_mutation_match(content) is not None:
                seen.add(kind)
                kinds.append(kind)
            continue
        if pattern.search(content):
            seen.add(kind)
            kinds.append(kind)
    return kinds


def _r231_validate_pw_goto_routes(
    content: str, project_id: str, dom_catalog: dict | None,
) -> list[GroundingViolation]:
    """R231 — flag `page.goto(<route>)` targets that match NO discovered SPA route
    (prefix-tolerant for sub-routes). Ground-truth = dom_catalog routes ∪
    `.arta/frontend_routes/<pid>.json`. Cold-start safe (no-op when 0 routes known).
    GENERIC; mirrors the axe route check so PW navigation is grounded too."""
    out: list[GroundingViolation] = []
    if not content or os.environ.get("ARTA_R231_PW_GOTO_ROUTE_DISABLE") == "1":
        return out
    known: set[str] = set()
    if isinstance(dom_catalog, dict):
        _rts = dom_catalog.get("routes")
        if isinstance(_rts, dict):
            known |= {str(k).lower().rstrip("/") for k in _rts.keys()}
        elif isinstance(_rts, (list, set)):
            known |= {str(k).lower().rstrip("/") for k in _rts}
    try:
        _frp = Path(os.environ.get("ARTA_FRONTEND_ROUTES_DIR", ".arta/frontend_routes")) / f"{project_id}.json"
        if _frp.is_file():
            _fj = json.loads(_frp.read_text())
            for _rec in (_fj if isinstance(_fj, list) else list((_fj or {}).values())):
                _rr = (_rec.get("resolved_route") or _rec.get("route")) if isinstance(_rec, dict) else _rec
                if isinstance(_rr, str) and _rr.startswith("/") and "/:" not in _rr:
                    known.add(_rr.lower().rstrip("/").split("?")[0])
    except Exception:
        pass
    known.discard("")
    if not known:
        return out
    for _gm in re.finditer(r"page\.goto\(\s*[`'\"]([^`'\"]+)[`'\"]", content):
        _gp = re.sub(r"^\$\{[^}]*\}", "", _gm.group(1)).split("?")[0].split("#")[0].rstrip("/")
        if not _gp.startswith("/"):
            continue
        _gpl = _gp.lower()
        if _gpl in ("", "/"):
            continue
        if any(_gpl == kr or _gpl.startswith(kr + "/") or kr.startswith(_gpl + "/") for kr in known):
            continue
        _line = content[:_gm.start()].count("\n") + 1
        out.append(GroundingViolation(
            tool="playwright", kind="unknown_route",
            symbol=f"page.goto('{_gp}')", location=f"line {_line}",
            hint=(
                f"`page.goto('{_gp}')` targets a route NOT in the SUT's discovered "
                f"SPA routes → it 404s/redirects and every selector below it times "
                f"out.\n\nAFTER — navigate a REAL discovered route:\n"
                f"  {sorted(known)[:6]}\n"
                f"If this is a LOGIN-flow test, the login page is often not "
                f"discoverable (the probe runs authenticated) — test the login API "
                f"endpoint via Newman instead of driving a hallucinated form."
            ),
        ))
    return out


_R313_VALID_ARRAY_RE = re.compile(
    r"const\s+(\w+)\s*=\s*\[([^\]]*)\]\s*;")
_R313_TOCONTAIN_RE = re.compile(
    r"expect\(\s*(\w+)\s*\)\s*\.toContain\(\s*([A-Za-z_][\w.\[\]']*)\s*\)")
_R313_STRLIT_RE = re.compile(r"""['"]([^'"]+)['"]""")
# R313.E — direct-equality on a runtime response field: expect(body.currentState)
# .toBe('ready'). Group1=full ref (body.currentState), 2=field (currentState),
# 3=string literal. `typeof x` refs never match (the space after `typeof` breaks the
# ref class); numeric literals (status codes) never match (quotes required).
_R313_E_TOBE_RE = re.compile(
    r"expect\(\s*([A-Za-z_][\w.\[\]]*\.(\w+))\s*\)\s*\.toBe\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _r313_value_domains_from_captured(
    captured_endpoints: "list[dict] | None",
) -> "dict[str, set[str]]":
    """R313 — union the ENUM-LIKE observed values (captured by _infer_shape as
    `{"type","value"}` leaves in response_body_shape) into a per-field allowed set.
    A field is treated as an enum DOMAIN only when it has a small bounded set of
    distinct short values (2..12) — a unique-id field (many distinct) is excluded.
    Empty until a value-capturing discovery re-ingest has run → cold-start-safe."""
    by_field: "dict[str, set[str]]" = {}
    for entry in (captured_endpoints or []):
        if not isinstance(entry, dict):
            continue
        # R313 — primary source: the flat {field: [values]} value-domain samples the
        # discovery probe captures (response_value_samples). Union across endpoints.
        _rvs = entry.get("response_value_samples")
        if isinstance(_rvs, dict):
            for _f, _vv in _rvs.items():
                if isinstance(_vv, list):
                    by_field.setdefault(_f, set()).update(str(x) for x in _vv if isinstance(x, (str, int, float, bool)))
        # Secondary source: inline {"type","value"} leaves (HAR _infer_shape path).
        shape = entry.get("response_body_shape")

        def _walk(node):
            if isinstance(node, dict):
                if node.get("type") and "value" not in node and "properties" not in node:
                    return
                props = node.get("properties") if isinstance(node.get("properties"), dict) else node
                for k, v in (props.items() if isinstance(props, dict) else []):
                    if isinstance(v, dict) and isinstance(v.get("value"), str):
                        by_field.setdefault(k, set()).add(v["value"])
                    _walk(v)
            elif isinstance(node, list):
                for it in node:
                    _walk(it)

        _walk(shape)
    # keep only genuine enum domains (small bounded distinct set)
    return {f: vs for f, vs in by_field.items() if 2 <= len(vs) <= 12}


def _r313_validate_pw_value_domain(
    content: str, value_domains: "dict[str, set[str]]",
) -> "list[GroundingViolation]":
    """R313 — flag a FABRICATED enum assertion: a hardcoded `const X=['A','B',...]`
    used as `expect(X).toContain(<body.field>)` where the SUT's OBSERVED domain for
    that field contains a value the hardcoded list OMITS (live: validStates omitted
    the real 'registered'). The LLM guessed the enum instead of grounding it → the
    test false-FAILs on valid SUT data. Emits a retry hint carrying the REAL observed
    values so the LLM regrounds (or asserts shape when the domain is unknown).

    Cold-start-safe: no captured domain → returns []. Killswitch
    ARTA_R313_VALUE_DOMAIN_VALIDATOR_DISABLE=1."""
    if (not content or not value_domains
            or os.environ.get("ARTA_R313_VALUE_DOMAIN_VALIDATOR_DISABLE") == "1"):
        return []
    # map local const-array name → its hardcoded string-literal set
    arrays: "dict[str, set[str]]" = {}
    for m in _R313_VALID_ARRAY_RE.finditer(content):
        lits = set(_R313_STRLIT_RE.findall(m.group(2)))
        if lits:
            arrays[m.group(1)] = lits
    out: "list[GroundingViolation]" = []
    seen: set[str] = set()
    for m in _R313_TOCONTAIN_RE.finditer(content):
        arr_name, checked = m.group(1), m.group(2)
        if arr_name not in arrays:
            continue
        field = checked.split(".")[-1].split("[")[0]   # body.currentState → currentState
        domain = value_domains.get(field)
        if not domain:
            continue
        missing = domain - arrays[arr_name]
        if missing and arr_name not in seen:
            seen.add(arr_name)
            out.append(GroundingViolation(
                tool="playwright",
                kind="fabricated_value_domain",
                symbol=f"const {arr_name} = [{', '.join(sorted(arrays[arr_name]))}]",
                location=f"expect({arr_name}).toContain({checked})",
                hint=(f"`{arr_name}` is a GUESSED enum for `{field}` — it omits real "
                      f"SUT value(s) {sorted(missing)[:6]}. The SUT's observed domain "
                      f"for `{field}` is {sorted(domain)[:12]}. Use the REAL set (or "
                      f"assert `typeof {checked} === 'string'` when the full domain is "
                      f"unknown) — never assert a guessed enum."),
            ))
    return out


def _r313_e_validate_tobe_literal(
    content: str, value_domains: "dict[str, set[str]]",
) -> "list[GroundingViolation]":
    """R313.E — flag a FABRICATED direct-equality assertion on a MUTABLE runtime
    field: `expect(body.currentState).toBe('ready')` where the SUT's observed domain
    for `currentState` is a multi-valued enum (e.g. {registered, queued}) or does not
    contain 'ready'. Asserting an EXACT value for a field whose value depends on live
    SUT state false-FAILs whenever the live value differs — this is the `toBe(literal)`
    sibling of the `toContain([...])` enum pattern (`_r313_validate_pw_value_domain`)
    and is the dominant residual FAIL class (run-9b3dc7: 32/44 FAILs were
    expect(...).toBe('<guessed-state>') vs a different real state).

    Fires ONLY when the field has an observed value domain (from R313.D capture) AND
    it is NOT a proven singleton constant (domain=={literal}). So a genuinely constant
    field asserted with its real single value is untouched. Cold-start-safe (no domain
    → no-op). Killswitch ARTA_R313_E_TOBE_VALIDATOR_DISABLE=1."""
    if (not content or not value_domains
            or os.environ.get("ARTA_R313_E_TOBE_VALIDATOR_DISABLE") == "1"):
        return []
    out: "list[GroundingViolation]" = []
    seen: set = set()
    for m in _R313_E_TOBE_RE.finditer(content):
        ref, field, lit = m.group(1), m.group(2), m.group(3)
        domain = value_domains.get(field)
        if not domain:
            continue
        # proven singleton constant asserted with its real value → legitimate
        if domain == {lit}:
            continue
        key = (field, lit)
        if key in seen:
            continue
        seen.add(key)
        observed = lit in domain
        why = (f"was never observed (SUT returned {sorted(domain)[:6]})" if not observed
               else "is only ONE of several live states")
        out.append(GroundingViolation(
            tool="playwright",
            kind="fabricated_value_domain",
            symbol=f"expect({ref}).toBe('{lit}')",
            location=f"expect({ref}).toBe('{lit}')",
            # Steer to the ROBUST shape assertion, NOT membership-of-observed. The
            # captured domain (R313.D) is a SAMPLE, not the complete enum — a
            # `[...observed].includes(field)` check would itself false-FAIL on a
            # valid-but-unobserved value, and adds parser-error risk. `typeof` shape
            # always passes for any valid state and can't be fabricated.
            hint=(f"`{field}` is a MUTABLE runtime state field — its value depends on "
                  f"live SUT state (sampled: {sorted(domain)[:8]}), so `toBe('{lit}')` "
                  f"{why} and false-FAILs whenever the live value differs. Replace it "
                  f"with the SHAPE assertion `expect(typeof {ref}).toBe('string')` — "
                  f"never a guessed exact value for a state that changes. Do NOT assert "
                  f"membership against the sampled set (it is incomplete → would also "
                  f"false-FAIL on valid unobserved states)."),
        ))
    return out


def _r313_e2_rewrite_fabricated_tobe(
    content: str, value_domains: "dict[str, set[str]]",
) -> "tuple[str, int]":
    """R313.E.2 — DETERMINISTIC rewriter (R102.E / R118.A pattern) for the fabricated
    `toBe(literal)` pattern R313.E detects. Rewrites
        expect(<ref>.<field>).toBe('<lit>')  →  expect(typeof <ref>).toBe('string')
    when <field> has a MUTABLE observed domain (R313.D capture) and <lit> is not a
    proven singleton constant (domain == {lit}).

    Why a rewriter and not just the detect+retry-hint: the retry path relies on the
    LLM to reground, which is FLAKY (claude_code ATDD retries) and can inject NEW
    syntax errors (a regen quarantined on unbalanced parens after taking the hint).
    A deterministic transform lands the safe shape assertion every time, no LLM
    round-trip, minimal syntax surface. The R313.E detector still runs (defence in
    depth / truthful stamp if the rewriter is disabled). Returns (new, n_rewrites).
    Killswitch ARTA_R313_E2_REWRITE_DISABLE=1."""
    if (not content or not value_domains
            or os.environ.get("ARTA_R313_E2_REWRITE_DISABLE") == "1"):
        return content, 0
    count = 0

    def _repl(m):
        nonlocal count
        ref, field, lit = m.group(1), m.group(2), m.group(3)
        domain = value_domains.get(field)
        if not domain or domain == {lit}:
            return m.group(0)          # unknown field / proven constant → leave as-is
        count += 1
        return f"expect(typeof {ref}).toBe('string')"

    new = _R313_E_TOBE_RE.sub(_repl, content)
    return new, count


# ── R316: unified structural parser for expect() assertions (durable AST-lite) ──
# The 7 regex passes (E.2/G/G.2/G.3/G.4/H/J) each hand-matched ONE syntactic form; the
# LLM keeps varying syntax (inline array → named const → optional chain → …), so each
# regen surfaced a new form = whack-a-mole. This parser extracts the STRUCTURE of every
# `expect(<subject>).<matcher>(<arg>)` once — balanced-paren + string/comment aware — so
# a single semantic classifier can decide, regardless of how the code is written.
_R316_EXPECT_HEAD_RE = re.compile(r"\bexpect\s*\(")


def _r316_match_balanced(content: str, open_idx: int) -> int:
    """Return the index of the `)` that closes the `(` at open_idx, skipping parens
    inside strings/templates/line+block comments. Returns -1 if unbalanced."""
    depth = 0
    i = open_idx
    n = len(content)
    state = None  # None | "'" | '"' | '`' | 'line' | 'block'
    while i < n:
        c = content[i]
        nxt = content[i + 1] if i + 1 < n else ""
        if state is None:
            if c in ("'", '"', "`"):
                state = c
            elif c == "/" and nxt == "/":
                state = "line"; i += 1
            elif c == "/" and nxt == "*":
                state = "block"; i += 1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i
        elif state in ("'", '"', "`"):
            if c == "\\":
                i += 1
            elif c == state:
                state = None
        elif state == "line":
            if c == "\n":
                state = None
        elif state == "block":
            if c == "*" and nxt == "/":
                state = None; i += 1
        i += 1
    return -1


_R316_MATCHER_RE = re.compile(r"\s*\.\s*(\w+)\s*\(")


def _iter_expect_calls(content: str):
    """Yield dicts {full_start, full_end, subject, matcher, arg} for each
    `expect(<subject>).<matcher>(<arg>)` — structural, so optional chaining, inline
    arrays, named consts, nesting, and templates are all handled the SAME way."""
    for m in _R316_EXPECT_HEAD_RE.finditer(content):
        open_paren = m.end() - 1
        close = _r316_match_balanced(content, open_paren)
        if close == -1:
            continue
        subject = content[open_paren + 1:close].strip()
        mm = _R316_MATCHER_RE.match(content, close + 1)
        if not mm:
            continue
        arg_open = mm.end() - 1
        arg_close = _r316_match_balanced(content, arg_open)
        if arg_close == -1:
            continue
        yield {
            "full_start": m.start(),
            "full_end": arg_close + 1,
            "subject": subject,
            "matcher": mm.group(1),
            "arg": content[arg_open + 1:arg_close].strip(),
        }


_R316_STRLIT = re.compile(r"^(['\"]).*\1$", re.DOTALL)
_R316_NUMLIT = re.compile(r"^-?\d+(?:\.\d+)?$")
_R316_MEMBER_REF = re.compile(r"^\w+(?:\??\.\w+|\[\d+\])+$")   # body.a?.b[0]
_R316_ISARRAY = re.compile(r"^Array\.isArray\(\s*(\w+(?:\??\.\w+|\[\d+\])*)\s*\)$")


def _r316_literal_jstype(arg: str):
    """Return the JS `typeof` string for a literal arg, or None if not a literal."""
    if _R316_STRLIT.match(arg):
        return "string"
    if _R316_NUMLIT.match(arg):
        return "number"
    if arg in ("true", "false"):
        return "boolean"
    return None


def _r316_unified_value_rewrite(content, value_domains=None):
    """R316 — UNIFIED value-fabrication rewriter built on the structural parser
    (_iter_expect_calls). One classifier handles every syntactic form of "exact-value
    / membership / presence assertion on a response-body ref", superseding the E.2/G/
    G.2/G.4/H regex passes and covering toEqual/toStrictEqual for free. Gated to
    `.json()` body vars (+ local aliases). Killswitch ARTA_R316_UNIFIED_DISABLE=1.
    Returns (new_content, n)."""
    import os as _os
    if not content or _os.environ.get("ARTA_R316_UNIFIED_DISABLE") == "1":
        return content, 0
    body_vars = set(_R313_G_JSON_VAR_RE.findall(content))
    if not body_vars:
        return content, 0
    for _ in range(2):
        for _m in re.finditer(r"(?:const|let|var)\s+(\w+)\s*=\s*(\w+)(?:\??\.\w+|\[\d+\])", content):
            if _m.group(2) in body_vars:
                body_vars.add(_m.group(1))
    const_arrays = {mm.group(1) for mm in _R313_VALID_ARRAY_RE.finditer(content)}
    value_domains = value_domains or {}

    def _is_body_ref(expr):
        return bool(_R316_MEMBER_REF.match(expr)) and re.match(r"\w+", expr).group(0) in body_vars

    edits = []
    for call in _iter_expect_calls(content):
        subj, matcher, arg = call["subject"], call["matcher"], call["arg"]
        repl = None
        # 1) exact-value equality on a body field → shape
        if matcher in ("toBe", "toEqual", "toStrictEqual"):
            jst = _r316_literal_jstype(arg)
            if jst and _is_body_ref(subj):
                field = re.findall(r"\.(\w+)", subj)
                dom = value_domains.get(field[-1]) if field else None
                if not (jst == "string" and dom == {arg.strip("'\"")}):   # keep proven constant
                    repl = f"expect(typeof {subj}).toBe('{jst}')"
            # 2) presence: expect(Array.isArray(bodyRef)).toBe(true) → guard
            elif matcher == "toBe" and arg == "true":
                am = _R316_ISARRAY.match(subj)
                if am and re.match(r"\w+", am.group(1)).group(0) in body_vars:
                    r = am.group(1)
                    repl = (f"expect({r} === undefined || {r} === null || "
                            f"Array.isArray({r})).toBe(true)")
        # 3) membership: expect(<array|const>).toContain(bodyRef) → shape
        elif matcher == "toContain":
            subj_is_array = subj.startswith("[") or subj in const_arrays
            if subj_is_array and _is_body_ref(arg):
                repl = f"expect(typeof {arg}).toBe('string')"
        if repl is not None:
            edits.append((call["full_start"], call["full_end"], repl))

    for s, e, r in sorted(edits, key=lambda t: t[0], reverse=True):
        content = content[:s] + r + content[e:]
    return content, len(edits)


# R313.G — a variable is a PARSED RESPONSE BODY when assigned from `.json()`. Only
# member-access assertions rooted at such a variable are treated as dynamic response
# data. This gate is what keeps status codes (`resp.status()`), DOM (`page.url()`),
# and computed constants OUT of the rewrite. Matches BOTH the declaration form
# (`const body = await resp.json()`) AND a bare reassignment (`body = await resp
# .json()` — the LLM often declares `let body: any;` up top and assigns inside a
# try). The lookbehind rejects a member-target (`x.body = …json()`) so only simple
# identifiers are treated as body vars.
_R313_G_JSON_VAR_RE = re.compile(
    r"(?:(?:const|let|var)\s+)?(?<![\w.])(\w+)\s*(?::[^=\n]+)?=\s*"
    r"(?:await\s+)?[\w.]+\.json\(\)")
# expect(<var><.field|?.field|[idx] chain>).toBe(<string|number|boolean literal>).
# The path allows OPTIONAL CHAINING (`?.`) — the LLM writes `hostData?.currentState`
# heavily, and a `\.\w+`-only path silently skipped every such fabrication.
_R313_G_PATH = r"(?:\??\.\w+|\[\d+\])+"
_R313_G_TOBE_RE = re.compile(
    r"expect\(\s*(\w+)(" + _R313_G_PATH + r")\s*\)\s*\.toBe\(\s*"
    r"(?:(['\"])([^'\"]*)\3|(-?\d+(?:\.\d+)?)|(true|false))\s*\)")
# expect([<inline literal array>]).toContain(<var>.<path>) — an INLINE guessed enum
# (R313's `_R313_validate_pw_value_domain` only handles a NAMED `const X=[...]`). The
# array is a fabrication → assert the field's shape instead. Group1 = runtime ref.
_R313_G_TOCONTAIN_INLINE_RE = re.compile(
    r"expect\(\s*\[[^\]]*\]\s*\)\s*\.toContain\(\s*(\w+" + _R313_G_PATH + r")\s*\)")
# named-const enum: `const validStates=[...]; expect(validStates).toContain(body.x)`.
# R313's validator DETECTS this but nothing REWROTE it → the LLM re-emits it every
# retry → block. Group1=const array name, group2=body ref (path incl `?.`).
_R313_G_CONST_CONTAIN_RE = re.compile(
    r"expect\(\s*(\w+)\s*\)\s*\.toContain\(\s*(\w+" + _R313_G_PATH + r")\s*\)")
# R313.H — presence class: expect(Array.isArray(<optionalField>)).toBe(true) FALSE-FAILs
# when the field is absent (Array.isArray(undefined)===false). An OPTIONAL response
# field (stateTransitionLog / failureReasons — present only in some states) must be
# asserted array-ONLY-when-present. Group1 = the runtime ref.
# path is OPTIONAL here (`*`): the subject is often a BARE local alias of a body
# field (`const log = hostData.stateTransitionLog; Array.isArray(log)`). The body_vars
# membership check (incl. R313.G.3 aliases) is what gates it, not the path.
_R313_H_ISARRAY_RE = re.compile(
    r"expect\(\s*Array\.isArray\(\s*(\w+(?:\??\.\w+|\[\d+\])*)\s*\)\s*\)\s*\.toBe\(true\)")


def _r313_g_rewrite_dynamic_value_asserts(
    content: str, value_domains: "dict[str, set[str]] | None" = None,
) -> "tuple[str, int]":
    """R313.G — GENERAL deterministic rewriter: an EXACT-value assertion on a dynamic
    RESPONSE-BODY field is a fabrication when the generator has no data oracle (the LLM
    guessed it), so it false-FAILs whenever the live value differs. Rewrite
        expect(<bodyVar>.<path>).toBe(<literal>)  →  expect(typeof <bodyVar>.<path>).toBe('<jstype>')
    where jstype ∈ {string, number, boolean} by the literal's type. This generalises
    R313.E.2 (which needed a CAPTURED string domain) to cover the residual FAIL forms
    the value-domain capture can never fully reach: numeric counts (`.length).toBe(3)`),
    and states on fields no probe happened to observe (`toBe('available')`).

    Safety (this changes assertion strategy broadly, so the gate is strict):
      • ONLY fires on a var proven to hold a parsed JSON body (assigned from `.json()`)
        → status codes (`resp.status()`), DOM (`page.url()`), and constants are NEVER
        touched (their subject is not a json-body var member-access).
      • A field PROVEN to be a singleton constant in the captured domain (== the
        literal) is left as an exact assertion (a real invariant worth checking).
      • Shape assertions are TRUTHFUL, not fake passes — they verify the contract type,
        which is the correct assertion level for SUT-runtime-dependent data.
    Killswitch ARTA_R313_G_REWRITE_DISABLE=1. Returns (new_content, n_rewrites)."""
    if (not content or os.environ.get("ARTA_R313_G_REWRITE_DISABLE") == "1"):
        return content, 0
    body_vars = set(_R313_G_JSON_VAR_RE.findall(content))
    if not body_vars:
        return content, 0
    # R313.G.3 — a local aliased from a body var's field IS a response ref: the LLM
    # writes `const transitionLog = hostData.stateTransitionLog` then asserts on
    # `transitionLog`. Two passes catch chained aliases. Safe: broadening only adds
    # dynamic-response locals (a scalar alias like `const s = body.state` is still a
    # correct target for the value→shape rewrite).
    for _ in range(2):
        for _m in re.finditer(r"(?:const|let|var)\s+(\w+)\s*=\s*(\w+)(?:\??\.\w+|\[\d+\])", content):
            if _m.group(2) in body_vars:
                body_vars.add(_m.group(1))
    value_domains = value_domains or {}
    count = 0

    def _repl(m):
        nonlocal count
        root, path = m.group(1), m.group(2)
        if root not in body_vars:
            return m.group(0)
        ref = root + path
        _fields = re.findall(r"\.(\w+)", path)   # handles both `.f` and `?.f`
        field = _fields[-1] if _fields else ""
        if m.group(4) is not None:            # string literal
            dom = value_domains.get(field)
            if dom and dom == {m.group(4)}:   # proven singleton constant → keep
                return m.group(0)
            jstype = "string"
        elif m.group(5) is not None:          # number literal
            jstype = "number"
        elif m.group(6) is not None:          # boolean literal
            jstype = "boolean"
        else:
            return m.group(0)
        count += 1
        return f"expect(typeof {ref}).toBe('{jstype}')"

    new = _R313_G_TOBE_RE.sub(_repl, content)

    # second pass — inline-array toContain on a response-body field (guessed enum)
    def _repl_contain(m):
        nonlocal count
        ref = m.group(1)
        root = re.match(r"\w+", ref).group(0)
        if root not in body_vars:
            return m.group(0)
        count += 1
        return f"expect(typeof {ref}).toBe('string')"

    new = _R313_G_TOCONTAIN_INLINE_RE.sub(_repl_contain, new)

    # third pass — R313.H presence guard for optional array fields
    def _repl_isarray(m):
        nonlocal count
        ref = m.group(1)
        root = re.match(r"\w+", ref).group(0)
        if root not in body_vars:
            return m.group(0)
        count += 1
        # present-and-array OR legitimately-absent → never false-FAILs on an
        # optional field, still catches a present-but-wrong-type value.
        return (f"expect({ref} === undefined || {ref} === null || "
                f"Array.isArray({ref})).toBe(true)")

    new = _R313_H_ISARRAY_RE.sub(_repl_isarray, new)

    # fourth pass — R313.G.4: NAMED-const enum toContain on a body field
    # (`const validStates=['active',...]; expect(validStates).toContain(body.currentState)`).
    # The inline-array pass (above) missed the named-const form the LLM prefers.
    const_arrays = {mm.group(1) for mm in _R313_VALID_ARRAY_RE.finditer(new)}

    def _repl_const_contain(m):
        nonlocal count
        arr, ref = m.group(1), m.group(2)
        if arr not in const_arrays:
            return m.group(0)                 # not a local const array → leave
        root = re.match(r"\w+", ref).group(0)
        if root not in body_vars:
            return m.group(0)                 # not a response field → leave
        count += 1
        return f"expect(typeof {ref}).toBe('string')"

    new = _R313_G_CONST_CONTAIN_RE.sub(_repl_const_contain, new)
    return new, count


# R313.J — server-state vocabulary (common lifecycle/health tokens the LLM guesses
# for state UI text). Used to identify a fabricated getByText('<state-value>').
_R313_J_STATE_VOCAB = frozenset({
    "running", "stopped", "pending", "active", "inactive", "ready", "notready",
    "error", "failed", "failure", "provisioning", "terminating", "terminated",
    "registered", "queued", "unknown", "healthy", "unhealthy", "degraded",
    "online", "offline", "available", "unavailable", "creating", "deleting",
    "updating", "succeeded", "starting", "stopping", "initializing", "reconciling",
    "synced", "outofsync", "warning", "critical", "normal", "up", "down",
})
_R313_J_GETBYTEXT_RE = re.compile(r"getByText\(\s*(['\"])([^'\"]+)\1\s*\)")


def _r313_j_reground_state_getbytext(
    content: str,
    catalog_texts: "set[str] | None",
    state_domain: "dict[str, set[str]] | None" = None,
) -> "tuple[str, int]":
    """R313.J (operator-approved) — reground a fabricated server-state `getByText`.
    The LLM writes `getByText('running')` asserting state UI text the SUT does not
    render as findable text (it shows a 'Current State' COLUMN, not the values).
    Reground the fabricated state-VALUE getByText → the catalog's state-LABEL (e.g.
    'Current State'), so the assertion verifies the state field IS DISPLAYED; the API
    response already verifies the actual value (R313). Per the operator's decision this
    intentionally changes intent value-check → field-present.

    Gated (all three): (1) the text is NOT in the catalog (confirmed fabricated),
    (2) it is a state VALUE — in the captured state domain OR the state vocabulary,
    (3) a state-LABEL ('…state…'/'…status…') exists in the catalog to reground to.
    Absent any, no-op. Killswitch ARTA_R313_J_REGROUND_DISABLE=1. Returns (new, n)."""
    if (not content or not catalog_texts
            or os.environ.get("ARTA_R313_J_REGROUND_DISABLE") == "1"):
        return content, 0
    state_labels = [t for t in catalog_texts
                    if isinstance(t, str) and re.search(r"\b(state|status)\b", t, re.I)]
    if not state_labels:
        return content, 0
    label = min(state_labels, key=len)          # shortest state-ish label
    state_vals = set(_R313_J_STATE_VOCAB)
    for _f, _vs in (state_domain or {}).items():
        if "state" in _f.lower() or "status" in _f.lower():
            state_vals |= {str(v).lower() for v in _vs}
    count = 0

    def _repl(m):
        nonlocal count
        q, txt = m.group(1), m.group(2)
        # already valid catalog text (exact or substring, mirroring the validator)
        if txt in catalog_texts or any(txt in t for t in catalog_texts):
            return m.group(0)
        if txt.lower() not in state_vals:        # not a state value → leave alone
            return m.group(0)
        count += 1
        return f"getByText({q}{label}{q})"

    return _R313_J_GETBYTEXT_RE.sub(_repl, content), count


def validate_playwright_grounded(
    content: str,
    *,
    project_id: str,
    dom_catalog: dict | None = None,
    stable_selectors: dict | None = None,
    captured_endpoints: list[dict] | None = None,
    expected_outputs: dict | None = None,
) -> list[GroundingViolation]:
    """Walk a Playwright spec, surface hallucinated testids AND
    hallucinated role+name pairs.

    R29.0 already does the testid check in a regen pass; R42.1 makes
    it a generic post-gen step so EVERY playwright generation flows
    through the same gate.

    R78.6 — extends the validator to ALSO check `getByRole(role,
    { name })` against the catalog's captured role+name pairs.
    Without this extension, R78.2's prompt change (which instructs
    the LLM to emit `getByRole` for testid-less SPAs) would have no
    grounding gate — the LLM could invent plausible-sounding role+name
    pairs that don't exist in the SUT and the validator would
    rubber-stamp them.

    Args:
        content: spec source text
        project_id: caller's project id for logging
        dom_catalog: testid catalog dict (legacy shape: `{"testids": [...]}`).
            When falsy/empty: skip testid validation with R55.5 WARN.
        stable_selectors: R78.6 expanded set including `role_names`.
            When falsy/empty: skip role-based validation silently
            (cold-start branch already handled by dom_catalog warn).
    """
    out: list[GroundingViolation] = []
    if not content:
        return out
    has_testid_signal = bool(dom_catalog and (dom_catalog.get("testids") or []))
    has_role_signal = bool(
        stable_selectors and (stable_selectors.get("role_names") or set())
    )
    # R140.A — aria_labels + texts also count as catalog signal for the
    # cold-start gate below. Pre-R140.A the gate only checked
    # testids+role_names — projects that have populated aria_labels/texts
    # but empty role_names would short-circuit and skip R140.A validation.
    has_label_signal = bool(
        stable_selectors and (stable_selectors.get("aria_labels") or set())
    )
    has_text_signal = bool(
        stable_selectors and (stable_selectors.get("texts") or set())
    )

    # R231 — page.goto ROUTE grounding (parity with the axe validator), run BEFORE
    # the R55.5 cold-start early-return because it only needs discovered `routes`
    # against discovered SPA routes → a hallucinated route (`page.goto('/login')`
    # → 404 "No static resource login") shipped and every selector below it timed
    # out (the 76 login-form fails). Killswitch ARTA_R231_PW_GOTO_ROUTE_DISABLE=1.
    out.extend(_r231_validate_pw_goto_routes(content, project_id, dom_catalog))

    # R101.E — when captured_endpoints is provided, run the endpoint
    # grounding check EVEN IF dom_catalog/stable_selectors are missing
    # (the endpoint check is independent of UI-selector signals).
    # Only the testid+role checks below need the UI catalog.
    if not has_testid_signal and not has_role_signal and not has_label_signal and not has_text_signal and not captured_endpoints:
        log.warning(
            "R55.5: playwright grounding SKIPPED for project=%s — "
            "no catalog signal (dom_catalog=%s, stable_selectors=%s, "
            "captured_endpoints=%s). Generated spec is UNVERIFIED "
            "(LLM-only); run discovery to populate catalog, then re-gen.",
            project_id,
            "missing" if not dom_catalog else "has 0 testids",
            "missing" if not stable_selectors else "has 0 role+name pairs",
            "missing",
        )
        return out

    # Testid check (existing behaviour). When the catalog has 0 testids
    # but DOES have role+name pairs, skip testid checks — the prompt
    # branched to role-based mode in that case (R78.2).
    if has_testid_signal:
        catalog_testids = set(dom_catalog.get("testids") or [])
        for match in _TESTID_REF_RE.finditer(content):
            testid = match.group(1)
            if testid not in catalog_testids:
                line_no = content[:match.start()].count("\n") + 1
                _catalog_sample = sorted(catalog_testids)[:6]
                out.append(GroundingViolation(
                    tool="playwright",
                    kind="hallucinated_testid",
                    symbol=testid,
                    location=f"line {line_no}",
                    hint=(
                        f"`getByTestId('{testid}')` references a testid not "
                        f"in the SUT's DOM catalog.\n\n"
                        f"BEFORE (BROKEN — selector returns 0 elements):\n"
                        f"  await page.getByTestId('{testid}').click();\n\n"
                        f"AFTER — use a testid from the SUT's catalog "
                        f"(verified via R45.3 discovery):\n"
                        f"  // Available testids in this SUT: {_catalog_sample}\n"
                        f"  await page.getByTestId('{_catalog_sample[0] if _catalog_sample else '<pick-from-list>'}').click();\n\n"
                        f"If no testid fits the scenario, use `getByRole('button', "
                        f"{{ name: '<visible text>' }})` with a role+name pair "
                        f"from the catalog instead."
                    ),
                ))

    # R146.D — STRICT role-name grounding when role_names catalog empty
    # but other catalog signals exist. R78.6's `if has_role_signal:` block
    # short-circuits when `role_names` is empty → hallucinated `getByRole`
    # calls slip through. Iter 5 (run-2b3b3d) evidence: 41 of 145 PW FAILs
    # were locator-timeout on hallucinated `getByRole('button', { name:
    # 'Continue with Google' })` etc. R45.3 discovery probe captured
    # aria_labels + texts but raced with SPA hydration → role_names empty.
    # Without strict-mode validation, the LLM's hallucinated role+name
    # pairs reach dispatch and fail at runtime.
    #
    # R146.D fires ONLY when: role_names empty AND (aria_labels OR texts
    # populated). When catalog is entirely empty (cold-start), R55.5 WARN
    # at line 2125-2136 already handles it; R146.D doesn't add noise.
    # Killswitch: ARTA_R146_D_STRICT_ROLE_DISABLE=1.
    import os as _os_r146d
    _r146d_disabled = _os_r146d.environ.get(
        "ARTA_R146_D_STRICT_ROLE_DISABLE"
    ) == "1"
    if (
        not _r146d_disabled
        and not has_role_signal
        and (has_label_signal or has_text_signal)
    ):
        _r146d_avail_labels = (
            sorted(stable_selectors.get("aria_labels") or [])[:5]
            if has_label_signal else []
        )
        _r146d_avail_texts = (
            sorted(stable_selectors.get("texts") or [])[:5]
            if has_text_signal else []
        )
        for match in _ROLE_NAME_REF_RE.finditer(content):
            spec_role = match.group(1).strip().lower()
            spec_name = " ".join(match.group(2).split())
            line_no = content[:match.start()].count("\n") + 1
            out.append(GroundingViolation(
                tool="playwright",
                kind="catalog_role_name_unknown_strict",
                symbol=f"getByRole('{spec_role}', {{ name: '{spec_name}' }})",
                location=f"line {line_no}",
                hint=(
                    f"R146.D STRICT — `getByRole('{spec_role}', "
                    f"{{ name: '{spec_name}' }})` rejected: SUT DOM catalog "
                    f"has NO role+name pairs captured (discovery probe ran "
                    f"but the SPA may have timing-raced before hydration "
                    f"exposed role+name attributes).\n\n"
                    f"BEFORE (BROKEN — selector returns 0 elements at "
                    f"runtime):\n"
                    f"  await page.getByRole('{spec_role}', {{ name: "
                    f"'{spec_name}' }}).click();\n\n"
                    f"AFTER 1 — use catalog aria_labels (R140.A grounded):\n"
                    f"  // Available aria_labels: {_r146d_avail_labels}\n"
                    f"  await page.getByLabel("
                    f"'{_r146d_avail_labels[0] if _r146d_avail_labels else '<label>'}').click();\n\n"
                    f"AFTER 2 — use catalog texts:\n"
                    f"  // Available texts: {_r146d_avail_texts}\n"
                    f"  await page.getByText("
                    f"'{_r146d_avail_texts[0] if _r146d_avail_texts else '<text>'}').click();\n\n"
                    f"AFTER 3 — re-run R45.3 discovery after SPA hydration "
                    f"completes (operator action) so role_names catalog "
                    f"populates."
                ),
            ))

    # R78.6 — role+name check. Run when stable_selectors is provided
    # AND the catalog has at least some role+name pairs. The name match
    # is fuzzy on the LLM side: the probe truncates text to 80 chars,
    # so the spec's longer text may not exactly match. Allow substring
    # match in either direction so a spec asserting `name: 'Submit'`
    # matches a catalog entry `name: 'Submit order'` (and vice versa).
    if has_role_signal:
        catalog_pairs = stable_selectors.get("role_names") or set()
        # Build a lowercase role index for case-insensitive role match.
        catalog_by_role: dict[str, list[str]] = {}
        for cr, cn in catalog_pairs:
            catalog_by_role.setdefault(cr.lower(), []).append(cn)
        for match in _ROLE_NAME_REF_RE.finditer(content):
            spec_role = match.group(1).strip().lower()
            spec_name = " ".join(match.group(2).split())
            candidates = catalog_by_role.get(spec_role) or []
            if not candidates:
                line_no = content[:match.start()].count("\n") + 1
                _avail_roles = sorted(catalog_by_role.keys())[:8]
                _first_role = _avail_roles[0] if _avail_roles else "button"
                _first_name = (catalog_by_role.get(_first_role) or ["<name>"])[0] if _avail_roles else "<name>"
                # R118.E.2 — when `spec_role` is fully absent AND it's in
                # the ROLE_TO_FALLBACK map of common semantic roles, surface
                # a concrete alternative selector recipe. Pre-R118.E.2 the
                # hint only said "use a role from the catalog" without
                # explaining how to address the LLM's actual intent
                # (e.g., role='textbox' usually means an `<input>` field).
                _r118_e2_fallback = ""
                if spec_role in _R118_E_2_ROLE_TO_FALLBACK:
                    _r118_e2_fallback = (
                        f"\n\nR118.E.2 — role '{spec_role}' is a common "
                        f"semantic role NOT exposed by this SUT's DOM. "
                        f"Concrete alternative selector for '{spec_role}':\n"
                        f"  {_R118_E_2_ROLE_TO_FALLBACK[spec_role]}"
                    )
                out.append(GroundingViolation(
                    tool="playwright",
                    kind="hallucinated_role",
                    symbol=f"getByRole('{spec_role}', {{ name: '{spec_name}' }})",
                    location=f"line {line_no}",
                    hint=(
                        f"Role '{spec_role}' not found in the SUT's DOM "
                        f"catalog (verified via R45.3 discovery).\n\n"
                        f"BEFORE (BROKEN — selector returns 0 elements):\n"
                        f"  await page.getByRole('{spec_role}', {{ name: '{spec_name}' }}).click();\n\n"
                        f"AFTER — use a role from the SUT's catalog:\n"
                        f"  // Available roles in this SUT: {_avail_roles}\n"
                        f"  await page.getByRole('{_first_role}', {{ name: '{_first_name}' }}).click();"
                        f"{_r118_e2_fallback}\n\n"
                        f"If the scenario truly needs role '{spec_role}', verify "
                        f"the gherkin maps to a SUT page that actually has that "
                        f"role exposed (may require R45.3 re-discovery)."
                    ),
                ))
                continue
            # Fuzzy name match: substring in either direction OR
            # case-insensitive equality.
            spec_name_lower = spec_name.lower()
            matched = any(
                cn == spec_name
                or cn.lower() == spec_name_lower
                or spec_name in cn
                or cn in spec_name
                for cn in candidates
            )
            if not matched:
                line_no = content[:match.start()].count("\n") + 1
                _avail_names = sorted(candidates)[:6]
                _first_name = _avail_names[0] if _avail_names else "<name>"
                # R117.B — when the rejected `spec_name` itself looks like
                # a smushed concatenation, split it into fragments and offer
                # the FIRST fragment as a `getByText` alternative. Pre-R117.B
                # the hint pointed to `_avail_names` from the catalog — but if
                # the catalog had smushed entries too (pre-R117.A), the
                # alternatives reinforced the hallucination. R117.B's split
                # gives the LLM a CORRECT fallback even when the catalog leaks.
                _r117_b_fragments = _r117_b_split_smushed_name(spec_name)
                _r117_b_block = ""
                if len(_r117_b_fragments) >= 2:
                    _r117_b_block = (
                        f"\n\nR117.B — `{spec_name[:60]}{'...' if len(spec_name) > 60 else ''}` "
                        f"looks like a SMUSHED concatenation of multiple DOM "
                        f"elements (not a single accessible-name). Likely "
                        f"fragments (split at camelCase + sentence boundaries):\n"
                        + "\n".join(
                            f"  {i + 1}. '{frag}'"
                            for i, frag in enumerate(_r117_b_fragments[:4])
                        )
                        + f"\n\nAFTER 2 — use `getByText` with the FIRST single-element fragment:\n"
                        f"  await page.getByText('{_r117_b_fragments[0]}');"
                    )
                out.append(GroundingViolation(
                    tool="playwright",
                    kind="hallucinated_role_name",
                    symbol=f"getByRole('{spec_role}', {{ name: '{spec_name}' }})",
                    location=f"line {line_no}",
                    hint=(
                        f"Name '{spec_name}' not found for role '{spec_role}' "
                        f"in the SUT's DOM catalog (verified via R45.3 discovery).\n\n"
                        f"BEFORE (BROKEN — selector returns 0 elements):\n"
                        f"  await page.getByRole('{spec_role}', {{ name: '{spec_name}' }}).click();\n\n"
                        f"AFTER 1 — use a name from the SUT's catalog for this role:\n"
                        f"  // Available names for role '{spec_role}': {_avail_names}\n"
                        f"  await page.getByRole('{spec_role}', {{ name: '{_first_name}' }}).click();"
                        f"{_r117_b_block}\n\n"
                        f"Note: name matching is fuzzy (substring + case-insensitive), "
                        f"so partial names like 'Submit' match 'Submit order'. Verify "
                        f"the scenario references a real SUT UI element."
                    ),
                ))

    # R140.A — getByLabel + getByText catalog grounding. Pre-R140.A
    # `validate_playwright_grounded` only validated testids + role+name
    # tuples. Live evidence (R135 Iter 1 run-10bf46): 19 PW FAILs from
    # hallucinated `getByLabel('Insight Metric')` + `getByLabel('Request
    # insight')` + `getByText('sales')` that don't exist in the DOM
    # catalog. R140.A adds two new violation kinds — both surface via
    # the existing R102.A/R102.C `playwright_grounding_violation` parent
    # reason (no backend stamp change needed; R111.E violation_kinds
    # aggregator surfaces the new kinds in the frontend tile per R140.B).
    #
    # Killswitch: ARTA_R140_GROUNDING_LABELS_DISABLE=1 skips both checks
    # (operator rollback path).
    if os.environ.get("ARTA_R140_GROUNDING_LABELS_DISABLE") != "1":
        catalog_labels = set((stable_selectors or {}).get("aria_labels") or set())
        catalog_texts = set((stable_selectors or {}).get("texts") or set())

        # getByLabel — exact match against the catalog's captured
        # aria-labels. Skip when catalog has zero labels (cold-start;
        # validator already warned via R55.5 above).
        if catalog_labels:
            for match in _LABEL_REF_RE.finditer(content):
                label_text = match.group(1)
                if label_text not in catalog_labels:
                    line_no = content[:match.start()].count("\n") + 1
                    _label_sample = sorted(catalog_labels, key=lambda s: len(s))[:5]
                    out.append(GroundingViolation(
                        tool="playwright",
                        kind="catalog_aria_label_unknown",
                        symbol=label_text,
                        location=f"line {line_no}",
                        hint=(
                            f"`getByLabel('{label_text}')` references an aria-label "
                            f"not in the SUT's DOM catalog. Playwright will return "
                            f"0 elements → `locator.click` / `toBeVisible` will "
                            f"time out at runtime.\n\n"
                            f"BEFORE (BROKEN):\n"
                            f"  await page.getByLabel('{label_text}').click();\n\n"
                            f"AFTER — use a label from the SUT's catalog:\n"
                            f"  // Available aria-labels in this SUT: {_label_sample}\n"
                            f"  await page.getByLabel('{_label_sample[0] if _label_sample else 'X'}').click();\n\n"
                            f"If '{label_text}' is essential to the scenario but absent "
                            f"from the catalog, the SUT's discovery probe may have "
                            f"missed the route. Operator: paste a fresh session token via R45.2; "
                            f"R140.C navigation-graph crawl will populate more routes "
                            f"in the catalog."
                        ),
                    ))

        # getByText — Playwright matches BY SUBSTRING. Accept the spec's
        # text when it exactly matches a catalog text OR appears as a
        # substring of any catalog text.
        #
        # R253.PW — ground against the UNION of ALL catalog text signals
        # (captured element texts ∪ aria-labels ∪ role-name labels), not just
        # the dedicated `texts` set. On testid-less SUTs the probe often
        # populates role_names/aria_labels richly but `texts` sparsely, so the
        # old `if catalog_texts:` gate SKIPPED validation entirely and let
        # hallucinated text through (run-d56816: 76 `getByText('BMC-unreachable'
        # / 'duplicate' / 'validation')` selectors for failure-state UI that the
        # read-only SUT never renders → toBeVisible timeouts). Validating
        # against the union catches these whenever the catalog has ANY signal.
        _role_name_texts = {
            n for (_r, n) in ((stable_selectors or {}).get("role_names") or set())
            if isinstance(n, str) and n
        }
        _text_grounding_set = set(catalog_texts) | set(catalog_labels) | _role_name_texts
        if _text_grounding_set:
            for match in _TEXT_REF_RE.finditer(content):
                spec_text = match.group(1)
                _matches = (
                    spec_text in _text_grounding_set
                    or any(spec_text in t for t in _text_grounding_set)
                )
                if not _matches:
                    line_no = content[:match.start()].count("\n") + 1
                    _text_sample = sorted(_text_grounding_set, key=lambda s: len(s))[:5]
                    out.append(GroundingViolation(
                        tool="playwright",
                        kind="catalog_text_unknown",
                        symbol=spec_text,
                        location=f"line {line_no}",
                        hint=(
                            f"`getByText('{spec_text}')` references visible text "
                            f"not in the SUT's DOM catalog (matched by Playwright's "
                            f"default substring semantics).\n\n"
                            f"BEFORE (BROKEN):\n"
                            f"  await expect(page.getByText('{spec_text}')).toBeVisible();\n\n"
                            f"AFTER — use a text from the SUT's catalog:\n"
                            f"  // Available texts in this SUT (sample): {_text_sample}\n"
                            f"  await expect(page.getByText('{_text_sample[0] if _text_sample else 'X'}')).toBeVisible();\n\n"
                            f"If '{spec_text}' is a Gherkin field-name being asserted "
                            f"verbatim (e.g., 'sales' for the Gherkin step `Then "
                            f"insight.metric should be 'sales'`), the spec is reading "
                            f"the LITERAL FIELD NAME from the UI — see R137.A. The "
                            f"correct assertion uses `expect(json.X).toEqual('sales')` "
                            f"on the API response, NOT `getByText('sales')` on the DOM."
                        ),
                    ))

    # R101.E — endpoint grounding for `page.request.<verb>(url)` AND
    # `request.<verb>(url)` calls in Playwright specs. Pre-R101.E Newman
    # had captured_endpoint validation (validate_newman_grounded) but
    # Playwright did not — so the LLM could emit `page.request.post(
    # `${apiBase}/api/v1/datasets`)` and the path got dispatched + 404'd
    # at runtime (run-8c03c9 TC-AM-013-AUTO001/002).
    #
    # The check uses captured_endpoints (same source Newman validates
    # against). When captured_endpoints is empty: skip silently
    # (cold-start project; R55.5 already warns above).
    if captured_endpoints:
        captured_paths_by_method: dict[str, list[str]] = {}
        for ep in captured_endpoints:
            if not isinstance(ep, dict):
                continue
            _method = (ep.get("method") or "GET").upper()
            _path = ep.get("path") or ep.get("url") or ""
            if isinstance(_path, str) and _path:
                captured_paths_by_method.setdefault(_method, []).append(_path)

        def _r101_e_normalize(p: str) -> str:
            """Collapse `{anything}`, `{{anything}}`, `${anything}`
            placeholders into a single `:p` token so structural match
            works across captured vs spec paths (captured uses {acct};
            spec may use {{account_id}} OR ${account_id}).

            R190 — ALSO collapse id-shaped LITERAL segments (UUID/hex/numeric)
            to `:p`. R180/R186 resolve `:org_id` to the literal session value, so
            the spec carries `/organization/424e744f-.../workspaces` while the
            captured path is templated `/organization/{organization_id}/...`.
            Without collapsing the literal id, the prefix `startswith` match (and
            the orphan-segment check) treated the resolved UUID as a hallucinated
            segment → FALSE unknown_endpoint that wrongly BLOCKED real endpoints.
            Now both sides normalize to `/organization/:p/workspaces` → match.
            """
            _q = re.sub(r"\$\{[^}]+\}", ":p", p)
            _q = re.sub(r"\{\{[^}]+\}\}", ":p", _q)
            _q = re.sub(r"\{[^}]+\}", ":p", _q)
            _q = "/".join(
                (":p" if (s and _R190_ID_SHAPED_RE.match(s)) else s)
                for s in _q.split("/")
            )
            return _q

        # R252.2 — resolve simple same-file const STRING literals into URL
        # templates before grounding. The R252 fabricated-id walker inspects
        # literals inside the URL, so `const serverId = 'mock-server-001'` one
        # line above `request.get(`.../servers/${encodeURIComponent(serverId)}`)`
        # guaranteed-404 FAILs). Only consts bound to a plain string literal
        # are substituted — env-derived / computed values keep their `${...}`
        # shape and still normalize to `:p` as before.
        _r252_2_consts = {
            _cm.group(1): _cm.group(2)
            for _cm in re.finditer(
                r"(?:const|let|var)\s+(\w+)\s*=\s*['\"`]([^'\"`\n]+)['\"`]",
                content,
            )
        }

        def _r252_2_resolve(url: str) -> str:
            return re.sub(
                r"\$\{\s*(?:encodeURIComponent\(\s*)?(\w+)\s*\)?\s*\}",
                lambda mm: _r252_2_consts.get(mm.group(1), mm.group(0)),
                url,
            )

        for m in _PW_API_REQUEST_RE.finditer(content):
            _http_verb = m.group(1).upper()
            _url_str = _r252_2_resolve(m.group(2))
            # Extract path portion: strip protocol+host (incl. template
            # vars like `${apiBase}`) so we compare against captured paths.
            _path_match = re.search(r"(/api/[^\s\?\#]+|/v\d+/[^\s\?\#]+|/[^\s/\?\#]+/[^\s\?\#]+)", _url_str)
            if not _path_match:
                continue
            _spec_path = _path_match.group(1)
            _spec_norm = _r101_e_normalize(_spec_path)
            # Check: any captured endpoint with the same HTTP verb starts
            # with the same first 2-3 path segments? (Per R97.C's prefix
            # match heuristic; both sides normalized.)
            _candidates_raw = captured_paths_by_method.get(_http_verb, [])
            _candidates_norm = [_r101_e_normalize(p) for p in _candidates_raw]
            _norm_segments = [s for s in _spec_norm.split("/") if s][:3]
            if len(_norm_segments) < 1:
                continue
            _prefix_check = "/" + "/".join(_norm_segments)
            _has_match = any(p.startswith(_prefix_check) for p in _candidates_norm)
            # R118.E.1 — segment-completeness secondary check. Pre-R118.E.1
            # the prefix-only match (first-3-segments only) let paths like
            # `/api/v1/datasets/pipeline/status` pass if any captured path
            # started with `/api/v1/datasets` — even though `pipeline` and
            # `status` weren't tokens in ANY captured path. R118.E.1
            # cross-checks EVERY spec segment (not just first 3) against
            # the union of all captured-endpoint tokens. Orphan segments
            # (absent from all captured paths AND not structural keywords
            # like 'api'/'v1') flip _has_match → False and surface the
            # orphan list in the violation hint.
            _orphan_segments: list[str] = []
            if _has_match:
                _all_captured_tokens: set[str] = set()
                for cp in _candidates_norm:
                    for tok in cp.split("/"):
                        tok = tok.strip()
                        if tok and not tok.startswith(":") and not tok.startswith("{"):
                            _all_captured_tokens.add(tok.lower())
                # Scan ALL spec segments (not just the first-3 used by
                # _prefix_check) — orphans can appear at any position.
                # R190 — EXCLUDE id-shaped segments (UUIDs, long hex, numeric).
                # R180/R186 RESOLVE templated path params (`:org_id`) to their
                # literal session values (e.g. `424e744f-94a5-4aae-...`), but
                # captured paths store them TEMPLATED as `{organization_id}`,
                # whose tokens are dropped from `_all_captured_tokens` (they
                # start with `{`). So a resolved literal UUID looked like an
                # "orphan" → FALSE unknown_endpoint that wrongly BLOCKED specs
                # whose endpoint IS real (e.g. GET /organization/<uuid>/workspaces).
                # An id-shaped segment corresponds to a `{var}` wildcard — never
                # an orphan.
                _all_spec_segments = [
                    s.strip().lower() for s in _spec_norm.split("/")
                    if s.strip() and not s.strip().startswith(":") and not s.strip().startswith("{")
                    and not _R190_ID_SHAPED_RE.match(s.strip())
                ]
                _STRUCTURAL = {"api", "v1", "v2", "v3", "v4", "rest", "graphql"}
                _orphan_segments = [
                    t for t in _all_spec_segments
                    if t not in _all_captured_tokens and t not in _STRUCTURAL
                ]
                if _orphan_segments:
                    # Prefix matched a structural ancestor, but spec
                    # contains tokens that never appear in any captured
                    # path. Treat as unknown_endpoint with segment-level
                    # diagnostic.
                    _has_match = False
            # R227 — POSITIONAL TEMPLATE-ALIGNMENT rescue (GENERIC). The
            # global-token orphan check (R118.E.1) has NO positional
            # awareness: a fabricated BUSINESS-ID path-param value that isn't
            # UUID/numeric-shaped — so R190 doesn't collapse it to `:p` —
            # looks like an orphan literal → FALSE unknown_endpoint, even
            # though the endpoint TEMPLATE is REAL and the segment merely
            # `GET /AssetManagement/api/Lease/GetLeaseHistoryByAssetId/
            # ASSET-20260115-001` (also `ASSET-UNLISTED-999`) — the
            # asset-id format `ASSET-<date>-<seq>` is neither UUID nor
            # numeric. Rescue: if ANY captured template of the SAME verb
            # aligns POSITIONALLY (same segment count; every LITERAL segment
            # matches case-insensitively; the spec's extra segment sits
            # exactly where the template has a param `:p`), the endpoint is
            # grounded. A genuinely-hallucinated path (no captured template
            # with matching literals at the same positions) still fails →
            # fail-fast preserved. Killswitch ARTA_R227_POS_ALIGN_DISABLE=1.
            if not _has_match and os.environ.get("ARTA_R227_POS_ALIGN_DISABLE") != "1":
                _spec_segs = [s for s in _spec_norm.split("/") if s]
                for _cand in _candidates_norm:
                    _cand_segs = [s for s in _cand.split("/") if s]
                    if len(_cand_segs) != len(_spec_segs):
                        continue
                    if all(_t == ":p" or _t.lower() == _s.lower()
                           for _s, _t in zip(_spec_segs, _cand_segs)):
                        _has_match = True
                        _orphan_segments = []
                        break
            # R252 (WS1c) — the endpoint can be REAL while its DATA is invented.
            #
            # R227 directly above deliberately RESCUES a real template carrying
            # a fabricated business-id param (its own examples:
            # `ASSET-20260115-001`, `ASSET-UNLISTED-999`) from a FALSE
            # unknown_endpoint. That rescue is CORRECT for endpoint grounding —
            # the `{assetId}` template does exist — but it is blind to whether
            # the id is real. Post-R227 a fabricated id passes every gate and
            # 404s at runtime, which R258 then has to triage after the fact.
            # R252 catches it at GEN time, as a different kind with a different
            # remedy: not "wrong endpoint" but "invented data".
            #
            # Mode (ARTA_R252_FABRICATED_ID_MODE):
            #   off   — skip entirely
            #   flag  — LOG only (default): measure the real rate before
            #           enforcing, since a violation here feeds the R57.1 retry
            #           ladder and can end in an R102.C dispatch BLOCK.
            #   block — return violations -> retry-with-hint -> truthful BLOCK.
            if _has_match:
                _r252_mode = os.environ.get(
                    "ARTA_R252_FABRICATED_ID_MODE", "flag").strip().lower()
                if (_r252_mode != "off"
                        and os.environ.get("ARTA_R252_FABRICATED_ID_DISABLE") != "1"):
                    try:
                        # R252.N — an INTENTIONALLY-invalid id in a NEGATIVE
                        # test is correct test design, not fabricated data.
                        #   test('AC-OR-012-03.3: Very large account ID returns error')
                        #     GET /api/ExportBillingDataByAccountId/999999999999999999999
                        # The id is SUPPOSED to be unreal — the test asserts the
                        # SUT rejects it. Flagging it is the same false-positive
                        # class R255 hit: absence of realism is the POINT here.
                        _r252_ctx = content[max(0, m.start() - 700):m.start() + 300].lower()
                        if _R252_NEGATIVE_CTX_RE.search(_r252_ctx):
                            continue
                        _r252_caps = {
                            ((ep.get("method") or "GET").upper(),
                             ep.get("path") or ep.get("url") or "")
                            for ep in captured_endpoints
                            if isinstance(ep, dict) and (ep.get("path") or ep.get("url"))
                        }
                        # Lazy imports: automation_engineer imports THIS module,
                        # so a module-level import would be circular. At call
                        # time both are already resolved — and this keeps
                        # _R186_PARAM_TO_SESSION_ID a single source of truth
                        # rather than duplicating the slot list here.
                        try:
                            from .automation_engineer import _R186_PARAM_TO_SESSION_ID as _r186m
                            _r252_sess = set(_r186m.keys())
                        except Exception:
                            _r252_sess = set()
                        from .real_id_store import all_real_id_values as _arv
                        _r252_v = validate_path_literal_ids(
                            _http_verb, _spec_path, _r252_caps,
                            real_ids=_arv(project_id),
                            session_slots=_r252_sess,
                            tool="playwright",
                            location=f"line {content[:m.start()].count(chr(10)) + 1}",
                        )
                        if _r252_v and _r252_mode == "block":
                            out.extend(_r252_v)
                        elif _r252_v:
                            log.warning(
                                "R252 [flag mode]: %s carries %d fabricated id(s) "
                                "%s — real endpoint, invented data (guaranteed "
                                "404). Set ARTA_R252_FABRICATED_ID_MODE=block to "
                                "enforce.",
                                f"{_http_verb} {_spec_path}", len(_r252_v),
                                [v.symbol for v in _r252_v],
                            )
                    except Exception as _r252_exc:
                        log.debug("R252: fabricated-id check skipped: %s", _r252_exc)
            if not _has_match:
                line_no = content[:m.start()].count("\n") + 1
                _alts = _candidates_raw[:5]
                _first_alt = _alts[0] if _alts else _spec_path
                _orphan_block = ""
                if _orphan_segments:
                    _orphan_block = (
                        f"\n\nR118.E.1 — orphan path segment(s) detected "
                        f"(not present in any captured endpoint):\n"
                        f"  {_orphan_segments}\n"
                        f"The SUT has zero endpoints with these segments "
                        f"as path tokens. The structural prefix "
                        f"{_prefix_check} matched, but the inner segments "
                        f"are hallucinated."
                    )
                out.append(GroundingViolation(
                    tool="playwright",
                    kind="unknown_endpoint",
                    symbol=f"{_http_verb} {_spec_path}",
                    location=f"line {line_no}",
                    hint=(
                        f"`{_http_verb} {_spec_path}` is NOT in the SUT's "
                        f"captured_endpoints (R45.3 runtime discovery).\n\n"
                        f"BEFORE (BROKEN — runtime 404):\n"
                        f"  const resp = await page.request.{_http_verb.lower()}("
                        f"`${{apiBase}}{_spec_path}`);\n\n"
                        f"AFTER — use a captured path with prefix {_prefix_check}:\n"
                        f"  // Captured {_http_verb} endpoints under this prefix: {_alts}\n"
                        f"  const resp = await page.request.{_http_verb.lower()}("
                        f"`${{apiBase}}{_first_alt}`);"
                        f"{_orphan_block}\n\n"
                        f"If no captured path fits the scenario, verify the "
                        f"gherkin matches the SUT's actual API surface (may "
                        f"require R45.3 re-discovery against fresh auth)."
                    ),
                ))

    # R150.E — PW response-assertion field grounding. Composes additively
    # with existing testid/role/label/text/endpoint checks above.
    # Conservative skip when no grounded paths (cold-start).
    out.extend(_r150_e_validate_pw_response_assertions(
        content,
        captured_endpoints=captured_endpoints,
        expected_outputs=expected_outputs,
    ))

    # R313 — VALUE-domain grounding (vs R150.E's PATH grounding): flag GUESSED enum
    # assertions (`const X=[...]; expect(X).toContain(body.field)`) that omit a real
    # observed value. Cold-start-safe: no captured value domain → no-op.
    _r313_domains = _r313_value_domains_from_captured(captured_endpoints)
    if _r313_domains:
        out.extend(_r313_validate_pw_value_domain(content, _r313_domains))
        # R313.E — the toBe(literal) sibling: direct-equality on a mutable runtime
        # field (the dominant residual FAIL class after the toContain enum fix).
        out.extend(_r313_e_validate_tobe_literal(content, _r313_domains))

    return out


# ────────────────────────────────────────────────────────────────────────────
# k6
# ────────────────────────────────────────────────────────────────────────────

_K6_ENV_REF_RE = re.compile(r"__ENV\.([A-Za-z_][A-Za-z0-9_]*)")


_K6_HTTP_URL_RE = re.compile(
    # R213.K.10 — match BOTH the raw `http.<m>(` calls AND the R213.K.8 per-family
    # wrappers (`artaGet(`/`artaPost(`/...), so endpoint grounding works on both
    # original and K.8-rewritten k6 specs (the dispatch gate scans rewritten ones).
    r"(?:http\.(?:get|post|put|patch|del|delete|head|options)"
    r"|arta(?:Get|Post|Put|Patch|Del|Head|Options))\s*\(\s*[`'\"]([^`'\"]+)[`'\"]",
    re.IGNORECASE,
)


def validate_k6_grounded(
    content: str,
    *,
    env_vars: dict | None = None,
    captured_endpoints: list[dict] | None = None,
) -> list[GroundingViolation]:
    """Walk a k6 script, surface __ENV.X references for X never
    declared in the project's env vars + (R124.A) URLs not in the
    SUT's discovered endpoint catalog.

    `env_vars` is the operator's project-level env-var DECLARATION
    (keys, not values). A k6 script that references an undeclared key
    will receive `undefined` at runtime → URL becomes 'http://undefined'
    → all checks fail.

    `captured_endpoints` (R124.A) is the project's R45.3 discovery
    harvest. When provided, k6 http.X() calls whose path doesn't match
    any captured endpoint are flagged as `unknown_endpoint` — same
    contract as Newman/PW grounding, closes the k6 hallucination gap
    from run-d52a8c.
    """
    out: list[GroundingViolation] = []
    if not content:
        return out
    declared = set((env_vars or {}).keys())
    if not declared:
        # R55.5 — cold-start blind-spot WARN for k6.
        log.warning(
            "R55.5: k6 grounding SKIPPED — no env vars declared yet "
            "(cold-start). Generated k6 script is UNVERIFIED for "
            "undeclared __ENV.X references; run discovery first."
        )
    # Always-present k6 env keys (set by the runtime / operator wrapper)
    declared.update({"BASE_URL", "API_BASE_URL", "TARGET_BASE_URL", "TARGET_API_BASE_URL"})
    seen: set[str] = set()
    for match in _K6_ENV_REF_RE.finditer(content):
        var = match.group(1)
        if var in seen:
            continue
        seen.add(var)
        # R72.2 — vars matching R43 substitution patterns are implicitly
        # declared (ARTA produces synthetic values at dispatch). Skip
        # flagging them so the LLM isn't penalised for `__ENV.USER_ID` /
        # `__ENV.VERSION` etc.
        if is_r43_substitutable_name(var):
            continue
        if declared and var not in declared:
            line_no = content[:match.start()].count("\n") + 1
            _avail = sorted(declared)[:8]
            _first_var = _avail[0] if _avail else "BASE_URL"
            out.append(GroundingViolation(
                tool="k6",
                kind="unset_env_var",
                symbol=var,
                location=f"line {line_no}",
                hint=(
                    f"`__ENV.{var}` references an env var not declared in the "
                    f"project AND not matching any R43 substitution pattern.\n\n"
                    f"BEFORE (BROKEN — k6 sees undefined → URL like 'http://undefined'):\n"
                    f"  http.get(`${{__ENV.{var}}}/api/path`);\n\n"
                    f"AFTER 1 — use a declared env var:\n"
                    f"  // Declared vars: {_avail}\n"
                    f"  http.get(`${{__ENV.{_first_var}}}/api/path`);\n\n"
                    f"AFTER 2 — declare `{var}` in projects.json env_block.variables "
                    f"OR rename to one of the R43 patterns (USER_ID, VERSION, "
                    f"DATASET_ID, etc.) which ARTA substitutes at dispatch."
                ),
            ))
        elif not declared:
            # R72.2 cold-start visibility (matches Newman branch).
            line_no = content[:match.start()].count("\n") + 1
            log.warning(
                "R72.2: k6 cold-start grounding — would flag undeclared "
                "__ENV.%s at line %d (discovery hasn't harvested env vars)",
                var, line_no,
            )

    # R124.A — k6 endpoint grounding (parallel to Newman/PW). Walks
    # http.X(`...`) URLs + flags paths not in captured_endpoints.
    if captured_endpoints:
        captured_paths: set[str] = set()
        for ep in captured_endpoints:
            if isinstance(ep, dict):
                p = ep.get("path") or ep.get("url") or ""
                if p:
                    captured_paths.add(p)
        seen_urls: set[str] = set()
        for m in _K6_HTTP_URL_RE.finditer(content):
            raw = m.group(1)
            if raw in seen_urls:
                continue
            seen_urls.add(raw)
            # Extract path component — strip protocol/host AND template
            # interpolations like ${__ENV.BASE_URL}. The remaining is
            # the static path suffix that should match captured.
            path = re.sub(r"\$\{[^}]+\}", "", raw)
            # Drop scheme + host if present
            path = re.sub(r"^https?://[^/]+", "", path)
            # Drop query string
            path = path.split("?", 1)[0]
            path = path.strip()
            if not path or not path.startswith("/"):
                continue
            # Check if the path matches any captured endpoint
            # (prefix match — captured paths may be templated like /api/v1/{id})
            matched = False
            for cp in captured_paths:
                # Normalize both: collapse {param} to placeholder
                norm_cp = re.sub(r"\{[^}]+\}", "{}", cp)
                norm_path = re.sub(r"\{[^}]+\}", "{}", path)
                if norm_path == norm_cp or norm_path.startswith(norm_cp):
                    matched = True
                    break
            if not matched:
                line_no = content[:m.start()].count("\n") + 1
                _alts = sorted(captured_paths)[:5]
                out.append(GroundingViolation(
                    tool="k6",
                    kind="unknown_endpoint",
                    symbol=path,
                    location=f"line {line_no}",
                    hint=(
                        f"k6 http call to `{path}` — this path is NOT in the SUT's "
                        f"discovered endpoint catalog ({len(captured_paths)} known).\n\n"
                        f"BEFORE (BROKEN — SUT returns 404):\n"
                        f"  http.get(`${{__ENV.BASE_URL}}{path}`);\n\n"
                        f"AFTER — use a real endpoint from the SUT (top 5 alternatives):\n"
                        + "\n".join(f"  http.get(`${{__ENV.BASE_URL}}{a}`);" for a in _alts)
                    ),
                ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Pytest (analytics)
# ────────────────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────────────────
# R123.A parity for the pytest/analytics lane — undefined-symbol validator.
# The pytest gen loop ran syntax (ast.parse) + assertion-value-drift only; a
# mangled call (e.g. `assert_intNone_consistent`) survived gen-time and was a
# NameError waiting to fire. Playwright has R123.A for exactly this; pytest had
# no equivalent. This closes the gap AT THE SOURCE (gen-time), not post-hoc lint.
# ────────────────────────────────────────────────────────────────────────────

_PY_BUILTINS = frozenset(dir(_builtins)) | {
    "__name__", "__file__", "__doc__", "self", "cls", "__class__", "__init__",
}


class _DeclaredCollector(ast.NodeVisitor):
    """Collect EVERY name bound anywhere in the module (imports, def/class,
    assignment targets, params, for/with/except/comprehension targets,
    global/nonlocal). Conservative by design: a name bound in ANY scope counts as
    declared, so we only ever flag names that appear NOWHERE (typos / mangled
    symbols) — never a legitimately-scoped name (no scoping false positives)."""

    def __init__(self):
        self.declared: set[str] = set()

    def _bind(self, name):
        if name:
            self.declared.add(name)

    def _bind_args(self, args):
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            self._bind(a.arg)
        if args.vararg:
            self._bind(args.vararg.arg)
        if args.kwarg:
            self._bind(args.kwarg.arg)

    def visit_Import(self, node):
        for a in node.names:
            self._bind((a.asname or a.name).split(".")[0])

    def visit_ImportFrom(self, node):
        for a in node.names:
            if a.name != "*":
                self._bind(a.asname or a.name)

    def visit_FunctionDef(self, node):
        self._bind(node.name)
        self._bind_args(node.args)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._bind(node.name)
        self.generic_visit(node)

    def visit_Lambda(self, node):
        self._bind_args(node.args)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._bind(node.id)
        self.generic_visit(node)

    def visit_Global(self, node):
        for n in node.names:
            self._bind(n)

    visit_Nonlocal = visit_Global

    def visit_ExceptHandler(self, node):
        self._bind(node.name)
        self.generic_visit(node)


_ARTA_RUNTIME_EXPORTS: set[str] | None = None


def _arta_runtime_exports() -> set[str]:
    """Statically-parsed top-level names of the arta_runtime package (no import,
    so it works at gen time). Used to catch `from arta_runtime import <typo>` —
    an ImportError bare undefined-name checks (and ruff F821) miss."""
    global _ARTA_RUNTIME_EXPORTS
    if _ARTA_RUNTIME_EXPORTS is not None:
        return _ARTA_RUNTIME_EXPORTS
    names: set[str] = set()
    try:
        p = (Path(__file__).resolve().parents[1] / "automation" / "python_tests"
             / "arta_runtime" / "__init__.py")
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for tg in node.targets:
                    if isinstance(tg, ast.Name):
                        names.add(tg.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name != "*":
                        names.add(a.asname or a.name.split(".")[0])
    except Exception:
        pass  # unavailable → skip the import-mismatch check (bare-undef still runs)
    _ARTA_RUNTIME_EXPORTS = names
    return names


def validate_pytest_undefined_symbols(content: str) -> list[GroundingViolation]:
    """R123.A parity — flag bare undefined names (NameError) and bad
    `from arta_runtime import` names (ImportError) in generated pytest, at gen
    time. Killswitch ARTA_PYTEST_UNDEF_DISABLE=1."""
    out: list[GroundingViolation] = []
    if not content or os.environ.get("ARTA_PYTEST_UNDEF_DISABLE") == "1":
        return out
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return out  # syntax is caught upstream (_validate_pytest_code)
    coll = _DeclaredCollector()
    coll.visit(tree)
    declared = coll.declared | _PY_BUILTINS

    # A `from X import *` brings in unknown names — the bare-undefined check would
    # false-positive on them, so skip it when a star-import is present. The
    # import-mismatch check (2) is unaffected and still runs.
    _has_star = any(
        isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
        for n in ast.walk(tree)
    )

    # (1) bare undefined names — used/called but bound nowhere.
    seen: set[str] = set()
    for node in ast.walk(tree) if not _has_star else []:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            nm = node.id
            if nm not in declared and nm not in seen:
                seen.add(nm)
                out.append(GroundingViolation(
                    tool="pytest", kind="undefined_symbol", symbol=nm,
                    location=f"line {getattr(node, 'lineno', '?')}",
                    hint=(f"`{nm}` is used but never imported or defined — a NameError "
                          f"at runtime. Only call helpers you import (e.g. from arta_runtime) "
                          f"or define; do not invent names."),
                ))

    # (2) import-mismatch — `from arta_runtime import <name>` that isn't exported.
    exports = _arta_runtime_exports()
    if exports:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "arta_runtime":
                for a in node.names:
                    if a.name != "*" and a.name not in exports and a.name not in seen:
                        seen.add(a.name)
                        out.append(GroundingViolation(
                            tool="pytest", kind="undefined_import", symbol=a.name,
                            location=f"line {getattr(node, 'lineno', '?')}",
                            hint=(f"`{a.name}` is imported from arta_runtime but that "
                                  f"module does not export it — an ImportError at runtime. "
                                  f"Use an actual arta_runtime helper."),
                        ))
    return out


def validate_pytest_grounded(
    content: str,
    *,
    recipe: dict | None = None,
    ac_id: str | None = None,
) -> list[GroundingViolation]:
    """R44.1 — pytest assertion grounding.

    Pytest analytics tests must assert against values the recipe
    materialises in `expected_outputs` (or `expected_outputs_by_ac`
    for per-AC variants). Pre-R44.1 the LLM was free to invent
    assertion values — run-349a4d had 58/224 pytest fails dominated
    by this drift (asserts citing values the recipe never
    materialised → fixture data didn't match → AssertionError).

    Approach:
      1. Pull the union of expected_outputs values + their
         JSON-stringified forms (numbers, dates, strings).
      2. AST-parse the pytest content to find every `assert <expr>`
         statement's literal RHS values.
      3. For each assert RHS literal that's a number/string,
         require it to appear somewhere in the recipe's
         expected_outputs.

    Skipped when recipe is None or empty (operator pre-recipe runs;
    no signal to enforce).

    NOTE: assertions on shape (e.g. `assert len(rows) == 100`,
    `assert isinstance(x, dict)`) are NOT grounded — the literal
    `100` may legitimately be the recipe's row_count, but matching
    that requires deeper recipe traversal. We allow integer
    literals ≤ 1000 to pass through as "structural" assertions.
    """
    out: list[GroundingViolation] = []
    if not content or not isinstance(recipe, dict):
        return out

    # Collect grounding values from the recipe.
    grounding_values: set[str] = set()
    grounding_values.update(_collect_recipe_values(recipe.get("expected_outputs")))
    by_ac = recipe.get("expected_outputs_by_ac") or {}
    if isinstance(by_ac, dict):
        if ac_id and ac_id in by_ac:
            grounding_values.update(_collect_recipe_values(by_ac[ac_id]))
        else:
            # No specific AC → union of all per-AC values is fair game.
            for v in by_ac.values():
                grounding_values.update(_collect_recipe_values(v))
    if not grounding_values:
        return out

    # Walk the AST for `assert <left> <op> <right>` style statements.
    try:
        import ast as _ast
        tree = _ast.parse(content)
    except SyntaxError:
        # Truncation / malformed code is caught by _validate_pytest_code
        # upstream — don't flag grounding violations on invalid AST.
        return out

    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Assert):
            continue
        # Walk the assertion expression for Constant literals (str/num).
        for sub in _ast.walk(node.test):
            if not isinstance(sub, _ast.Constant):
                continue
            val = sub.value
            if val is None or isinstance(val, bool):
                continue
            if isinstance(val, int) and abs(val) <= 1000:
                # Treat small ints as structural — could be row_count,
                # an index, etc. Don't ground these.
                continue
            literal = str(val).strip()
            if len(literal) < 3:
                continue
            if literal in grounding_values:
                continue
            # Acceptable substring match — recipe value "Q1 2024"
            # vs assertion "Q1 2024 (preview)" is fine.
            if any(literal in g or g in literal for g in grounding_values):
                continue
            _recipe_sample = sorted(grounding_values)[:6]
            _first_val = _recipe_sample[0] if _recipe_sample else "<expected>"
            out.append(GroundingViolation(
                tool="pytest",
                kind="assertion_value_drift",
                symbol=literal[:120],
                location=f"line {sub.lineno}",
                hint=(
                    f"assert at line {sub.lineno} cites value `{literal!r}` "
                    f"which is NOT in the recipe's expected_outputs.\n\n"
                    f"BEFORE (BROKEN — assertion drifts from recipe):\n"
                    f"  assert response.insight.metric == {literal!r}\n\n"
                    f"AFTER 1 — use a recipe-materialised value:\n"
                    f"  # Recipe values: {_recipe_sample}\n"
                    f"  assert response.insight.metric == {_first_val!r}\n\n"
                    f"AFTER 2 — use tolerant_assert for stub-default cases:\n"
                    f"  from arta_runtime import tolerant_assert\n"
                    f"  tolerant_assert(response.insight.metric, {_first_val!r})\n\n"
                    f"AFTER 3 — defensive attr access for forward-compat (R95.5/R97.B):\n"
                    f"  assert getattr(response.insight, 'metric', None) == {_first_val!r}"
                ),
            ))
    return out


def _collect_recipe_values(eo: Any) -> set[str]:
    """Flatten an expected_outputs dict/list/scalar into a set of
    str-valued literals. Handles nested structures the LLM commonly
    produces in recipes."""
    out: set[str] = set()
    if eo is None:
        return out
    if isinstance(eo, dict):
        for v in eo.values():
            out.update(_collect_recipe_values(v))
        return out
    if isinstance(eo, list):
        for v in eo:
            out.update(_collect_recipe_values(v))
        return out
    if isinstance(eo, bool):
        return out  # not useful for grounding
    if isinstance(eo, (int, float)):
        out.add(str(eo))
        return out
    if isinstance(eo, str):
        s = eo.strip()
        if len(s) >= 3:
            out.add(s)
    return out


# ────────────────────────────────────────────────────────────────────────────
# Loader helpers
# ────────────────────────────────────────────────────────────────────────────
#
# R130.F — `load_dom_catalog` previously lived here as an orphan (zero
# callers per `grep -rn`). The production loader is at
# `api_discovery.py:945` (returns empty dict on miss; 7 production callers).
# Two loaders with different return-type semantics (None vs {}) was a
# latent footgun — a future refactor swapping imports could silently
# weaken grounding validation. Removed per R130.F single-source-of-truth
# principle. Use `from .api_discovery import load_dom_catalog` instead.


def load_project_env_vars(project_id: str, environment: str = "staging") -> dict | None:
    """Read declared env-var KEYS for the project's environment block.
    Used by Newman + k6 grounding. Values aren't checked here (R36.2
    handles unfilled values); we only care that the key is declared.
    """
    if not project_id:
        return None
    try:
        from ..api.routers.projects import _PROJECTS
    except Exception:
        return None
    project = _PROJECTS.get(project_id) or {}
    env_block = (project.get("environments") or {}).get(environment) or {}
    return dict(env_block.get("variables") or {})


def validate_recipe_grounded(
    recipe: dict,
    *,
    captured_endpoints: list[dict] | None = None,
) -> list[GroundingViolation]:
    """R55.12 — WARN-level open-loop check that a DatasetRecipe's columns
    + expected_outputs reference symbols somewhat consistent with the
    SUT's known response shapes.

    Analytics recipes legitimately introduce DERIVED columns
    (e.g., `nl_to_query`, `result_to_insight`) that don't map 1:1 to any
    SUT API response — so this validator emits WARN, never BLOCK. The
    recipe_verifier (closed-loop) remains the authority for whether the
    recipe actually produces correct outputs.

    Args:
        recipe: serialised DatasetRecipe dict (`columns`, `expected_outputs`,
                `expected_outputs_by_ac`)
        captured_endpoints: list of dicts with `response_body_shape` keys,
                            loaded via api_discovery._load_captured_endpoints

    Returns:
        list[GroundingViolation] with kind="recipe_column_not_in_sut_shape".
        Empty list when captured_endpoints unavailable (cold-start; matches
        R55.5's convention of skip-with-WARN-log rather than false alarm).
    """
    out: list[GroundingViolation] = []
    if not recipe or not isinstance(recipe, dict):
        return out
    if not captured_endpoints:
        log.debug("R55.12: recipe grounding skipped — no captured_endpoints")
        return out

    # Recursively collect all keys observed across captured response shapes.
    sut_keys: set[str] = set()

    def _collect_keys(shape: object, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(shape, dict):
            for k, v in shape.items():
                if isinstance(k, str):
                    sut_keys.add(k.lower())
                _collect_keys(v, depth + 1)
        elif isinstance(shape, list) and shape:
            _collect_keys(shape[0], depth + 1)

    for ep in captured_endpoints or []:
        if isinstance(ep, dict):
            _collect_keys(ep.get("response_body_shape"))

    if not sut_keys:
        log.debug(
            "R55.12: recipe grounding skipped — 0 keys in captured response shapes"
        )
        return out

    # Allow-list pipeline-internal column prefixes that legitimately diverge
    # from SUT response shape. These match the analytics layer test_ids
    # (e.g., TC-AM-014-analytics-nl_to_query → column `nl_to_query`).
    PIPELINE_PREFIXES = (
        "nl_to_", "query_to_", "result_to_", "insight_to_", "_arta_",
        "partition_", "synth_",
    )

    candidates: list[tuple[str, str]] = []   # (location, name)
    for col in (recipe.get("columns") or []):
        if isinstance(col, dict):
            name = col.get("name")
            if isinstance(name, str):
                candidates.append((f"columns[{name}]", name))
    for k in (recipe.get("expected_outputs") or {}):
        if isinstance(k, str):
            candidates.append(("expected_outputs", k))
    for ac_id, ac_outs in (recipe.get("expected_outputs_by_ac") or {}).items():
        if isinstance(ac_outs, dict):
            for k in ac_outs:
                if isinstance(k, str):
                    candidates.append((f"expected_outputs_by_ac[{ac_id}]", k))

    seen: set[tuple[str, str]] = set()
    for location, name in candidates:
        key = (location, name)
        if key in seen:
            continue
        seen.add(key)
        n_low = name.lower()
        if any(n_low.startswith(p) for p in PIPELINE_PREFIXES):
            continue
        if n_low in sut_keys:
            continue
        out.append(GroundingViolation(
            tool="recipe",
            kind="recipe_column_not_in_sut_shape",
            symbol=name,
            location=location,
            hint=(
                f"Column `{name}` cited in recipe.{location} but absent "
                f"from all {len(sut_keys)} keys observed in captured SUT "
                f"response shapes. If this is a derived/pipeline-internal "
                f"column, prefix it with one of {PIPELINE_PREFIXES} to "
                f"signal intent. Otherwise: re-run discovery to refresh the "
                f"captured-endpoints store, OR update the recipe to use a "
                f"column the SUT actually returns."
            ),
        ))
    return out


def format_violations_as_hint(
    violations: list[GroundingViolation],
    *,
    captured_endpoints: list[dict] | None = None,
) -> str:
    """Compose a single hint string the LLM sees on retry. Caps at 5
    violations + 1200 chars to keep prompts compact.

    R93.B — when a violation is `unknown_endpoint`, surface VALID
    ALTERNATIVES from the captured_endpoints store under the rejected
    endpoint's path-prefix. Pre-R93.B the hint only said "X is invalid"
    — LLM kept hallucinating different invalid endpoints on retry. With
    valid alternatives the LLM has a positive recovery signal.

    Live evidence (run 97a2e255): 96 of 1446 Newman fails were
    `unknown_endpoint` violations. Dominant pattern: LLM invented paths
    like `POST /api/v1/pipeline/run`, `GET /api/analytics/embed` —
    plausible-sounding but absent from the SUT's 472 captured endpoints.
    """
    if not violations:
        return ""
    bullet_lines: list[str] = []
    for v in violations[:5]:
        bullet_lines.append(f"- {v.hint}")
        # R93.B — append VALID ALTERNATIVES for unknown_endpoint violations
        if v.kind == "unknown_endpoint" and captured_endpoints:
            alternatives = _r93_b_find_alternative_endpoints(
                rejected_symbol=v.symbol or "",
                captured_endpoints=captured_endpoints,
                limit=5,
            )
            if alternatives:
                bullet_lines.append("  VALID ALTERNATIVES from captured_endpoints (use one of these):")
                for alt in alternatives:
                    bullet_lines.append(f"    - {alt}")
    extra = (
        f"\n  (+ {len(violations) - 5} more violations)"
        if len(violations) > 5 else ""
    )
    blob = "Grounding violations from previous gen:\n" + "\n".join(bullet_lines) + extra
    return blob[:1200]


def _r93_b_find_alternative_endpoints(
    *, rejected_symbol: str, captured_endpoints: list[dict], limit: int = 5,
) -> list[str]:
    """R93.B helper — given a rejected endpoint symbol like
    "POST /api/v1/pipeline/run", return up to `limit` REAL endpoints
    from captured_endpoints that share the rejected path's prefix.

    Strategy: extract the path-prefix (first 2-3 segments) and find
    captured endpoints starting with that prefix. If no prefix match,
    fall back to "any endpoint with this method".
    """
    import re as _re_r93_b
    m = _re_r93_b.match(
        r"(\w+)\s+(/[^/?#]+(?:/[^/?#]+){0,2})", rejected_symbol or "",
    )
    if not m:
        return []
    method = m.group(1).upper()
    prefix = m.group(2)

    matches: list[str] = []
    seen: set[str] = set()
    # Tier 1: same-prefix matches (most relevant)
    for ep in captured_endpoints or []:
        if not isinstance(ep, dict):
            continue
        ep_path = ep.get("path") or ep.get("url") or ""
        ep_method = (ep.get("method") or "GET").upper()
        if not isinstance(ep_path, str) or not ep_path:
            continue
        if ep_path.startswith(prefix):
            key = f"{ep_method} {ep_path}"
            if key not in seen:
                seen.add(key)
                matches.append(key)
                if len(matches) >= limit:
                    return matches

    # Tier 2: any endpoint with the same method (broader fallback)
    if len(matches) < limit:
        for ep in captured_endpoints or []:
            if not isinstance(ep, dict):
                continue
            ep_path = ep.get("path") or ep.get("url") or ""
            ep_method = (ep.get("method") or "GET").upper()
            if not isinstance(ep_path, str) or not ep_path:
                continue
            if ep_method == method:
                key = f"{ep_method} {ep_path}"
                if key not in seen:
                    seen.add(key)
                    matches.append(key)
                    if len(matches) >= limit:
                        break
    return matches


def _r97_c_classify_irrecoverable(
    violations: list[GroundingViolation],
    captured_endpoints: list[dict] | None,
) -> bool:
    """R97.C — classify whether a grounding violation set is architecturally
    irrecoverable (i.e., the requirement has NO API surface in the SUT).

    Returns True when EVERY violation is an `unknown_endpoint` AND each
    rejected endpoint's path-prefix matches NO captured endpoint. Such
    requirements should route to UI-only (Playwright) testing rather
    than loop forever on grounding retries.

    Live evidence (run-a1f111): 14 Newman reqs (req_am_001 OAuth,
    req_am_002 Create Org, req_am_003 gRPC, ...) all BLOCKED with
    grounding_violation. Some genuinely lack an API surface (e.g.
    Google OAuth happens out-of-band at the IdP, not on
    the SUT backend host); others were healable via R93.B retry.
    R97.C distinguishes the two states so the operator dashboard
    surfaces architectural truth instead of opaque "grounding violation".

    Edge cases:
    - Empty captured_endpoints → False (insufficient evidence to conclude
      irrecoverability; treat as needs-discovery rather than no-API).
    - Mixed kinds (unknown_endpoint + recipe_column_not_in_sut_shape) →
      False (don't conflate distinct architectural issues).
    - Empty violations → False (nothing to classify).
    - Note: uses ONLY the Tier-1 prefix match (NOT the Tier-2 same-method
      fallback) because Tier-2's broad "any GET endpoint" suggestion is a
      generic recovery hint, not an architectural match for the rejected
      endpoint's domain.
    """
    if not violations or not captured_endpoints:
        return False
    import re as _re_r97_c
    for v in violations:
        if v.kind != "unknown_endpoint":
            return False   # mixed kinds → not purely no-API
        m = _re_r97_c.match(
            r"(\w+)\s+(/[^/?#]+(?:/[^/?#]+){0,2})", v.symbol or "",
        )
        if not m:
            return False   # unparseable symbol → can't conclude
        prefix = m.group(2)
        has_prefix_match = any(
            isinstance(ep, dict)
            and isinstance(ep.get("path") or ep.get("url") or "", str)
            and (ep.get("path") or ep.get("url") or "").startswith(prefix)
            for ep in captured_endpoints
        )
        if has_prefix_match:
            return False   # at least one violation has a prefix-matching
                           # alternative → recoverable via R93.B retry
    return True


# ────────────────────────────────────────────────────────────────────────────
# R112.L — ZAP scan-config + axe spec light-touch validators
# ────────────────────────────────────────────────────────────────────────────

def validate_zap_scan_config(scan_config: dict | str, *, auth_method: str | None = None) -> list[GroundingViolation]:
    """R112.L — light-touch validator for ZAP scan-config YAML/dict.

    Catches the dominant gen-time bugs without second-guessing scan-profile
    semantics (the user's directive: ZAP scan config is healthy baseline;
    do not rewrite it). Checks:
      - `target.urls` array must be present + non-empty
      - When `auth_method=='cookie'`, scan must declare a cookie auth block
        (auth.cookie_name + auth.cookie_value) so the scanner can probe
        authenticated endpoints
      - When `auth_method=='bearer'`, scan must declare a bearer header
        (auth.bearer_token OR auth.headers with Authorization)

    Returns empty list when config is healthy. Skipped silently on parse
    failure (operator-supplied YAML may have legitimate quirks).
    """
    out: list[GroundingViolation] = []
    if isinstance(scan_config, str):
        try:
            import yaml as _yaml
            scan_config = _yaml.safe_load(scan_config) or {}
        except Exception:
            return out
    if not isinstance(scan_config, dict):
        return out
    # target URLs present + non-empty — accept EITHER the simplified top-level
    # `target.urls` schema OR the real ZAP Automation Framework v2 schema
    # (`env.contexts[].urls`), which is what `_generate_zap` emits and the
    # executor (`zap.sh -cmd -autorun`) actually consumes. The prior version
    # recognized ONLY top-level `target.urls`, so every AF-v2 config the
    # generator produced was falsely rejected → a 3-retry storm that produced
    # rejected on BOTH automation attempts → gen-stage TimeoutError). The
    # validator's intent is "the scan has real URLs to probe"; AF-v2
    # `env.contexts[].urls` satisfies that intent.
    def _zap_url_list(v) -> list:
        if isinstance(v, str):
            return [v] if v else []
        return list(v or [])

    _has_target_urls = False
    _target = scan_config.get("target") or {}
    if isinstance(_target, dict) and _zap_url_list(_target.get("urls") or _target.get("url")):
        _has_target_urls = True
    if not _has_target_urls:
        _env = scan_config.get("env") or {}
        if isinstance(_env, dict):
            for _ctx in (_env.get("contexts") or []):
                if isinstance(_ctx, dict) and _zap_url_list(_ctx.get("urls") or _ctx.get("url")):
                    _has_target_urls = True
                    break
    if not _has_target_urls:
        out.append(GroundingViolation(
            tool="zap",
            kind="zap_no_target_urls",
            symbol="target.urls",
            location="scan-config",
            hint=(
                "ZAP scan-config has no target URLs — scanner has nothing "
                "to probe. Provide EITHER top-level `target.urls` OR AF-v2 "
                "`env.contexts[].urls`.\n\n"
                "BEFORE (BROKEN — empty/missing target):\n"
                "  target:\n"
                "    urls: []\n\n"
                "AFTER (AF-v2, as the executor consumes):\n"
                "  env:\n"
                "    contexts:\n"
                "      - name: ctx\n"
                "        urls: [\"{{target_url}}\"]\n"
                "        includePaths: [\"{{target_url}}/api/v1/.*\"]\n"
            ),
        ))
    # auth block when auth_method demands it — accept EITHER top-level `auth.*`
    # OR AF-v2 `env.contexts[].authentication` (a non-"none" method with an
    # Authorization/cookie declaration), which is what `_generate_zap` emits.
    def _zap_af_v2_auth_declared() -> bool:
        _env = scan_config.get("env") or {}
        if not isinstance(_env, dict):
            return False
        for _ctx in (_env.get("contexts") or []):
            if not isinstance(_ctx, dict):
                continue
            _a = _ctx.get("authentication") or {}
            if not isinstance(_a, dict):
                continue
            _m = str(_a.get("method") or "").strip().lower()
            if _m and _m != "none":
                return True
        return False

    if auth_method:
        auth_block = scan_config.get("auth") or {}
        _af_v2_auth = _zap_af_v2_auth_declared()
        if auth_method == "cookie":
            if not (auth_block.get("cookie_name") or auth_block.get("cookies") or _af_v2_auth):
                out.append(GroundingViolation(
                    tool="zap",
                    kind="zap_missing_cookie_auth",
                    symbol="auth.cookie_name",
                    location="scan-config",
                    hint=(
                        "ZAP scan-config lacks cookie auth, but project uses "
                        "auth_method='cookie' — scanner can't probe authenticated "
                        "endpoints.\n\n"
                        "AFTER:\n"
                        "  auth:\n"
                        "    cookie_name: session-token\n"
                        "    cookie_value: ${TARGET_AUTH_COOKIE_VALUE}\n"
                    ),
                ))
        elif auth_method == "bearer":
            if not (auth_block.get("bearer_token") or auth_block.get("headers") or _af_v2_auth):
                out.append(GroundingViolation(
                    tool="zap",
                    kind="zap_missing_bearer_auth",
                    symbol="auth.bearer_token",
                    location="scan-config",
                    hint=(
                        "ZAP scan-config lacks bearer auth, but project uses "
                        "auth_method='bearer'.\n\n"
                        "AFTER:\n"
                        "  auth:\n"
                        "    bearer_token: ${TARGET_AUTH_BEARER_TOKEN}\n"
                    ),
                ))
    return out


def validate_axe_spec_grounded(
    content: str,
    *,
    captured_endpoints: list[dict] | None = None,
    dom_catalog: dict | None = None,
) -> list[GroundingViolation]:
    """R112.L — light-touch validator for axe-playwright `*_a11y.spec.ts`.

    Catches gen-time hallucinations:
      - `page.goto('/<path>')` where `/<path>` not in any known UI route
        (cross-check against captured_endpoints OR dom_catalog routes)
      - Missing `injectAxe()` + `checkA11y()` calls

    Returns empty list when spec is grounded. Skipped silently when neither
    captured_endpoints nor dom_catalog is provided (cold-start).
    """
    out: list[GroundingViolation] = []
    if not content:
        return out
    # Must call injectAxe + checkA11y
    if "injectAxe" not in content:
        out.append(GroundingViolation(
            tool="axe",
            kind="axe_missing_inject",
            symbol="injectAxe",
            location="axe_spec",
            hint=(
                "Axe spec doesn't call `injectAxe(page)` — the a11y scanner "
                "is never injected into the page context.\n\n"
                "AFTER:\n"
                "  import { injectAxe, checkA11y } from 'axe-playwright';\n"
                "  test('a11y', async ({ page }) => {\n"
                "    await page.goto('/dashboard');\n"
                "    await injectAxe(page);\n"
                "    await checkA11y(page);\n"
                "  });"
            ),
        ))
    if "checkA11y" not in content:
        out.append(GroundingViolation(
            tool="axe",
            kind="axe_missing_check",
            symbol="checkA11y",
            location="axe_spec",
            hint=(
                "Axe spec doesn't call `checkA11y(page)` — no a11y assertions "
                "ever run.\n\n"
                "AFTER:\n"
                "  await checkA11y(page, undefined, {{\n"
                "    detailedReport: true,\n"
                "    detailedReportOptions: {{ html: true }},\n"
                "  }});"
            ),
        ))
    # Cross-check page.goto paths if catalog provided
    if dom_catalog and isinstance(dom_catalog, dict):
        known_routes = set((dom_catalog.get("routes") or []))
        if isinstance(known_routes, set) and known_routes:
            import re as _re_r112_l
            # R227 — match BOTH the literal form `page.goto('/path')` AND the
            # template-literal form the ACCESSIBILITY_GENERATION prompt actually
            # emits: `page.goto(`${process.env.BASE_URL}/path`)`. The old regex
            # required the path to start right after the quote with `/`, so the
            # `${BASE_URL}`-prefixed form silently BYPASSED the route check → the
            # LLM's fabricated `/portal/xss/...` axe routes shipped un-grounded.
            # Now capture the whole goto arg, strip a leading `${...}` host prefix,
            # then compare the path against the catalog. GENERIC.
            for m in _re_r112_l.finditer(
                r"page\.goto\(\s*[`'\"]([^`'\"]+)[`'\"]", content,
            ):
                _raw = m.group(1)
                spec_path = _re_r112_l.sub(r"^\$\{[^}]*\}", "", _raw).split("?")[0].rstrip("/")
                if not spec_path.startswith("/"):
                    continue          # non-path arg (full external URL / var) — skip
                # Allow root + login routes always (R45.3 auth fixture)
                if spec_path in ("", "/", "/login", "/signup"):
                    continue
                if spec_path not in known_routes:
                    out.append(GroundingViolation(
                        tool="axe",
                        kind="axe_unknown_route",
                        symbol=f"page.goto('{spec_path}')",
                        location="axe_spec",
                        hint=(
                            f"`page.goto('{spec_path}')` targets a route NOT in "
                            f"the SUT's DOM catalog (R45.3 discovery).\n\n"
                            f"AFTER — use a known route:\n"
                            f"  Available routes: {sorted(known_routes)[:6]}"
                        ),
                    ))
    # B2 — strict auth/SPA-ready grounding. The legacy checks above are always
    # on; these add the guards that make axe scan a REAL authenticated page
    # (vs an un-hydrated shell or the stale-cookie login wall → vacuous PASS).
    # Killswitch ARTA_AXE_GROUND_STRICT_DISABLE=1.
    import os as _os_axe_strict
    if _os_axe_strict.environ.get("ARTA_AXE_GROUND_STRICT_DISABLE") != "1" and content:
        if "waitForSPAReady" not in content:
            out.append(GroundingViolation(
                tool="axe", kind="axe_missing_spa_ready", symbol="waitForSPAReady",
                location="axe_spec",
                hint=("Axe spec doesn't call `waitForSPAReady(page)` after navigation — it may "
                      "scan an un-hydrated shell.\nAFTER:\n"
                      "  import { waitForSPAReady, skipIfAuthStale } from '../common/sub_flows';\n"
                      "  await page.goto(`${process.env.BASE_URL}/<real-route>`);\n"
                      "  await waitForSPAReady(page); await skipIfAuthStale(page); await injectAxe(page);"),
            ))
        if "skipIfAuthStale" not in content:
            out.append(GroundingViolation(
                tool="axe", kind="axe_missing_auth_verify", symbol="skipIfAuthStale",
                location="axe_spec",
                hint=("Axe spec doesn't call `skipIfAuthStale(page)` — on a stale cookie it would "
                      "scan the login wall and vacuously PASS. Import from '../common/sub_flows' "
                      "and call it after goto, before injectAxe."),
            ))
        # Flag goto('/')-only specs when the SUT has real routes (scanning the shell).
        if dom_catalog and isinstance(dom_catalog, dict) and (dom_catalog.get("routes") or []):
            import re as _re_axe_root
            _gotos = _re_axe_root.findall(r"page\.goto\(\s*[`'\"]([^`'\"]*)[`'\"]", content)

            def _axe_path_of(_g: str) -> str:
                return _re_axe_root.sub(r"\$\{[^}]*\}", "", _g).strip().rstrip("/")
            if _gotos and not any(
                    _axe_path_of(_g) not in ("", "/login", "/signup") for _g in _gotos):
                out.append(GroundingViolation(
                    tool="axe", kind="axe_root_only_goto", symbol="page.goto('/')",
                    location="axe_spec",
                    hint=("Axe spec only navigates the root/login shell, but the SUT has real "
                          f"routes — it scans the shell, not feature pages. Use a real route, "
                          f"e.g. {sorted(set(dom_catalog.get('routes') or []))[:5]}."),
                ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# R126.Q — per-spec gen-quality score
# ────────────────────────────────────────────────────────────────────────────
# Mission contract: "report the quality of the SUT" extends to "report
# the quality of ARTA's own gen pipeline". Without a quality score,
# operators cannot verify R126's cost+perf wins preserved (or improved)
# quality. Each spec is scored across 5 dimensions on [0.0, 1.0]:
#
#   1. ac_coverage         — (test() blocks tagged w/ AC) / (ACs in requirement)
#   2. grounding_density   — (selectors+endpoints from catalogs) / (total used)
#   3. assertion_substance — (test blocks with non-trivial expects) / (total blocks)
#   4. structural_validity — 1.0 unless R102.A-stamped
#   5. gherkin_translation — 1.0 unless incomplete_gherkin_translation stamped
#
# Aggregate = mean of the 5 components. Surfaces on R125.I gen-health

_R126_Q_PLACEHOLDER_ASSERTION_RE = re.compile(
    r"expect\(\s*true\s*\)\.toBe\(\s*true\s*\)"
    r"|expect\(\s*[^)]+\s*\)\.not\.toBeNull\(\s*\)"
    r"|expect\(\s*page\s*\)\.toHaveURL\(\s*/\.\*/\s*\)",  # the R126.B default placeholder
    re.IGNORECASE,
)

_R126_Q_TEST_BLOCK_RE = re.compile(r"\btest\(\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_R126_Q_AC_TAG_RE = re.compile(r"//\s*AC[:\s]*(AC-?[A-Z0-9\-]+)", re.IGNORECASE)
_R126_Q_GET_BY_ROLE_RE = re.compile(
    r"getByRole\(\s*['\"](\w+)['\"]\s*,\s*\{\s*name:\s*['\"]([^'\"]+)['\"]",
)
_R126_Q_GOTO_OR_REQUEST_RE = re.compile(
    r"(?:page\.request\.\w+|page\.goto|request\.\w+)\(\s*[`'\"]([^`'\"]+)",
)


def score_pw_spec_quality(
    spec_content: str,
    *,
    ac_count: int = 0,
    dom_catalog: dict | None = None,
    captured_endpoints: list[dict] | None = None,
    gherkin_text: str | None = None,
    intent_alignment_threshold: float = 0.20,
) -> dict:
    """R126.Q — score a generated PW spec across 5 quality dimensions.

    Returns dict with per-component scores + an aggregate (mean). Used by
    the R125.I gen-health dashboard to compare quality per-provider.

    The score is INTRINSIC (computed from the spec content alone, given
    optional catalog references for grounding density). No SUT execution
    is required — this is a gen-quality measurement, not an execution
    measurement.

    Graceful: empty spec → all zeros + aggregate=0. Missing catalog →
    grounding_density is computed as best-effort from path heuristics.
    """
    if not spec_content or not spec_content.strip():
        return {
            "ac_coverage": 0.0,
            "grounding_density": 0.0,
            "assertion_substance": 0.0,
            "structural_validity": 0.0,
            "gherkin_translation": 0.0,
            # R127.E.1 — 3 new structural dimensions (mirror Fix FF
            # dry-run quarantine triggers). Empty spec → all zeros.
            "markdown_fence_clean": 0.0,
            "single_import_per_module": 0.0,
            "balanced_parens": 0.0,
            # R127.E.2 — semantic alignment dim; empty spec can't match
            # any Gherkin keywords, so this is 0 when Gherkin is given.
            # When `gherkin_text=None` (legacy caller), defaults to 0
            # to keep early-return contract consistent with non-empty
            # path which also defaults to "1.0 when no signal".
            "gherkin_intent_alignment": 0.0,
            "aggregate": 0.0,
        }

    # 1. AC coverage
    test_titles = _R126_Q_TEST_BLOCK_RE.findall(spec_content)
    test_count = len(test_titles)
    if ac_count > 0:
        ac_tags_in_titles = sum(1 for t in test_titles if re.search(r"AC[-\s]?\d+", t, re.IGNORECASE))
        ac_tags_in_comments = len(_R126_Q_AC_TAG_RE.findall(spec_content))
        ac_tagged = max(ac_tags_in_titles, min(test_count, ac_tags_in_comments))
        ac_coverage = min(1.0, ac_tagged / ac_count)
    else:
        # No AC count known — give full credit if any tests exist
        ac_coverage = 1.0 if test_count > 0 else 0.0

    # 2. Grounding density (selectors + endpoints from catalog vs invented)
    catalog_role_names: set[tuple[str, str]] = set()
    if isinstance(dom_catalog, dict):
        for entry in (dom_catalog.get("role_names") or []):
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                catalog_role_names.add((str(entry[0]).lower(), str(entry[1])))
    captured_paths: set[str] = set()
    if captured_endpoints:
        for ep in captured_endpoints:
            if isinstance(ep, dict):
                p = ep.get("path") or ep.get("url") or ""
                if isinstance(p, str) and p:
                    captured_paths.add(p)
    grounded = 0
    invented = 0
    for m in _R126_Q_GET_BY_ROLE_RE.finditer(spec_content):
        role, name = m.group(1).lower(), m.group(2)
        if catalog_role_names and (role, name) in catalog_role_names:
            grounded += 1
        elif catalog_role_names:
            invented += 1
        else:
            # No catalog signal — count as grounded (best-effort)
            grounded += 1
    for m in _R126_Q_GOTO_OR_REQUEST_RE.finditer(spec_content):
        path = m.group(1).split("?")[0]
        # Strip ${var} template prefix for comparison
        normalized = re.sub(r"\$\{[^}]+\}", "", path)
        if not normalized or normalized == "/":
            grounded += 1  # root path is always valid
            continue
        if captured_paths and any(normalized.endswith(p) or p in normalized for p in captured_paths):
            grounded += 1
        elif captured_paths:
            invented += 1
        else:
            grounded += 1  # no captured signal — best-effort
    total_refs = grounded + invented
    grounding_density = (grounded / total_refs) if total_refs > 0 else 1.0

    # 3. Assertion substance — non-placeholder expects
    placeholder_count = len(_R126_Q_PLACEHOLDER_ASSERTION_RE.findall(spec_content))
    expect_count = len(re.findall(r"\bexpect\s*\(", spec_content))
    non_placeholder = max(0, expect_count - placeholder_count)
    if expect_count > 0:
        assertion_substance = non_placeholder / expect_count
    else:
        assertion_substance = 0.0

    # 4. Structural validity — 0 if R102.A-stamped (in first 2KB header)
    head = spec_content[:2000]
    structural_validity = 0.0 if "_dispatch_block_kind: playwright_grounding_violation" in head else 1.0

    # 5. Gherkin translation — 0 if R125.B incomplete-gherkin stamp present
    gherkin_translation = 0.0 if "incomplete_gherkin_translation" in head else 1.0

    # R127.E.1 — three structural dimensions detecting the issues Fix FF
    # catches but R126.Q's original 5 dims scored as "perfect" (1.0).
    # quarantining it for fence leak + dup imports + truncation; post:
    # the same content scores ~0.62 because 3 of 8 dims = 0.0. This
    # aligns the dashboard quality signal with on-disk compile/parse
    # validity → operator sees the truth.

    # 6. markdown_fence_clean — 0 if any ```typescript / ``` fence leaks
    #    into the TS file (Claude CLI fence wrapping evidence)
    _md_fence_re = re.compile(r"^```\w*\s*$|^```$", re.MULTILINE)
    markdown_fence_clean = 0.0 if _md_fence_re.search(spec_content) else 1.0

    # 7. single_import_per_module — 0 if any module is imported in more
    #    than one import line (TS2300 duplicate identifier risk)
    _import_module_re = re.compile(
        r"^\s*import\s+(?:\{[^}]+\}|\*\s+as\s+\w+|\w+)\s+from\s+['\"]([^'\"]+)['\"]\s*;",
        re.MULTILINE,
    )
    _module_imports: dict[str, int] = {}
    for m in _import_module_re.finditer(spec_content):
        mod = m.group(1)
        _module_imports[mod] = _module_imports.get(mod, 0) + 1
    single_import_per_module = (
        0.0 if any(c > 1 for c in _module_imports.values()) else 1.0
    )

    # 8. balanced_parens — 0 if open/close paren counts differ (detects
    #    mid-expression truncation; Fix FF unbalanced-delta check)
    _open_p = spec_content.count("(")
    _close_p = spec_content.count(")")
    balanced_parens = 1.0 if _open_p == _close_p else 0.0

    # 9. R127.E.2 — gherkin_intent_alignment. Catches the
    #    click-cycle evidence). When `gherkin_text` is None (legacy
    #    callers), defaults to 1.0 (no signal to measure → not a
    #    violation). Aggregate becomes mean of 9 dims.
    try:
        from .semantic_intent_validator import score_gherkin_intent_alignment
        if gherkin_text:
            gherkin_intent_alignment = score_gherkin_intent_alignment(
                spec_content, gherkin_text,
                min_keyword_overlap=intent_alignment_threshold,
            )
        else:
            gherkin_intent_alignment = 1.0
    except Exception:
        # Defensive — never let a validator import error poison the score
        gherkin_intent_alignment = 1.0

    components = {
        "ac_coverage": round(ac_coverage, 3),
        "grounding_density": round(grounding_density, 3),
        "assertion_substance": round(assertion_substance, 3),
        "structural_validity": round(structural_validity, 3),
        "gherkin_translation": round(gherkin_translation, 3),
        # R127.E.1 — new structural dims
        "markdown_fence_clean": round(markdown_fence_clean, 3),
        "single_import_per_module": round(single_import_per_module, 3),
        "balanced_parens": round(balanced_parens, 3),
        # R127.E.2 — semantic intent dim (aggregate is mean of 9 now)
        "gherkin_intent_alignment": round(gherkin_intent_alignment, 3),
    }
    aggregate = sum(components.values()) / len(components)
    components["aggregate"] = round(aggregate, 3)
    return components
