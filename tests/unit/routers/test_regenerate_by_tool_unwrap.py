"""R79.4 — regression tests for `regenerate_by_tool`'s response unwrap.

Pre-R79.4 the per-req loop at tests.py:4291 unwrapped `generate_tests()`
with a raw `(res or {}).get("tests_generated", 0)`. When generate_tests
returned a `fastapi.JSONResponse` on the F8-A4 automation-failure path
(status_code=202 wrapping a failure body), `.get()` raised
`AttributeError: 'JSONResponse' object has no attribute 'get'`. The
loop's generic `except Exception` then reported the unwrap exception
as the per-req error, masking the real failure cause (e.g. "LLM
returned empty scripts after 3 retries"). Operators saw confusing
'JSONResponse...' errors for every failed req in the R78.4 rescue
output instead of the actual upstream Ollama/validator failure.

R79.4 reuses F10-2's `_normalize_generate_result` helper that
`_generate_all_background` already uses for the same purpose, with an
explicit branch that surfaces the normalised `status="failed"` body's
`message` field as the per-req error.

These tests target the helper directly because the full
`regenerate_by_tool` endpoint requires extensive auth + DB + project
setup; the helper-level tests cover the unwrap semantics
deterministically.
"""
from __future__ import annotations

import json

from fastapi.responses import JSONResponse

from src.api.routers.tests_helpers import _normalize_generate_result


def test_plain_dict_passes_through():
    res = {"status": "ok", "test_count": 3, "tests_generated": 3}
    out = _normalize_generate_result(res)
    assert out == res


def test_jsonresponse_with_failed_body_unwraps_to_dict():
    """The F8-A4 failure path: generate_tests returns
    JSONResponse(status_code=202, content={status: failed, ...}). The
    R78.4 rescue + R79.4 loop must see the inner status=failed and
    report it as a per-req error rather than crash on `.get()`."""
    failure_body = {
        "status": "failed",
        "message": "LLM returned empty scripts after 3 retries",
        "test_count": 0,
    }
    resp = JSONResponse(status_code=202, content=failure_body)
    out = _normalize_generate_result(resp)
    assert isinstance(out, dict)
    assert out.get("status") == "failed"
    assert "empty scripts" in (out.get("message") or "")


def test_jsonresponse_ok_path_unwraps_correctly():
    """Successful gen could also return JSONResponse in some paths.
    Unwrap must extract test_count + status normally."""
    ok_body = {"status": "ok", "test_count": 5, "tests_generated": 5}
    resp = JSONResponse(status_code=200, content=ok_body)
    out = _normalize_generate_result(resp)
    assert out.get("status") == "ok"
    assert out.get("test_count") == 5


def test_jsonresponse_empty_body_returns_empty_dict():
    """Defensive: JSONResponse with no body shouldn't crash the helper."""
    resp = JSONResponse(status_code=204, content=None)
    out = _normalize_generate_result(resp)
    assert out == {}


def test_jsonresponse_corrupted_body_returns_failure_dict():
    """If the body bytes can't be JSON-decoded, return a failure
    indicator rather than crashing the per-req loop. The loop then
    correctly categorises this as a failure with a meaningful message."""
    # Build a JSONResponse-shaped duck-type carrying invalid JSON bytes
    class _Bad:
        status_code = 500
        body = b"not valid json {"

    out = _normalize_generate_result(_Bad())
    assert isinstance(out, dict)
    assert out.get("status") == "failed"
    assert "decode" in (out.get("message") or "").lower()


def test_none_input_returns_empty_dict():
    """`generate_tests` should never return None, but defensively
    handle it so a bug there doesn't cascade into the rescue loop."""
    assert _normalize_generate_result(None) == {}


def test_regenerate_by_tool_loop_pattern_reports_failure_status():
    """End-to-end shape check of the R79.4 unwrap-and-report pattern
    used in `regenerate_by_tool`. Simulates the per-req block:
        res = _normalize_generate_result(await generate_tests(...))
        if res.get("status") == "failed": → errors.append(...)
        else: → results.append({tests_generated: ...})
    """
    # Failure path
    failure_resp = JSONResponse(
        status_code=202,
        content={"status": "failed", "message": "Ollama empty response"},
    )
    res = _normalize_generate_result(failure_resp)
    assert res.get("status") == "failed"
    if res.get("status") == "failed":
        # This is what the post-R79.4 loop does — extract the message
        # as the per-req error, NOT crash with AttributeError.
        msg = res.get("message") or "generate_tests returned failed status"
        assert "Ollama empty response" in msg

    # Success path
    ok_resp = JSONResponse(
        status_code=200,
        content={"status": "ok", "test_count": 2, "tests_generated": 2},
    )
    res = _normalize_generate_result(ok_resp)
    assert res.get("status") == "ok"
    tests_generated = res.get("tests_generated") or res.get("test_count") or 0
    assert tests_generated == 2
