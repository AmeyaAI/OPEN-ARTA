"""R86.2a — regression tests for OpenAPI `requestBody.content.<mime>`
extraction via `get_request_content_type`.

Pre-R86.2a, Newman POST/PUT/PATCH items were emitted WITHOUT a
Content-Type header. The SUT rejected with 415. R86.2a reads the
OpenAPI spec's `requestBody.content.<media-type>` declaration per
endpoint + injects the correct Content-Type at gen time.

Selection priority (when multiple media types are declared):
  1. application/json (most common)
  2. application/vnd.api+json
  3. multipart/form-data (file uploads)
  4. application/x-www-form-urlencoded
  5. text/plain
  6. First key in content (deterministic fallback)

This priority avoids picking `text/plain` over `application/json` when
both are declared, but still handles file-upload endpoints (multipart)
correctly when JSON isn't an option.
"""
from __future__ import annotations

from src.agents.openapi_cache import get_request_content_type


# ── Standard JSON-body endpoint ───────────────────────────────────────

def test_r86_2a_application_json_returned_for_typical_post():
    op = {
        "requestBody": {
            "content": {"application/json": {"schema": {"type": "object"}}}
        },
    }
    assert get_request_content_type(op) == "application/json"


def test_r86_2a_application_json_preferred_over_text_plain():
    """When BOTH application/json and text/plain are declared, JSON wins
    (it's the modern default + safer for Newman's raw-body mode)."""
    op = {
        "requestBody": {
            "content": {
                "text/plain": {},
                "application/json": {"schema": {}},
            },
        },
    }
    assert get_request_content_type(op) == "application/json"


# ── Multipart endpoint (file upload) ─────────────────────────────────

def test_r86_2a_multipart_form_data_returned_when_only_option():
    """File-upload endpoints typically only declare multipart/form-data."""
    op = {
        "requestBody": {
            "content": {"multipart/form-data": {"schema": {}}},
        },
    }
    assert get_request_content_type(op) == "multipart/form-data"


def test_r86_2a_json_preferred_over_multipart_when_both_offered():
    """If the SUT accepts BOTH JSON and multipart, prefer JSON for our
    Newman items (raw-body mode is simpler than formdata mode)."""
    op = {
        "requestBody": {
            "content": {
                "multipart/form-data": {},
                "application/json": {},
            },
        },
    }
    assert get_request_content_type(op) == "application/json"


# ── Edge cases ───────────────────────────────────────────────────────

def test_r86_2a_none_when_op_has_no_requestBody():
    """GET endpoints don't have requestBody → return None."""
    op = {"parameters": [{"name": "id", "in": "path"}]}
    assert get_request_content_type(op) is None


def test_r86_2a_none_when_requestBody_has_no_content():
    """Defensive: some operations may have requestBody with empty
    content (rare; usually a spec bug)."""
    op = {"requestBody": {"required": False}}
    assert get_request_content_type(op) is None
    op = {"requestBody": {"required": True, "content": {}}}
    assert get_request_content_type(op) is None


def test_r86_2a_none_for_invalid_input():
    assert get_request_content_type(None) is None
    assert get_request_content_type("not a dict") is None
    assert get_request_content_type([]) is None


# ── Custom JSON variants ─────────────────────────────────────────────

def test_r86_2a_vendor_json_priority():
    """application/vnd.api+json (JSON:API spec) is preferred over text
    and form-encoded if `application/json` isn't declared."""
    op = {
        "requestBody": {
            "content": {
                "application/vnd.api+json": {},
                "text/plain": {},
            },
        },
    }
    assert get_request_content_type(op) == "application/vnd.api+json"


def test_r86_2a_form_urlencoded_returned_for_legacy_endpoints():
    """Some legacy endpoints (login, OAuth flows) only declare form-encoded."""
    op = {
        "requestBody": {
            "content": {"application/x-www-form-urlencoded": {}},
        },
    }
    assert get_request_content_type(op) == "application/x-www-form-urlencoded"


# ── Deterministic fallback ───────────────────────────────────────────

def test_r86_2a_first_key_returned_when_no_priority_match():
    """When the SUT declares a custom/exotic media type not in the
    priority list, return the first declared key (deterministic)."""
    op = {
        "requestBody": {
            "content": {
                "application/x-custom-format": {},
                "application/x-other-format": {},
            },
        },
    }
    # First key in dict insertion order
    assert get_request_content_type(op) == "application/x-custom-format"
