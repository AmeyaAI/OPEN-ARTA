"""Phase 3 — Requirement→code traceability + test-correctness gate.

The directive's central correctness principle:
  *"If a generated test case cannot be traced to actual code implementation,
    ARTA must flag it as potentially incorrect."*

ARTA already verifies a test's endpoints EXIST on the SUT (grounding_validator
`unknown_endpoint`). This module adds the missing link: does the test exercise
at least one endpoint that IMPLEMENTS its requirement? It:
  1. computes a requirement's implementing endpoints (the captured SUT surface
     filtered by the requirement's Gherkin keywords — deterministic, no LLM),
  2. extracts the endpoints a generated test actually exercises (Newman URLs /
     Playwright request paths),
  3. flags the test `potentially_incorrect` when it touches API endpoints but
     NONE of them trace to the requirement's implementing surface.

The flag is a SOFT signal by default (metadata + metric), not a hard dispatch
block — over-blocking would tank the pass rate, and a UI-only test legitimately
has no API endpoints. Env `ARTA_TRACEABILITY_GATE`: off | flag (default).
Completeness % is surfaced for the directive's 100%-traceability target.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from pathlib import Path

from .semantic_intent_validator import extract_gherkin_keywords

log = logging.getLogger("arta.traceability")

_TRACE_DIR = Path(".arta/traceability")
# segment tokens that carry no requirement signal (version/api scaffolding)
_NOISE_SEGMENTS = {"api", "v1", "v2", "v3", "rest", "graphql", ""}


def _norm_path(raw: str) -> str:
    """Normalise a URL/path to a leading-slash path (strip host, query, vars)."""
    if not raw:
        return ""
    s = urllib.parse.unquote(str(raw))
    s = re.sub(r"\{\{[^}]+\}\}", "", s)          # {{base_url}}
    s = re.sub(r"^https?://[^/]+", "", s)        # scheme+host
    s = s.split("?", 1)[0].split("#", 1)[0]
    if not s.startswith("/"):
        s = "/" + s
    return re.sub(r"/+", "/", s).rstrip("/") or "/"


def _path_tokens(path: str) -> set[str]:
    """Meaningful (non-noise, non-id) path segment tokens for matching."""
    toks: set[str] = set()
    for seg in _norm_path(path).split("/"):
        seg = seg.lower()
        if not seg or seg in _NOISE_SEGMENTS:
            continue
        if seg.startswith("{") or re.fullmatch(r"[0-9a-f]{6,}", seg) or seg.isdigit():
            continue  # path param / id
        if len(seg) >= 3:
            toks.add(seg)
    return toks


def _seg_list(path: str) -> list[str]:
    return [s for s in _norm_path(path).split("/") if s != ""]


def _is_param_seg(seg: str) -> bool:
    """A path segment that is a variable/id (template `{x}`/`:x`/`{{x}}`, a
    numeric id, a hex/uuid id) — these are wildcards for template matching."""
    s = (seg or "").lower()
    if seg.startswith("{") or seg.startswith(":") or seg.startswith("{{"):
        return True
    if s.isdigit():
        return True
    if re.fullmatch(r"[0-9a-f]{6,}", s):                       # hex id
        return True
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{12,}", s):         # uuid
        return True
    return False


def path_matches_template(concrete: str, template: str) -> bool:
    """R211 B3.1 — does a CONCRETE request path match an endpoint TEMPLATE?

    True when they have the same segment count and every LITERAL template
    segment equals the concrete segment; template `{param}`/`:param` and
    concrete id segments are wildcards. This is set-membership matching — it
    does NOT treat `/users/{id}/profile` and `/users/{id}/projects` as the
    same (the keyword-overlap approach did, on the shared `users` token)."""
    cs, ts = _seg_list(concrete), _seg_list(template)
    if not cs or not ts or len(cs) != len(ts):
        return False
    for c, t in zip(cs, ts):
        if _is_param_seg(t) or _is_param_seg(c):
            continue
        if c.lower() != t.lower():
            return False
    return True


# ── R301 — source-derived endpoint surface (traceability completeness) ──────
# The captured runtime surface (discovered_endpoints) UNDER-captures the SUT:
# fieldset/definition`) are absent from the 155 captured endpoints, so a genuine
# SUT 500 there gets misattributed as test_gen ("untraceable"). The Architecture
# Discovery phase already builds a RICHER surface from SUT source + OpenAPI at
# `.arta/architecture_discovery/<pid>/api_graph.json` (318 eps incl. the fieldset
# family). R301 loads it so attribution can verify an endpoint is REAL-in-source
# even when discovery's runtime capture missed it. GENERIC (any pid); reuse-first
# (no new extraction). Killswitch ARTA_R301_SOURCE_ROUTE_SURFACE_DISABLE=1.
_SOURCE_SURFACE_CACHE: dict[str, list[dict]] = {}
_ARCH_DISCOVERY_DIR = Path(".arta/architecture_discovery")


def load_source_endpoint_surface(project_id: str) -> list[dict]:
    """R301 — the SUT's source+OpenAPI-derived endpoint templates for `project_id`
    (from the Architecture Discovery api_graph). Returns [{method, path}]; []
    when disabled, absent, or unreadable (fail-truthful: no surface → no false
    source-verified verdict). Cached per pid."""
    import os as _os
    if _os.environ.get("ARTA_R301_SOURCE_ROUTE_SURFACE_DISABLE") == "1" or not project_id:
        return []
    if project_id in _SOURCE_SURFACE_CACHE:
        return _SOURCE_SURFACE_CACHE[project_id]
    out: list[dict] = []
    try:
        f = _ARCH_DISCOVERY_DIR / str(project_id) / "api_graph.json"
        if f.exists():
            data = json.loads(f.read_text())
            seen: set = set()
            for n in (data.get("nodes") or []):
                if not isinstance(n, dict) or n.get("kind") != "endpoint":
                    continue
                p = _norm_path(n.get("path") or "")
                if not p or p == "/":
                    continue
                m = str(n.get("method") or "GET").upper()
                if (m, p) in seen:
                    continue
                seen.add((m, p))
                out.append({"method": m, "path": p})
    except Exception as _exc:
        log.debug("R301: source surface load failed for %s: %s", project_id, _exc)
    _SOURCE_SURFACE_CACHE[project_id] = out
    return out


def _distinctive_suffix(path: str, k: int = 2) -> list[str]:
    """The last `k` LITERAL (non-param, non-noise) segments of a path — the
    distinctive operation tail (e.g. `.../fieldset/definition` → [fieldset,
    definition]). Used for prefix-tolerant source-surface matching (the live
    path carries account/service prefixes the source template lacks)."""
    lits = [s.lower() for s in _seg_list(path)
            if not _is_param_seg(s) and s.lower() not in _NOISE_SEGMENTS]
    return lits[-k:] if len(lits) >= k else lits


def endpoint_in_source_surface(concrete_path: str, surface: list[dict],
                               *, min_suffix: int = 2) -> bool:
    """R301 — is `concrete_path` a REAL SUT route per the source surface, tolerant
    of account/prefix differences? First tries exact template match
    (`path_matches_template`); then falls back to distinctive-suffix membership:
    the concrete path's last `min_suffix` literal segments appear, in order, as a
    trailing subsequence of some source template. Requires ≥`min_suffix`
    distinctive segments so a short/generic tail can't false-match."""
    if not concrete_path or not surface:
        return False
    for e in surface:
        if path_matches_template(concrete_path, e.get("path", "")):
            return True
    csuf = _distinctive_suffix(concrete_path, min_suffix)
    if len(csuf) < min_suffix:
        return False
    for e in surface:
        tlits = [s.lower() for s in _seg_list(e.get("path", ""))
                 if not _is_param_seg(s) and s.lower() not in _NOISE_SEGMENTS]
        if len(tlits) >= min_suffix and tlits[-min_suffix:] == csuf:
            return True
    return False


def build_requirement_endpoint_map(
    captured_endpoints: list[dict],
    gherkin_text: str,
    *,
    openapi_templates: list | None = None,
    source_endpoints: list | None = None,
    min_score: int = 1,
) -> dict:
    """R211 A2/B3.1 — GROUNDED requirement→endpoint mapping (replaces the
    fail-open keyword `implementing_paths`).

    Score the REAL endpoint surface (captured runtime endpoints + OpenAPI
    templates) by Gherkin-keyword overlap and return the matched REAL
    endpoints. When ZERO real endpoints match (or there is no real surface),
    mark `ungroundable=True` — the truthful "this requirement cannot be
    grounded → BLOCK, don't ship a guess" signal — instead of silently
    returning everything (the old fail-open behavior that produced false
    'traceable' verdicts).

    Returns {"endpoints": [{method, path, score}], "ungroundable": bool, "reason": str}.
    """
    real: list[dict] = []
    seen: set = set()
    _src_files: dict = {}   # (m,p) → SUT source file backing the route (Code→API identity)
    for e in (captured_endpoints or []):
        p = _norm_path((e or {}).get("path", ""))
        if not p or p == "/":
            continue
        key = (str((e or {}).get("method") or "GET").upper(), p)
        # Source-derived backend routes (source="github", persisted into the
        # captured store by discovery_executor Fix-2/P8) carry the handler `file`
        # — the real Code→API identity. Record it even if the (m,p) was already
        # seen from a runtime capture (runtime rows lack the file).
        if (e or {}).get("file"):
            _src_files.setdefault(key, e["file"])
        if key in seen:
            continue
        seen.add(key)
        real.append({"method": key[0], "path": p})
    for t in (openapi_templates or []):
        if isinstance(t, str):
            p, m = _norm_path(t), "GET"
        else:
            p, m = _norm_path((t or {}).get("path") or ""), str((t or {}).get("method") or "GET").upper()
        if not p or p == "/" or (m, p) in seen:
            continue
        seen.add((m, p))
        real.append({"method": m, "path": p})
    # R301 — augment the surface with SOURCE-derived routes (Architecture
    # Discovery api_graph). These carry source_verified=True so a keyword match
    # against them proves the requirement traces to a route that EXISTS in the
    # SUT's source, not merely a runtime-captured path. Completes the surface
    # the runtime probe under-captured.
    _src_keys: set = set()
    for t in (source_endpoints or []):
        if isinstance(t, str):
            p, m, _f = _norm_path(t), "GET", None
        else:
            p, m = _norm_path((t or {}).get("path") or ""), str((t or {}).get("method") or "GET").upper()
            _f = (t or {}).get("file") or None
        if not p or p == "/":
            continue
        _src_keys.add((m, p))
        if _f:
            _src_files[(m, p)] = _f
        if (m, p) in seen:
            continue
        seen.add((m, p))
        real.append({"method": m, "path": p})
    if not real:
        return {"endpoints": [], "ungroundable": True, "reason": "no_real_endpoint_surface"}
    kws = extract_gherkin_keywords(gherkin_text or "")
    mapped: list[dict] = []
    for e in real:
        ptoks = _path_tokens(e["path"])
        score = sum(1 for tok in ptoks if any(tok in k or k in tok for k in kws))
        if score >= min_score:
            mapped.append({**e, "score": score,
                           "source_verified": (e["method"], e["path"]) in _src_keys,
                           "source_file": _src_files.get((e["method"], e["path"]))})
    mapped.sort(key=lambda x: (-x["score"], x["path"]))
    if not mapped:
        return {"endpoints": [], "ungroundable": True, "reason": "no_keyword_match"}
    # NOTE (R212): subject-vs-surface ungroundability (auth reqs spuriously
    # matching cm paths via the generic "user" token) was attempted here but a
    # keyword-count heuristic false-positives on cm reqs that incidentally
    # mention auth ("user signs in and creates an organization"). Detecting the
    # PRIMARY subject vs an incidental mention needs a real classifier or an
    # authenticated-flow probe that captures the auth surface — left as a
    # documented limitation rather than shipping a fragile heuristic.
    return {"endpoints": mapped, "ungroundable": False, "reason": "matched"}


def untested_endpoints(real_endpoints: list[dict], exercised_paths: set[str]) -> list[dict]:
    """R211 B3.4 — real endpoints that NO test exercises (a true coverage gap).
    `real_endpoints` = [{method, path(template)}]; `exercised_paths` = concrete
    paths the test suite touches. Template-aware membership."""
    out: list[dict] = []
    ex = {p for p in (exercised_paths or set()) if p and p != "/"}
    for e in (real_endpoints or []):
        tmpl = e.get("path") if isinstance(e, dict) else e
        if not tmpl:
            continue
        if not any(path_matches_template(c, tmpl) for c in ex):
            out.append(e if isinstance(e, dict) else {"path": tmpl})
    return out


def extract_test_endpoints(content, tool: str) -> set[str]:
    """Extract the API paths a generated test exercises.

    Newman: walk the (possibly nested) collection items → request.url.
    Playwright/pytest: regex request.<verb>(...) / page.goto / '/api/...' literals.
    """
    paths: set[str] = set()
    tool = (tool or "").lower()

    if tool == "newman":
        coll = content
        if isinstance(content, str):
            try:
                coll = json.loads(content)
            except Exception:
                coll = {}

        def _walk(items):
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                if isinstance(it.get("item"), list):
                    _walk(it["item"])
                req = it.get("request") or {}
                url = req.get("url") if isinstance(req, dict) else None
                if isinstance(url, dict):
                    seg = url.get("path")
                    if isinstance(seg, list):
                        paths.add(_norm_path("/" + "/".join(str(s) for s in seg)))
                    elif url.get("raw"):
                        paths.add(_norm_path(url["raw"]))
                elif isinstance(url, str):
                    paths.add(_norm_path(url))
        if isinstance(coll, dict):
            _walk(coll.get("item"))
    else:
        text = content if isinstance(content, str) else json.dumps(content)
        for m in re.finditer(r"""(?:request|page|context)\.(?:get|post|put|patch|delete|fetch|goto|request)\s*\(\s*[`'"]([^`'"]+)[`'"]""", text, re.I):
            paths.add(_norm_path(m.group(1)))
        for m in re.finditer(r"""[`'"](/(?:api|v1|v2|rest|graphql)/[^`'"\s]+)[`'"]""", text, re.I):
            paths.add(_norm_path(m.group(1)))

    return {p for p in paths if p and p != "/"}


def implementing_paths(captured_endpoints: list[dict], gherkin_text: str,
                       *, min_score: int = 1) -> set[str]:
    """The requirement's implementing endpoints = captured SUT paths whose
    tokens overlap the requirement's Gherkin keywords. Falls back to ALL
    captured paths when the Gherkin is too thin to score (avoids false flags)."""
    paths = {_norm_path(e.get("path", "")) for e in captured_endpoints or [] if e.get("path")}
    kws = extract_gherkin_keywords(gherkin_text or "")
    if len(kws) < 3:
        return paths  # not enough signal → don't narrow (fail-open)
    scored = set()
    for p in paths:
        ptoks = _path_tokens(p)
        if sum(1 for t in ptoks if any(t in k or k in t for k in kws)) >= min_score:
            scored.add(p)
    return scored or paths


def assess_traceability(test_paths: set[str], impl_paths: set[str] | None = None,
                        *, mapped_endpoints: list[dict] | None = None) -> dict:
    """Does the test trace to the requirement's implementing code?

    GROUNDED mode (R211 B3.1 — pass `mapped_endpoints`): set-membership. A test
    endpoint traces iff it template-matches a REAL mapped endpoint (no token
    overlap, no fail-open). Reports `off_target` (test endpoints matching no
    mapped endpoint) so the caller can BLOCK a fully-off-target test.

    LEGACY mode (token-overlap, back-compat — pass `impl_paths`): traceable
    when ≥1 test endpoint shares meaningful path tokens with an implementing
    endpoint.

    Either mode: a test exercising no API endpoint is traceable (UI-only —
    nothing to trace).
    """
    impl_paths = impl_paths or set()
    if not test_paths:
        return {"traceable": True, "reason": "no_api_endpoints", "matched": [],
                "test_endpoint_count": 0,
                "implementing_count": len(mapped_endpoints if mapped_endpoints is not None else impl_paths)}

    if mapped_endpoints is not None:
        norm_map = [
            {"path": (m.get("path") if isinstance(m, dict) else m),
             "method": (m.get("method") if isinstance(m, dict) else "GET")}
            for m in mapped_endpoints
        ]
        norm_map = [m for m in norm_map if m["path"]]
        matched = sorted(tp for tp in test_paths
                         if any(path_matches_template(tp, m["path"]) for m in norm_map))
        off_target = sorted(tp for tp in test_paths
                            if not any(path_matches_template(tp, m["path"]) for m in norm_map))
        # endpoint_keys (method:template) for the matched mapped endpoints —
        # used to write TestCase-[:EXERCISES]->Endpoint edges (B3.2).
        matched_keys = sorted({
            f"{(m.get('method') or 'GET')}:{m['path']}"
            for tp in matched for m in norm_map if path_matches_template(tp, m["path"])
        })
        return {
            "traceable": bool(matched),
            "grounded": True,
            "reason": "matched_mapped_endpoint" if matched else "no_mapped_endpoint_match",
            "matched": matched,
            "matched_endpoint_keys": matched_keys,
            "off_target": off_target,
            "test_endpoint_count": len(test_paths),
            "implementing_count": len(norm_map),
            "hint": ("" if matched else
                     "Test exercises no endpoint in this requirement's mapped real "
                     "surface — it likely targets the wrong API (regenerate) or the "
                     "requirement is ungroundable."),
        }

    impl_tok = set()
    for p in impl_paths:
        impl_tok |= _path_tokens(p)
    matched = [tp for tp in test_paths if _path_tokens(tp) & impl_tok]
    return {
        "traceable": bool(matched),
        "reason": "matched_implementing" if matched else "no_overlap_with_implementing",
        "matched": sorted(matched),
        "test_endpoint_count": len(test_paths),
        "implementing_count": len(impl_paths),
        "hint": ("" if matched else
                 "Test exercises endpoints that do not trace to this requirement's "
                 "implementing surface — verify it targets the right APIs."),
    }


# ── P2 — authorization-dimension stamp for the traceability spine ─────────────

def authz_stamp(matched_endpoint_keys, authz_model: dict | None) -> dict:
    """The authorization dimension of a test's traceability: which of the
    endpoints it actually exercises are authz-gated, with each op's
    permission/scope/visibility/expected-status from the derived route catalog.

    `matched_endpoint_keys` are `METHOD:/template` (from assess_traceability);
    the authz model's ops key identically (both OpenAPI-derived). Returns
    {gated_endpoints:[{key,permission,scope,visibility,expected_status}],
    gated_count}. Empty when no model / no gated match — the caller stamps only
    when gated_count>0. Deterministic; no LLM."""
    ops = (authz_model or {}).get("operations") or []
    if not ops or not matched_endpoint_keys:
        return {"gated_endpoints": [], "gated_count": 0}
    by_key = {f"{(o.get('method') or 'GET').upper()}:{o.get('path')}": o
              for o in ops if o.get("auth_gated")}
    gated = []
    for k in matched_endpoint_keys:
        op = by_key.get(k)
        if op:
            gated.append({"key": k, "permission": op.get("permission_guess"),
                          "scope": op.get("scope"), "visibility": op.get("visibility"),
                          "expected_status": op.get("success_status")})
    return {"gated_endpoints": gated, "gated_count": len(gated)}


# ── Data-Object stamp — the domain-entity dimension of a test's traceability ──

def data_object_stamp(matched_endpoint_keys, entity_map: dict | None) -> dict:
    """The Data-Object dimension of a test's traceability: which SUT domain
    entities (OpenAPI component schemas — `WidgetDto`, `Order`, …) the
    endpoints it exercises read or write. Completes the Req→…→API→Data spine.

    `matched_endpoint_keys` are `METHOD:/template` (from assess_traceability);
    `entity_map` is `{METHOD:/path → entity}` from sut_topology.openapi_entity_index
    (both OpenAPI-derived, keyed identically). Returns
    {objects:[{key,entity}], object_count, entities:[distinct]}. Empty when no
    entity match — the caller stamps only when object_count>0. Deterministic; no LLM."""
    if not matched_endpoint_keys or not entity_map:
        return {"objects": [], "object_count": 0, "entities": []}
    objs = [{"key": k, "entity": entity_map[k]} for k in matched_endpoint_keys if k in entity_map]
    entities = sorted({o["entity"] for o in objs})
    return {"objects": objs, "object_count": len(objs), "entities": entities}


# ── Source-Code-Component stamp — the Code→API link of a test's traceability ──

def source_component_stamp(matched_endpoint_keys, mapped_endpoints: list | None) -> dict:
    """The backend Source-Code-Component dimension of a test's traceability:
    which SUT source file backs each endpoint the test exercises (real handler
    identity, not merely `source_verified=True`). Completes the Req→Code→API→Test
    spine.

    `matched_endpoint_keys` are `METHOD:/template` (from assess_traceability);
    `mapped_endpoints` carry `source_file` (threaded from Architecture-Discovery
    backend-route extraction via build_requirement_endpoint_map). Returns
    {components:[{key,file}], component_count}. Empty when no source_file match —
    the caller stamps only when component_count>0. Deterministic; no LLM."""
    if not matched_endpoint_keys or not mapped_endpoints:
        return {"components": [], "component_count": 0}
    by_key = {f"{(m.get('method') or 'GET').upper()}:{m.get('path')}": m.get("source_file")
              for m in mapped_endpoints
              if isinstance(m, dict) and m.get("source_file")}
    comps = [{"key": k, "file": by_key[k]} for k in matched_endpoint_keys if k in by_key]
    return {"components": comps, "component_count": len(comps)}


# ── Business-Workflow stamp — which captured CallChain(s) a test exercises ────

def build_chain_index(chains: list | None) -> dict:
    """Inverted index `endpoint_key(METHOD:/path_template) → [{chain_id,
    endpoint_count}]` from the project's captured CallChains
    (api_discovery.load_chains). Built ONCE per gate run so per-test lookup is
    O(matched_keys), not O(chains × nodes). Deterministic; {} on no chains."""
    index: dict[str, list] = {}
    for ch in (chains or []):
        if not isinstance(ch, dict):
            continue
        nodes = ch.get("nodes") or []
        meta = {"chain_id": ch.get("chain_id"), "endpoint_count": len(nodes)}
        seen: set = set()
        for n in nodes:
            if not isinstance(n, dict):
                continue
            key = f"{(n.get('method') or 'GET').upper()}:{n.get('path_template') or n.get('path')}"
            if key in seen:              # a chain counts once per endpoint_key
                continue
            seen.add(key)
            index.setdefault(key, []).append(meta)
    return index


def workflow_stamp(matched_endpoint_keys, chain_index: dict | None) -> dict:
    """The Business-Workflow dimension of a test's traceability: which captured
    CallChain(s) — ordered API sequences with provides/consumes data
    dependencies — the endpoints this test exercises belong to. Completes the
    Req→AC→WORKFLOW→Code→API→Data→Test chain.

    `matched_endpoint_keys` are `METHOD:/template` (from assess_traceability);
    `chain_index` is the prebuilt `build_chain_index` map (same key space).
    Returns {workflows:[{chain_id, endpoint_count, matched_count}], workflow_count}
    (sorted by matched_count desc, cap 5). Empty when no chain match — the caller
    stamps only when workflow_count>0. Deterministic; no LLM; fail-open {}."""
    if not matched_endpoint_keys or not chain_index:
        return {"workflows": [], "workflow_count": 0}
    hits: dict = {}   # chain_id → {endpoint_count, matched_count}
    for mk in matched_endpoint_keys:
        for meta in chain_index.get(mk, []):
            cid = meta.get("chain_id")
            h = hits.setdefault(cid, {"chain_id": cid,
                                      "endpoint_count": meta.get("endpoint_count", 0),
                                      "matched_count": 0})
            h["matched_count"] += 1
    workflows = sorted(hits.values(), key=lambda w: w["matched_count"], reverse=True)
    return {"workflows": workflows[:5], "workflow_count": len(workflows)}


# ── R330 P1d — per-test grounding provenance (extracted from the tests.py gate
# so it is unit-testable and evidence-preserving) ────────────────────────────

_PROV_STRENGTH = {"source_grounded": 5, "human_corrected": 4,
                  "requirement_declared": 3, "observed": 2, "unlabeled": 1}


def derive_grounded_by(test_endpoint_count: int, matched_keys: list,
                       cap_by_key: dict) -> str:
    """Honest grounding provenance for ONE test.

    `cap_by_key` maps "METHOD:path" → the FULL captured-endpoint dict (not just
    its `source` string) so evidence-only endpoints (source_har/discovered_at,
    no `source`) rank as `observed` — the synthetic {"source": best} rebuild
    lost that branch and understated grounding.
    """
    from .api_discovery import endpoint_provenance
    if not test_endpoint_count:
        return "ui"                     # no API endpoints — nothing to ground
    if not matched_keys:
        return "guess"                  # hits endpoints ARTA never captured
    provs = [endpoint_provenance(cap_by_key.get(k) or {}) for k in matched_keys]
    best = max(provs, key=lambda p: _PROV_STRENGTH.get(p, 0))
    return "guess" if best == "unlabeled" else best


# ── persistence + completeness metric ────────────────────────────────────────

def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(s))[:120]


def persist_traceability(project_id: str, test_id: str, req_id: str, result: dict) -> None:
    try:
        d = _TRACE_DIR / project_id
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{_safe(test_id)}.json").write_text(json.dumps(
            {"test_id": test_id, "req_id": req_id, **result}, indent=2))
    except Exception:
        pass


def prune_traceability(project_id: str, req_id: str, keep_test_ids: set) -> int:
    """R330 P1d — sediment control. The store is append-only across regens, so
    rows for tests that no longer exist keep voting in read_traceability (they
    showed as `unknown` grounded_by). After a requirement's gate pass, drop that
    requirement's rows whose test_id is not in the current batch.
    Killswitch: ARTA_TRACE_PRUNE_DISABLE=1. Returns rows removed."""
    import os
    if os.environ.get("ARTA_TRACE_PRUNE_DISABLE") == "1":
        return 0
    removed = 0
    d = _TRACE_DIR / project_id
    if not d.is_dir():
        return 0
    keep = {str(t) for t in (keep_test_ids or set())}
    for p in d.glob("*.json"):
        try:
            row = json.loads(p.read_text())
        except Exception:
            continue
        if str(row.get("req_id")) == str(req_id) and str(row.get("test_id")) not in keep:
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
    return removed


def read_traceability(project_id: str) -> dict:
    """Aggregate per-test traceability → completeness % (directive target 100%)."""
    rows: list[dict] = []
    d = _TRACE_DIR / project_id
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                rows.append(json.loads(p.read_text()))
            except Exception:
                continue
    n = len(rows)
    if n == 0:
        return {"project_id": project_id, "test_count": 0, "traceability_pct": None}
    traceable = sum(1 for r in rows if r.get("traceable"))
    flagged = [{"test_id": r.get("test_id"), "req_id": r.get("req_id"),
                "reason": r.get("reason")} for r in rows if not r.get("traceable")]
    # R330 P1d — surface the honest per-test grounding distribution (source_grounded
    # / human_corrected / requirement_declared / observed / guess / ui) so the
    # operator sees how well-grounded the generated tests are, not just traceable %.
    from collections import Counter as _Counter
    grounded_by = _Counter(r.get("grounded_by") or "unknown" for r in rows)
    # R330 P1d — per-generation source-grounding status distribution (fail-loud
    # surfacing: "unavailable:no_github_token" etc. stamped by the gate).
    src_status = _Counter(r["source_grounding"] for r in rows if r.get("source_grounding"))
    # Distinct "needs attention" count for the panel CTA: a `guess` test is
    # usually ALSO flagged untraceable — summing the two double-counted.
    needs_attention = sum(1 for r in rows
                          if not r.get("traceable") or r.get("grounded_by") == "guess")
    # Code→API spine completeness: tests whose exercised endpoints resolve to a
    # real SUT source file (source_component_stamp), not just source_verified.
    source_component_count = sum(
        1 for r in rows if (r.get("source_components") or {}).get("component_count"))
    # API→Data spine: tests whose endpoints touch a named SUT domain entity, plus
    # the distinct set of entities under test (coverage-by-entity).
    data_object_count = sum(
        1 for r in rows if (r.get("data_objects") or {}).get("object_count"))
    entities_under_test = sorted({
        e for r in rows for e in ((r.get("data_objects") or {}).get("entities") or [])})
    # Business-Workflow spine: tests linked to a captured business workflow (chain).
    workflow_linked_count = sum(
        1 for r in rows if (r.get("workflows") or {}).get("workflow_count"))
    return {
        "project_id": project_id,
        "test_count": n,
        "traceable_count": traceable,
        "traceability_pct": round(100 * traceable / n, 1),
        "potentially_incorrect": flagged[:50],
        "potentially_incorrect_count": len(flagged),
        "grounded_by": dict(grounded_by),
        "source_grounding": dict(src_status),
        "needs_attention_count": needs_attention,
        "source_component_count": source_component_count,
        "data_object_count": data_object_count,
        "entities_under_test": entities_under_test,
        "workflow_linked_count": workflow_linked_count,
    }
