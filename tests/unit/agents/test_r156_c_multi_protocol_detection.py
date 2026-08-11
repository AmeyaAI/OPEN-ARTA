"""R156.C — multi-protocol detection + audit + handoff routing tests.

Three helpers tested:
1. `_r156_c_1_classify_route_protocol(file_content, start, end)` —
   per-route classifier returning rest/grpc/sse/websocket/graphql.
2. `_r156_c_classify_routes_inplace(routes)` — backfills `protocol`
   default + always populates `gen_handoff`.
3. `_r156_c_2_audit_protocol_coverage(routes)` — per-protocol counts
   + analytics-domain non-REST routes flagged.
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent


def _route_span(content: str, marker: str) -> tuple[int, int]:
    idx = content.find(marker)
    assert idx >= 0, f"marker {marker!r} not in content"
    return idx, idx + len(marker)


# ── R156.C.1: per-route protocol classifier ───────────────────────────


def test_r156_c1_websocket_decorator_classified_as_websocket():
    """`@app.websocket("/ws/...")` → protocol=`websocket`."""
    content = '''
@app.websocket("/ws/notifications")
async def notifications(ws: WebSocket):
    await ws.accept()
'''
    start, end = _route_span(content, '"/ws/notifications"')
    proto = AutomationEngineerAgent._r156_c_1_classify_route_protocol(
        content, start, end,
    )
    assert proto == "websocket"


def test_r156_c1_sse_event_source_response_classified_as_sse():
    """Handler that returns `EventSourceResponse(...)` → sse."""
    content = '''
@router.get("/api/v1/insight/stream")
async def stream_insights():
    return EventSourceResponse(generator())
'''
    start, end = _route_span(content, '"/api/v1/insight/stream"')
    proto = AutomationEngineerAgent._r156_c_1_classify_route_protocol(
        content, start, end,
    )
    assert proto == "sse"


def test_r156_c1_sse_setheader_in_express_handler():
    """Express handler that does `res.setHeader('Content-Type',
    'text/event-stream')` → sse."""
    content = '''
router.get("/api/v1/chat/stream", async (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.write('data: hello\\n\\n');
});
'''
    start, end = _route_span(content, '"/api/v1/chat/stream"')
    proto = AutomationEngineerAgent._r156_c_1_classify_route_protocol(
        content, start, end,
    )
    assert proto == "sse"


def test_r156_c1_socket_io_handler_classified_as_websocket():
    """Express handler with `socket.on(...)` body → websocket."""
    content = '''
router.post("/api/v1/chat/connect", async (req, res) => {
  io.on('connection', (socket) => {
    socket.on('message', () => {});
  });
});
'''
    start, end = _route_span(content, '"/api/v1/chat/connect"')
    proto = AutomationEngineerAgent._r156_c_1_classify_route_protocol(
        content, start, end,
    )
    assert proto == "websocket"


def test_r156_c1_grpc_file_level_indicators_classify_as_grpc():
    """File contains `import grpc` + `add_X_Servicer_to_server` →
    routes in this file classify as gRPC (servicers may not use the
    REST decorator, but co-located REST helpers in the same file
    inherit the file's protocol context)."""
    content = '''
import grpc
from auth_pb2_grpc import add_AuthServiceServicer_to_server

@router.post("/api/internal/grpc-bridge")
async def grpc_bridge():
    return {"ok": True}
'''
    start, end = _route_span(content, '"/api/internal/grpc-bridge"')
    proto = AutomationEngineerAgent._r156_c_1_classify_route_protocol(
        content, start, end,
    )
    assert proto == "grpc"


def test_r156_c1_graphql_file_level_indicators():
    """File contains `strawberry.field` → graphql."""
    content = '''
import strawberry

@strawberry.field
def resolve_user():
    pass

@router.get("/graphql")
async def graphql_endpoint():
    pass
'''
    start, end = _route_span(content, '"/graphql"')
    proto = AutomationEngineerAgent._r156_c_1_classify_route_protocol(
        content, start, end,
    )
    assert proto == "graphql"


def test_r156_c1_default_classifies_as_rest():
    """Vanilla FastAPI/Express POST with no protocol markers → rest."""
    content = '''
@router.post("/api/v1/datasets")
async def create_dataset(payload: DatasetCreate):
    return {"id": "new"}
'''
    start, end = _route_span(content, '"/api/v1/datasets"')
    proto = AutomationEngineerAgent._r156_c_1_classify_route_protocol(
        content, start, end,
    )
    assert proto == "rest"


def test_r156_c1_sse_beats_grpc_when_both_present():
    """Handler-level SSE wins over file-level gRPC because per-route
    signals beat file-level signals in the priority order."""
    content = '''
import grpc

@router.get("/api/v1/stream")
async def stream():
    return EventSourceResponse(events())
'''
    start, end = _route_span(content, '"/api/v1/stream"')
    proto = AutomationEngineerAgent._r156_c_1_classify_route_protocol(
        content, start, end,
    )
    assert proto == "sse"


def test_r156_c1_websocket_decorator_beats_grpc_file_marker():
    """Per-route `@app.websocket(...)` wins over file-level `import grpc`."""
    content = '''
import grpc

@app.websocket("/ws/chat")
async def chat(ws):
    pass
'''
    start, end = _route_span(content, '"/ws/chat"')
    proto = AutomationEngineerAgent._r156_c_1_classify_route_protocol(
        content, start, end,
    )
    assert proto == "websocket"


# ── R156.C.3: handoff mapping ────────────────────────────────────────


def test_r156_c3_handoff_for_each_protocol():
    """Each known protocol maps to its canonical gen-pipeline name."""
    assert AutomationEngineerAgent._r156_c_3_assign_gen_handoff("rest") == "rest_default"
    assert AutomationEngineerAgent._r156_c_3_assign_gen_handoff("grpc") == "grpc_pytest"
    assert AutomationEngineerAgent._r156_c_3_assign_gen_handoff("sse") == "sse_pw"
    assert AutomationEngineerAgent._r156_c_3_assign_gen_handoff("websocket") == "websocket_pw"
    assert AutomationEngineerAgent._r156_c_3_assign_gen_handoff("graphql") == "graphql_deferred"


def test_r156_c3_unknown_protocol_falls_back_to_rest_default():
    assert AutomationEngineerAgent._r156_c_3_assign_gen_handoff("unknown_proto") == "rest_default"
    assert AutomationEngineerAgent._r156_c_3_assign_gen_handoff("") == "rest_default"


# ── R156.C wrapper: in-place classification ──────────────────────────


def test_r156_c_inplace_backfills_default_protocol_and_handoff():
    """Routes lacking `protocol` field → defaults to `rest`; `gen_handoff`
    always populated."""
    routes = [
        {"method": "POST", "path": "/api/v1/datasets"},                # no protocol field
        {"method": "GET",  "path": "/api/v1/stream", "protocol": "sse"},
    ]
    AutomationEngineerAgent._r156_c_classify_routes_inplace(routes)
    assert routes[0]["protocol"] == "rest"
    assert routes[0]["gen_handoff"] == "rest_default"
    assert routes[1]["protocol"] == "sse"
    assert routes[1]["gen_handoff"] == "sse_pw"


def test_r156_c_inplace_skips_non_dict_entries():
    routes: list = [
        None,  # type: ignore[list-item]
        {"method": "POST", "path": "/x"},
        "not a dict",  # type: ignore[list-item]
    ]
    AutomationEngineerAgent._r156_c_classify_routes_inplace(routes)
    assert routes[1]["protocol"] == "rest"
    assert routes[1]["gen_handoff"] == "rest_default"


# ── R156.C.2: audit / coverage aggregation ───────────────────────────


def test_r156_c2_counts_protocols():
    """Aggregator returns per-protocol counts."""
    routes = [
        {"method": "POST", "path": "/api/v1/datasets",  "protocol": "rest"},
        {"method": "POST", "path": "/api/v1/users",     "protocol": "rest"},
        {"method": "GET",  "path": "/api/v1/insight/stream", "protocol": "sse"},
        {"method": "POST", "path": "/api/v1/chat",      "protocol": "websocket"},
    ]
    audit = AutomationEngineerAgent._r156_c_2_audit_protocol_coverage(routes)
    counts = audit["counts"]
    assert counts["rest"] == 2
    assert counts["sse"] == 1
    assert counts["websocket"] == 1


def test_r156_c2_flags_analytics_non_rest_routes():
    """Routes matching analytics keywords (`insight`, `analytics`,
    `stream`, etc.) AND non-REST protocol → flagged."""
    routes = [
        {"method": "GET", "path": "/api/v1/insight/stream", "protocol": "sse"},
        {"method": "POST", "path": "/api/v1/chat/connect", "protocol": "websocket"},
        {"method": "POST", "path": "/api/v1/datasets", "protocol": "rest"},
        {"method": "POST", "path": "/api/v1/metric/realtime", "protocol": "sse"},
    ]
    audit = AutomationEngineerAgent._r156_c_2_audit_protocol_coverage(routes)
    flagged_paths = {r["path"] for r in audit["non_rest_analytics_routes"]}
    assert "/api/v1/insight/stream" in flagged_paths
    assert "/api/v1/chat/connect" in flagged_paths
    assert "/api/v1/metric/realtime" in flagged_paths
    # REST route NOT flagged
    assert "/api/v1/datasets" not in flagged_paths


def test_r156_c2_routes_without_protocol_default_to_rest():
    """Routes missing `protocol` field → counted as rest."""
    routes = [
        {"method": "POST", "path": "/x"},
        {"method": "GET",  "path": "/y"},
    ]
    audit = AutomationEngineerAgent._r156_c_2_audit_protocol_coverage(routes)
    assert audit["counts"]["rest"] == 2


def test_r156_c2_empty_input_returns_empty_counts():
    audit = AutomationEngineerAgent._r156_c_2_audit_protocol_coverage([])
    assert audit["counts"] == {}
    assert audit["non_rest_analytics_routes"] == []
