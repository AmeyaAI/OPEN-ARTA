"""R156.H — WebSocket test-generation helper integration tests.

Python-side verification of:
  1. `src/automation/common/websocket_helpers.ts` exists + exports
     the canonical API (`openAuthenticatedWebSocket`,
     `expectWsMessages`, types)
  2. The helper reads R156.J.3-propagated `AUTH_TOKEN` env var for
     auth injection (query param OR init-message pattern)
  3. The PLAYWRIGHT_GENERATION HARD CONSTRAINT prompt teaches the
     LLM to use the helper for WebSocket endpoints
  4. Auth-mode patterns (query / init_message / none) are documented
     so operators can pick the right pattern for their SUT

Runtime behavior (actual WebSocket open + message capture inside
`page.evaluate`) is covered by integration tests against a fixture
WS server — out of scope for this Python unit suite.

Mission contract (Pillar 1b — generate high quality test scripts):
chat + notifications + bidirectional SUTs that expose WebSocket need
a canonical client in ARTA's gen path so the LLM stops emitting raw
`new WebSocket(...)` calls that bypass auth chain + bounds.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _helper_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "src" / "automation" / "common" / "websocket_helpers.ts"
    )


# ── File presence + exports ─────────────────────────────────────────


def test_r156_h_helper_file_exists():
    """websocket_helpers.ts lives at the conventional path."""
    assert _helper_path().is_file()


def test_r156_h_exports_open_function():
    """The canonical open function is exported."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "export async function openAuthenticatedWebSocket" in content


def test_r156_h_exports_expect_helper():
    """The assertion helper is exported for operator-friendly checks."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "export async function expectWsMessages" in content


def test_r156_h_exports_ws_message_type():
    """The `WsMessage` interface is exported for operator type-safety."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "export interface WsMessage" in content


def test_r156_h_exports_options_type():
    """Options interface is exported so callers can use named options."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "export interface WsOpenOptions" in content


def test_r156_h_exports_auth_mode_union():
    """`WsAuthMode` union type is exported."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "export type WsAuthMode" in content


# ── Token chain integration ─────────────────────────────────────────


def test_r156_h_helper_reads_auth_token_env():
    """Helper sources auth token from `process.env.AUTH_TOKEN`
    (populated by R156.J.3 dispatcher + R95.1 token precedence)."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "process.env.AUTH_TOKEN" in content


def test_r156_h_helper_supports_custom_query_param():
    """Operators with SUTs using non-standard query param names (e.g.,
    `access_token` instead of `token`) can override via env var."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "ARTA_WS_AUTH_QUERY_PARAM" in content


# ── Auth-mode coverage ──────────────────────────────────────────────


def test_r156_h_supports_query_param_auth():
    """authMode='query' appends `?token=<value>` to URL."""
    content = _helper_path().read_text(encoding="utf-8")
    # Query mode appends the token via URL composition
    assert "'query'" in content
    assert "encodeURIComponent" in content
    assert "authQueryParam" in content


def test_r156_h_supports_init_message_auth():
    """authMode='init_message' sends token as first WS message after onopen."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "'init_message'" in content
    assert "initMessageTemplate" in content
    # Template variable substitution: ${token}
    assert "${token}" in content


def test_r156_h_supports_none_auth_mode():
    """authMode='none' is a valid option (cookie-based / unauthenticated)."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "'none'" in content


# ── Bounded execution (timeout + max-messages) ──────────────────────


def test_r156_h_helper_enforces_timeout():
    """Helper has a deadline check + setTimeout so the WS never hangs
    past the operator-supplied timeout."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "Date.now() + args.timeoutMs" in content
    assert "setTimeout(" in content


def test_r156_h_helper_enforces_max_messages():
    """Helper caps message collection at maxMessages to bound memory + cost."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "messages.length >= args.maxMessages" in content


def test_r156_h_helper_closes_websocket_on_finalize():
    """Helper closes the WS connection on timeout / max-messages /
    explicit close — so the SUT's open connection releases."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "ws.close()" in content


# ── Event-listener coverage ─────────────────────────────────────────


def test_r156_h_listens_for_all_ws_events():
    """Helper attaches listeners for open/message/error/close — full
    lifecycle coverage."""
    content = _helper_path().read_text(encoding="utf-8")
    assert "addEventListener('open'," in content
    assert "addEventListener('message'," in content
    assert "addEventListener('error'," in content
    assert "addEventListener('close'," in content


def test_r156_h_captures_sent_messages():
    """When operator supplies `sendMessages`, the helper records each
    sent message as `type: 'sent'` in the trace alongside received ones."""
    content = _helper_path().read_text(encoding="utf-8")
    # Sent messages get pushed to the trace with type='sent'
    assert "type: 'sent'" in content
    assert "type: 'received'" in content


# ── PLAYWRIGHT_GENERATION HARD CONSTRAINT teaches the LLM ───────────


def test_r156_h_prompt_constraint_present():
    """PLAYWRIGHT_GENERATION mentions R156.H + helper import path."""
    from src.prompts import tea_prompts
    pw_prompt = tea_prompts.PLAYWRIGHT_GENERATION
    assert "R156.H" in pw_prompt
    assert "openAuthenticatedWebSocket" in pw_prompt
    assert "../common/websocket_helpers" in pw_prompt


def test_r156_h_prompt_forbids_raw_new_websocket():
    """Prompt explicitly warns LLM NOT to emit raw `new WebSocket(...)`
    calls (bypasses R156.J auto-refresh + R154 guarantees)."""
    from src.prompts import tea_prompts
    pw_prompt = tea_prompts.PLAYWRIGHT_GENERATION
    assert "DO NOT emit raw `new WebSocket" in pw_prompt
