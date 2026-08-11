"""R156.J.1 — Newman pre-request auto-refresh script injection tests.

The renderer (`_r156_j_1_render_refresh_prerequest_script`) loads the
canonical template at `src/automation/common/newman_refresh_prerequest.js`
and substitutes the project's refresh-flow values. The injector
(`_r156_j_1_inject_refresh_prerequest`) places the rendered script in
the Newman collection's collection-level `event[]` so it fires before
every item.

Mission contract (Pillar 2 — execute flawlessly): auto-refresh closes
the agent_token TTL exhaust class that dominates long smokes (>60min)
on SUTs with ~60min token TTLs.
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent


# ── Render: happy path ──────────────────────────────────────────────


def test_r156_j1_render_substitutes_all_template_variables():
    """Renderer fills every template placeholder when refresh_flow is complete."""
    rendered = AutomationEngineerAgent._r156_j_1_render_refresh_prerequest_script({
        "endpoint": "POST /api/auth/refresh",
        "request_body_field": "refresh_token",
        "response_access_token_field": "access_token",
        "response_refresh_token_field": "refresh_token",
        "response_expires_in_field": "expires_in",
        "refresh_threshold_seconds": 120,
    })
    assert rendered is not None
    # All template placeholders must be replaced.
    assert "{{REFRESH_ENDPOINT}}" not in rendered
    assert "{{REFRESH_REQUEST_BODY_FIELD}}" not in rendered
    assert "{{REFRESH_RESPONSE_ACCESS_FIELD}}" not in rendered
    assert "{{REFRESH_RESPONSE_REFRESH_FIELD}}" not in rendered
    assert "{{REFRESH_EXPIRES_IN_FIELD}}" not in rendered
    assert "{{REFRESH_THRESHOLD_SECONDS}}" not in rendered
    # Substituted values appear in the rendered output.
    assert "/api/auth/refresh" in rendered
    assert '"refresh_token"' in rendered
    assert '"access_token"' in rendered
    assert "120" in rendered


def test_r156_j1_render_endpoint_with_method_prefix_strips_method():
    """Renderer normalises 'POST /api/auth/refresh' to '/api/auth/refresh'."""
    rendered = AutomationEngineerAgent._r156_j_1_render_refresh_prerequest_script({
        "endpoint": "POST https://example.com/auth/refresh",
        "request_body_field": "rt",
        "response_access_token_field": "at",
    })
    assert rendered is not None
    # The bare URL ends up in the template; the leading "POST " is stripped.
    assert "https://example.com/auth/refresh" in rendered
    assert "POST https://" not in rendered


def test_r156_j1_render_default_threshold_when_missing():
    """When refresh_threshold_seconds is absent, renderer uses default=60."""
    rendered = AutomationEngineerAgent._r156_j_1_render_refresh_prerequest_script({
        "endpoint": "/api/refresh",
        "request_body_field": "refresh_token",
        "response_access_token_field": "access_token",
    })
    assert rendered is not None
    # Default 60s threshold appears in the rendered template.
    assert "60" in rendered


def test_r156_j1_render_no_rotation_leaves_refresh_field_blank():
    """When response_refresh_token_field absent, the rotation block
    sees an empty field name and skips the rotation update at runtime."""
    rendered = AutomationEngineerAgent._r156_j_1_render_refresh_prerequest_script({
        "endpoint": "/api/refresh",
        "request_body_field": "refresh_token",
        "response_access_token_field": "access_token",
        # No response_refresh_token_field → "" substituted.
    })
    assert rendered is not None
    # The empty rotation field manifests as the literal "" check at runtime
    # (we verify by searching for the runtime guard text):
    assert '_R156_J_REFRESH_FIELD' in rendered


# ── Render: no-op cases ─────────────────────────────────────────────


def test_r156_j1_render_returns_none_for_empty_flow():
    """Empty/None flow → returns None (caller skips injection)."""
    assert AutomationEngineerAgent._r156_j_1_render_refresh_prerequest_script(
        {},
    ) is None
    assert AutomationEngineerAgent._r156_j_1_render_refresh_prerequest_script(
        None,  # type: ignore[arg-type]
    ) is None


def test_r156_j1_render_returns_none_when_endpoint_missing():
    """Flow without `endpoint` field is incomplete; renderer returns None."""
    assert AutomationEngineerAgent._r156_j_1_render_refresh_prerequest_script({
        "request_body_field": "rt",
        "response_access_token_field": "at",
    }) is None


# ── Inject: collection-level event placement ────────────────────────


def test_r156_j1_inject_adds_collection_event_with_marker():
    """Injection places ONE event with listen=prerequest + R156.J.1 marker."""
    collection: dict = {"info": {"name": "test"}, "item": []}
    injected = AutomationEngineerAgent._r156_j_1_inject_refresh_prerequest(
        collection,
        {
            "endpoint": "/api/refresh",
            "request_body_field": "refresh_token",
            "response_access_token_field": "access_token",
        },
    )
    assert injected is True
    events = collection["event"]
    assert isinstance(events, list)
    assert len(events) == 1
    ev = events[0]
    assert ev["listen"] == "prerequest"
    assert ev["_arta_meta"]["injected_by"] == "R156.J.1"
    # exec is a list of lines (Newman convention)
    assert isinstance(ev["script"]["exec"], list)
    assert ev["script"]["type"] == "text/javascript"


def test_r156_j1_inject_is_idempotent_replaces_existing_marker():
    """Re-injection updates the existing R156.J.1 event's script body."""
    collection: dict = {"info": {"name": "test"}}
    # First inject with threshold=60.
    AutomationEngineerAgent._r156_j_1_inject_refresh_prerequest(
        collection,
        {
            "endpoint": "/api/refresh",
            "request_body_field": "rt",
            "response_access_token_field": "at",
            "refresh_threshold_seconds": 60,
        },
    )
    # Re-inject with threshold=300.
    AutomationEngineerAgent._r156_j_1_inject_refresh_prerequest(
        collection,
        {
            "endpoint": "/api/refresh",
            "request_body_field": "rt",
            "response_access_token_field": "at",
            "refresh_threshold_seconds": 300,
        },
    )
    events = collection["event"]
    # Still ONE event (not appended).
    r156_j1_events = [
        e for e in events
        if isinstance(e, dict)
        and isinstance(e.get("_arta_meta"), dict)
        and e["_arta_meta"].get("injected_by") == "R156.J.1"
    ]
    assert len(r156_j1_events) == 1
    # New threshold appears in the updated exec body.
    body = "\n".join(r156_j1_events[0]["script"]["exec"])
    assert "300" in body
    assert "60" not in body or body.count("300") >= body.count("60")


def test_r156_j1_inject_returns_false_for_empty_flow():
    """Empty flow → no injection, returns False."""
    collection: dict = {"info": {"name": "test"}, "item": []}
    assert AutomationEngineerAgent._r156_j_1_inject_refresh_prerequest(
        collection, {},
    ) is False
    # No event added.
    assert collection.get("event") in (None, [])


def test_r156_j1_inject_preserves_pre_existing_unrelated_events():
    """Pre-existing events (e.g., R143.B custom prerequest) are preserved
    when R156.J.1 injects its own marker-tagged event."""
    pre_existing = {
        "listen": "prerequest",
        "script": {"type": "text/javascript", "exec": ["console.log('legacy');"]},
    }
    collection: dict = {"info": {"name": "test"}, "event": [pre_existing]}
    injected = AutomationEngineerAgent._r156_j_1_inject_refresh_prerequest(
        collection,
        {
            "endpoint": "/api/refresh",
            "request_body_field": "rt",
            "response_access_token_field": "at",
        },
    )
    assert injected is True
    events = collection["event"]
    # 2 events total: legacy + R156.J.1
    assert len(events) == 2
    # Legacy event still in the list (untouched)
    assert pre_existing in events
