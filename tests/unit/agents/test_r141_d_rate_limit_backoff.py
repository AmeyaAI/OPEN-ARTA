"""R141.D — Anthropic rate-limit retry-with-backoff for Claude CLI cascade.

Pre-R141.D: transient 429s raised RuntimeError immediately, wasting spend
on caller-side retries that hit the same rate-limit again. Long-window
"hit your limit" path was already handled; short-window per-minute/
per-account bursts had no protection.

Post-R141.D: tenacity-style backoff with Retry-After hint parsing.
Cost-aware: honor Anthropic's Retry-After header when present, otherwise
exponential backoff with jitter ±25%. Clamped to [_R141_D_BASE_DELAY,
_R141_D_MAX_DELAY] to prevent unbounded wallclock blowup.
"""
from __future__ import annotations

from src.agents.claude_cli_client import (
    _R141DRateLimited,
    _r141_d_extract_retry_after,
    _r141_d_is_transient_rate_limit,
    _r141_d_max_attempts,
    _r141_d_max_delay,
    _r141_d_retry_disabled,
)


def test_r141_d_default_max_attempts(monkeypatch):
    monkeypatch.delenv("ARTA_R141_D_MAX_ATTEMPTS", raising=False)
    assert _r141_d_max_attempts() == 3


def test_r141_d_max_attempts_clamped(monkeypatch):
    monkeypatch.setenv("ARTA_R141_D_MAX_ATTEMPTS", "0")
    assert _r141_d_max_attempts() == 1
    monkeypatch.setenv("ARTA_R141_D_MAX_ATTEMPTS", "999")
    assert _r141_d_max_attempts() == 10


def test_r141_d_backoff_disable_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R141_D_BACKOFF_DISABLE", "1")
    assert _r141_d_retry_disabled() is True
    monkeypatch.setenv("ARTA_R141_D_BACKOFF_DISABLE", "0")
    assert _r141_d_retry_disabled() is False


def test_r141_d_extract_retry_after_present():
    """Anthropic CLI may emit `Retry-After: 12s` in stderr. Parser must
    honor the API's preferred wait."""
    assert _r141_d_extract_retry_after("Error 429: Retry-After: 12s") == 12.0
    assert _r141_d_extract_retry_after("retry-after 8") == 8.0
    assert _r141_d_extract_retry_after("Retry-After:5") == 5.0


def test_r141_d_extract_retry_after_clamped_to_max(monkeypatch):
    """Bogus large Retry-After must be clamped to MAX_DELAY ceiling."""
    monkeypatch.setenv("ARTA_R141_D_MAX_DELAY_S", "20.0")
    assert _r141_d_extract_retry_after("Retry-After: 999") == 20.0


def test_r141_d_extract_retry_after_absent():
    assert _r141_d_extract_retry_after("") is None
    assert _r141_d_extract_retry_after("Some unrelated error") is None
    assert _r141_d_extract_retry_after(None) is None  # type: ignore


def test_r141_d_transient_detector_recognizes_429():
    """Plain `429` in stderr OR `too many requests` → transient."""
    assert _r141_d_is_transient_rate_limit("HTTP 429 Too Many Requests", "") is True
    assert _r141_d_is_transient_rate_limit("rate_limit_error", "") is True
    assert _r141_d_is_transient_rate_limit("", "quota exceeded") is True


def test_r141_d_transient_detector_excludes_long_window():
    """Long-window markers ("hit your limit", "resets at") must STAY in
    the existing global-flag path, NOT trigger R141.D retry."""
    assert _r141_d_is_transient_rate_limit("You've hit your limit · resets 9pm", "") is False
    assert _r141_d_is_transient_rate_limit("resets at 8:30pm (UTC)", "") is False


def test_r141_d_transient_detector_negative_clean_output():
    assert _r141_d_is_transient_rate_limit("", "Some valid LLM output") is False
    assert _r141_d_is_transient_rate_limit("Just an error", "no rate markers") is False


def test_r141_d_rate_limited_exception_carries_retry_after():
    exc = _R141DRateLimited("test msg", retry_after_s=12.5)
    assert exc.retry_after_s == 12.5
    assert "test msg" in str(exc)
    # When no retry_after hint:
    exc_no_hint = _R141DRateLimited("other")
    assert exc_no_hint.retry_after_s is None


# ── R217 0b — job-level rate-limit governor ─────────────────────────────────
import asyncio

import pytest

from src.agents import claude_cli_client as _cli
from src.agents.claude_cli_client import (
    ClaudeCLIClient,
    _r217_governor_cooldown_s,
    _r217_governor_disabled,
)


def test_r217_max_delay_default_raised_to_60(monkeypatch):
    """R217 0b — the 30s cap was too short for the OAuth reset window."""
    monkeypatch.delenv("ARTA_R141_D_MAX_DELAY_S", raising=False)
    assert _r141_d_max_delay() == 60.0


def test_r217_governor_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R217_GOVERNOR_DISABLE", "1")
    assert _r217_governor_disabled() is True
    monkeypatch.delenv("ARTA_R217_GOVERNOR_DISABLE", raising=False)
    assert _r217_governor_disabled() is False


def test_r217_governor_cooldown_default_and_clamp(monkeypatch):
    monkeypatch.delenv("ARTA_R217_GOVERNOR_COOLDOWN_S", raising=False)
    assert _r217_governor_cooldown_s() == 60.0
    monkeypatch.setenv("ARTA_R217_GOVERNOR_COOLDOWN_S", "1")      # below floor
    assert _r217_governor_cooldown_s() == 10.0
    monkeypatch.setenv("ARTA_R217_GOVERNOR_COOLDOWN_S", "99999")  # above ceiling
    assert _r217_governor_cooldown_s() == 3600.0


def _reset_globals():
    _cli._RATE_LIMITED = False
    _cli._RATE_LIMIT_MSG = ""
    _cli._RATE_LIMIT_RESET = 0


def test_r217_reset_respects_active_window():
    """Per-req clear must NOT wipe a still-future governor window."""
    import time as _t
    try:
        _cli._RATE_LIMITED = True
        _cli._RATE_LIMIT_RESET = _t.time() + 300  # active window
        _cli._RATE_LIMIT_MSG = "active"
        # respect=True on an ACTIVE window → flag preserved
        ClaudeCLIClient.reset_rate_limit(respect_active_window=True)
        assert _cli._RATE_LIMITED is True
        assert ClaudeCLIClient.get_rate_limit_info()["limited"] is True
        # respect=False (job-start / post-wait) → always cleared
        ClaudeCLIClient.reset_rate_limit(respect_active_window=False)
        assert _cli._RATE_LIMITED is False
    finally:
        _reset_globals()


def test_r217_reset_respect_clears_expired_window():
    """respect=True on an EXPIRED window still clears (no stale block)."""
    import time as _t
    try:
        _cli._RATE_LIMITED = True
        _cli._RATE_LIMIT_RESET = _t.time() - 5  # already elapsed
        ClaudeCLIClient.reset_rate_limit(respect_active_window=True)
        assert _cli._RATE_LIMITED is False
    finally:
        _reset_globals()


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes, rc: int):
        self._stdout, self._stderr, self.returncode = stdout, stderr, rc

    async def communicate(self, input=None):  # noqa: A002
        return self._stdout, self._stderr

    def kill(self):
        pass


@pytest.mark.asyncio
async def test_r217_transient_exhaustion_stamps_governor_flag(monkeypatch):
    """KEYSTONE: when transient 429 backoff EXHAUSTS, the governor flag is
    stamped so the job-level worker pauses instead of churning the next req."""
    monkeypatch.setenv("ARTA_R141_D_MAX_ATTEMPTS", "1")   # exhaust immediately
    monkeypatch.delenv("ARTA_R217_GOVERNOR_DISABLE", raising=False)
    monkeypatch.setenv("ARTA_R217_GOVERNOR_COOLDOWN_S", "120")

    async def _fake_exec(*a, **k):
        return _FakeProc(b"", b"HTTP 429 too many requests", 1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    _reset_globals()
    client = ClaudeCLIClient(cli_path="claude")
    try:
        with pytest.raises(RuntimeError, match="rate-limited after"):
            await client.create(model="claude-haiku-4-5-20251001", max_tokens=64,
                                 messages=[{"role": "user", "content": "hi"}])
        info = ClaudeCLIClient.get_rate_limit_info()
        assert info["limited"] is True
        assert info["reset"] > 0
    finally:
        _reset_globals()


@pytest.mark.asyncio
async def test_r217_governor_disabled_does_not_stamp(monkeypatch):
    """Killswitch: exhaustion re-raises WITHOUT stamping the flag (pre-R217)."""
    monkeypatch.setenv("ARTA_R141_D_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("ARTA_R217_GOVERNOR_DISABLE", "1")

    async def _fake_exec(*a, **k):
        return _FakeProc(b"", b"HTTP 429 too many requests", 1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    _reset_globals()
    client = ClaudeCLIClient(cli_path="claude")
    try:
        with pytest.raises(RuntimeError, match="rate-limited after"):
            await client.create(model="claude-haiku-4-5-20251001", max_tokens=64,
                                 messages=[{"role": "user", "content": "hi"}])
        assert ClaudeCLIClient.get_rate_limit_info()["limited"] is False
    finally:
        _reset_globals()
