"""R156.J.2 — PW auth_refresh.ts shared helper integration tests.

This verifies the Python-side integration points:
  1. The canonical helper file `src/automation/common/auth_refresh.ts`
     exists and exports `refreshAuthIfExpiring`
  2. The R126.B skeleton template imports + calls the helper in beforeEach
  3. The PLAYWRIGHT_GENERATION HARD CONSTRAINT prompt mentions R156.J.2
     so the LLM gen path produces specs that honor the import + call

End-to-end TS-side behavior (JWT decode, refresh POST, env update,
LocalStorage push) is verified by integration tests in
`tests/integration/test_r156_j2_auth_refresh_ts.spec.ts` (out of scope
for this Python unit suite).

Mission contract (Pillar 2 — execute flawlessly): refreshAuthIfExpiring
closes the agent_token TTL exhaust class for PW dispatch the same way
R156.J.1 closes it for Newman.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Canonical helper file exists + exports the public API ────────────


def test_r156_j2_auth_refresh_ts_file_exists():
    """The canonical helper file lives at the conventional location."""
    helper_path = Path(__file__).resolve().parents[3] / "src" / "automation" / "common" / "auth_refresh.ts"
    assert helper_path.is_file(), (
        f"R156.J.2 helper missing at {helper_path}"
    )


def test_r156_j2_helper_exports_refresh_auth_if_expiring():
    """The TS helper exports the `refreshAuthIfExpiring` symbol."""
    helper_path = Path(__file__).resolve().parents[3] / "src" / "automation" / "common" / "auth_refresh.ts"
    content = helper_path.read_text(encoding="utf-8")
    assert "export async function refreshAuthIfExpiring" in content


def test_r156_j2_helper_reads_canonical_env_vars():
    """The helper reads AUTH_TOKEN + REFRESH_TOKEN + ARTA_REFRESH_*
    env vars per R156.J.3 dispatcher propagation contract."""
    helper_path = Path(__file__).resolve().parents[3] / "src" / "automation" / "common" / "auth_refresh.ts"
    content = helper_path.read_text(encoding="utf-8")
    assert "process.env.AUTH_TOKEN" in content
    assert "process.env.REFRESH_TOKEN" in content
    assert "process.env.ARTA_REFRESH_ENDPOINT" in content
    assert "process.env.ARTA_REFRESH_REQUEST_BODY_FIELD" in content
    assert "process.env.ARTA_REFRESH_RESPONSE_ACCESS_FIELD" in content
    assert "process.env.ARTA_REFRESH_RESPONSE_REFRESH_FIELD" in content
    assert "process.env.ARTA_REFRESH_THRESHOLD_SEC" in content


def test_r156_j2_helper_returns_outcome_with_reason_field():
    """RefreshOutcome includes optional `reason` so the operator can
    see WHY the helper skipped (no_refresh_or_login_configured,
    ttl_above_threshold, etc.) — Pillar 4 truthful signal.

    Fix 1 (login re-mint) renamed two of these: the helper now proceeds on
    EITHER a refresh-token flow or a source-discovered login flow, so
    `no_refresh_endpoint_configured` became `no_refresh_or_login_configured`
    and `no_refresh_token_available` became `no_auth_token_to_refresh`. The
    contract under test — every skip path names its reason — is unchanged.
    """
    helper_path = Path(__file__).resolve().parents[3] / "src" / "automation" / "common" / "auth_refresh.ts"
    content = helper_path.read_text(encoding="utf-8")
    assert "no_refresh_or_login_configured" in content
    assert "ttl_above_threshold" in content
    assert "no_auth_token_to_refresh" in content


def test_r156_j2_helper_decodes_jwt_exp_claim():
    """Helper has a `decodeJwtExp` function for JWT TTL inspection."""
    helper_path = Path(__file__).resolve().parents[3] / "src" / "automation" / "common" / "auth_refresh.ts"
    content = helper_path.read_text(encoding="utf-8")
    assert "function decodeJwtExp" in content
    # Base64URL → standard base64 dance is present.
    assert "replace(/-/g, '+')" in content
    assert "replace(/_/g, '/')" in content


def test_r156_j2_helper_updates_spa_localstorage_via_page_eval():
    """When `page` is supplied, the helper pushes the new token into
    the SPA's LocalStorage so in-page fetch wrappers see it."""
    helper_path = Path(__file__).resolve().parents[3] / "src" / "automation" / "common" / "auth_refresh.ts"
    content = helper_path.read_text(encoding="utf-8")
    assert "window.localStorage.setItem" in content
    assert "page.evaluate" in content


# ── R126.B skeleton integrates the helper ────────────────────────────


def test_r156_j2_skeleton_imports_refresh_helper():
    """R126.B skeleton template imports `refreshAuthIfExpiring`."""
    # Inspect source — skeleton is built inline in `_r126_b_compose_pw_skeleton`
    from src.agents import automation_engineer as ae_mod
    src = Path(ae_mod.__file__).read_text(encoding="utf-8")
    # The skeleton template f-string is what we verify; the import line
    # should appear in the template body.
    assert "from '../common/auth_refresh'" in src
    assert "refreshAuthIfExpiring" in src


def test_r156_j2_skeleton_calls_helper_in_beforeeach():
    """R126.B skeleton calls `refreshAuthIfExpiring(page, request)` in
    beforeEach so the refresh runs before EVERY test."""
    from src.agents import automation_engineer as ae_mod
    src = Path(ae_mod.__file__).read_text(encoding="utf-8")
    # The call is `await refreshAuthIfExpiring(page, request)` inside a
    # beforeEach that destructures `{ page, request }`.
    assert "await refreshAuthIfExpiring(page, request)" in src


# ── Prompt-level HARD CONSTRAINT teaches the LLM ─────────────────────


def test_r156_j2_prompt_hard_constraint_present():
    """PLAYWRIGHT_GENERATION prompt mentions R156.J.2 + the import path."""
    from src.prompts import tea_prompts
    pw_prompt = tea_prompts.PLAYWRIGHT_GENERATION
    assert "R156.J.2" in pw_prompt
    assert "refreshAuthIfExpiring" in pw_prompt
    assert "../common/auth_refresh" in pw_prompt


def test_r156_j2_prompt_warns_against_inline_refresh():
    """Prompt warns the LLM NOT to implement inline refresh logic."""
    from src.prompts import tea_prompts
    pw_prompt = tea_prompts.PLAYWRIGHT_GENERATION
    assert "DO NOT implement refresh logic inline" in pw_prompt
    assert "DO NOT remove the import" in pw_prompt
