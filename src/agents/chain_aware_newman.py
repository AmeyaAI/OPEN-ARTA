"""Phase D — chain-aware Newman generation.

Generates a Postman/Newman collection deterministically from a CallChain
(Phase C). Replaces the prior flat-list Newman emission for endpoints
that are part of an observed chain.

Key design points (per DR-2 — the corrected Postman primitives):

  - Chain-internal env vars use `pm.collectionVariables.set/get` (scoped to
    this run, not polluting the project env).
  - Project-level env vars (operator-typed or Phase B harvested) come from
    `pm.environment.get`.
  - Halting on missing dependency uses `postman.setNextRequest(null)` —
    `pm.execution.skipRequest()` does NOT exist in the Postman runtime.
  - Cascade markers are stamped via `pm.collectionVariables.set('cascade_skip:<item>', reason)`
    so the JSON reporter dumps them and the result-parser (Phase E) can
    distinguish cascade-skip from happy-path failure.
  - Item ordering matches `chain.nodes[*].sequence_index` — Newman runs
    items in collection order by default, so this is sufficient.
  - Adversarial chains (Phase I4) are emitted as a sibling collection or
    extra item-set with `auth: noauth` and expected 4xx assertions.

The output is a Postman 2.1 collection ready for `newman run`.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

log = logging.getLogger("arta.chain_aware_newman")


# ── Postman primitive snippets ─────────────────────────────────────────────


_PREREQ_TEMPLATE = """// Phase D — chain dependency gate
const _required = {required};
for (const _v of _required) {{
    const _val = pm.collectionVariables.get(_v) || pm.environment.get(_v);
    if (_val === undefined || _val === null || _val === '') {{
        // DR-2: halt the collection AND tag the cascade so the result parser
        // distinguishes a missing-dep skip from a real failure.
        const _reason = `${{pm.info.requestName}}: missing required ${{_v}}`;
        pm.collectionVariables.set(`cascade_skip:${{pm.info.requestName}}`, _reason);
        console.log(`[ARTA] cascade_skip ${{_reason}}`);
        postman.setNextRequest(null);
        return;
    }}
}}
"""


_TEST_TEMPLATE = """// Phase D — happy-path assertions + provider extraction
pm.test('status is 2xx', function () {{
    pm.expect(pm.response.code).to.be.within(200, 299);
}});

const _body = (function() {{ try {{ return pm.response.json(); }} catch (e) {{ return null; }} }})();
const _provides = {provides};
for (const [_name, _path] of Object.entries(_provides)) {{
    if (!_body) continue;
    // R215 Item 3 — cm collection_id is a UUID-shaped object KEY (not a field):
    // the cm list response is {{"Collection":…, "<collection-uuid>":[…]}}. The
    // dataflow inference emits the sentinel jsonpath `$.__cm_collection_key__`
    // for such providers; capture the (dynamic) UUID key at runtime by scanning
    // the response's top-level keys. Standard `$.foo.bar` navigation can't do
    // this (it reads values, not keys).
    if (_path === '$.__cm_collection_key__') {{
        const _UUID = /^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$/i;
        const _SKIP = {{'collection': 1, 'last_evaluated_key': 1, 'published_collections_detail': 1}};
        let _ck = null;
        for (const _k of Object.keys(_body)) {{
            if (_UUID.test(_k) && !_SKIP[String(_k).toLowerCase()] && Array.isArray(_body[_k])) {{ _ck = _k; break; }}
        }}
        if (_ck) {{ pm.collectionVariables.set(_name, String(_ck)); }}
        continue;
    }}
    // Walk simple `$.foo.bar` paths.
    let _val = _body;
    const _parts = _path.replace(/^\\$\\.?/, '').split('.').filter(Boolean);
    for (const _seg of _parts) {{
        if (_val === null || _val === undefined) break;
        if (_seg.includes('[0]')) {{
            const _key = _seg.replace('[0]', '');
            _val = _key ? (_val[_key] || [])[0] : _val[0];
        }} else {{
            _val = _val[_seg];
        }}
    }}
    if (_val !== null && _val !== undefined && _val !== '') {{
        pm.collectionVariables.set(_name, String(_val));
    }} else {{
        // Provider contract violation surfaces here — Phase F gate's
        // _check_call_sequence_integrity reads this from the reporter output.
        const _msg = `provider_contract_violation:${{pm.info.requestName}}:${{_name}} (path ${{_path}})`;
        pm.collectionVariables.set(`pcv:${{pm.info.requestName}}:${{_name}}`, _msg);
        console.warn(`[ARTA] ${{_msg}}`);
    }}
}}
"""


_ADVERSARIAL_TEST_TEMPLATE = """// Phase I4 — adversarial chain assertion: expect 4xx
pm.test('adversarial assertion: status is 4xx', function () {{
    pm.expect(pm.response.code).to.be.within(400, 499);
}});
"""


def _substitute_url(path_template: str, base_url: str | None = None) -> str:
    """Convert `/v1/datasets/{dataset_id}` → `{{base_url}}/v1/datasets/{{dataset_id}}`.

    Postman uses `{{var}}` for variable substitution. Path-template `{var}` is
    converted to `{{var}}` so chain vars resolve from collectionVariables OR
    environment at runtime.
    """
    base = base_url or "{{base_url}}"
    rewritten = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", r"{{\1}}", path_template)
    if not rewritten.startswith("/"):
        rewritten = "/" + rewritten
    return base + rewritten


def _build_request(node: dict, *, base_url: str | None) -> dict:
    """Construct the Postman v2.1 `request` block for a CallNode dict."""
    method = (node.get("method") or "GET").upper()
    path_template = node.get("path_template") or "/"
    url = _substitute_url(path_template, base_url)
    request: dict[str, Any] = {
        "method": method,
        "header": [
            {"key": "Content-Type", "value": "application/json"},
        ],
        "url": {
            "raw": url,
            "host": [base_url or "{{base_url}}"],
            "path": [p for p in path_template.strip("/").split("/") if p],
        },
    }
    # Add a request body placeholder if shape suggests one. Phase D doesn't
    # invent values — operators / LLM second-pass fill in actual content.
    if method in {"POST", "PUT", "PATCH"} and node.get("request_body_shape"):
        request["body"] = {
            "mode": "raw",
            "raw": "{}",   # Phase D follow-up: scaffold from request_body_shape
            "options": {"raw": {"language": "json"}},
        }
    return request


def _build_item_event_block(node: dict, *, is_adversarial: bool) -> list[dict]:
    """Build the Postman `event` array (pre-request + test scripts).

    The pre-request script enforces `consumes` dependencies; the test script
    extracts `provides` and stamps cascade markers as needed.
    """
    consumes = node.get("consumes") or {}
    provides = node.get("provides") or {}

    required_vars = sorted(consumes.keys())
    prereq_exec = _PREREQ_TEMPLATE.format(required=json.dumps(required_vars))

    if is_adversarial:
        test_exec = _ADVERSARIAL_TEST_TEMPLATE
    else:
        # JSON-encode provides dict for embedding into JS
        test_exec = _TEST_TEMPLATE.format(provides=json.dumps(provides, sort_keys=True))

    return [
        {
            "listen": "prerequest",
            "script": {"type": "text/javascript", "exec": prereq_exec.split("\n")},
        },
        {
            "listen": "test",
            "script": {"type": "text/javascript", "exec": test_exec.split("\n")},
        },
    ]


_R217_UUID_SEG_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_R217_VAR_SEG_RE = re.compile(r"\{\{?[a-z_][a-z0-9_]*\}?\}", re.IGNORECASE)


def _r217_norm_template(path: str) -> str:
    """Normalize a path for surface-grounding comparison: strip query, lower,
    trailing slash, and collapse UUID + `{var}`/`{{var}}` segments to `*` so a
    CONCRETE chain path (`/955934e1/.../organizationss`) and its templated
    discovered record (`/{id_id}/.../organizationss`) compare equal. Without
    this, a real endpoint that ALSO has a dead concrete-account duplicate in
    `discovered_endpoints` would be falsely skipped."""
    import urllib.parse as _up
    p = _up.unquote(path or "").split("?")[0].rstrip("/").lower()
    p = _R217_UUID_SEG_RE.sub("*", p)
    p = _R217_VAR_SEG_RE.sub("*", p)
    return p


def _load_dead_endpoint_paths(project_id: "str | None") -> "set[str]":
    """R217 — return the set of NORMALIZED discovered paths that the probe
    OBSERVED but that returned NO data (`response_body_shape is None`) AND
    carry no 2xx status = probe self-guesses / footprint, NOT real SUT
    endpoints — AND that have NO real (data-returning) sibling.

    Live example: the `/api/v1/*` paths (`/api/v1/organizations`,
    `/api/v1/users/me`, …) the discovery probe REQUESTED during the walk but
    that the SUT 404s — recorded in `discovered_endpoints` (source: network)
    with `response_body_shape: None` and no status. The R-PreserveAPIPaths rule
    in `_r138_a_should_skip_chain_node` deliberately keeps `/api/v1/*` as a
    valid API surface (correct in general), so a chain built from them ships
    guaranteed-404 steps (run-60af67: 68 chain 404s, all `/api/v1/*`).
    Surface-grounding against the REAL endpoints is the SUT-agnostic fix.

    CRITICAL: `dead = dead_norms − real_norms`. A logical endpoint that has ANY
    record with a body or 2xx status is REAL and must never be skipped — even
    if a dead concrete-account duplicate exists (the cm `organizationss` case).

    Empty when no project / no file / killswitch — so synthetic chains (unit
    tests) and pre-discovery runs are unaffected (filter is data-driven, not a
    path-pattern blanket skip). Killswitch
    ARTA_R217_CHAIN_DEAD_ENDPOINT_SKIP_DISABLE=1.
    """
    import os as _os
    if not project_id or _os.environ.get(
            "ARTA_R217_CHAIN_DEAD_ENDPOINT_SKIP_DISABLE") == "1":
        return set()
    try:
        import json as _json
        from pathlib import Path as _P
        # H2 (R218) — honour the canonical captured-endpoints dir (R151.D) so this
        # reader sees the SAME surface as every other consumer; the bare
        # `.arta/discovered_endpoints/` literal ignored ARTA_CAPTURED_ENDPOINTS_DIR.
        _dir = _os.environ.get("ARTA_CAPTURED_ENDPOINTS_DIR", ".arta/discovered_endpoints")
        fp = _P(_dir) / f"{project_id}.json"
        if not fp.is_file():
            return set()
        data = _json.loads(fp.read_text())
        recs = data if isinstance(data, list) else (
            data.get("endpoints") or list(data.values()))
        real_norms: set[str] = set()
        dead_norms: set[str] = set()
        for r in recs:
            if not isinstance(r, dict):
                continue
            p = r.get("path", "") or ""
            if not p:
                continue
            norm = _r217_norm_template(p)
            if not norm:
                continue
            st = r.get("status") or r.get("status_code")
            is_real = r.get("response_body_shape") is not None or (
                isinstance(st, int) and 200 <= st < 300)
            (real_norms if is_real else dead_norms).add(norm)
        # a normalized endpoint is DEAD only if NO record for it was ever real
        return dead_norms - real_norms
    except Exception:
        return set()


def _r138_a_should_skip_chain_node(
    n: dict, dead_paths: "set[str] | None" = None,
) -> tuple[bool, str]:
    """R138.A — grounding filter for chain Newman items.

    Pre-R138.A: chain_aware_newman emitted EVERY HAR-captured URL as a
    Postman item — including static assets (`*.css`, `*.js`, `*.woff`,
    `/icon`), third-party origins (Google Firestore, CDNs), and unresolved
    template variables (`{{id_id}}` where the template wasn't substituted
    at HAR-capture time). Live evidence (R135 Iter 0 run-e1c10e): 169 of
    340 chain_adv items returned 404 because they targeted non-testable
    paths (`/static/`, `/google.firestore...`, `/{{id_id}}`, etc.).

    R138.A returns (True, reason) when a chain node should be SKIPPED.
    Categories rejected:
      1. Static assets — extensions like .css, .js, .woff, .png, .ico, .svg, .map
      2. Static-path prefixes — `/static/`, `/assets/`, `/files/`, `/ajax/libs/`,
         `/icons/`, `/fonts/`
      3. Unresolved Postman variables — path contains `{{X}}` patterns the
         chain extractor couldn't resolve at HAR-capture time
      4. Third-party origins — paths starting with known third-party prefixes
         (Google APIs, Firestore, gRPC channels)
      5. Path templates ending with single slash + empty segment (truncated)
    """
    import re as _re_r138
    path = n.get("path_template") or ""
    if not path or path == "/":
        return True, "empty_path"
    p_lower = path.lower()

    # (1) Static asset extensions
    _STATIC_EXT_RE = _re_r138.compile(
        r"\.(css|js|mjs|woff|woff2|ttf|eot|svg|png|jpg|jpeg|gif|ico|webp|map|json|xml|txt|pdf)(\?|$)",
        _re_r138.IGNORECASE,
    )
    if _STATIC_EXT_RE.search(p_lower):
        return True, f"static_asset_ext"

    # (2) Static-path prefixes (case-insensitive)
    _STATIC_PREFIXES = (
        "/static/", "/assets/", "/files/", "/ajax/libs/", "/icons/",
        "/fonts/", "/img/", "/images/", "/media/", "/cdn/",
    )
    for prefix in _STATIC_PREFIXES:
        if p_lower.startswith(prefix):
            return True, f"static_prefix:{prefix}"

    # (3) Unresolved Postman variables — chain extractor failed to substitute.
    # These templates emit URLs like `{{base_url}}/{{id_id}}` which always 404.
    if _re_r138.search(r"\{\{[a-zA-Z_][a-zA-Z0-9_]*\}\}", path):
        return True, "unresolved_postman_variable"

    # (4) Third-party origins (path-fragment matching; HAR captures these
    # when the SPA references third-party services)
    _THIRD_PARTY_FRAGMENTS = (
        "google.firestore", "googleapis.com", "/grpc/", ".grpc.",
        "doubleclick.net", "googletagmanager", "gstatic.com",
        "/cloudfront.net/", "/cdn-cgi/", "/__cfduid",
    )
    for frag in _THIRD_PARTY_FRAGMENTS:
        if frag in p_lower:
            return True, f"third_party:{frag}"

    # (5) Truncated paths — single-segment paths ending without an extension
    # AND not a recognized API prefix. R-PreserveAPIPaths: paths starting
    # with /api/, /v1/, /v2/, /v3/ are API surfaces; preserve them.
    if not p_lower.startswith(("/api/", "/v1/", "/v2/", "/v3/", "/rest/", "/graphql")):
        # Single-token shallow paths like `/icon`, `/config.`, `/google.`
        # are typically truncated HAR captures (path was actually `/icon.png`
        # or `/config.json` but got truncated). Two reject conditions:
        #   (a) path ends with `.` (trailing-dot truncation: `/google.`)
        #   (b) single segment + no `.` + length<16 (`/icon`)
        if path.endswith(".") or path.endswith("./"):
            return True, "truncated_trailing_dot"
        _seg_count = sum(1 for s in path.split("/") if s.strip())
        if _seg_count <= 1 and len(path) < 16 and "." not in path.split("/")[-1]:
            return True, "truncated_single_segment"

    # (6) R217 SURFACE-GROUNDING — skip a node whose path is a DEAD discovered
    # `/api/v1/*`). Data-driven (vs the reverted blanket `/api/v1/` skip): only
    # fires when `dead_paths` is populated from a real discovered surface, so
    # synthetic chains + the R-PreserveAPIPaths design stay intact.
    if dead_paths:
        if _r217_norm_template(path) in dead_paths:
            return True, "dead_endpoint_no_response"

    return False, ""


def build_collection_from_chain(
    chain: dict,
    *,
    collection_name: str,
    base_url: str | None = None,
    adversarial: bool = False,
    dead_paths: "set[str] | None" = None,
) -> dict:
    """Phase D entrypoint. Returns a full Postman v2.1 collection JSON.

    Args:
        chain: dict form of a CallChain (`CallChain.to_dict()`).
        collection_name: human-readable name, e.g. "REQ-016 chain".
        base_url: optional explicit base URL; default uses `{{base_url}}`.
        adversarial: when True, emits Phase I4 adversarial sibling — auth
            stripped + 4xx-expected assertions.

    The collection is ready for `newman run <file.json>`. No external
    dependencies; Postman 2.1 schema is universally supported by Newman 5+.

    R138.A — chain nodes are filtered via `_r138_a_should_skip_chain_node`
    BEFORE item emission. Rejects static assets, third-party origins,
    unresolved Postman variables, and truncated paths. Live evidence from
    R135 Iter 0: 169 of 340 chain_adv items 404'd on non-testable paths.
    """
    nodes = chain.get("nodes") or []
    items: list[dict] = []
    _r138_a_skipped: dict[str, int] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        # R138.A — grounding filter: skip non-testable paths
        # R217 — + surface-grounding skip of dead discovered endpoints.
        _skip, _reason = _r138_a_should_skip_chain_node(n, dead_paths=dead_paths)
        if _skip:
            _r138_a_skipped[_reason] = _r138_a_skipped.get(_reason, 0) + 1
            continue
        seq = int(n.get("sequence_index") or 0)
        method = (n.get("method") or "GET").upper()
        path_template = n.get("path_template") or "/"
        item_name = f"step_{seq:02d}_{method}_{path_template}"
        request = _build_request(n, base_url=base_url)
        if adversarial:
            # Strip Authorization header so server returns 401/403
            request["header"] = [h for h in request["header"]
                                 if h.get("key", "").lower() != "authorization"]
            # Mark adversarial intent
            request["header"].append(
                {"key": "X-ARTA-Adversarial", "value": "auth_stripped"},
            )
        items.append({
            "name": item_name,
            "request": request,
            "event": _build_item_event_block(n, is_adversarial=adversarial),
            "response": [],
        })

    # Initialize collection-scoped variables (chain-internal) — operators
    # can override via project env-vars (`{{base_url}}` etc).
    collection_variables = []

    # R138.A — log skipped-node breakdown (operator-visible)
    if _r138_a_skipped:
        log.info(
            "R138.A: chain_aware_newman filtered %d nodes (collection=%r, "
            "adversarial=%s): %s",
            sum(_r138_a_skipped.values()), collection_name, adversarial,
            dict(_r138_a_skipped),
        )

    return {
        "info": {
            "_postman_id": str(uuid.uuid4()),
            "name": collection_name + (" (adversarial)" if adversarial else ""),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            "_arta_chain_id": chain.get("chain_id"),
            "_arta_semantic_hash": chain.get("semantic_hash"),
            # R138.A — telemetry for operator dashboard
            "_arta_r138_a_skipped": dict(_r138_a_skipped),
        },
        "item": items,
        "variable": collection_variables,
    }


def build_chain_aware_newman(
    chain: dict,
    *,
    requirement_id: str,
    base_url: str | None = None,
    emit_adversarial: bool = True,
    project_id: str | None = None,
) -> list[dict]:
    """High-level Phase D wrapper.

    Returns a list of Postman collection dicts: one happy-path + one
    adversarial (when `emit_adversarial=True`). The automation_engineer
    persists each as `<req>_chain.postman_collection.json` and
    `<req>_chain_adv.postman_collection.json`.

    R217 — `project_id` (when given) loads the DEAD discovered-endpoint set so
    chain nodes hitting probe-self-guess paths (e.g. `/api/v1/*`) are
    surface-grounded out before emission.
    """
    name = f"{requirement_id} chain"
    dead_paths = _load_dead_endpoint_paths(project_id)
    out = [build_collection_from_chain(
        chain, collection_name=name, base_url=base_url, dead_paths=dead_paths)]
    if emit_adversarial:
        out.append(build_collection_from_chain(
            chain, collection_name=name, base_url=base_url, adversarial=True,
            dead_paths=dead_paths,
        ))
    return out


__all__ = [
    "build_collection_from_chain",
    "build_chain_aware_newman",
    "_load_dead_endpoint_paths",
]
