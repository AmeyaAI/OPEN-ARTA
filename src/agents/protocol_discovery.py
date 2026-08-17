"""Phase 5 — multi-protocol discovery (beyond REST).

The directive requires ARTA to handle SUTs that aren't exclusively REST:
GraphQL, gRPC, SSE/WebSocket, and event-driven (Kafka/RabbitMQ/MQ). R156.C
already CLASSIFIES rest/grpc/sse/websocket/graphql routes from source; this
module adds the missing SCHEMA-level discovery so generated tests can be
grounded in real operations/messages/channels:

  - `parse_graphql_introspection(data)` — query/mutation/subscription + types
  - `parse_proto(text)`               — gRPC services/methods/messages
  - `detect_event_channels(text)`     — Kafka/RabbitMQ/AMQP/NATS producers +
                                        consumers + topics/queues (closes the
                                        previously-complete absence)
  - best-effort network fetch helpers (fail-open)

All parsers are DETERMINISTIC (no LLM). Network fetches are best-effort and
return empty on failure. Results feed the Architecture Discovery api_graph
(Phase 1) as protocol-specific node kinds.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("arta.protocol_discovery")


# ── GraphQL ──────────────────────────────────────────────────────────────────

_GQL_INTROSPECTION_QUERY = (
    "{ __schema { queryType { name } mutationType { name } "
    "subscriptionType { name } types { name kind fields { name } } } }"
)


def classify_protocol(path: str, content_type: str | None = None, summary: str = "") -> str:
    """Heuristic, zero-LLM protocol classification for a captured endpoint.

    R330 P3 — moved here from architecture_discovery (its private
    `_classify_protocol`) so automation_engineer can classify backend routes
    WITHOUT importing architecture_discovery (which itself imports from
    AutomationEngineerAgent — a cycle). architecture_discovery re-imports this
    under its old name; this module is the single source of truth.

    C1 (2026-07-25, charter conformance) — the prior version misclassified the example SUT's
    real SSE endpoint `.../query-engine/event/response-stream` as REST and could not
    see SOAP/XML (.NET) or SignalR at all. Added `response-stream`/`streaming` SSE
    tokens + SOAP + SignalR branches, all with word-ish boundary guards so tokens
    like `asset`/`assetsmodule`/`github` don't false-match `sse`/`soap`/`hub`.
    SOAP requires a SOAP-SPECIFIC marker (not a bare `xml` content-type)."""
    blob = f"{path or ''} {summary or ''}".lower()
    ct = (content_type or "").lower()
    # SOAP — SOAP-specific markers only (never a bare application/xml, which REST uses)
    if ("soap+xml" in ct or "?wsdl" in blob or ".asmx" in blob
            or "soapaction" in blob or "soap:envelope" in blob or "/soap" in blob):
        return "soap"
    # SSE — event-stream mime OR a streaming path token (bounded to avoid 'asset'→'sse')
    if ("text/event-stream" in ct or "eventsource" in blob
            or "response-stream" in blob or "event-stream" in blob
            or re.search(r"(^|[/_.-])sse([/_.-]|$)", blob)
            or re.search(r"(^|[/_.-])streaming([/_.-]|$)", blob)):
        return "sse"
    # .NET SignalR — negotiate handshake / hub route / explicit 'signalr' word
    if ("/negotiate" in blob
            or re.search(r"(^|[/_.\s-])signalr([/_.\s-]|$)", blob)
            or re.search(r"(^|[/_.-])hubs?([/_.-]|$)", blob)):
        return "signalr"
    if "graphql" in blob:
        return "graphql"
    if path and (str(path).startswith("ws") or "/ws" in blob or "websocket" in blob
                 or re.search(r"(^|[/_.-])socket([/_.-]|$)", blob)):
        return "websocket"
    if "grpc" in blob or (".v1." in blob and "/grpc" in blob):
        return "grpc"
    return "rest"


def parse_graphql_introspection(data: dict) -> dict:
    """Extract operations + types from a GraphQL introspection result.

    Accepts the raw `{"data": {"__schema": ...}}` or the inner `__schema`."""
    schema = (data or {}).get("data", {}).get("__schema") or (data or {}).get("__schema") or {}
    types = schema.get("types") or []
    by_name = {t.get("name"): t for t in types if isinstance(t, dict)}

    def _ops(root_key: str) -> list[str]:
        root = (schema.get(root_key) or {}).get("name")
        t = by_name.get(root) or {}
        return sorted([f.get("name") for f in (t.get("fields") or []) if f.get("name")])

    return {
        "queries": _ops("queryType"),
        "mutations": _ops("mutationType"),
        "subscriptions": _ops("subscriptionType"),
        "types": sorted([n for n in by_name
                         if n and not n.startswith("__")])[:200],
    }


async def graphql_introspect(endpoint_url: str) -> dict:
    """GraphQL introspection POST.

    C3 (2026-07-25, charter conformance) — distinguish an INTROSPECTION FAILURE from a
    genuinely-empty schema. The prior version returned `{}` on BOTH, so a live SUT that
    DOES expose GraphQL but errors on introspection was silently reported as having no
    non-REST protocol (a fail-open on the exact pillar this discovers). Now a failure
    returns `{"_error": <reason>, "_endpoint": url}` so the caller can surface a truthful
    `graphql_introspection_failed` signal instead of 'no non-REST'. Success returns the
    parsed schema (which may legitimately be empty for a non-GraphQL 200)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            r = await client.post(endpoint_url, json={"query": _GQL_INTROSPECTION_QUERY})
            if r.status_code == 200:
                return parse_graphql_introspection(r.json())
            return {"_error": f"HTTP {r.status_code}", "_endpoint": endpoint_url}
    except Exception as exc:
        log.info("graphql introspection error (%s): %s", endpoint_url, exc)
        return {"_error": f"{type(exc).__name__}: {exc}", "_endpoint": endpoint_url}


# ── gRPC (.proto) ────────────────────────────────────────────────────────────

_PROTO_SERVICE_RE = re.compile(r"\bservice\s+(\w+)\s*\{([^}]*)\}", re.DOTALL)
_PROTO_RPC_RE = re.compile(
    r"\brpc\s+(\w+)\s*\(\s*(stream\s+)?([\w.]+)\s*\)\s*returns\s*\(\s*(stream\s+)?([\w.]+)\s*\)")
_PROTO_MSG_RE = re.compile(r"\bmessage\s+(\w+)\s*\{")


def parse_proto(text: str) -> dict:
    """Parse a `.proto` file into services/methods/messages (gRPC schema).

    NOTE (R330 P3, reviewed): automation_engineer._r156_f_parse_proto_* look
    similar but are NOT duplicates — they extract per-FIELD detail (name/type/
    tag/rule, per-direction streaming) for request-body synthesis; this parser
    is a name-level census for the architecture graph. Keep both; consumers
    differ in fidelity needs."""
    if not text:
        return {"services": [], "messages": []}
    services = []
    for sm in _PROTO_SERVICE_RE.finditer(text):
        name, body = sm.group(1), sm.group(2)
        methods = []
        for rm in _PROTO_RPC_RE.finditer(body):
            methods.append({
                "name": rm.group(1),
                "request": rm.group(3),
                "response": rm.group(5),
                "streaming": bool(rm.group(2) or rm.group(4)),
            })
        services.append({"name": name, "methods": methods})
    messages = sorted(set(_PROTO_MSG_RE.findall(text)))
    return {"services": services, "messages": messages}


# ── Event-driven (Kafka / RabbitMQ / AMQP / NATS) ────────────────────────────

# (regex, system, role) — role is producer|consumer; channel captured in group 1
_EVENT_PATTERNS = [
    # Kafka
    (re.compile(r"""@KafkaListener\([^)]*topics?\s*=\s*[{\[]?\s*['"]([^'"]+)"""), "kafka", "consumer"),
    (re.compile(r"""\.subscribe\(\s*\[?\s*['"]([^'"]+)['"]"""), "kafka", "consumer"),
    (re.compile(r"""(?:producer\.send|\.produce)\(\s*['"]([^'"]+)['"]"""), "kafka", "producer"),
    (re.compile(r"""KafkaProducer\("""), "kafka", "producer"),
    (re.compile(r"""KafkaConsumer\(\s*['"]([^'"]+)['"]"""), "kafka", "consumer"),
    # RabbitMQ / AMQP
    (re.compile(r"""basic_publish\([^)]*routing_key\s*=\s*['"]([^'"]+)"""), "rabbitmq", "producer"),
    (re.compile(r"""queue_declare\(\s*(?:queue\s*=\s*)?['"]([^'"]+)"""), "rabbitmq", "consumer"),
    (re.compile(r"""@RabbitListener\([^)]*queues?\s*=\s*['"]([^'"]+)"""), "rabbitmq", "consumer"),
    (re.compile(r"""basic_consume\([^)]*queue\s*=\s*['"]([^'"]+)"""), "rabbitmq", "consumer"),
    # NATS
    (re.compile(r"""\bnats\b[^\n]*\.(?:subscribe|publish)\(\s*['"]([^'"]+)"""), "nats", "channel"),
]
_EVENT_LIB_HINTS = re.compile(
    r"\b(kafka|confluent_kafka|aiokafka|pika|aio_pika|amqp|rabbitmq|nats|kombu|celery)\b", re.I)


def detect_event_channels(text: str) -> list[dict]:
    """Detect message-bus producers/consumers + their topics/queues from source.

    Closes the directive's flagged 'complete absence' of event-driven support.
    Returns [{system, role, channel}] (channel '' when only a producer ctor
    was matched). De-duplicated."""
    if not text or not _EVENT_LIB_HINTS.search(text):
        return []
    out: list[dict] = []
    seen: set[tuple] = set()
    for rx, system, role in _EVENT_PATTERNS:
        for m in rx.finditer(text):
            channel = m.group(1) if m.groups() else ""
            key = (system, role, channel)
            if key in seen:
                continue
            seen.add(key)
            out.append({"system": system, "role": role, "channel": channel})
    return out


# ── Auth-refresh endpoint discovery (SUT-agnostic, source-derived) ───────────

# Path segments that signal a token-refresh / re-auth route.
_REFRESH_HINTS = ("refresh", "renew", "reauth", "reissue", "token/new")
# Path-param syntaxes across frameworks: {x} (FastAPI/Express5), :x (Express),
# <x> / <int:x> (Flask/Django converters).
_PATH_PARAM_RE = re.compile(r"\{(\w+)\}|:(\w+)\b|<(?:\w+:)?(\w+)>")
# Names that denote the refresh token itself (when it appears as a path param).
_TOKEN_PARAM_NAMES = {"refresh_token", "refreshtoken", "refreshToken", "token", "rt"}


def _normalize_route_path(path: str) -> tuple[str, list[str]]:
    """Return (template, param_names) — every framework path-param syntax
    rewritten to the canonical `{name}` form so it can be substituted from the
    expired cookie's JWT claims. `:id` → `{id}`, `<int:id>` → `{id}`."""
    names: list[str] = []

    def _repl(m: "re.Match") -> str:
        name = m.group(1) or m.group(2) or m.group(3)
        names.append(name)
        return "{" + name + "}"

    template = _PATH_PARAM_RE.sub(_repl, path)
    return template, names


def derive_auth_refresh_config(
    routes: list[dict],
    identifier_names: "set[str] | list[str]",
    *,
    api_base_url: str | None = None,
) -> dict | None:
    """SUT-AGNOSTIC auth-refresh discovery — derive an `auth.refresh` config
    from route declarations ARTA extracted from the SUT's OWN source code
    (`github_context.extract_backend_routes_with_client`) instead of operator
    hand-wiring per project.

    The autonomy insight: a refresh route's path-param NAMES (e.g. a SUT's
    `/refresh/{subscription_id}/{provider}/{schema_id}/{user_id}`) are the SAME
    identifiers the expired session cookie already carries as JWT claims. So
    once the route is found in source, every placeholder resolves automatically
    from the claim map — no SUT-specific config.

    Args:
      routes: `[{method, path, file}, …]` from the source-route extractor.
      identifier_names: names ARTA can fill (JWT claim keys + env-var keys +
        `refresh_token`). Used to (a) pick the refresh route whose params are
        ALL fillable and (b) decide whether the token rides in the path.
      api_base_url: prepended as the `{api_base_url}` placeholder host.

    Returns an `auth.refresh` dict (endpoint/method/query|body/...) or None when
    no fully-resolvable refresh route is found (caller falls back gracefully).
    Deterministic — no LLM, no network.
    """
    ids = set(identifier_names or [])
    ids |= {"refresh_token"}  # always fillable from storage-state localStorage
    best: tuple | None = None
    for r in routes or []:
        path = (r.get("path") or "").strip()
        if not path.startswith("/"):
            continue
        low = path.lower()
        if not any(h in low for h in _REFRESH_HINTS):
            continue
        method = (r.get("method") or "GET").upper()
        template, names = _normalize_route_path(path)
        resolvable = [n for n in names if n in ids]
        unresolvable = [n for n in names if n not in ids]
        # Score: prefer a literal "refresh" route whose params we can ALL fill.
        score = (10 if "refresh" in low else 0) + len(resolvable) - 5 * len(unresolvable)
        cand = (score, method, template, names, unresolvable, r)
        if best is None or score > best[0]:
            best = cand
    if best is None:
        return None
    score, method, template, names, unresolvable, r = best
    # Refuse to claim a refresh route we can't fully address — an unresolved
    # `{param}` would produce a malformed URL + a misleading "refresh failed".
    if unresolvable:
        log.info(
            "protocol_discovery: refresh route %s has unresolvable path params "
            "%s (not in identifier set) — skipping source-derived config.",
            template, unresolvable,
        )
        return None
    token_in_path = any(n in _TOKEN_PARAM_NAMES for n in names)
    endpoint = ("{api_base_url}" if api_base_url is not None else "") + template
    cfg: dict = {
        "endpoint": endpoint,
        "method": method,
        "access_token_paths": ["$.token", "$.session_token", "$.access_token", "$.id_token"],
        "cookie_header": "set-cookie",
        "_derived_from": r.get("file") or r.get("path"),
        "_derived": True,
    }
    if not token_in_path:
        # Token rides in the query (GET convention) or body (POST convention).
        if method == "GET":
            cfg["query"] = {"refresh_token": "{refresh_token}"}
        else:
            cfg["body"] = {"refresh_token": "{refresh_token}"}
    return cfg


# ── graph surfacing ──────────────────────────────────────────────────────────

def build_protocol_nodes(*, graphql: dict | None = None, proto: dict | None = None,
                         event_channels: list[dict] | None = None) -> dict:
    """Turn discovered non-REST schemas into Architecture Discovery nodes/edges
    (merged into the api_graph by the run() aggregator)."""
    nodes: list[dict] = []
    edges: list[dict] = []
    counts: dict[str, int] = {}

    g = graphql or {}
    for op_kind in ("queries", "mutations", "subscriptions"):
        for name in g.get(op_kind, []):
            nodes.append({"id": f"graphql:{op_kind[:-1]}:{name}", "kind": "graphql_operation",
                          "operation": op_kind[:-1], "name": name})
            counts["graphql"] = counts.get("graphql", 0) + 1

    for svc in (proto or {}).get("services", []):
        for mth in svc.get("methods", []):
            mid = f"grpc:{svc['name']}/{mth['name']}"
            nodes.append({"id": mid, "kind": "grpc_method", "service": svc["name"],
                          "method": mth["name"], "request": mth.get("request"),
                          "response": mth.get("response"), "streaming": mth.get("streaming")})
            counts["grpc"] = counts.get("grpc", 0) + 1

    for ch in event_channels or []:
        cid = f"event:{ch['system']}:{ch.get('channel') or '?'}"
        nodes.append({"id": cid, "kind": "event_channel", "system": ch["system"],
                      "role": ch.get("role"), "channel": ch.get("channel")})
        counts["event"] = counts.get("event", 0) + 1

    return {"nodes": nodes, "edges": edges, "protocol_counts": counts}
