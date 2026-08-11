"""R156.G — SSE event-stream consumer helper integration tests.

Python-side verification of:
  1. `src/automation/common/sse_helpers.ts` exists + exports the
     canonical API (`subscribeToEventStream`, `expectSseEvents`, types)
  2. The helper reads R156.J.3-propagated `AUTH_TOKEN` env var for
     Authorization header (composes with R156.B token chain)
  3. The PLAYWRIGHT_GENERATION HARD CONSTRAINT prompt teaches the
     LLM to use the helper for SSE endpoints
  4. The helper's wire-format parser handles all 4 SSE field types
     (event/data/id/retry) per W3C SSE spec

Runtime behavior (actual fetch + ReadableStream parsing) is verified
by integration tests against a fixture SSE server — out of scope
for this Python unit suite.

Mission contract (Pillar 1b — generate high quality test scripts):
analytics + chat-based SUTs that expose SSE need a canonical consumer
in ARTA's gen path so the LLM stops emitting brittle
`page.waitForResponse` patterns that catch one HTTP response.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _helper_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "src" / "automation" / "common" / "sse_helpers.ts"
    )


# ── File presence + exports ─────────────────────────────────────────


def test_r156_g_helper_file_exists():
    """sse_helpers.ts lives at the conventional path."""
    assert _helper_path().is_file()


def test_r156_g_exports_subscribe_function():
    """The canonical subscribe function is exported."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "export async function subscribeToEventStream" in content


def test_r156_g_exports_expect_helper():
    """The assertion helper is exported for operator-friendly checks."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "export async function expectSseEvents" in content


def test_r156_g_exports_sse_event_type():
    """The `SseEvent` interface is exported for operator type-safety
    (and to give the LLM-emitted spec deterministic types)."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "export interface SseEvent" in content


def test_r156_g_exports_subscribe_options_type():
    """Options interface is exported so callers can use named options."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "export interface SseSubscribeOptions" in content


# ── Token chain integration ─────────────────────────────────────────


def test_r156_g_helper_reads_auth_token_env():
    """Helper sources Authorization from `process.env.AUTH_TOKEN`
    (populated by R156.J.3 dispatcher + R95.1 token precedence)."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "process.env.AUTH_TOKEN" in content


def test_r156_g_helper_supports_custom_auth_header():
    """Operators with SUTs using non-standard headers (e.g.,
    `X-Auth-Token`) can override via env var."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "ARTA_SSE_AUTH_HEADER_NAME" in content
    assert "ARTA_SSE_AUTH_HEADER_PREFIX" in content


def test_r156_g_helper_supports_post_init_payload():
    """Some SUTs require POST with an init body to bind the
    subscription (e.g., query params via JSON body). Helper supports
    method=POST + body arg."""
    content = _helper_path().read_text(encoding="utf-8")
    # The options interface declares method + body
    assert "method?: 'GET' | 'POST'" in content
    assert "body?: unknown" in content


# ── Wire-format parsing covers all 4 SSE fields ─────────────────────


def test_r156_g_parser_handles_event_field():
    """Parser extracts `event:` field per W3C SSE spec."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "line.startsWith('event:')" in content


def test_r156_g_parser_handles_data_field_multiline():
    """Parser concatenates multi-line `data:` blocks per W3C SSE spec."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "line.startsWith('data:')" in content
    # multi-line concat: dataLines.join('\n')
    assert "dataLines.join" in content


def test_r156_g_parser_handles_id_field():
    """Parser extracts `id:` field (last-event-id reconnect support)."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "line.startsWith('id:')" in content


def test_r156_g_parser_handles_retry_field():
    """Parser extracts `retry:` field (reconnect-delay hint)."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "line.startsWith('retry:')" in content


# ── Bounded execution (timeout + max-events) ────────────────────────


def test_r156_g_helper_enforces_timeout():
    """Helper has a deadline check + Promise.race against remaining
    time so it never hangs past the operator-supplied timeout."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "Date.now() < deadline" in content
    assert "Promise.race" in content


def test_r156_g_helper_enforces_max_events():
    """Helper caps event collection at maxEvents to bound memory + cost."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "events.length < args.maxEvents" in content
    assert "events.length >= args.maxEvents" in content


def test_r156_g_helper_calls_reader_cancel_on_close():
    """Helper best-effort closes the ReadableStream when timeout OR
    max-events triggers exit, so the SUT's open connection releases."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "reader.cancel()" in content


# ── PLAYWRIGHT_GENERATION HARD CONSTRAINT teaches the LLM ───────────


def test_r156_g_prompt_constraint_present():
    """PLAYWRIGHT_GENERATION mentions R156.G + helper import path."""
    from src.prompts import tea_prompts
    pw_prompt = tea_prompts.PLAYWRIGHT_GENERATION
    assert "R156.G" in pw_prompt
    assert "subscribeToEventStream" in pw_prompt
    assert "../common/sse_helpers" in pw_prompt


def test_r156_g_prompt_forbids_waitForResponse_for_sse():
    """Prompt explicitly warns LLM NOT to use page.waitForResponse for
    SSE endpoints (it only catches ONE HTTP response, not the stream)."""
    from src.prompts import tea_prompts
    pw_prompt = tea_prompts.PLAYWRIGHT_GENERATION
    assert "DO NOT use `page.waitForResponse` for SSE" in pw_prompt
