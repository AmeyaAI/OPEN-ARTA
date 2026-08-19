"""Phase AD — Architecture Discovery.

The platform directive requires ARTA to understand the System Under Test
BEFORE generating any artifact, and to emit first-class, queryable graphs of
its architecture. ARTA already DISCOVERS the raw material (runtime
captured_endpoints + DOM, Phase C4 CallChain data-flow, R104.B source routes,
R156 token-chains + protocol detection) but in a fragmented, un-persisted,
un-traced way.

This module is the consolidation layer: a DETERMINISTIC (zero-LLM) phase that
AGGREGATES existing discovery outputs into 6 graph artifacts emitted before
generation and injectable into gen prompts. It introduces NO new discovery —
it only reads existing loaders and links what they produce. Honors the
small-on-prem-Ollama constraint (no model calls) and single-source-of-truth
(reuses api_discovery loaders, call_chain shapes, R156 token-chains).

Graphs: architecture_map, api_graph, dependency_graph, auth_graph,
workflow_graph, data_flow_graph (+ discovery_summary with coverage gaps).

Killswitch: ARTA_ARCH_DISCOVERY_DISABLE=1 (the discovery_executor caller
skips the phase). Persistence + Neo4j writes are best-effort.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("arta.arch_discovery")

_OUT_DIR = Path(".arta/architecture_discovery")
_GRAPH_NAMES = (
    "architecture_map", "api_graph", "dependency_graph",
    "auth_graph", "workflow_graph", "data_flow_graph",
)


# ── small helpers ────────────────────────────────────────────────────────────

def _ep_id(method: str, path: str) -> str:
    return f"{(method or 'GET').upper()} {path or '/'}"


# R330 P3 — classify_protocol moved to protocol_discovery (single source of
# truth; breaks the automation_engineer↔architecture_discovery import cycle).
# Re-imported under the old private name so all existing callers keep working.
from .protocol_discovery import classify_protocol as _classify_protocol  # noqa: E402


def _shape_present(v: Any) -> bool:
    return v is not None and v != {} and v != []


# ── builders (pure, sync, unit-testable) ─────────────────────────────────────

def build_architecture_map(captured: list[dict], fe_routes: list[dict],
                           repos: list[dict]) -> dict:
    """SUT topology: services (repos) + frontend routes + backend endpoints,
    linked by which FE route calls which BE endpoint (api_calls ↔ captured)."""
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ep: set[str] = set()

    for r in repos or []:
        name = (r.get("name") or r.get("repo") or "") if isinstance(r, dict) else str(r)
        if not name:
            continue
        nodes.append({"id": f"service:{name}", "kind": "service", "name": name,
                      "provider": (r.get("provider") if isinstance(r, dict) else None)})

    captured_paths = {(e.get("path") or "") for e in captured or []}
    for e in captured or []:
        eid = _ep_id(e.get("method"), e.get("path"))
        if eid in seen_ep:
            continue
        seen_ep.add(eid)
        nodes.append({"id": eid, "kind": "backend_endpoint",
                      "method": (e.get("method") or "GET").upper(),
                      "path": e.get("path"), "evidence_count": e.get("evidence_count", 0)})

    for fr in fe_routes or []:
        route = fr.get("route") or fr.get("path") or ""
        if not route:
            continue
        api_calls = fr.get("api_calls") or []
        nodes.append({"id": f"route:{route}", "kind": "frontend_route", "route": route,
                      "file": fr.get("file"), "api_calls": api_calls,
                      "test_ids": len(fr.get("test_ids") or [])})
        for call in api_calls:
            # Link FE route → BE endpoint when the called path matches a captured path.
            match = next((p for p in captured_paths if p and (p == call or p.endswith(call) or call.endswith(p))), None)
            if match:
                # Find any method for that path (prefer GET).
                m = next((e.get("method") for e in captured if (e.get("path") == match)), "GET")
                edges.append({"from": f"route:{route}", "to": _ep_id(m, match),
                              "kind": "route_calls_api"})

    return {"nodes": nodes, "edges": edges}


def build_api_graph(captured: list[dict], be_routes: list[dict] | None = None,
                    openapi_endpoints: list[dict] | None = None) -> dict:
    """Endpoint catalog with protocol classification + request/response shape
    flags. Phase 4: when an OpenAPI spec is available, endpoints are enriched
    with per-param constraints (`params_detail`), request-body `required`
    fields, and auth scheme types."""
    # Phase 4 — index OpenAPI enrichment by METHOD path for merge.
    oa_by_id: dict[str, dict] = {}
    for oe in openapi_endpoints or []:
        oa_by_id[_ep_id(oe.get("method"), oe.get("path"))] = oe

    nodes: list[dict] = []
    seen: set[str] = set()
    protocol_counts: dict[str, int] = {}

    def _enrich(node: dict, oe: dict | None) -> dict:
        if oe:
            if oe.get("params_detail"):
                node["params_detail"] = oe["params_detail"]
            if oe.get("request_body_required"):
                node["request_body_required"] = oe["request_body_required"]
            if oe.get("auth_schemes"):
                node["auth_schemes"] = oe["auth_schemes"]
                node["auth_required"] = True
        return node

    for e in captured or []:
        eid = _ep_id(e.get("method"), e.get("path"))
        if eid in seen:
            continue
        seen.add(eid)
        proto = _classify_protocol(e.get("path", ""), e.get("content_type"), e.get("summary", ""))
        protocol_counts[proto] = protocol_counts.get(proto, 0) + 1
        nodes.append(_enrich({
            "id": eid, "kind": "endpoint",
            "method": (e.get("method") or "GET").upper(), "path": e.get("path"),
            "protocol": proto,
            "has_request_shape": _shape_present(e.get("request_body_shape")),
            "has_response_shape": _shape_present(e.get("response_body_shape")),
            "evidence_count": e.get("evidence_count", 0),
            "auth_required": e.get("auth_required"),
        }, oa_by_id.get(eid)))

    # Phase 4 — OpenAPI-only endpoints (declared in spec, not yet seen at runtime).
    for eid, oe in oa_by_id.items():
        if eid in seen:
            continue
        seen.add(eid)
        proto = _classify_protocol(oe.get("path", ""), None, oe.get("summary", ""))
        if (oe.get("response_type") or "") == "sse":
            proto = "sse"
        protocol_counts[proto] = protocol_counts.get(proto, 0) + 1
        nodes.append(_enrich({
            "id": eid, "kind": "endpoint", "source": "openapi",
            "method": (oe.get("method") or "GET").upper(), "path": oe.get("path"),
            "protocol": proto, "evidence_count": 0,
            "auth_required": oe.get("auth_required"),
        }, oe))

    # Optional: precise R156.C classification when backend source routes are supplied.
    audit = None
    if be_routes:
        try:
            from .automation_engineer import AutomationEngineerAgent as _AE
            audit = _AE._r156_c_2_audit_protocol_coverage(be_routes)
        except Exception as exc:  # pragma: no cover
            # C3 — surface at INFO (not silent debug) so a protocol-audit failure is visible.
            log.info("arch_discovery: R156.C protocol audit unavailable: %s", exc)

    return {"nodes": nodes, "edges": [], "protocol_counts": protocol_counts,
            "r156_c_audit": audit}


def build_dependency_graph(chains: list[dict]) -> dict:
    """Endpoint→endpoint data dependencies from CallChain provides/consumes."""
    ep_nodes: dict[str, dict] = {}
    var_nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for ch in chains or []:
        nodes = ch.get("nodes") or []
        # index endpoint id per node position for consumes→provider resolution
        idx_to_eid = {}
        for n in nodes:
            eid = _ep_id(n.get("method"), n.get("path_template") or n.get("path"))
            idx_to_eid[n.get("sequence_index")] = eid
            ep_nodes.setdefault(eid, {"id": eid, "kind": "endpoint",
                                      "method": (n.get("method") or "GET").upper(),
                                      "path": n.get("path_template") or n.get("path")})
        for n in nodes:
            eid = _ep_id(n.get("method"), n.get("path_template") or n.get("path"))
            for var, jsonpath in (n.get("provides") or {}).items():
                var_nodes.setdefault(var, {"id": f"var:{var}", "kind": "env_var", "name": var})
                edges.append({"from": eid, "to": f"var:{var}", "kind": "provides",
                              "jsonpath": jsonpath})
            for var, provider_idx in (n.get("consumes") or {}).items():
                var_nodes.setdefault(var, {"id": f"var:{var}", "kind": "env_var", "name": var})
                edges.append({"from": eid, "to": f"var:{var}", "kind": "consumes"})
                # depends_on: consumer endpoint → provider endpoint (by index)
                prov_eid = idx_to_eid.get(provider_idx) if isinstance(provider_idx, int) else None
                if prov_eid and prov_eid != eid:
                    edges.append({"from": eid, "to": prov_eid, "kind": "depends_on",
                                  "via_var": var})

    return {"nodes": list(ep_nodes.values()) + list(var_nodes.values()), "edges": edges}


def build_auth_graph(captured: list[dict], token_chains: dict | None = None,
                     claim_map: dict | None = None,
                     openapi_endpoints: list[dict] | None = None) -> dict:
    """Token derivation topology + JWT claims + per-endpoint auth (when known).
    Phase 4: also surfaces OpenAPI `securitySchemes` (oauth2/apiKey/http-bearer/
    openIdConnect) as auth_scheme nodes with requires_scheme edges."""
    nodes: list[dict] = []
    edges: list[dict] = []

    # Phase 4 — OpenAPI security schemes (distinct types) + per-endpoint usage.
    _scheme_seen: set[str] = set()
    for oe in openapi_endpoints or []:
        for scheme in (oe.get("auth_schemes") or []):
            if scheme not in _scheme_seen:
                _scheme_seen.add(scheme)
                nodes.append({"id": f"scheme:{scheme}", "kind": "auth_scheme", "name": scheme})
            eid = _ep_id(oe.get("method"), oe.get("path"))
            edges.append({"from": eid, "to": f"scheme:{scheme}", "kind": "requires_scheme"})

    token_chains = token_chains or {}
    for kind, spec in token_chains.items():
        spec = spec or {}
        nodes.append({"id": f"token:{kind}", "kind": "token", "name": kind,
                      "source": spec.get("source"), "via": spec.get("via"),
                      "ttl_min": spec.get("ttl_min"),
                      "refresh_flow": bool(spec.get("refresh_flow"))})
        upstream = spec.get("from")
        if upstream:
            edges.append({"from": f"token:{kind}", "to": f"token:{upstream}",
                          "kind": "derives_from", "via": spec.get("via"),
                          "ttl_min": spec.get("ttl_min")})

    claim_map = claim_map or {}
    # claims map onto the root token (the operator-pasted credential).
    root = next((k for k, s in token_chains.items() if not (s or {}).get("from")), None)
    for claim, field in claim_map.items():
        nodes.append({"id": f"claim:{claim}", "kind": "claim", "name": claim,
                      "maps_to": field})
        if root:
            edges.append({"from": f"token:{root}", "to": f"claim:{claim}",
                          "kind": "maps_claim"})

    # Per-endpoint auth, only when the captured store recorded it.
    for e in captured or []:
        if e.get("auth_required"):
            eid = _ep_id(e.get("method"), e.get("path"))
            nodes.append({"id": eid, "kind": "endpoint", "path": e.get("path")})
            tk = e.get("auth_token_kind") or "agent_token"
            edges.append({"from": eid, "to": f"token:{tk}", "kind": "requires_token"})

    return {"nodes": nodes, "edges": edges}


_GHERKIN_STEP_RE = re.compile(r"^\s*(Given|When|Then|And|But)\b\s*(.+)$", re.I)
_GHERKIN_SCEN_RE = re.compile(r"^\s*Scenario(?:\s+Outline)?\s*:\s*(.+)$", re.I)
_GHERKIN_FEAT_RE = re.compile(r"^\s*Feature\s*:\s*(.+)$", re.I)


def build_workflow_graph(gherkin: list[str], chains: list[dict],
                         captured: list[dict]) -> dict:
    """Gherkin scenarios → steps, linked to chains by endpoint overlap. Falls
    back to a chain-only workflow graph when no Gherkin is available (the
    discovery-probe-only path)."""
    nodes: list[dict] = []
    edges: list[dict] = []
    sidx = 0

    for feature_text in gherkin or []:
        if not isinstance(feature_text, str):
            continue
        feature = ""
        cur_scen = None
        order = 0
        for line in feature_text.splitlines():
            mf = _GHERKIN_FEAT_RE.match(line)
            if mf:
                feature = mf.group(1).strip()
                continue
            ms = _GHERKIN_SCEN_RE.match(line)
            if ms:
                sidx += 1
                order = 0
                cur_scen = f"scenario:{sidx}"
                nodes.append({"id": cur_scen, "kind": "scenario",
                              "title": ms.group(1).strip(), "feature": feature})
                continue
            mstep = _GHERKIN_STEP_RE.match(line)
            if mstep and cur_scen:
                order += 1
                step_id = f"{cur_scen}:step:{order}"
                nodes.append({"id": step_id, "kind": "step",
                              "keyword": mstep.group(1).lower(), "text": mstep.group(2).strip()})
                edges.append({"from": cur_scen, "to": step_id, "kind": "scenario_has_step",
                              "order": order})

    # chain nodes + (optional) scenario→chain links by endpoint-token overlap
    scen_titles = {n["id"]: (n.get("title") or "").lower() for n in nodes if n["kind"] == "scenario"}
    for ch in chains or []:
        cid = ch.get("chain_id") or ""
        if not cid:
            continue
        ep_count = len(ch.get("nodes") or [])
        nodes.append({"id": f"chain:{cid}", "kind": "chain", "chain_id": cid,
                      "endpoint_count": ep_count,
                      "occurrence_count": ch.get("occurrence_count", 0)})
        chain_blob = " ".join((n.get("path_template") or "") for n in (ch.get("nodes") or [])).lower()
        for sid, title in scen_titles.items():
            toks = [t for t in re.findall(r"[a-z]{4,}", title)]
            if toks and any(t in chain_blob for t in toks):
                edges.append({"from": sid, "to": f"chain:{cid}", "kind": "scenario_exercises_chain"})

    return {"nodes": nodes, "edges": edges}


def build_data_flow_graph(chains: list[dict]) -> dict:
    """Project-wide variable lineage (which endpoint produces a var, which
    consume it) rolled up across all chains."""
    var_meta: dict[str, dict] = {}
    ep_nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for ch in chains or []:
        nodes = ch.get("nodes") or []
        for n in nodes:
            eid = _ep_id(n.get("method"), n.get("path_template") or n.get("path"))
            ep_nodes.setdefault(eid, {"id": eid, "kind": "endpoint",
                                      "path": n.get("path_template") or n.get("path")})
            for var, jsonpath in (n.get("provides") or {}).items():
                m = var_meta.setdefault(var, {"providers": set(), "consumers": set()})
                m["providers"].add(eid)
                edges.append({"from": f"var:{var}", "to": eid, "kind": "flows_from",
                              "jsonpath": jsonpath})
            for var in (n.get("consumes") or {}):
                m = var_meta.setdefault(var, {"providers": set(), "consumers": set()})
                m["consumers"].add(eid)
                edges.append({"from": f"var:{var}", "to": eid, "kind": "flows_to"})

    var_nodes = []
    for var, m in var_meta.items():
        var_nodes.append({"id": f"var:{var}", "kind": "env_var", "name": var,
                          "provider_count": len(m["providers"]),
                          "consumer_count": len(m["consumers"])})
        # lineage edges provider→consumer
        for p in m["providers"]:
            for c in m["consumers"]:
                if p != c:
                    edges.append({"from": p, "to": c, "kind": "lineage", "var": var})

    return {"nodes": list(ep_nodes.values()) + var_nodes, "edges": edges, "_var_meta_keys": list(var_meta.keys())}


# ── summary + coverage gaps ──────────────────────────────────────────────────

def build_summary(graphs: dict[str, dict], inputs: dict, *,
                  captured: list[dict], fe_routes: list[dict]) -> dict:
    api = graphs.get("api_graph", {})
    dfg = graphs.get("data_flow_graph", {})
    auth = graphs.get("auth_graph", {})

    # gap: source FE api_calls never seen in captured runtime surface
    captured_paths = {(e.get("path") or "") for e in captured or []}
    without_runtime = []
    for fr in fe_routes or []:
        for call in (fr.get("api_calls") or []):
            if call and not any(p and (p == call or p.endswith(call) or call.endswith(p)) for p in captured_paths):
                without_runtime.append(call)
    # gap: non-rest endpoints
    non_rest = [n["id"] for n in api.get("nodes", []) if n.get("protocol") != "rest"]
    # gap: vars provided-but-never-consumed / consumed-but-never-provided
    prov, cons = set(), set()
    for e in dfg.get("edges", []):
        if e.get("kind") == "flows_from":
            prov.add(e["from"])
        elif e.get("kind") == "flows_to":
            cons.add(e["from"])
    provided_never_consumed = sorted(prov - cons)
    consumed_never_provided = sorted(cons - prov)
    # gap: endpoints with no auth attribution (auth_required not captured)
    auth_eps = {n["id"] for n in auth.get("nodes", []) if n.get("kind") == "endpoint"}
    endpoints_without_auth_info = [n["id"] for n in api.get("nodes", []) if n["id"] not in auth_eps]
    # gap: how many cross-request data dependencies were inferred (0 ⇒ the
    # captured chains carry no provides/consumes — DR-1 found no dataflow, so
    # chained-request grounding is unavailable for this SUT).
    dep = graphs.get("dependency_graph", {})
    dataflow_dependencies = sum(1 for e in dep.get("edges", []) if e.get("kind") == "depends_on")

    return {
        "graphs": {name: {"nodes": len(g.get("nodes", [])), "edges": len(g.get("edges", []))}
                   for name, g in graphs.items()},
        "protocol_counts": api.get("protocol_counts", {}),
        "coverage_gaps": {
            "endpoints_without_runtime_evidence": sorted(set(without_runtime))[:50],
            "non_rest_endpoints": non_rest[:50],
            "vars_provided_never_consumed": provided_never_consumed[:50],
            "vars_consumed_never_provided": consumed_never_provided[:50],
            "endpoints_without_auth_info_count": len(endpoints_without_auth_info),
            "dataflow_dependencies_inferred": dataflow_dependencies,
        },
        "inputs": inputs,
    }


# ── persistence ──────────────────────────────────────────────────────────────

def _persist(project_id: str, name: str, payload: dict) -> None:
    try:
        d = _OUT_DIR / project_id
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(json.dumps(payload, indent=2, default=str))
    except Exception as exc:  # pragma: no cover
        log.debug("arch_discovery: persist %s/%s failed: %s", project_id, name, exc)


def load_graph(project_id: str, name: str) -> dict:
    try:
        p = _OUT_DIR / project_id / f"{name}.json"
        if p.is_file():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


def load_summary(project_id: str) -> dict:
    return load_graph(project_id, "discovery_summary")


# ── public entrypoint ────────────────────────────────────────────────────────

async def run(*, project: dict, project_id: str, neo4j_driver: Any = None,
              gherkin: list[str] | None = None) -> dict:
    """Aggregate existing discovery → 6 graphs + summary → persist → optional
    Neo4j. Never raises (best-effort); returns the summary dict."""
    from . import api_discovery

    try:
        captured = api_discovery._load_captured_endpoints(project_id) or []
    except Exception:
        captured = []
    try:
        chains = api_discovery.load_chains(project_id) or []
    except Exception:
        chains = []

    fe_routes: list[dict] = []
    try:
        from . import github_context
        fe_routes = await github_context.extract_frontend_routes(project) or []
    except Exception as exc:
        log.info("arch_discovery: frontend-route extract skipped (%s)", exc)

    repos = []
    try:
        repos = ((project.get("integrations") or {}).get("repositories")) or []
    except Exception:
        pass

    # Resolve the SUT base URL once (used by Phase 4 OpenAPI + Phase 5 GraphQL).
    import os as _os
    base_url = None
    try:
        envs = (project.get("environments") or {})
        env = envs.get("staging") or envs.get("local") or next(iter(envs.values()), {})
        if hasattr(env, "model_dump"):
            env = env.model_dump()
        base_url = (env or {}).get("api_base_url") or (env or {}).get("base_url") \
            or ((project.get("integrations") or {}).get("base_url"))
    except Exception:
        pass

    # Phase 4 — best-effort OpenAPI fetch for param-constraint + auth-scheme
    # enrichment. Network, short-timeout, fail-open. Killswitch: ARTA_ARCH_OPENAPI_DISABLE.
    openapi_endpoints: list[dict] = []
    if base_url and _os.environ.get("ARTA_ARCH_OPENAPI_DISABLE", "").lower() not in ("1", "true", "yes"):
        try:
            openapi_endpoints = await api_discovery._try_openapi(base_url) or []
        except Exception as exc:
            log.info("arch_discovery: OpenAPI fetch skipped (%s)", exc)

    token_chains = {}
    claim_map = {}
    try:
        from .automation_engineer import AutomationEngineerAgent as _AE
        token_chains = dict(getattr(_AE, "_R156_B_1_TOKEN_CHAINS", {}) or {})
    except Exception:
        pass
    try:
        claim_map = dict(getattr(api_discovery, "_R96_1_DEFAULT_CLAIM_MAP", {}) or {})
    except Exception:
        pass

    graphs = {
        "architecture_map": build_architecture_map(captured, fe_routes, repos),
        "api_graph": build_api_graph(captured, openapi_endpoints=openapi_endpoints),
        "dependency_graph": build_dependency_graph(chains),
        "auth_graph": build_auth_graph(captured, token_chains, claim_map,
                                       openapi_endpoints=openapi_endpoints),
        "workflow_graph": build_workflow_graph(gherkin or [], chains, captured),
        "data_flow_graph": build_data_flow_graph(chains),
    }

    # Phase 5 — best-effort GraphQL introspection when a graphql endpoint is
    # discovered: merge query/mutation/subscription operations into the api
    # graph. (gRPC .proto + event-channel parsers exist in protocol_discovery
    # and are unit-tested; live ingestion of those needs SUT source via the
    # MCP-owning agent, so they're not fetched in this disk-only aggregator.)
    _protocol_warnings: list[dict] = []
    if base_url and _os.environ.get("ARTA_ARCH_PROTOCOL_DISABLE", "").lower() not in ("1", "true", "yes"):
        try:
            from . import protocol_discovery as _proto
            _api = graphs["api_graph"]
            gql_eps = [n for n in _api.get("nodes", [])
                       if n.get("protocol") == "graphql" or "graphql" in (n.get("path") or "").lower()]
            if gql_eps:
                gpath = gql_eps[0].get("path") or "/graphql"
                gql = await _proto.graphql_introspect(base_url.rstrip("/") + gpath)
                # C3 — a GraphQL endpoint IS present; if introspection failed, surface a
                # TRUTHFUL signal rather than silently reporting 'no non-REST'.
                if isinstance(gql, dict) and gql.get("_error"):
                    _protocol_warnings.append({
                        "protocol": "graphql", "endpoint": gpath,
                        "signal": "graphql_introspection_failed",
                        "detail": str(gql.get("_error"))})
                    log.warning(
                        "arch_discovery C3: GraphQL endpoint %s present but introspection "
                        "FAILED (%s) — reporting graphql_introspection_failed, NOT "
                        "'no non-REST'", gpath, gql.get("_error"))
                else:
                    pnodes = _proto.build_protocol_nodes(
                        graphql=gql, graphql_endpoint=base_url.rstrip("/") + gpath)
                    if pnodes["nodes"]:
                        _api["nodes"].extend(pnodes["nodes"])
                        pcounts = _api.setdefault("protocol_counts", {})
                        for k, v in pnodes["protocol_counts"].items():
                            pcounts[k] = pcounts.get(k, 0) + v
        except Exception as exc:
            # C3 — do NOT swallow silently: record the failure as a truthful signal.
            _protocol_warnings.append({"protocol": "graphql",
                                       "signal": "graphql_discovery_error",
                                       "detail": f"{type(exc).__name__}: {exc}"})
            log.warning("arch_discovery C3: protocol (graphql) discovery ERROR (%s) — "
                        "recorded as a protocol_warning, not silently dropped", exc)

    # R330 P3 — wire the previously-ORPHANED gRPC .proto + event-channel parsers
    # (finished + unit-tested in protocol_discovery, zero production callers)
    # using the source-fetch pattern the Module-Federation parser proved
    # (github_context REST). Capped fetches; failures become truthful
    # protocol_warnings, never silence. Same killswitch as GraphQL discovery.
    if _os.environ.get("ARTA_ARCH_PROTOCOL_DISABLE", "").lower() not in ("1", "true", "yes"):
        try:
            from . import protocol_discovery as _proto_p3
            from . import github_context as _ghc
            import json as _json_p3

            async def _p3_fetch_hits(query: str, cap: int) -> list[str]:
                texts: list[str] = []
                raw = await _ghc.search_code(project, query)
                items = (_json_p3.loads(raw).get("items") or []) if raw else []
                for it in items[:cap]:
                    repo_name = (((it.get("repository") or {}).get("full_name") or "")
                                 .split("/")[-1])
                    txt = await _ghc.get_file(project, repo_name, it.get("path") or "")
                    if txt:
                        texts.append(txt)
                return texts

            _proto_agg = {"services": [], "messages": []}
            # Prefer a DETERMINISTIC tree-walk of the configured repos — GitHub
            # code-search does not index every private repo, so `search_code`
            # returns 0 `.proto` for a private SUT auth service (the gRPC blind
            # spot). Fall back to the org-wide code search when no repos are
            # configured / the tree-walk finds nothing.
            _proto_files = await _ghc.fetch_files_by_extension(project, ".proto", cap=10)
            if not _proto_files:
                _proto_files = [{"path": f"hit{_i}.proto", "text": _t}
                                for _i, _t in enumerate(await _p3_fetch_hits("rpc extension:proto", 5))]
            for _pf in _proto_files:
                _pp = _proto_p3.parse_proto(_pf.get("text") or "")
                _proto_agg["services"].extend(_pp.get("services") or [])
                _proto_agg["messages"].extend(_pp.get("messages") or [])
            # Persist a durable, gen-ready gRPC SURFACE (services + methods +
            # message names + read-side flags + stub imports) so generation can
            # be grounded in the SUT's REAL rpc surface (understanding → gen).
            if _proto_files and project_id:
                try:
                    from .grpc_stub_gen import build_grpc_surface, persist_grpc_surface
                    persist_grpc_surface(project_id, build_grpc_surface(_proto_files))
                except Exception as _gs_exc:
                    log.debug("grpc surface persist skipped for %s: %s", project_id, _gs_exc)
            _channels: list[dict] = []
            for _q in ("KafkaListener", "RabbitTemplate", "nats.connect"):
                for _txt in await _p3_fetch_hits(_q, 3):
                    _channels.extend(_proto_p3.detect_event_channels(_txt) or [])
            _uniq_ch, _seen_ch = [], set()
            for _c in _channels:
                _k = (_c.get("system"), _c.get("role"), _c.get("channel"))
                if _k not in _seen_ch:
                    _seen_ch.add(_k)
                    _uniq_ch.append(_c)
            if _proto_agg["services"] or _uniq_ch:
                _pn2 = _proto_p3.build_protocol_nodes(
                    proto=_proto_agg if _proto_agg["services"] else None,
                    event_channels=_uniq_ch or None)
                if _pn2["nodes"]:
                    _api2 = graphs["api_graph"]
                    _api2["nodes"].extend(_pn2["nodes"])
                    _pc2 = _api2.setdefault("protocol_counts", {})
                    for _k2, _v2 in _pn2["protocol_counts"].items():
                        _pc2[_k2] = _pc2.get(_k2, 0) + _v2
                    log.info("arch_discovery R330 P3: wired %d grpc/event node(s) "
                             "from SUT source", len(_pn2["nodes"]))
        except Exception as exc:
            _protocol_warnings.append({"protocol": "grpc/event",
                                       "signal": "source_protocol_discovery_error",
                                       "detail": f"{type(exc).__name__}: {exc}"})
            log.warning("arch_discovery R330 P3: proto/MQ source discovery ERROR (%s) — "
                        "recorded as a protocol_warning, not silently dropped", exc)

    inputs = {"captured_endpoints": len(captured), "chains": len(chains),
              "frontend_routes": len(fe_routes), "repos": len(repos),
              "openapi_endpoints": len(openapi_endpoints)}
    summary = build_summary(graphs, inputs, captured=captured, fe_routes=fe_routes)
    # C3 — carry any truthful protocol-discovery warnings (e.g. a GraphQL endpoint that
    # errored on introspection) so a non-REST surface is never SILENTLY reported empty.
    if _protocol_warnings:
        summary["protocol_warnings"] = _protocol_warnings

    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat()
    for name, g in graphs.items():
        g.update({"project_id": project_id, "graph": name, "generated_at": stamp})
        _persist(project_id, name, g)
    summary.update({"project_id": project_id, "generated_at": stamp, "neo4j_written": False})

    if neo4j_driver is not None:
        try:
            from ..graph import writer as _writer
            await _writer.upsert_architecture_graphs(neo4j_driver, project_id=project_id, graphs=graphs)
            summary["neo4j_written"] = True
        except Exception as exc:
            log.info("arch_discovery: neo4j write skipped (%s)", exc)

    _persist(project_id, "discovery_summary", summary)
    log.info("arch_discovery: project=%s graphs=%s gaps=%s", project_id,
             summary.get("graphs"), {k: (len(v) if isinstance(v, list) else v)
                                     for k, v in summary.get("coverage_gaps", {}).items()})
    return summary


# ── prompt serializer ────────────────────────────────────────────────────────

def summarize_for_prompt(project_id: str, *, max_chars: int = 2000) -> str:
    """Compact, deterministic SUT-topology block for injection into ATDD /
    automation prompts. Returns "" when no discovery on disk (preserves prior
    behavior — same contract as api_discovery.format_dom_catalog_for_prompt)."""
    summary = load_summary(project_id)
    api = load_graph(project_id, "api_graph")
    auth = load_graph(project_id, "auth_graph")
    dep = load_graph(project_id, "dependency_graph")
    if not summary and not api:
        return ""

    lines = ["[ARCHITECTURE DISCOVERY — SUT TOPOLOGY (grounded; do not invent beyond this)]"]
    pc = summary.get("protocol_counts") or api.get("protocol_counts") or {}
    if pc:
        lines.append("Protocols: " + ", ".join(f"{k}={v}" for k, v in sorted(pc.items())))
    # top endpoints by evidence
    eps = sorted([n for n in api.get("nodes", []) if n.get("kind") == "endpoint"],
                 key=lambda n: n.get("evidence_count", 0), reverse=True)[:25]
    if eps:
        lines.append("Real endpoints (METHOD path[ — required/constrained params]):")
        for n in eps:
            extra = ""
            # Phase 4 — surface required + constrained params so gen respects them.
            pd = n.get("params_detail") or []
            req_p = [p for p in pd if p.get("required") or p.get("enum")]
            if req_p:
                parts = []
                for p in req_p[:5]:
                    seg = p.get("name", "")
                    if p.get("enum"):
                        seg += "=" + "|".join(str(x) for x in p["enum"][:4])
                    elif p.get("required"):
                        seg += "(required)"
                    parts.append(seg)
                extra = " — params: " + ", ".join(parts)
            if n.get("request_body_required"):
                extra += " — body requires: " + ",".join(n["request_body_required"][:6])
            lines.append(f"  {n.get('method')} {n.get('path')}{extra}")
    # token chain
    toks = [n for n in auth.get("nodes", []) if n.get("kind") == "token"]
    if toks:
        chain = " → ".join(t["name"] for t in toks)
        lines.append("Auth token chain: " + chain)
    # Phase 4 — OpenAPI auth schemes (oauth2/apiKey/http-bearer/openIdConnect)
    schemes = sorted({n["name"] for n in auth.get("nodes", []) if n.get("kind") == "auth_scheme"})
    if schemes:
        lines.append("Auth schemes (OpenAPI): " + ", ".join(schemes))
    # top dependencies
    deps = [e for e in dep.get("edges", []) if e.get("kind") == "depends_on"][:10]
    if deps:
        lines.append("API dependencies (consumer needs provider via var):")
        for e in deps:
            lines.append(f"  {e['from']} depends_on {e['to']} (via {e.get('via_var')})")
    out = "\n".join(lines)
    return out[:max_chars]
