"""G4.1 / I5 — OpenAPI Contract Test Generator.

Newman tests are LLM-probabilistic. OpenAPI specs are deterministic. When a project
declares `openapi_url`, we parse the spec and generate a contract test for every
endpoint × documented response code. Merges with LLM-generated Newman collection.

Supports OpenAPI 3.0 / 3.1 minimally:
  - paths.{path}.{method} — skip extensions starting with 'x-'
  - responses.{code} — generate test per status code
  - parameters[in=header|query|path] — build URL + headers from declared types
  - requestBody — use minimal valid example per schema type
  - No `$ref` resolution in v1 (complex; future work)

Usage:
    from src.agents.contract_test_generator import generate_contract_collection
    collection = await generate_contract_collection(openapi_url, requirement_id)
    # → a dict with Postman v2.1 schema
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any

import httpx

log = logging.getLogger("arta.contract")


def _r212_path_skeleton(p: str) -> str:
    """R212 — lowercased path with param/id segments collapsed to `*` so a
    concrete captured path and an OpenAPI template compare equal
    (`/api/x/{id}/y` == `/api/x/0aee.../y`). Used to scope contract gen to a
    requirement's mapped endpoints."""
    import re as _re_skel
    p = (p or "").split("?")[0].split("#")[0]
    p = _re_skel.sub(r"^https?://[^/]+", "", p).rstrip("/")
    out: list[str] = []
    for s in p.split("/"):
        sl = s.lower()
        if not s:
            continue
        if (s.startswith("{") or sl.isdigit()
                or _re_skel.fullmatch(r"[0-9a-f]{6,}", sl)
                or _re_skel.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{8,}", sl)):
            out.append("*")
        else:
            out.append(sl)
    return "/" + "/".join(out)


async def generate_contract_collection(
    openapi_url: str,
    requirement_id: str,
    timeout: float = 30.0,
    auth_method: str = "none",
    cookie_name: str = "",
    relevant_paths: list | None = None,
) -> dict | None:
    """Fetch an OpenAPI spec and return a Newman (Postman v2.1) collection.

    Returns None if the spec is unreachable or unparseable — caller should fall
    back to LLM-generated Newman.

    auth_method / cookie_name: threaded from project config so generated
    collections use the correct auth header type (Cookie vs Bearer) from the
    start, rather than requiring runtime patching.

    R212 — `relevant_paths` (the requirement's R211-mapped endpoint paths): when
    provided + non-empty, ONLY emit contract items for ops whose path skeleton
    matches one of them. Pre-R212 the generator emitted EVERY OpenAPI op (151 on
    one pilot SUT) → 230-item collections that timeout-loop the claude_code Pass-2 gen and
    never complete. Scoping to the req's real surface keeps gen bounded + the
    items relevant to the requirement. Killswitch ARTA_R212_CONTRACT_SCOPE_DISABLE=1.
    """
    spec = await _fetch_spec(openapi_url, timeout=timeout)
    if not spec:
        return None

    components: dict = spec.get("components") or {}

    info = spec.get("info", {})
    title = info.get("title", "API")
    servers = spec.get("servers") or []
    base_url_default = "{{base_url}}"
    if servers:
        url = servers[0].get("url", base_url_default)
        # Prefer env var placeholder over hardcoded host
        if url and not url.startswith("{{"):
            base_url_default = "{{base_url}}"  # always use env var at runtime

    if auth_method == "cookie":
        _coll_vars = [
            {"key": "base_url", "value": base_url_default},
            {"key": "cookie_value", "value": ""},  # injected at runtime via --env-var
            {"key": "test_user", "value": ""},
        ]
    else:
        _coll_vars = [
            {"key": "base_url", "value": base_url_default},
            {"key": "auth_token", "value": ""},
            {"key": "test_user", "value": ""},
        ]

    collection = {
        "info": {
            "_postman_id": str(uuid.uuid4()),
            "name": f"ARTA Contract Tests — {title} — {requirement_id}",
            "description": f"Generated from OpenAPI spec at {openapi_url}\nRequirement: {requirement_id}",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            # R111.I — stamp gen-metrics so defect_intel knows the body was
            # OpenAPI-schema-grounded. R37.4 / R111.H can then distinguish
            # 5xx-on-grounded-body (likely REAL sut_regression) from
            # 5xx-on-LLM-hallucinated-body (likely cascade test_gen_bug).
            "_gen_metrics": {
                "body_schema_grounded": True,
                "body_required_only": True,  # R115.A.1: optional fields excluded
                "source": "contract_test_generator",
                "openapi_url": openapi_url,
            },
        },
        "item": [],
        "variable": _coll_vars,
    }

    # R177 — security schemes ARTA can satisfy with a user session vs cannot.
    # Endpoints whose ONLY security requirement is an apiKey-type scheme (a
    # cannot be authenticated with the operator's user session → they 401 by
    # construction (run-1d8a17: 44× 401 were all /generate-temp-creds +
    # /generate-presigned-url, both security=[{internalApiKey}]). Generating
    # contract tests for them just produces guaranteed-401 noise. R177 SKIPS
    # them (ARTA truthfully cannot test internal-only endpoints with user
    # creds). Killswitch ARTA_R177_SKIP_INTERNAL_AUTH_DISABLE=1.
    _sec_defs = spec.get("securityDefinitions") or (
        (spec.get("components") or {}).get("securitySchemes")) or {}
    _r177_on = os.environ.get("ARTA_R177_SKIP_INTERNAL_AUTH_DISABLE") != "1"
    _r177_skipped = 0

    def _op_user_testable(op: dict) -> bool:
        # An operation is user-testable unless EVERY security requirement names
        # only schemes ARTA can't provide (apiKey internal keys). No security,
        # or any oauth2/basic/bearer/cookie scheme → testable.
        sec = op.get("security")
        if not sec:  # no per-op security → inherits global / open → testable
            return True
        for requirement in sec:
            if not isinstance(requirement, dict):
                return True
            schemes = list(requirement.keys())
            if not schemes:
                return True  # empty {} = no-auth alternative → testable
            satisfiable = False
            for sch in schemes:
                d = _sec_defs.get(sch) or {}
                t = (d.get("type") or "").lower()
                # apiKey = internal server key ARTA doesn't hold; others
                # (oauth2/basic/http-bearer/openIdConnect) ride the user session.
                if t != "apikey":
                    satisfiable = True
            if satisfiable:
                return True  # this alternative is satisfiable → testable
        return False

    # R212 — scope to the requirement's mapped endpoints (skeleton match).
    import os as _os_r212
    _r212_scope_on = _os_r212.environ.get("ARTA_R212_CONTRACT_SCOPE_DISABLE") != "1"
    _r212_relevant_skels: set = set()
    if _r212_scope_on and relevant_paths:
        for rp in relevant_paths:
            _p = (rp.get("path") if isinstance(rp, dict) else rp) or ""
            if _p:
                _r212_relevant_skels.add(_r212_path_skeleton(_p))
    _r212_scoped_out = 0

    paths = spec.get("paths", {})
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        # R212 — skip ops whose path isn't in the requirement's mapped surface.
        if _r212_relevant_skels and _r212_path_skeleton(path) not in _r212_relevant_skels:
            _r212_scoped_out += 1
            continue
        for method, operation in methods.items():
            method_lower = method.lower()
            if method_lower not in ("get", "post", "put", "patch", "delete", "head", "options"):
                continue
            if not isinstance(operation, dict):
                continue
            if _r177_on and not _op_user_testable(operation):
                _r177_skipped += 1
                continue

            summary = operation.get("summary") or f"{method.upper()} {path}"
            responses = operation.get("responses", {})
            # R176 — ONE item per (path, method), not one per response code. The
            # contract test sends a single happy-path request and asserts the
            # status is one of the codes the spec DOCUMENTS for this operation.
            # Pre-R176 it emitted a separate item per declared code, each
            # asserting that EXACT code — so the assert-401 / assert-404 items
            # sent a happy-path request and were GUARANTEED to fail on a 200
            # (noise that looked like SUT failures). A "status ∈ documented"
            # assertion passes on any documented response + fails only on an
            # UNdocumented status (a real contract violation).
            _documented = [c for c in responses if _is_status_code(c)]
            if not _documented:
                continue
            # Representative response_def for the body-schema check: prefer the
            # primary 2xx (that's what a happy-path request returns).
            _primary = next((c for c in _documented if str(c).startswith("2")), _documented[0])
            _primary_def = responses.get(_primary)
            collection["item"].append(
                _build_request_item(
                    path=path,
                    method=method_lower,
                    operation=operation,
                    response_code=_primary,
                    response_def=_primary_def if isinstance(_primary_def, dict) else {},
                    summary=summary,
                    components=components,
                    auth_method=auth_method,
                    cookie_name=cookie_name,
                    documented_codes=_documented,
                )
            )

    if _r177_skipped:
        log.info("R177: skipped %d internal-only operation(s) (security requires an "
                 "apiKey ARTA's user session can't provide → would 401 by construction)",
                 _r177_skipped)
    if _r212_relevant_skels:
        log.info("R212: scoped contract gen to %d mapped endpoint skeleton(s) — "
                 "kept %d item(s), skipped %d off-surface path(s) for %s",
                 len(_r212_relevant_skels), len(collection["item"]), _r212_scoped_out,
                 requirement_id)

    # Step 1.1: stamp required Postman vars onto the collection so callers
    # can compare against project config and surface "missing" vars in the
    # generation result (operator-actionable without trial-and-error).
    import re as _re_required
    _coll_text = json.dumps(collection)
    required_vars = sorted(set(_re_required.findall(r"\{\{(\w+)\}\}", _coll_text)))
    # Filter out infra vars that ARTA always injects automatically:
    _ARTA_INJECTED = {"base_url", "auth_token", "cookie_value", "cookie_name", "test_user"}
    collection["_arta_required_vars"] = [v for v in required_vars if v not in _ARTA_INJECTED]
    return collection


async def _fetch_spec(url: str, timeout: float = 30.0) -> dict | None:
    """Fetch + parse the OpenAPI spec (JSON or YAML)."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=timeout, follow_redirects=True)
            if resp.status_code != 200:
                log.warning("contract: %s returned status %d", url, resp.status_code)
                return None
            content_type = resp.headers.get("content-type", "").lower()
            text = resp.text

            if "yaml" in content_type or url.endswith(".yaml") or url.endswith(".yml"):
                try:
                    import yaml  # type: ignore
                    return yaml.safe_load(text)
                except ImportError:
                    log.warning("contract: YAML spec but PyYAML not installed")
                    return None
            # Default: JSON
            return json.loads(text)
    except Exception as exc:
        log.warning("contract: failed to fetch %s: %s", url, exc)
        return None


def _is_status_code(code: str) -> bool:
    return bool(re.fullmatch(r"[1-5]\d{2}", str(code)))


def _build_request_item(
    path: str,
    method: str,
    operation: dict,
    response_code: str,
    response_def: dict,
    summary: str,
    components: dict | None = None,
    auth_method: str = "none",
    cookie_name: str = "",
    documented_codes: list | None = None,
) -> dict:
    """Build a Postman v2.1 item (request + test script) for one endpoint."""
    # Separate parameters by location
    path_params = []
    query_params = []
    header_params = []
    for param in operation.get("parameters", []) or []:
        if not isinstance(param, dict):
            continue
        loc = param.get("in", "")
        if loc == "path":
            path_params.append(param)
        elif loc == "query":
            query_params.append(param)
        elif loc == "header":
            header_params.append(param)

    # F20-44: Substitute path params with Postman variables when the param name
    # matches a known project-level variable (account_id, user_id, organization_id,
    # etc.). The Newman runner injects these via --env-var from projects.json
    # `environments.<env>.variables`. Hardcoded UUIDs would only work for one
    # tenant; using {{var}} makes the same generated test usable across staging/
    # prod/per-tenant accounts.
    #
    # Param-name → Postman-variable mapping for IDs the runner already injects:
    _KNOWN_ID_VARS = {
        "account_id":      "{{account_id}}",
        "accountId":       "{{account_id}}",
        "user_id":         "{{user_id}}",
        "userId":          "{{user_id}}",
        "organization_id": "{{organization_id}}",
        "organizationId":  "{{organization_id}}",
        "org_id":          "{{organization_id}}",
        "orgId":           "{{organization_id}}",
        "subscriber_id":   "{{subscriber_id}}",
        "subscriberId":    "{{subscriber_id}}",
        "subscription_id": "{{subscription_id}}",
        "subscriptionId":  "{{subscription_id}}",
        "product_id":      "{{product_id}}",
        "productId":       "{{product_id}}",
        "workspace_id":    "{{workspace_id}}",
        "workspaceId":     "{{workspace_id}}",
        "team_id":         "{{team_id}}",
        "teamId":          "{{team_id}}",
        "dataset_id":      "{{dataset_id}}",
        "datasetId":       "{{dataset_id}}",
        "report_id":       "{{report_id}}",
        "reportId":        "{{report_id}}",
        "document_id":     "{{document_id}}",
        "documentId":      "{{document_id}}",
        "company_id":      "{{company_id}}",
        "companyId":       "{{company_id}}",
        "tenant_id":       "{{tenant_id}}",
        "tenantId":        "{{tenant_id}}",
    }
    resolved_path = path
    # First pass: resolve via spec's `parameters` list (when explicitly declared).
    declared_param_names = set()
    for p in path_params:
        name = p.get("name", "")
        declared_param_names.add(name)
        param_schema = p.get("schema") or {}
        param_format = param_schema.get("format", "")
        # Step 1.3: prefer OpenAPI-declared example/default over Postman {{var}}.
        # When the spec author wrote `example: 550e8400-…` for `schema_id`,
        # using that example avoids a runtime injection requirement entirely.
        # ONLY falls back to {{var}} when no example/default exists AND the
        # param looks like a tenant-scoped ID that operators must inject.
        spec_example = p.get("example")
        if spec_example is None:
            spec_example = param_schema.get("example")
        if spec_example is None:
            spec_example = param_schema.get("default")
        if spec_example is not None:
            example = spec_example
        elif name in _KNOWN_ID_VARS:
            example = _KNOWN_ID_VARS[name]
        elif param_format == "uuid" or name.endswith("_id") or name.endswith("Id"):
            # Generic UUID/id param — reference by its own name as a Postman var
            example = f"{{{{{name}}}}}"
        else:
            example = _example_for_param(p, components=components) or f"{{{{{name}}}}}"
        resolved_path = resolved_path.replace("{" + name + "}", str(example))

    # F20-44 fallback: many specs (notably flask-restplus / Swagger 2.0) DON'T
    # declare path params explicitly under `parameters` — they only appear in
    # stay as literal `{account_id}` and break Newman's variable resolution.
    # Extract any remaining `{name}` placeholders from the template and apply
    # the same name-mapping logic.
    import re as _re_path
    for placeholder in _re_path.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", resolved_path):
        if placeholder in declared_param_names:
            continue   # already handled above
        if placeholder in _KNOWN_ID_VARS:
            substitution = _KNOWN_ID_VARS[placeholder]
        elif placeholder.endswith("_id") or placeholder.endswith("Id") or "uuid" in placeholder.lower():
            substitution = f"{{{{{placeholder}}}}}"
        else:
            # Unknown placeholder — still use the placeholder name as Postman var
            # so Newman can substitute via env-var if the operator adds it.
            substitution = f"{{{{{placeholder}}}}}"
        resolved_path = resolved_path.replace("{" + placeholder + "}", substitution)

    # Build URL
    url_obj: dict[str, Any] = {
        "raw": "{{base_url}}" + resolved_path + _query_string(query_params, components=components),
        "host": ["{{base_url}}"],
        "path": [seg for seg in resolved_path.split("/") if seg],
    }
    if query_params:
        url_obj["query"] = [
            {"key": p.get("name", ""), "value": str(_example_for_param(p, components=components) or ""), "description": p.get("description", "")}
            for p in query_params
        ]

    # Headers
    headers_out: list[dict] = []
    for p in header_params:
        headers_out.append({
            "key": p.get("name", ""),
            "value": str(_example_for_param(p, components=components) or ""),
        })
    # Auth header — method-aware. Cookie projects use Cookie: header; all others
    # use Authorization: Bearer. The collection variable (cookie_value vs auth_token)
    # is injected at runtime via Newman --env-var from the project's credentials.
    if auth_method == "cookie" and cookie_name:
        headers_out.append({"key": "Cookie", "value": f"{cookie_name}={{{{cookie_value}}}}"})
    else:
        headers_out.append({"key": "Authorization", "value": "Bearer {{auth_token}}"})

    # Request body for POST/PUT/PATCH
    body: dict[str, Any] = {}
    request_body = operation.get("requestBody")
    if request_body and isinstance(request_body, dict) and method in ("post", "put", "patch"):
        content = request_body.get("content", {})
        json_schema = (content.get("application/json") or {}).get("schema") or {}
        example_body = _example_for_schema(json_schema, components=components)
        if example_body is not None:
            body = {
                "mode": "raw",
                "raw": json.dumps(example_body, indent=2),
                "options": {"raw": {"language": "json"}},
            }
            headers_out.append({"key": "Content-Type", "value": "application/json"})

    # Test script asserting status + schema
    test_script = _build_test_script(response_code, response_def, documented_codes)

    return {
        "name": f"{method.upper()} {path} → {response_code} — {summary[:80]}",
        "request": {
            "method": method.upper(),
            "header": headers_out,
            "url": url_obj,
            **({"body": body} if body else {}),
        },
        "event": [
            {
                "listen": "test",
                "script": {
                    "exec": test_script,
                    "type": "text/javascript",
                },
            }
        ],
    }


def _query_string(params: list[dict], components: dict | None = None) -> str:
    if not params:
        return ""
    pairs = []
    for p in params:
        name = p.get("name", "")
        example = _example_for_param(p, components=components)
        if example is not None:
            pairs.append(f"{name}={example}")
    return ("?" + "&".join(pairs)) if pairs else ""


def _example_for_param(param: dict, components: dict | None = None) -> Any:
    if "example" in param:
        return param["example"]
    schema = param.get("schema", {}) or {}
    return _example_for_schema(schema, components=components) or ""


def _example_for_schema(schema: dict | None, components: dict | None = None, _depth: int = 0) -> Any:
    """Generate a minimal valid example from a schema. Resolves $ref against components."""
    if _depth > 5:
        return {}
    if not isinstance(schema, dict):
        return None
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/components/schemas/") and components:
            schema_name = ref.split("/")[-1]
            resolved = (components.get("schemas") or {}).get(schema_name, {})
            return _example_for_schema(resolved, components, _depth + 1)
        return {}
    if "allOf" in schema:
        result: dict = {}
        for sub in schema["allOf"][:3]:
            sub_example = _example_for_schema(sub, components, _depth + 1)
            if isinstance(sub_example, dict):
                result.update(sub_example)
        return result or None
    if "oneOf" in schema or "anyOf" in schema:
        variants = schema.get("oneOf") or schema.get("anyOf") or []
        if variants:
            return _example_for_schema(variants[0], components, _depth + 1)
    stype = schema.get("type", "")
    if stype == "object":
        props = schema.get("properties", {}) or {}
        # R115.A.1 — body-required-only. Pre-R115.A.1: when `required: []`
        # was empty, fallback emitted `list(props.keys())[:3]` — first 3
        # OPTIONAL fields as if required. SUT rejected with 400 "Missing
        # required field X" because the field WAS optional in the SUT's
        # actual schema but the generated body emitted it as required.
        # Live evidence (run-8da91d): 153 × 400, 67% (~102) carry
        # "Missing required field" pattern → R111.H classified as
        # malformed_body_cascade test_gen_bug. Most of those are
        # ARTA-side over-specification, NOT real SUT contract drift.
        #
        # R115.A.1: emit ONLY explicitly-required fields. When the spec
        # declares no `required:`, return `{}` (empty body). The SUT
        # defines minimum acceptable shape; ARTA doesn't second-guess.
        required = schema.get("required", []) or []
        return {k: _example_for_schema(props.get(k, {}), components, _depth + 1) for k in required if k in props}
    if stype == "array":
        items = schema.get("items", {}) or {}
        return [_example_for_schema(items, components, _depth + 1)] if items else []
    if stype == "string":
        fmt = schema.get("format", "")
        if fmt == "email":
            return "test@example.com"
        if fmt == "date":
            return "2024-01-01"
        if fmt == "date-time":
            return "2024-01-01T00:00:00Z"
        if fmt == "uuid":
            return "00000000-0000-0000-0000-000000000000"
        return "example"
    if stype in ("integer", "number"):
        return 1
    if stype == "boolean":
        return True
    return None


def _build_test_script(response_code: str, response_def: dict,
                       documented_codes: list | None = None) -> list[str]:
    """Build Postman/Newman test script asserting CORRECTNESS (status is a
    documented code + body schema). R176: status ∈ documented set (not == one
    code) so a happy-path 200 passes; and NO functional latency assertion —
    response time is a PERFORMANCE concern measured by k6 with a threshold from
    the requirement's AC, not an arbitrary 5000ms hardcoded into every API
    contract test (pre-R176 a correct-but-slow 200-in-9s FAILED the functional
    test). The responseTime is still recorded in the Newman result metadata."""
    _codes = [str(c) for c in (documented_codes or [response_code]) if _is_status_code(str(c))]
    if not _codes:
        _codes = [str(response_code)]
    _codes_js = ", ".join(_codes)
    lines = [
        "// ARTA contract test — generated from OpenAPI spec (R176: documented-status, no latency fail)",
        f'pm.test("Status code is documented ({"/".join(_codes)})", function () {{',
        f"    pm.expect([{_codes_js}]).to.include(pm.response.code);",
        "});",
    ]

    # If response has a JSON schema, add minimal shape check
    content = response_def.get("content", {}) or {}
    json_schema = (content.get("application/json") or {}).get("schema") or {}
    if json_schema and response_code.startswith("2"):
        lines += [
            "",
            'pm.test("Response content-type is JSON", function () {',
            '    pm.response.to.have.header("Content-Type");',
            '    pm.expect(pm.response.headers.get("Content-Type")).to.include("json");',
            "});",
            "",
            'pm.test("Response body is valid JSON", function () {',
            "    pm.response.to.be.json;",
            "});",
        ]
        required = json_schema.get("required", []) if isinstance(json_schema, dict) else []
        if required:
            required_list = ", ".join(f'"{r}"' for r in required[:5])
            lines += [
                "",
                'pm.test("Response has required fields", function () {',
                "    const body = pm.response.json();",
                f"    const required = [{required_list}];",
                "    required.forEach(function (field) {",
                "        pm.expect(body, `missing required field ${field}`).to.have.property(field);",
                "    });",
                "});",
            ]

    return lines
