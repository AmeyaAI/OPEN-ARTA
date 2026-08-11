"""
Claude Code CLI Client — invokes `claude --print` as a subprocess.

Claude Code manages OAuth token refresh automatically. By calling the CLI
instead of making direct API calls, we get auto-refreshing auth without
managing token lifecycle ourselves.

Uses session persistence per project: the first call creates a named session,
subsequent calls for the same project resume it via `--continue`. This lets
Claude reuse conversation context (requirements, codebase knowledge, prior
generated tests) across calls — reducing tokens and cost significantly.

Provides a `.messages.create()` interface compatible with the Anthropic SDK
so it can be used as a drop-in replacement for `AsyncAnthropic`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re as _re_r141
from pathlib import Path
from typing import Any

log = logging.getLogger("arta.claude_cli")

# Track active sessions per project: {project_id: session_call_count}
_PROJECT_SESSIONS: dict[str, int] = {}

# Rate limit flag — when set, skip all LLM calls until reset time
_RATE_LIMITED: bool = False
_RATE_LIMIT_MSG: str = ""
_RATE_LIMIT_RESET: float = 0  # UTC timestamp when rate limit resets (parsed from error message)


# ── R161 — claude_code CLI concurrency limiter ───────────────────────────────
# The single CLI session uses per-project `--continue` (see _PROJECT_SESSIONS)
# and corrupts under concurrency: the in-process regen_consumer + interactive
# test gen + per-tool/per-subreq asyncio.gather all hit the one CLI session at
# once → short "unexpected response" replies → the CLAUDE circuit breaker trips
# gen during regen-consumer churn). A global limiter bounds concurrent CLI
# invocations. Default 1 (fully serialize the shared session); raise via
# ARTA_CLAUDE_CLI_MAX_CONCURRENCY only if the deployment's CLI tolerates it.
# Net: fewer circuit trips + fewer wasted retries (reliability + cost win), at
# the price of serial CLI throughput (which the single session requires anyway).
import functools as _functools

_CLI_SEM: "asyncio.Semaphore | None" = None
_CLI_SEM_LOOP = None


def _cli_max_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("ARTA_CLAUDE_CLI_MAX_CONCURRENCY", "1") or "1"))
    except (TypeError, ValueError):
        return 1


def _cli_semaphore() -> "asyncio.Semaphore":
    """Lazily build (and rebind on event-loop change) the global CLI semaphore."""
    global _CLI_SEM, _CLI_SEM_LOOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _CLI_SEM is None or _CLI_SEM_LOOP is not loop:
        _CLI_SEM = asyncio.Semaphore(_cli_max_concurrency())
        _CLI_SEM_LOOP = loop
    return _CLI_SEM


def _cli_serialized(fn):
    """Wrap an async CLI method so concurrent calls are bounded by the global
    semaphore (R161) — prevents `--continue` session corruption + circuit trips."""
    @_functools.wraps(fn)
    async def _wrapper(*args, **kwargs):
        async with _cli_semaphore():
            return await fn(*args, **kwargs)
    return _wrapper


# R141.A — configurable --max-turns via env. Default 2 (Plan-agent
# critique point 3: 3 triples per-cluster wallclock and may convert
# turn-exhaust into TimeoutError). Operator opt-in for higher values.
# Cost ceiling: clamped to [1, 10] to prevent runaway spend.
def _r141_a_max_turns_default() -> int:
    # R215 Item 1 — default 2→1. ARTA's CLI calls are single-shot structured
    # output (`--print` risk/ATDD/script JSON); a 2nd turn is pure latency/cost
    # (the CLI waits to self-correct) and was a contributor to the 209s
    # risk-scoring calls that tripped the timeout. Single-shot is the
    # mission-efficient default; raise via ARTA_CLAUDE_CLI_MAX_TURNS for the rare
    # call that genuinely needs multi-turn. Clamped [1,10].
    try:
        raw = int(os.environ.get("ARTA_CLAUDE_CLI_MAX_TURNS", "1"))
        return max(1, min(10, raw))
    except (TypeError, ValueError):
        return 1


# R141.A.0 — diagnostic helper: count how many turns the CLI actually
# used in its output. Anthropic CLI emits one `--- Turn N ---` marker
# per turn when --max-turns > 1. For --max-turns 1 the marker is absent
# so a clean exit returns 1 (single turn). Generic counter.
_R141_A_0_TURN_RE = _re_r141.compile(r"^---\s*Turn\s+\d+\s*---", _re_r141.MULTILINE | _re_r141.IGNORECASE)


def _r141_a_0_count_turns_in_output(stdout: str) -> int:
    """R141.A.0 — count Turn markers in CLI output for diagnostic logging.

    Returns 1 when no Turn markers found (single-turn execution OR turn
    markers not emitted at --max-turns 1). Returns N when N markers
    present (each call to Claude inside the multi-turn loop emits one).
    """
    if not stdout:
        return 0
    matches = _R141_A_0_TURN_RE.findall(stdout)
    return max(1, len(matches)) if matches else 1


# R141.D — Anthropic rate-limit retry-with-backoff. Tenacity wraps
# the subprocess call; transient 429s raise _R141DRateLimited which
# tenacity catches + backs off (honoring Retry-After header when
# present). Distinct from the existing LONG-window rate-limit at line
# 244-250 (hours-scale; sets process-global skip flag) — R141.D handles
# TRANSIENT bursts (seconds-scale per-account/per-tier limits) that
# previously raised RuntimeError immediately + wasted spend on caller-
# side retries.
_R141_D_RETRY_AFTER_RE = _re_r141.compile(r"[Rr]etry-?[Aa]fter[:\s]+(\d+)")


def _r141_d_retry_disabled() -> bool:
    return os.environ.get("ARTA_R141_D_BACKOFF_DISABLE") == "1"


def _r141_d_max_attempts() -> int:
    try:
        raw = int(os.environ.get("ARTA_R141_D_MAX_ATTEMPTS", "3"))
        return max(1, min(10, raw))
    except (TypeError, ValueError):
        return 3


def _r141_d_base_delay() -> float:
    try:
        return max(0.1, min(60.0, float(os.environ.get("ARTA_R141_D_BASE_DELAY_S", "4.0"))))
    except (TypeError, ValueError):
        return 4.0


def _r141_d_max_delay() -> float:
    try:
        # R217 0b — default 30→60. The 30s cap was too short for the OAuth
        # account's real reset window (bulk-gen saw 110× transient 429s that
        # outlived a 30s backoff). Raising the default gives a Retry-After
        # hint room to be honored; env still wins (ceiling 300s).
        return max(1.0, min(300.0, float(os.environ.get("ARTA_R141_D_MAX_DELAY_S", "60.0"))))
    except (TypeError, ValueError):
        return 60.0


# ── R217 0b — job-level rate-limit GOVERNOR ──────────────────────────────────
# Root cause of the bulk-gen collapse: when R141.D transient-429 backoff
# EXHAUSTS (line ~583), it re-raises but NEVER sets the global _RATE_LIMITED
# flag — so the job-level governor in the generate-all worker (tests.py ~5842,
# which already pauses on get_rate_limit_info().limited) only ever fired for the
# LONG-window "hit your limit" class. The 110× short-window 429s just failed
# each req and the NEXT req fired straight back into the limit. R217 0b connects
# transient-exhaustion to the governor: on exhaust, stamp _RATE_LIMITED with a
# real cooldown window (Retry-After hint when present, else a configured floor)
# so the worker PAUSES the whole job to the reset window instead of churning.
# Killswitch ARTA_R217_GOVERNOR_DISABLE=1 → exhaust just re-raises (pre-R217).
def _r217_governor_disabled() -> bool:
    return os.environ.get("ARTA_R217_GOVERNOR_DISABLE") == "1"


def _r217_governor_cooldown_s() -> float:
    """Cooldown window stamped onto _RATE_LIMITED when transient 429s exhaust.

    Used only when the CLI gave no Retry-After hint. Long enough to let the
    OAuth per-minute/per-tier budget recover (the burst that caused exhaustion);
    env-tunable. Clamped to a sane [10s, 1h] band.
    """
    try:
        return max(10.0, min(3600.0, float(os.environ.get("ARTA_R217_GOVERNOR_COOLDOWN_S", "60.0"))))
    except (TypeError, ValueError):
        return 60.0


def _r141_d_extract_retry_after(stderr: str) -> float | None:
    """R141.D — parse Anthropic Retry-After hint from CLI stderr.

    Cost-aware: honor the API's preferred wait instead of guessing
    via exponential backoff. Clamped to MAX_DELAY ceiling so a
    bogus large value can't blow wallclock budget.
    """
    if not stderr:
        return None
    m = _R141_D_RETRY_AFTER_RE.search(stderr)
    if not m:
        return None
    try:
        return min(float(m.group(1)), _r141_d_max_delay())
    except (TypeError, ValueError):
        return None


class _R141DRateLimited(RuntimeError):
    """R141.D — raised when subprocess output indicates a TRANSIENT rate
    limit. Carries optional Retry-After hint from Anthropic for adaptive
    backoff. Distinct from the LONG-window rate-limit detection which
    sets _RATE_LIMITED process-global flag.
    """

    def __init__(self, msg: str, retry_after_s: float | None = None):
        super().__init__(msg)
        self.retry_after_s = retry_after_s


# R141.D — transient (per-minute / per-second burst) markers, distinct
# from the LONG-window "hit your limit" + "resets at <time>" markers
# already handled at line 244-250. The long-window markers stay on the
# existing global-flag path. R141.D handles the SHORT-window 429s that
# previously raised RuntimeError immediately.
_R141_D_TRANSIENT_MARKERS = (
    "429",                          # HTTP status code in stderr
    "too many requests",
    "rate_limit_error",             # Anthropic error type
    "quota exceeded",
    "concurrent request limit",
)


def _r141_d_is_transient_rate_limit(stderr: str, stdout_text: str) -> bool:
    """Detect transient (short-window) rate-limit vs long-window.

    Long-window markers ("hit your limit", "resets at") are explicitly
    EXCLUDED so they continue to flow through the existing global-flag
    path (sets _RATE_LIMITED, skips all subsequent calls until reset
    time). Transient markers are the new R141.D retry surface.
    """
    blob = ((stderr or "") + " " + (stdout_text or "")).lower()
    # Long-window markers — let existing global-flag path handle these
    if "hit your limit" in blob or "resets " in blob:
        return False
    return any(m in blob for m in _R141_D_TRANSIENT_MARKERS)


def _parse_rate_limit_reset(error_text: str) -> float | None:
    """Parse the reset time from Claude's rate limit error message.

    Examples:
      "You've hit your limit · resets 9pm (UTC)"
      "You've hit your limit · resets 8:30pm (Asia/Calcutta)"

    Returns UTC timestamp, or None if parsing fails.
    """
    import re as _re
    import time as _time
    from datetime import datetime, timezone, timedelta

    # Match: resets <time> (<timezone>)
    match = _re.search(r"resets\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*\(([^)]+)\)", error_text, _re.IGNORECASE)
    if not match:
        return None

    time_str = match.group(1).strip()  # e.g., "9pm" or "8:30pm"
    tz_str = match.group(2).strip()    # e.g., "UTC" or "Asia/Calcutta"

    try:
        # Parse time
        if ":" in time_str:
            parsed = datetime.strptime(time_str, "%I:%M%p")
        else:
            parsed = datetime.strptime(time_str, "%I%p")

        # Build today's datetime with that time
        now_utc = datetime.now(timezone.utc)

        if tz_str == "UTC":
            reset_dt = now_utc.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        else:
            # For non-UTC timezones, try pytz or estimate offset
            try:
                import pytz
                tz = pytz.timezone(tz_str)
                now_local = datetime.now(tz)
                reset_local = now_local.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
                reset_dt = reset_local.astimezone(timezone.utc)
            except Exception:
                # Fallback: assume the time is in UTC
                reset_dt = now_utc.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)

        # If reset time is in the past, it's tomorrow
        if reset_dt < now_utc:
            reset_dt += timedelta(days=1)

        return reset_dt.timestamp()
    except Exception:
        return None


class ClaudeCLIClient:
    """Invokes Claude Code CLI (claude --print) for LLM calls.

    Drop-in replacement for AsyncAnthropic — uses host's Claude Code
    with auto-refreshing OAuth tokens. Maintains one session per project
    for token optimization.

    R141.E AUDIT FINDING (cost+performance optimization gap):
      R128.B LLM cache wrapping lives in LiteLLMClientAdapter
      (llm_client.py:244-335). It is NOT applied to this ClaudeCLIClient
      subprocess path. When project's LLM provider is `claude_code` —
      e.g., the R141 defect classifier cascade case observed in R135
      Iter 0+1 — each cluster classification call pays full LLM cost
      on repeat regens, even when an identical (model, system, prompt,
      max_tokens) tuple was processed in a prior iter.
      Cross-iter overlapping failure signatures are common (same SUT
      bugs recur across iters), so the cost lever here is material.
      Follow-on (R141.E.1, operator-confirmed before shipping):
        Add env-gated cache wrap inside create() — gate via
        ARTA_R141_E1_CLI_CACHE_ENABLED=1, key by hash of
        (model, system, messages, max_tokens, max_turns), skip when
        use_continue=True (session-dependent). Reuse the existing
        _r128_b_compute_cache_key helper from llm_client.py and the
        observability/cache.py singleton. Expected savings: ~30%
        of LLM spend on cross-iter repeat clusters; zero risk to
        first-iter calls (always MISS).
    """

    def __init__(self, cli_path: str = "claude"):
        self.cli_path = cli_path
        self.messages = self  # Support client.messages.create() pattern
        self.active_project_id: str | None = None  # Set per-project for session reuse

    @_cli_serialized
    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list,
        system: str | None = None,
        project_id: str | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        disable_tools: bool = False,
        _skip_rate_check: bool = False,
        **kwargs: Any,
    ) -> _CLIResponse:
        """Call Claude Code CLI with the given prompt.

        Args:
            project_id: If provided, reuses the same session for all calls
                        within this project (--continue on subsequent calls).
                        Falls back to self.active_project_id if not provided.
            max_turns: R141.A — explicit override for --max-turns. Falls back
                       to ARTA_CLAUDE_CLI_MAX_TURNS env (default 2).
            disable_tools: R215.T — pass `--tools none` so the CLI exposes NO
                       tools. ARTA's CLI calls are single-shot STRUCTURED output
                       (JSON gen). With tools available, "investigate"-style
                       prompts (e.g. defect RCA) make the model attempt a tool
                       call, burning the whole --max-turns budget → the CLI exits
                       `Error: Reached max turns (N)` with no answer. Disabling
                       tools lets the model respond directly in one turn. This is
                       the ROOT cause of the "Reached max turns" cluster the R141
                       machinery was built to cope with.
        """
        global _RATE_LIMITED, _RATE_LIMIT_MSG, _RATE_LIMIT_RESET
        import time as _time

        # Early exit if rate limited — check if reset time has passed
        if _RATE_LIMITED:
            now = _time.time()
            if now >= _RATE_LIMIT_RESET:
                log.info("Rate limit reset time reached — resuming LLM calls")
                _RATE_LIMITED = False
                _RATE_LIMIT_MSG = ""
            else:
                remaining = int(_RATE_LIMIT_RESET - now)
                raise RuntimeError(f"Claude CLI rate limited (resets in {remaining}s): {_RATE_LIMIT_MSG}")

        # Use instance-level project_id as default
        project_id = project_id or self.active_project_id

        # Build prompt from messages (use last user message)
        prompt = ""
        for msg in messages:
            if msg.get("role") == "user":
                prompt = msg["content"]
        if system:
            prompt = f"{system}\n\n{prompt}"

        # Determine session strategy based on project_id
        use_continue = False
        session_dir = Path("/tmp/arta-sessions")
        if project_id:
            session_dir = session_dir / project_id
            session_dir.mkdir(parents=True, exist_ok=True)
            call_count = _PROJECT_SESSIONS.get(project_id, 0)
            if call_count > 0:
                use_continue = True  # Resume existing session
            _PROJECT_SESSIONS[project_id] = call_count + 1

        # R141.A — configurable --max-turns. Explicit arg wins; otherwise
        # env default (ARTA_CLAUDE_CLI_MAX_TURNS, default 2). Cost-bounded
        # via clamp to [1, 10] in _r141_a_max_turns_default().
        _r141_a_turns = max_turns if isinstance(max_turns, int) and max_turns >= 1 else _r141_a_max_turns_default()

        cmd = [
            self.cli_path,
            "--print",
            "--model", model,
            "--max-turns", str(_r141_a_turns),
            "--dangerously-skip-permissions",
        ]

        # R215.T — expose NO tools for single-shot structured-output calls so the
        # model can't burn the turn budget on tool attempts (see `disable_tools`).
        if disable_tools:
            cmd += ["--tools", "none"]

        # Session reuse: subsequent calls for the same project use --continue
        # to resume context, reducing token usage significantly
        if use_continue:
            cmd.append("--continue")

        # Set HOME to where .claude credentials are mounted.
        # ALWAYS strip ANTHROPIC_API_KEY / CLAUDE_CODE_API_KEY — the CLI manages
        # its own OAuth tokens via .credentials.json and a stale env var will
        # override the CLI's valid token, causing "Invalid API key" errors.
        env = {**os.environ}
        for claude_home in [
            Path(os.environ.get("HOME", "")) / ".claude",
            Path("/home/arta/.claude"),
        ]:
            if claude_home.exists():
                env["HOME"] = str(claude_home.parent)
                break
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("CLAUDE_CODE_API_KEY", None)

        log.info("Claude CLI: model=%s project=%s continue=%s prompt_len=%d",
                 model, project_id or "none", use_continue, len(prompt))

        # R141.D — retry-with-backoff for transient rate-limits. Wraps the
        # subprocess invocation + result-parsing. Long-window rate-limits
        # ("hit your limit") continue to flow through the existing global
        # _RATE_LIMITED flag path. R141.D only catches TRANSIENT 429s
        # (short-window per-account/per-tier limits) which previously
        # raised RuntimeError immediately, wasting spend on caller retries.
        _r141_d_max = 1 if _r141_d_retry_disabled() else _r141_d_max_attempts()
        _r141_d_attempt = 0
        _r141_d_last_exc: _R141DRateLimited | None = None

        while _r141_d_attempt < _r141_d_max:
            _r141_d_attempt += 1
            try:
                # C8: Feed prompt via stdin pipe instead of shell-cat redirection.
                # Previous implementation used `create_subprocess_shell(f"cat {file} | {cmd}")`
                # which is a shell-injection surface. `create_subprocess_exec(*cmd, stdin=PIPE)`
                # is safer — no shell involved, prompt delivered through OS pipe directly.
                # Additionally: no tempfile on disk → no cleanup race on timeout / SIGKILL.
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=str(session_dir) if project_id else None,
                        env=env,
                    )
                    try:
                        # R204 — the default per-call CLI timeout (120s) is too
                        # low for large-context gen calls (ATDD Gherkin / PW spec
                        # gen) on a slow/contended claude_code CLI: when Stage-2
                        # ATDD *succeeds* it can take 261s total across calls, so
                        # single big calls routinely hit 120s → TimeoutError →
                        # ATDD FAIL-FAST → whole generation fails. Make the
                        # default env-configurable (ARTA_CLI_TIMEOUT_S); an
                        # explicit `timeout=` arg still wins. Default unchanged.
                        try:
                            _env_to = float(os.environ.get("ARTA_CLI_TIMEOUT_S", "") or 0)
                        except (TypeError, ValueError):
                            _env_to = 0.0
                        # R215 Item 1 — default 120→300 (STOPGAP). The slow
                        # cold-start calls SUCCEED, they just need headroom while
                        # max-turns=1 + prompt-trim + warm-session land. A 300s
                        # ceiling is not "flawless" — it's the safety net so a
                        # working-but-slow call doesn't FAIL the whole regen.
                        _cli_timeout = timeout or (_env_to if _env_to > 0 else 300.0)
                        stdout, stderr = await asyncio.wait_for(
                            proc.communicate(input=prompt.encode("utf-8")),
                            timeout=_cli_timeout,
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        # Drain and discard any partial output to release pipe buffers.
                        try:
                            await asyncio.wait_for(proc.communicate(), timeout=5.0)
                        except Exception:
                            pass
                        raise RuntimeError(f"Claude CLI timed out after {int(_cli_timeout)}s")
                except FileNotFoundError as exc:
                    raise RuntimeError(
                        f"Claude CLI binary not found at {cmd[0]} — ensure CLAUDE_CLI_PATH is mounted"
                    ) from exc

                text = stdout.decode("utf-8", errors="replace").strip()
                err = stderr.decode("utf-8", errors="replace").strip()

                # R141.A.0 — diagnostic INFO log per subprocess return. Captures
                # exit_code + first stderr line + visible turn count + configured
                # max_turns so operators can correlate "Reached max turns" stalls
                # with the configured budget. NO behavior change; just emits
                # structured signal that downstream R141.B fallback + R141.C
                # dashboard tile read for truthfulness.
                try:
                    _r141_a_0_turns_seen = _r141_a_0_count_turns_in_output(text)
                    log.info(
                        "R141.A.0 CLI diagnostic: exit_code=%s, stdout_len=%d, "
                        "stderr_first_line=%r, turn_count_visible=%d, "
                        "max_turns_configured=%d, attempt=%d/%d",
                        proc.returncode, len(stdout),
                        err.split("\n", 1)[0][:200] if err else "",
                        _r141_a_0_turns_seen, _r141_a_turns,
                        _r141_d_attempt, _r141_d_max,
                    )
                except Exception as _r141_a_0_exc:
                    log.debug("R141.A.0 diagnostic logging skipped: %s", _r141_a_0_exc)

                # R141.D — detect TRANSIENT rate-limit (short-window) BEFORE
                # the long-window error_markers check. Transient markers raise
                # _R141DRateLimited → caught by the surrounding retry loop →
                # backoff + retry. Long-window markers fall through to the
                # existing global _RATE_LIMITED flag path.
                if proc.returncode != 0 and _r141_d_is_transient_rate_limit(err, text):
                    _retry_after = _r141_d_extract_retry_after(err)
                    raise _R141DRateLimited(
                        f"R141.D: Anthropic transient rate-limit (rc={proc.returncode}, "
                        f"stderr={err[:150]})",
                        retry_after_s=_retry_after,
                    )

                # Detect JSON error responses from Claude CLI (non-zero RC with JSON error body)
                if text.startswith('{"type":"result"') and '"is_error":true' in text:
                    log.error("Claude CLI returned error response: %s", text[:300])
                    raise RuntimeError(f"Claude CLI error: {text[:200]}")

                # Detect rate limit / auth errors in short plain-text responses.
                # Only check short responses (< 200 chars) — valid LLM output often contains
                # words like "unauthorized" or "authentication" in generated security content.
                error_markers = [
                    "You've hit your limit", "rate limit", "Invalid API key", "Fix external API key",
                    "Not logged in", "Please run /login", "session expired", "unauthorized",
                    "authentication required", "permission denied", "access denied",
                    "Reached max turns", "max output limit",
                ]
                if len(text) < 200:
                    text_lower = text.lower()
                    for marker in error_markers:
                        if marker.lower() in text_lower:
                            log.error("Claude CLI auth/rate error: %s", text[:200])
                            # Set rate limit flag for "hit your limit" to skip all subsequent calls
                            if "hit your limit" in text_lower or "rate limit" in text_lower:
                                import time as _time
                                _RATE_LIMITED = True
                                _RATE_LIMIT_MSG = text[:150]
                                _RATE_LIMIT_RESET = _parse_rate_limit_reset(text) or (_time.time() + 300)
                                remaining = int(_RATE_LIMIT_RESET - _time.time())
                                log.warning("Rate limit detected — skipping LLM calls for %ds (until reset)", remaining)
                            raise RuntimeError(f"Claude CLI: {text[:150]}")

                # Generic check: very short responses that don't look like code/config are likely errors
                _code_keywords = [
                    "import ", "def ", "class ", "const ", "test(", "describe(",
                    "{", "Feature:", "Scenario:", "env:", "jobs:", "---", "contexts:",
                    "version:", "name:", "http.", "export ", "let ", "var ",
                ]
                if text and len(text) < 100 and not any(kw in text for kw in _code_keywords):
                    log.error("Claude CLI returned suspicious short response: %s", text[:200])
                    raise RuntimeError(f"Claude CLI: unexpected response — {text[:150]}")

                if proc.returncode != 0 and not text:
                    # If --continue fails (e.g., session expired), retry without it
                    if use_continue:
                        log.warning("Claude CLI --continue failed for project %s, starting fresh session", project_id)
                        _PROJECT_SESSIONS[project_id] = 0
                        return await self.create(
                            model=model, max_tokens=max_tokens, messages=messages,
                            system=system, project_id=project_id, **kwargs,
                        )
                    log.error("Claude CLI failed (rc=%d): %s", proc.returncode, err[:500])
                    raise RuntimeError(f"Claude CLI error (rc={proc.returncode}): {err[:200]}")

                if err:
                    log.debug("Claude CLI stderr: %s", err[:200])

                log.info("Claude CLI: got %d chars response (project=%s, call #%d)",
                         len(text), project_id or "none",
                         _PROJECT_SESSIONS.get(project_id, 0) if project_id else 0)
                return _CLIResponse(text, model)

            except _R141DRateLimited as exc:
                # R141.D — transient rate-limit: back off + retry. Final
                # attempt re-raises as RuntimeError for caller compatibility.
                _r141_d_last_exc = exc
                if _r141_d_attempt >= _r141_d_max:
                    break  # exit retry loop; raise below
                # Compute backoff: honor Retry-After hint if present (cost-aware),
                # otherwise exponential with jitter ±25%.
                if exc.retry_after_s is not None:
                    _r141_d_sleep_s = exc.retry_after_s
                else:
                    import random as _r141_d_random
                    _r141_d_sleep_s = min(
                        _r141_d_base_delay() * (2 ** (_r141_d_attempt - 1)),
                        _r141_d_max_delay(),
                    )
                    _r141_d_sleep_s *= (1.0 + (_r141_d_random.random() - 0.5) * 0.5)
                log.warning(
                    "R141.D: transient rate-limit on attempt %d/%d for project=%s — "
                    "backing off %.1fs (retry_after_hint=%s): %s",
                    _r141_d_attempt, _r141_d_max, project_id or "none",
                    _r141_d_sleep_s, exc.retry_after_s, str(exc)[:200],
                )
                await asyncio.sleep(_r141_d_sleep_s)
                # Loop continues — next iteration retries the subprocess.

        # R217 0b — transient 429 backoff EXHAUSTED. Before re-raising, stamp
        # the global _RATE_LIMITED flag with a real cooldown window so the
        # job-level governor (generate-all worker) PAUSES the whole job to the
        # reset window instead of firing the next req straight back into the
        # same limit (the 110×-429 churn that collapsed bulk-gen). Honor the
        # CLI's Retry-After hint when present; else a configured cooldown floor.
        if not _r217_governor_disabled():
            _hint = getattr(_r141_d_last_exc, "retry_after_s", None)
            _cooldown = float(_hint) if (_hint is not None and _hint > 0) else _r217_governor_cooldown_s()
            _RATE_LIMITED = True
            _RATE_LIMIT_MSG = f"transient 429 exhausted after {_r141_d_max} attempts"
            _RATE_LIMIT_RESET = _time.time() + _cooldown
            log.warning(
                "R217 0b: transient rate-limit exhausted — stamping governor flag "
                "for %.0fs (until reset) so the job pauses instead of churning",
                _cooldown,
            )
        # R141.D — re-raise as RuntimeError preserving the original CLI-style
        # error shape so callers don't need new handling.
        raise RuntimeError(
            f"R141.D: Claude CLI rate-limited after {_r141_d_max} attempts: "
            f"{_r141_d_last_exc}"
        ) from _r141_d_last_exc

    @staticmethod
    def reset_session(project_id: str) -> None:
        """Reset session for a project, forcing a fresh session on next call."""
        _PROJECT_SESSIONS.pop(project_id, None)

    @staticmethod
    def get_rate_limit_info() -> dict:
        """Get current rate limit status. Returns {"limited": bool, "reset": float (UTC epoch)}."""
        import time as _time
        if _RATE_LIMITED and _RATE_LIMIT_RESET > _time.time():
            return {"limited": True, "reset": _RATE_LIMIT_RESET}
        return {"limited": False, "reset": 0}

    @staticmethod
    def reset_rate_limit(respect_active_window: bool = False) -> None:
        """Clear the rate limit flag (called at start of new generate-all job).

        R217 0b — `respect_active_window`: when True, refuse to clear a flag
        whose reset window is still in the FUTURE. The per-req interactive
        generate_tests() path clears the flag on EVERY requirement (123×
        "resetting rate limits" observed in the bulk-gen collapse); that
        eager clear would defeat the governor if a genuine long/transient
        window is still active. Job-start + post-wait clears pass False
        (unconditional) — they legitimately reset stale state.
        """
        global _RATE_LIMITED, _RATE_LIMIT_MSG, _RATE_LIMIT_RESET
        if respect_active_window and _RATE_LIMITED:
            import time as _time
            if _RATE_LIMIT_RESET > _time.time():
                # Active window — leave it for the governor to honor.
                return
        _RATE_LIMITED = False
        _RATE_LIMIT_MSG = ""
        _RATE_LIMIT_RESET = 0


class _CLIResponse:
    """Mimics the Anthropic SDK response object structure."""

    def __init__(self, text: str, model: str):
        self.id = "cli-response"
        self.type = "message"
        self.role = "assistant"
        self.content = [_ContentBlock(text)]
        self.model = model
        self.stop_reason = "end_turn"
        self.usage = {"input_tokens": 0, "output_tokens": 0}


class _ContentBlock:
    """Mimics an Anthropic SDK content block."""

    def __init__(self, text: str):
        self.type = "text"
        self.text = text
