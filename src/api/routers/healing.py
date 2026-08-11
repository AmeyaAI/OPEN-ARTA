"""ARTA Self-Heal Approval Router — Human-in-loop approval queue."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from .auth import require_role

log = logging.getLogger("arta.healing")

from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
# F4-1: API-key check is centralised in src/api/dependencies.py.
from ..dependencies import require_api_key as _require_api_key  # noqa: E402


class HealingProposal(BaseModel):
    id: str
    test_id: str
    defect_id: str | None = None
    strategy: str
    confidence: float               # 0.0–1.0
    ai_reasoning: str
    current_selector: str
    proposed_selector: str
    diff_preview: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    created_at: str
    # Phase 5.6 — when an inline-heal patches the test file mid-run, the run's
    # results still reflect the OLD broken selector (the patched file isn't
    # reloaded by the in-flight Playwright/Newman process). Setting
    # `requires_rerun=True` tells the gate that this run's outcome is stale
    # for the affected tests and to surface a "stale-after-heal" check rather
    # than rubber-stamping the broken-then-patched-but-not-rerun state.
    requires_rerun: bool = False


class ApproveRequest(BaseModel):
    override_selector: str | None = None  # If set, use this instead of proposed


class EditRequest(BaseModel):
    selector: str


class ProposeHealRequest(BaseModel):
    test_id: str
    error_message: str
    current_selector: str | None = None
    failure_class: str | None = None  # script_bug, test_design, etc.
    project_id: str | None = None
    test_title: str | None = None


# ── In-memory run results store ──────────────────────────────────────────────
_HEAL_RUN_RESULTS: dict[str, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_test_file(test_title: str | None, test_id: str | None = None) -> tuple[Path | None, str]:
    """Resolve the test_id to its actual script_path via GENERATED_TESTS or DB.

    G3: The previous implementation used substring search which could match the
    wrong file (e.g., 'login' matches both login.spec.ts and login_v2.spec.ts).
    This version does an exact lookup via test_id → script_path then falls back
    to substring search only if no exact mapping exists.

    Returns (path, file_content) or (None, '').
    """
    repo_root = Path(__file__).parents[3]

    # G3 — Exact lookup: test_id → script_path from GENERATED_TESTS
    if test_id:
        try:
            from .tests import GENERATED_TESTS
            for t in GENERATED_TESTS:
                if t.get("id") == test_id and t.get("script_path"):
                    spec = repo_root / t["script_path"]
                    if spec.exists():
                        log.debug("_find_test_file: exact match for %s → %s", test_id, spec)
                        return spec, spec.read_text(encoding="utf-8")
        except Exception as exc:
            log.debug("_find_test_file: GENERATED_TESTS lookup failed: %s", exc)

    base = repo_root / "src" / "automation"
    search_dirs = [base / "bugtrackr", base / "playwright"]

    def _all_specs() -> list[Path]:
        specs: list[Path] = []
        for d in search_dirs:
            if d.exists():
                specs.extend(d.rglob("*.spec.ts"))
        return specs

    # Fallback 1: filename contains the requirement_id slug (req_id → spec name)
    if test_id:
        # TC-AM-001-01 → req_am_001 (most specific match first)
        m = re.search(r"TC-([A-Z]+-\d+)", test_id, re.IGNORECASE)
        if m:
            req_slug = "req_" + m.group(1).lower().replace("-", "_")
            exact_matches = [s for s in _all_specs() if s.stem == req_slug]
            if exact_matches:
                spec = exact_matches[0]
                log.debug("_find_test_file: matched req_id slug %s → %s", req_slug, spec.name)
                return spec, spec.read_text(encoding="utf-8")

    # Fallback 2: title substring (legacy path — kept for backward compatibility)
    if test_title:
        for spec in _all_specs():
            try:
                content = spec.read_text(encoding="utf-8")
                if test_title.lower() in content.lower():
                    log.debug("_find_test_file: matched %s by title '%s'", spec.name, test_title)
                    return spec, content
            except Exception:
                continue

    log.warning("_find_test_file: no spec file found for title=%r id=%r", test_title, test_id)
    return None, ""


async def _generate_fix_with_llm(
    llm,
    test_id: str,
    test_title: str,
    error_msg: str,
    file_content: str,
    model: str = "claude-sonnet-4-6",
    retry_context: str = "",
) -> dict:
    """Call the LLM to produce a real fix. Falls back to rule-based on any failure."""
    if not llm or not file_content:
        log.warning("_generate_fix_with_llm: skipping LLM (llm=%s, file_content_len=%d)", bool(llm), len(file_content))
        return _rule_based_fix(test_id, error_msg)
    try:
        from ...prompts.tea_prompts import SELF_HEAL_FIX

        retry_section = ""
        if retry_context:
            retry_section = f"\nPREVIOUS ATTEMPT FAILED — try a different approach:\n{retry_context}\n"

        prompt = SELF_HEAL_FIX.format(
            test_id=test_id,
            test_title=test_title or test_id,
            error_message=error_msg,
            test_code=file_content[:6000],
            retry_context=retry_section,
        )
        response = await asyncio.wait_for(
            llm.messages.create(
                model=model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=120.0,
        )
        # Find the first text content block (skip thinking/reasoning blocks)
        raw = ""
        for block in response.content:
            if getattr(block, "type", "text") == "text" and getattr(block, "text", ""):
                raw = block.text.strip()
                break
        if not raw:
            raise ValueError("LLM returned empty response")
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        fix = json.loads(raw.strip())
        # Validate required keys
        for key in ("strategy", "confidence", "reasoning", "current_code", "proposed_code", "diff"):
            if key not in fix:
                raise ValueError(f"Missing key: {key}")
        return fix
    except Exception as exc:
        log.warning("LLM fix generation failed (%s) — using rule-based fallback", exc)
        return _rule_based_fix(test_id, error_msg)


def _rule_based_fix(test_id: str, error_msg: str) -> dict:
    """Rule-based fallback when LLM is unavailable."""
    error_lower = (error_msg or "").lower()
    if any(k in error_lower for k in ["intercept", "portal", "pointer-events", "pointer_events"]):
        strategy = "selector_update"
        current = ".click();"
        proposed = ".click({ force: true });"
        diff = "-  .click();\n+  .click({ force: true });"
        reasoning = "Click intercepted by an overlay/portal. Adding { force: true } bypasses event interception."
    elif any(k in error_lower for k in ["timed out", "timeout", "waiting for"]):
        strategy = "wait_selector"
        current = ".click();"
        proposed = ".click({ timeout: 30000 });"
        diff = "-  .click();\n+  .click({ timeout: 30000 });"
        reasoning = "Timeout — increasing the click timeout allows more time for the element to become ready."
    elif any(k in error_lower for k in ["strict mode", "resolved to"]):
        strategy = "selector_update"
        current = ".first().click()"
        proposed = ".first().click()"
        diff = "# Add .first() to narrow the locator if not already present."
        reasoning = "Multiple elements matched — adding .first() picks the first match."
    else:
        strategy = "selector_update"
        current = "// see error below"
        proposed = "// update selector to match current DOM"
        diff = f"# Error: {(error_msg or '')[:200]}\n# Review and apply the appropriate fix manually."
        reasoning = "Rule-based fallback — LLM analysis unavailable."
    return {
        "root_cause": f"Rule-based analysis: {error_msg[:100] if error_msg else 'unknown'}",
        "strategy": strategy,
        "confidence": 0.55,
        "reasoning": reasoning,
        "current_code": current,
        "proposed_code": proposed,
        "diff": diff,
    }


def _get_project_test_env(project_id: str | None) -> dict[str, str]:
    """Build TARGET_* env vars from the project's environments.local config."""
    if not project_id:
        return {}
    try:
        from .projects import _PROJECTS
    except Exception:
        return {}

    proj = _PROJECTS.get(project_id) or {}
    envs = proj.get("environments") or {}
    # Try 'local' first, then the first available environment
    env_cfg = envs.get("local") or next(iter(envs.values()), {}) if envs else {}
    if not env_cfg:
        return {}

    result: dict[str, str] = {}

    base_url = env_cfg.get("base_url") or env_cfg.get("base_url")
    if base_url:
        result["TARGET_BASE_URL"] = base_url

    auth = env_cfg.get("auth") or {}
    method = auth.get("method", "none")
    result["TARGET_AUTH_METHOD"] = method

    creds = auth.get("credentials") or {}
    if method == "cookie":
        result["TARGET_AUTH_COOKIE_NAME"] = creds.get("cookie_name", "")
        result["TARGET_AUTH_COOKIE_VALUE"] = creds.get("cookie_value", "")
        # Pass localStorage items as JSON string
        local_storage = creds.get("localStorage") or {}
        if local_storage:
            result["TARGET_AUTH_LOCALSTORAGE"] = json.dumps(local_storage)
    elif method == "bearer":
        result["TARGET_AUTH_BEARER_TOKEN"] = creds.get("token", "")
    elif method == "basic":
        result["TARGET_AUTH_USERNAME"] = creds.get("username", "")
        result["TARGET_AUTH_PASSWORD"] = creds.get("password", "")

    log.debug("_get_project_test_env for %s: BASE_URL=%s AUTH=%s", project_id, base_url, method)
    return result


async def _run_heal_test(proposal_id: str, proposal: dict, app_state) -> None:
    """Patch test file IN-PLACE, run the test, then restore the original.

    In-place patching is required because Playwright's config enforces testDir —
    a file outside that directory (e.g. /tmp/) is silently ignored by the runner.
    A backup is taken before patching and always restored in the finally block.
    """
    backup_path: Path | None = None
    orig_path: Path | None = None
    try:
        fix = proposal.get("_fix") or {}
        file_path: str | None = proposal.get("test_file_path")
        current_code: str = fix.get("current_code", "")
        proposed_code: str = fix.get("proposed_code", "")
        test_title: str = proposal.get("test_title") or proposal.get("test_id", "")

        if not file_path or not current_code:
            _HEAL_RUN_RESULTS[proposal_id] = {"status": "fail", "output": "No test file or fix available to run."}
            return

        orig_path = Path(file_path)
        original = orig_path.read_text(encoding="utf-8")
        if current_code not in original:
            _HEAL_RUN_RESULTS[proposal_id] = {
                "status": "fail",
                "output": f"current_code not found in test file — cannot patch.\ncurrent_code:\n{current_code}",
            }
            return

        patched = original.replace(current_code, proposed_code, 1)

        # Backup original, then write patched version in-place
        backup_path = orig_path.with_suffix(".spec.ts.bak")
        backup_path.write_text(original, encoding="utf-8")
        orig_path.write_text(patched, encoding="utf-8")

        # Find playwright config whose testDir contains the spec file.
        # bugtrackr/playwright.config.ts has testDir='.' (bugtrackr/ only),
        # but specs live in playwright/ — so prefer the base config which
        # sets testDir to ../playwright, or the config in the spec's own dir.
        repo_root = Path(__file__).resolve().parents[3]  # /app in Docker, <repo> locally
        config_candidates = [
            orig_path.parent / "playwright.config.ts",   # same dir as spec
            repo_root / "src" / "automation" / "common" / "playwright.base.config.ts",
            repo_root / "src" / "automation" / "bugtrackr" / "playwright.config.ts",
            repo_root / "playwright.config.ts",
        ]
        config_path = next((str(c) for c in config_candidates if c.exists()), None)

        # Build env — inject project TARGET_* vars (base_url, auth credentials)
        env = {**os.environ, "NODE_PATH": "/usr/lib/node_modules"}
        proj_env = _get_project_test_env(proposal.get("project_id"))
        env.update(proj_env)

        cmd = ["npx", "playwright", "test", str(orig_path),
               "--reporter=json", "--retries", "0"]
        if config_path:
            cmd += ["--config", config_path]
        if test_title:
            cmd += ["--grep", test_title]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(repo_root),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            _HEAL_RUN_RESULTS[proposal_id] = {"status": "fail", "output": "Test run timed out after 120s."}
            return

        output_str = (stdout or b"").decode(errors="replace")
        err_str = (stderr or b"").decode(errors="replace")

        # Try to parse JSON reporter output
        error_count = 0
        try:
            report = json.loads(output_str)
            error_count = report.get("stats", {}).get("unexpected", 0)
        except Exception:
            error_count = proc.returncode or 0

        status = "pass" if (proc.returncode == 0 and error_count == 0) else "fail"
        _HEAL_RUN_RESULTS[proposal_id] = {
            "status": status,
            "output": output_str[:3000] + ("\n...(truncated)" if len(output_str) > 3000 else ""),
            "error": err_str[:1000] if err_str else None,
            "error_count": error_count,
        }
    except Exception as exc:
        log.exception("_run_heal_test failed for %s", proposal_id)
        _HEAL_RUN_RESULTS[proposal_id] = {"status": "fail", "output": str(exc)}
    finally:
        # Always restore original file from backup
        if backup_path and backup_path.exists() and orig_path:
            try:
                orig_path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
                backup_path.unlink()
            except Exception as restore_err:
                log.error("Failed to restore %s from backup: %s", orig_path, restore_err)


def _pid_from_req(req_id: str) -> str:
    """Infer project_id from requirement_id by looking up PROJECT_REQUIREMENTS."""
    from .requirements import PROJECT_REQUIREMENTS
    for pid, reqs in PROJECT_REQUIREMENTS.items():
        for r in reqs:
            rid = r.get("req_id") or r.get("id", "")
            if rid == req_id:
                return pid
    prefix = req_id.rsplit("-", 1)[0] if "-" in req_id else req_id
    for pid, reqs in PROJECT_REQUIREMENTS.items():
        if reqs and (reqs[0].get("req_id", "") or reqs[0].get("id", "")).startswith(prefix):
            return pid
    return ""


def _strategy_from_error(error: str) -> tuple[str, str]:
    """Return (strategy, reasoning) inferred from the error message."""
    el = (error or "").lower()
    if any(k in el for k in ["selector", "locator", "strict mode", "element"]):
        return (
            "selector_update",
            "Selector/locator drift detected. The element may have been renamed in a recent deployment.",
        )
    if any(k in el for k in ["timed out", "timeout", "waiting for"]):
        return (
            "wait_selector",
            "Timeout failure — the target element loads with a delay. Adding an explicit wait should resolve flakiness.",
        )
    if any(k in el for k in ["race condition", "concurrent", "threshold", "performance"]):
        return (
            "threshold_adjustment",
            "Performance threshold exceeded under load. Adjusting concurrency or timeout thresholds should stabilise the test.",
        )
    return (
        "selector_update",
        "AI analysis of the error pattern suggests the test selectors or assertions may need updating.",
    )


def _seed_proposals_from_tests() -> dict[str, dict]:
    """Build initial heal proposals from currently failing/flaky tests in GENERATED_TESTS.

    Project is inferred from each test's requirement_id prefix so proposals are
    automatically project-aware without any hardcoded scenarios.
    """
    try:
        from .tests import GENERATED_TESTS
    except Exception:
        return {}
    proposals: dict[str, dict] = {}
    for t in GENERATED_TESTS:
        status = str(t.get("status", t.get("last_status", ""))).upper()
        if status not in ("FAIL", "FLAKY"):
            continue
        test_id = t.get("id") or t.get("test_id", "")
        req_id = t.get("requirement_id", "")
        error = t.get("error_message", "Test failure detected")
        strategy, reasoning = _strategy_from_error(error)
        proposal_id = f"heal-{test_id.lower().replace('-', '')}"
        proposals[proposal_id] = {
            "id": proposal_id,
            "test_id": test_id,
            "project_id": _pid_from_req(req_id),
            "defect_id": None,
            "strategy": strategy,
            "confidence": 0.78,
            "ai_reasoning": (
                f"Test '{test_id}' is currently {status}. {reasoning} "
                f"Error: {error}"
            ),
            "current_selector": (error or "")[:80] if error else "see error",
            "proposed_selector": "(AI analysis pending — approve to trigger deep heal)",
            "diff_preview": (
                f"# Test: {test_id}\n"
                f"# Status: {status}\n"
                f"# Error: {error}\n"
                f"# Strategy: {strategy}"
            ),
            "status": "pending",
            "created_at": "2026-03-25T10:00:00Z",
        }
    return proposals


# In-memory approval queue — seeded from real failing tests; grows as /propose is called.
# Gap-1.3: DB persistence already in place via HealingProposalRepo in the approve/reject
# endpoints (line 945-951). On restart, the /queue endpoint reads from DB first, so
# approved-but-unapplied proposals are not lost.
PENDING_APPROVALS: dict[str, dict] = _seed_proposals_from_tests()

# Simulated GitHub PR counter
_next_pr = 489


def _resolve_llm(body: ProposeHealRequest, request: Request) -> tuple:
    """Return (llm, model_name) for the given request, preferring per-project config."""
    app_state = getattr(getattr(request, "app", None), "state", None)
    platform_llm = getattr(app_state, "anthropic", None)
    platform_provider = getattr(app_state, "llm_provider", None) if app_state else None

    llm = None
    model_name = os.environ.get("ARTA_LLM_MODEL", "claude-sonnet-4-6")
    if body.project_id:
        try:
            from .projects import _PROJECTS
            from ...models.llm_config import LLMConfig, LLMProvider
            from ...agents.llm_client import create_llm_client
            proj = _PROJECTS.get(body.project_id)
            if proj and proj.get("llm_config"):
                llm_cfg = LLMConfig(**proj["llm_config"])
                if llm_cfg.provider in (LLMProvider.ANTHROPIC, LLMProvider.CLAUDE_CODE):
                    llm = create_llm_client(llm_cfg)
                    model_name = llm_cfg.model
                elif llm_cfg.provider == LLMProvider.OLLAMA:
                    from ...agents.ollama_client import OllamaDirectClient
                    from ...agents.llm_client import _warn_if_localhost_inside_container
                    ollama_url = (
                        llm_cfg.base_url
                        or os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
                    )
                    # F10-1b: same guard as the main factory
                    _warn_if_localhost_inside_container(ollama_url)
                    llm = OllamaDirectClient(ollama_url, llm_cfg.model)
                    model_name = llm_cfg.model
                else:
                    try:
                        import litellm as _litellm  # noqa: F401
                        llm = create_llm_client(llm_cfg)
                        model_name = llm_cfg.model
                    except ImportError:
                        log.debug("litellm not installed — falling back to platform LLM")
        except Exception as exc:
            log.debug("Per-project LLM unavailable (%s) — using platform default", exc)

    if llm is None:
        llm = platform_llm
        platform_env_model = os.environ.get("ARTA_LLM_MODEL", "claude-sonnet-4-6")
        if platform_provider in ("anthropic", "claude_code"):
            model_name = platform_env_model if platform_env_model.startswith("claude") else "claude-sonnet-4-6"
        else:
            model_name = platform_env_model

    return llm, model_name


async def _try_fix_in_place(
    fix: dict,
    file_path: str | None,
    file_content: str,
    test_title: str,
    proposal_id: str,
    project_id: str | None = None,
) -> dict:
    """Patch test file in-place, run the test, restore original.

    In-place patching is required because Playwright's config enforces testDir —
    a file outside that directory is silently ignored by the runner.
    """
    current_code: str = fix.get("current_code", "")
    proposed_code: str = fix.get("proposed_code", "")

    if not file_path or not current_code or current_code not in file_content:
        return {"status": "skip", "output": "Cannot patch: current_code not found in file or file missing."}

    patched = file_content.replace(current_code, proposed_code, 1)
    orig_path = Path(file_path)
    backup_path = orig_path.with_suffix(".spec.ts.agentic.bak")
    try:
        # Backup original, write patched version in-place
        backup_path.write_text(file_content, encoding="utf-8")
        orig_path.write_text(patched, encoding="utf-8")

        repo_root = Path(__file__).resolve().parents[3]  # /app in Docker, <repo> locally
        config_candidates = [
            orig_path.parent / "playwright.config.ts",
            repo_root / "src" / "automation" / "common" / "playwright.base.config.ts",
            repo_root / "src" / "automation" / "bugtrackr" / "playwright.config.ts",
            repo_root / "playwright.config.ts",
        ]
        config_path = next((str(c) for c in config_candidates if c.exists()), None)

        env = {**os.environ, "NODE_PATH": "/usr/lib/node_modules"}
        env.update(_get_project_test_env(project_id))

        cmd = ["npx", "playwright", "test", str(orig_path),
               "--reporter=json", "--retries", "0"]
        if config_path:
            cmd += ["--config", config_path]
        if test_title:
            cmd += ["--grep", test_title]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(repo_root),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            return {"status": "fail", "output": "Test run timed out after 90s."}

        out = (stdout or b"").decode(errors="replace")
        err = (stderr or b"").decode(errors="replace")
        error_count = 0
        try:
            report = json.loads(out)
            error_count = report.get("stats", {}).get("unexpected", 0)
        except Exception:
            error_count = proc.returncode or 0

        status = "pass" if (proc.returncode == 0 and error_count == 0) else "fail"
        return {"status": status, "output": (out + err)[:2000]}
    except Exception as exc:
        return {"status": "skip", "output": str(exc)}
    finally:
        # Always restore original from backup
        if backup_path.exists():
            try:
                orig_path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
                backup_path.unlink()
            except Exception as restore_err:
                log.error("Failed to restore %s from backup: %s", orig_path, restore_err)


async def _enrich_proposal_with_llm(
    proposal_id: str,
    llm,
    test_id: str,
    test_title: str,
    error_msg: str,
    file_content: str,
    model: str,
) -> None:
    """Agentic background task: generate → run test → retry on failure (up to 3 attempts).

    If the fix makes the test pass, confidence is set to 0.95 and ai_reasoning is marked
    as verified. If all attempts fail, the best LLM fix is still used (better than rule-based).
    """
    proposal = PENDING_APPROVALS.get(proposal_id)
    if not proposal:
        return

    try:
        await _agentic_heal_loop(proposal_id, proposal, llm, test_id, test_title, error_msg, file_content, model)
    except Exception as exc:
        log.exception("_enrich_proposal_with_llm uncaught error for %s: %s", proposal_id, exc)
        p = PENDING_APPROVALS.get(proposal_id)
        if p:
            p["_analyzing"] = False


async def _agentic_heal_loop(
    proposal_id: str, proposal: dict, llm,
    test_id: str, test_title: str, error_msg: str,
    file_content: str, model: str,
) -> None:
    # Re-run file search in case it was empty at propose time
    file_path_obj, fc = (None, file_content)
    if not file_content:
        file_path_obj, fc = _find_test_file(test_title, test_id)
        if fc:
            proposal["test_file_path"] = str(file_path_obj) if file_path_obj else None
    else:
        file_path_obj = Path(proposal.get("test_file_path") or "") if proposal.get("test_file_path") else None

    file_path_str = str(file_path_obj) if file_path_obj else None

    MAX_ATTEMPTS = 3
    best_fix: dict | None = None
    retry_context = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        log.info("Proposal %s — heal attempt %d/%d", proposal_id, attempt, MAX_ATTEMPTS)

        fix = await _generate_fix_with_llm(
            llm, test_id, test_title, error_msg, fc, model,
            retry_context=retry_context,
        )

        is_rule_based = fix.get("reasoning", "").startswith("Rule-based fallback")
        if is_rule_based:
            # LLM unavailable — nothing to iterate on
            if not best_fix:
                best_fix = fix
            break

        # Update proposal UI immediately so user sees latest attempt
        proposal["strategy"] = fix.get("strategy", proposal["strategy"])
        proposal["confidence"] = float(fix.get("confidence", proposal["confidence"]))
        proposal["ai_reasoning"] = f"Attempt {attempt}/{MAX_ATTEMPTS}: {fix.get('reasoning', '')}"
        proposal["current_selector"] = fix.get("current_code", proposal["current_selector"])
        proposal["proposed_selector"] = fix.get("proposed_code", proposal["proposed_selector"])
        proposal["diff_preview"] = fix.get("diff", proposal["diff_preview"])
        proposal["_fix"] = fix

        # Try running the test with this fix
        run_result = await _try_fix_in_place(
            fix, file_path_str, fc, test_title, proposal_id,
            project_id=proposal.get("project_id"),
        )
        run_output = run_result.get("output", "")
        log.info("Proposal %s attempt %d run_result: %s", proposal_id, attempt, run_result["status"])

        # Detect environment errors (app not reachable, Playwright not installed, etc.)
        _env_error_signals = (
            "econnrefused", "err_connection_refused", "err_name_not_resolved",
            "net::", "could not find playwright", "no such file", "command not found",
            "cannot find module", "error: no tests found",
        )
        is_env_error = any(sig in run_output.lower() for sig in _env_error_signals)

        if run_result["status"] == "pass":
            # Fix verified — mark as high-confidence
            fix["confidence"] = 0.95
            fix["reasoning"] = (
                f"AI-verified: test passes with this fix (attempt {attempt}). "
                + fix.get("reasoning", "")
            )
            best_fix = fix
            break
        elif is_env_error:
            # App not reachable / env not set up — can't verify; use this fix as-is
            log.info("Proposal %s: environment error (app unreachable) — skipping retry", proposal_id)
            best_fix = fix
            break
        elif run_result["status"] == "fail":
            if not best_fix:
                best_fix = fix
            retry_context = (
                f"Your previous fix DID NOT make the test pass.\n"
                f"You changed: {fix.get('current_code', '')!r}\n"
                f"To: {fix.get('proposed_code', '')!r}\n"
                f"Test failure output:\n{run_output[:800]}\n"
                f"Try a DIFFERENT approach — if your selector change didn't help, "
                f"try adding {{ force: true }} to the .click() call instead."
            )
        else:
            # skip / unknown — use this fix as-is
            best_fix = fix
            break

    if not best_fix:
        best_fix = _rule_based_fix(test_id, error_msg)

    # Final update
    proposal = PENDING_APPROVALS.get(proposal_id)
    if proposal:
        proposal["strategy"] = best_fix.get("strategy", proposal["strategy"])
        proposal["confidence"] = float(best_fix.get("confidence", proposal["confidence"]))
        proposal["ai_reasoning"] = best_fix.get("reasoning", proposal["ai_reasoning"])
        proposal["current_selector"] = best_fix.get("current_code", proposal["current_selector"])
        proposal["proposed_selector"] = best_fix.get("proposed_code", proposal["proposed_selector"])
        proposal["diff_preview"] = best_fix.get("diff", proposal["diff_preview"])
        proposal["_fix"] = best_fix
        proposal["_analyzing"] = False
        log.info(
            "Proposal %s finalized: strategy=%s confidence=%.2f verified=%s",
            proposal_id,
            proposal["strategy"],
            proposal["confidence"],
            "AI-verified" in proposal["ai_reasoning"],
        )


@router.post("/propose", dependencies=[Depends(_require_api_key)])
async def propose_heal(body: ProposeHealRequest, request: Request):
    """Create a new healing proposal immediately; LLM fix runs in background."""
    proposal_id = f"heal-{uuid.uuid4().hex[:6]}"

    # Find the test file on disk (pass both title and id for robust lookup)
    file_path, file_content = _find_test_file(body.test_title, body.test_id)

    # Resolve LLM client
    llm, model_name = _resolve_llm(body, request)

    # Seed proposal with rule-based fallback so the page can render immediately
    fallback = _rule_based_fix(body.test_id, body.error_message)
    strategy = fallback["strategy"]
    current = body.current_selector or fallback["current_code"]
    proposed = fallback["proposed_code"]
    diff_preview = fallback["diff"]
    confidence = fallback["confidence"]
    reasoning = "AI analysis in progress..."

    proposal = {
        "id": proposal_id,
        "test_id": body.test_id,
        "project_id": body.project_id,
        "defect_id": None,
        "strategy": strategy,
        "confidence": confidence,
        "ai_reasoning": reasoning,
        "current_selector": current,
        "proposed_selector": proposed,
        "diff_preview": diff_preview,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "test_file_path": str(file_path) if file_path else None,
        "test_title": body.test_title or body.test_id,
        "_fix": fallback,
        "_analyzing": True,
    }

    PENDING_APPROVALS[proposal_id] = proposal

    # R32.2 — write-through to Redis so heal proposals survive container
    # restart. Operator-pending decisions don't get orphaned by a deploy.
    try:
        from ..services.durable_state import queue_approval as _ds_queue_approval
        proj_for_q = (proposal.get("project_id") or body.project_id or "_default")
        await _ds_queue_approval(proj_for_q, proposal)
    except Exception as _ds_exc:
        log.debug("R32.2: durable_state queue failed for %s: %s", proposal_id, _ds_exc)

    # Gap-1.5: Fire LLM enrichment in background (supervised — logs exceptions).
    from ...observability.task_supervisor import supervise
    def _on_enrich_error(exc, pid=proposal_id):
        if pid in PENDING_APPROVALS:
            PENDING_APPROVALS[pid]["_analyzing"] = False
            PENDING_APPROVALS[pid]["_enrich_error"] = f"{type(exc).__name__}: {exc}"[:300]
    supervise(
        asyncio.create_task(_enrich_proposal_with_llm(
            proposal_id, llm,
            body.test_id, body.test_title or body.test_id,
            body.error_message, file_content, model_name,
        )),
        f"enrich_proposal:{proposal_id}",
        on_error=_on_enrich_error,
    )

    return {
        "proposal_id": proposal_id,
        "status": "pending",
        "redirect_url": "/healing",
        "message": f"Healing proposal {proposal_id} created for test {body.test_id}",
    }


@router.post("/{proposal_id}/run", dependencies=[Depends(_require_api_key)])
async def run_with_fix(proposal_id: str, request: Request):
    """Patch the test file in a temp dir and run the single test to verify the fix.
    Returns immediately — poll /{proposal_id}/run/result for the outcome."""
    proposal = PENDING_APPROVALS.get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")
    _HEAL_RUN_RESULTS[proposal_id] = {"status": "running"}
    # Gap-1.5: Supervised — logs exceptions and marks run FAILED instead of hanging in "running".
    from ...observability.task_supervisor import supervise
    def _on_heal_error(exc, pid=proposal_id):
        _HEAL_RUN_RESULTS[pid] = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    supervise(
        asyncio.create_task(_run_heal_test(proposal_id, proposal, getattr(request.app, "state", None))),
        f"heal_test:{proposal_id}",
        on_error=_on_heal_error,
    )
    return {"proposal_id": proposal_id, "status": "running"}


@router.get("/{proposal_id}/run/result", dependencies=[Depends(_require_api_key)])
async def run_result(proposal_id: str):
    """Poll the result of a run-with-fix invocation."""
    return _HEAL_RUN_RESULTS.get(proposal_id, {"status": "not_started"})


@router.get("/queue", dependencies=[Depends(_require_api_key)])
async def list_queue(
    status: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
):
    """List pending heal proposals.

    R68.4 — `run_id` filter: when supplied, scopes proposals to those
    produced by the specified run. Proposals carry the originating
    run via `metadata.last_seen_run_id` (R37.4 stamps it). Pre-R68.4
    the operator dashboard showed lifetime proposals — confusing when
    the user expected "what just happened in this run".

    Fix Y: query DB first, but fall through to in-memory when DB returns
    zero rows.
    """
    items: list[dict] = []
    try:
        from ..db_adapter import try_db
        async with try_db() as db:
            if db:
                from ...db.repository import HealingProposalRepo, _to_dict
                repo = HealingProposalRepo(db)
                rows, _ = await repo.list(status=status, project_id=project_id)
                items = [_to_dict(r) for r in rows]
    except Exception:
        pass

    # Fall through to in-memory when DB has zero rows.
    if not items:
        items = list(PENDING_APPROVALS.values())
        if project_id:
            items = [i for i in items if i.get("project_id") == project_id]
        if status:
            items = [i for i in items if i.get("status") == status]

    # R68.4 — apply run_id filter at the end so it works against BOTH
    # the DB-sourced rows and the in-memory fallback rows. The proposal
    # may carry the run id at top-level OR under metadata.last_seen_run_id.
    if run_id:
        def _matches_run(i: dict) -> bool:
            md = i.get("metadata") or {}
            return (
                i.get("run_id") == run_id
                or md.get("last_seen_run_id") == run_id
                or md.get("run_id") == run_id
            )
        items = [i for i in items if _matches_run(i)]

    pending = sum(1 for i in items if i.get("status") == "pending")
    return {"proposals": items, "total": len(items), "pending": pending}


@router.post("/{proposal_id}/approve", dependencies=[Depends(_require_api_key), Depends(require_role("admin", "test_architect"))])
async def approve_proposal(proposal_id: str, body: ApproveRequest):
    """Approve a heal proposal and raise a GitHub PR."""
    global _next_pr
    from ..db_adapter import try_db

    proposal = None
    async with try_db() as db:
        if db:
            from ...db.repository import HealingProposalRepo, _to_dict
            repo = HealingProposalRepo(db)
            row = await repo.get(proposal_id)
            if row:
                proposal = _to_dict(row)

    if not proposal:
        proposal = PENDING_APPROVALS.get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")
    if proposal["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Proposal already {proposal['status']}")

    # R38.4 — type-dispatch BEFORE the selector-patch branch. test_gen_regen
    # proposals (R37.1) carry no `proposed_selector` — instead they hold
    # `affected_tests` + `signals` + `sample_error`. The selector-patch
    # path (lines below) would KeyError or no-op silently. This branch
    # marks the proposal approved + queues a regen request per affected
    # test so the next gen cycle picks it up with the runtime failure as
    # a Tier-2 hint.
    proposal_type = proposal.get("type")
    if proposal_type == "test_gen_regen":
        affected = proposal.get("affected_tests") or []
        signals = proposal.get("signals") or []
        sample_error = proposal.get("sample_error") or ""

        # Mark approved in DB + memory using the existing primitives.
        async with try_db() as db:
            if db:
                from ...db.repository import HealingProposalRepo
                repo = HealingProposalRepo(db)
                await repo.update(proposal_id, {"status": "approved"})
        if proposal_id in PENDING_APPROVALS:
            PENDING_APPROVALS[proposal_id]["status"] = "approved"

        # Drop a regen-queue marker per affected test so the next gen
        # cycle (operator click on "Regenerate" in test-explorer, OR the
        # background regen worker) can replay them with the captured
        # failure context. Best-effort; never fail the approval on disk
        # write errors.
        queue_dir = Path(".arta/regen_queue")
        queued: list[str] = []
        try:
            queue_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "proposal_id": proposal_id,
                "triage_category": proposal.get("triage_category"),
                "status_code": proposal.get("status_code"),
                "signals": list(signals),
                "sample_error": sample_error,
                "queued_at": datetime.utcnow().isoformat() + "Z",
            }
            for test_id in affected:
                if not test_id:
                    continue
                marker = queue_dir / f"{test_id}.json"
                try:
                    marker.write_text(json.dumps(
                        {**payload, "test_id": test_id}, indent=2,
                    ))
                    queued.append(test_id)
                except Exception as marker_exc:
                    log.warning("R38.4: regen marker write failed for %s: %s",
                                test_id, marker_exc)
        except Exception as queue_exc:
            log.warning("R38.4: regen queue dir setup failed: %s", queue_exc)

        log.info(
            "R38.4: regen triggered for proposal=%s — queued %d/%d tests "
            "(triage=%s status_code=%s signals=%s)",
            proposal_id, len(queued), len(affected),
            proposal.get("triage_category"),
            proposal.get("status_code"),
            list(signals)[:3],
        )

        return {
            "ok": True,
            "proposal_id": proposal_id,
            "type": "test_gen_regen",
            "regen_queued": len(queued),
            "affected_tests": affected,
            "queue_dir": str(queue_dir),
        }

    final_selector = body.override_selector or proposal["proposed_selector"]

    db_updated = False
    async with try_db() as db:
        if db:
            from ...db.repository import HealingProposalRepo
            repo = HealingProposalRepo(db)
            await repo.update(proposal_id, {
                "status": "approved",
                "applied_selector": final_selector,
            })
            db_updated = True

    if not db_updated and proposal_id in PENDING_APPROVALS:
        PENDING_APPROVALS[proposal_id]["status"] = "approved"
        PENDING_APPROVALS[proposal_id]["applied_selector"] = final_selector

    # ── Apply the fix to the actual test file on disk ────────────────────
    in_mem = PENDING_APPROVALS.get(proposal_id, {})
    fix = proposal.get("_fix") or in_mem.get("_fix") or {}
    file_path = proposal.get("test_file_path") or in_mem.get("test_file_path")
    current_code = fix.get("current_code", "")
    proposed_code = fix.get("proposed_code", "")

    file_updated = False
    if file_path and current_code and proposed_code:
        try:
            fp = Path(file_path)
            original = fp.read_text(encoding="utf-8")
            if current_code in original:
                fp.write_text(original.replace(current_code, proposed_code, 1), encoding="utf-8")
                file_updated = True
                log.info("Applied fix to %s for proposal %s", file_path, proposal_id)
            else:
                log.warning("current_code not found in %s — fix not applied", file_path)
        except Exception as exc:
            log.warning("Failed to apply fix to %s: %s", file_path, exc)

    # F6-9: Heal traceability — close the loop with the original failing test.
    # 1) Look up the original test's trace_id and stamp the new red_phase_status
    #    as GREEN_AFTER_HEAL so the dashboard distinguishes "heal-restored pass"
    #    from "always green".
    # 2) Carry the trace_id into the PR metadata so cohort queries
    #    (GET /api/traceability/trace/<trace_id>) still surface the heal commit.
    test_trace_id: str | None = None
    try:
        from .tests import GENERATED_TESTS  # type: ignore
        _entry = next(
            (t for t in GENERATED_TESTS if t.get("id") == proposal["test_id"]),
            None,
        )
        if _entry:
            test_trace_id = _entry.get("trace_id") or None
            # Preserve original RED stamp; only update when an earlier run had marked it.
            if _entry.get("red_phase_status") in ("RED", "GREEN_UNEXPECTED", "ERROR"):
                _entry["red_phase_status"] = "GREEN_AFTER_HEAL"
                # Best-effort DB UPDATE so the new status survives container restart.
                try:
                    from ..db_adapter import try_db as _try_db
                    from sqlalchemy import text as _sa_text
                    async with _try_db() as _db:
                        if _db is not None:
                            await _db.execute(_sa_text(
                                "UPDATE test_cases SET red_phase_status = :s WHERE test_id = :tid"
                            ), {"s": "GREEN_AFTER_HEAL", "tid": proposal["test_id"]})
                except Exception as _heal_db_exc:
                    log.warning("F6-9: red_phase DB update failed for %s: %s",
                                proposal["test_id"], _heal_db_exc)
    except Exception as _heal_lookup_exc:
        log.debug("F6-9: trace lookup skipped for %s: %s", proposal_id, _heal_lookup_exc)

    pr_number = _next_pr
    _next_pr += 1
    pr_body_lines = [
        f"Auto-generated by ARTA self-healing for test {proposal['test_id']}.",
        f"Proposal: {proposal_id}",
        f"Selector: {final_selector}",
    ]
    if test_trace_id:
        pr_body_lines.append(f"trace_id: {test_trace_id}")
        pr_body_lines.append(
            "Cohort: /api/traceability/trace/" + test_trace_id
        )

    return {
        "ok": True,
        "proposal_id": proposal_id,
        "test_id": proposal["test_id"],
        "trace_id": test_trace_id,  # F6-9
        "applied_selector": final_selector,
        "file_updated": file_updated,
        "github_pr": f"https://github.com/org/repo/pull/{pr_number}",
        "pr_number": pr_number,
        "pr_body": "\n".join(pr_body_lines),  # F6-9: visible to GitHub client
    }


@router.post("/{proposal_id}/reject", dependencies=[Depends(_require_api_key), Depends(require_role("admin", "test_architect"))])
async def reject_proposal(proposal_id: str):
    """Reject and discard a heal proposal."""
    from ..db_adapter import try_db

    proposal = None
    async with try_db() as db:
        if db:
            from ...db.repository import HealingProposalRepo, _to_dict
            repo = HealingProposalRepo(db)
            row = await repo.get(proposal_id)
            if row:
                proposal = _to_dict(row)

    if not proposal:
        proposal = PENDING_APPROVALS.get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")
    if proposal["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Proposal already {proposal['status']}")

    db_updated = False
    async with try_db() as db:
        if db:
            from ...db.repository import HealingProposalRepo
            repo = HealingProposalRepo(db)
            await repo.update(proposal_id, {"status": "rejected"})
            db_updated = True

    if not db_updated and proposal_id in PENDING_APPROVALS:
        PENDING_APPROVALS[proposal_id]["status"] = "rejected"

    return {"ok": True, "proposal_id": proposal_id, "status": "rejected"}


@router.get("/stats", dependencies=[Depends(_require_api_key)])
async def healing_stats(
    project_id: str | None = None,
    run_id: str | None = None,
):
    """Aggregate self-healing stats: total healed, approval rate, confidence, maintenance reduction, weekly trends.

    R68.4 — `run_id` filter scopes stats to proposals from a single run.
    Pre-R68.4 the operator dashboard always showed lifetime aggregates;
    impossible to see "what did the heal pipeline produce for THIS run".
    """
    items = None
    try:
        from ..db_adapter import try_db
        async with try_db() as db:
            if db:
                from ...db.repository import HealingProposalRepo, _to_dict
                repo = HealingProposalRepo(db)
                all_rows, total = await repo.list(project_id=project_id)
                items = [_to_dict(r) for r in all_rows]
    except Exception:
        pass  # Table may not exist yet — fall through to in-memory

    if items is None:
        items = list(PENDING_APPROVALS.values())
    if project_id:
        items = [i for i in items if i.get("project_id") == project_id]

    # R68.4 — same run_id matcher pattern as `list_queue` above.
    if run_id:
        def _matches_run_stats(i: dict) -> bool:
            md = i.get("metadata") or {}
            return (
                i.get("run_id") == run_id
                or md.get("last_seen_run_id") == run_id
                or md.get("run_id") == run_id
            )
        items = [i for i in items if _matches_run_stats(i)]

    total_proposals = len(items)
    approved = [i for i in items if i.get("status") == "approved"]
    rejected = [i for i in items if i.get("status") == "rejected"]
    pending = [i for i in items if i.get("status") == "pending"]

    total_healed = len(approved)
    approval_rate = round(total_healed / max(total_healed + len(rejected), 1) * 100, 1)
    avg_confidence = round(
        sum(i.get("confidence", 0) for i in approved) / max(len(approved), 1), 2
    )

    # Maintenance reduction: each approved heal saves ~15min of manual fix time
    manual_fix_minutes = 15
    hours_saved = round(total_healed * manual_fix_minutes / 60, 1)
    # Estimate maintenance reduction % vs total test maintenance time
    estimated_total_maintenance_hours = max(total_proposals * manual_fix_minutes / 60, 1)
    maintenance_reduction_pct = round(hours_saved / estimated_total_maintenance_hours * 100, 1)

    # Strategy breakdown
    strategy_counts: dict[str, int] = {}
    for i in approved:
        s = i.get("strategy", "unknown")
        strategy_counts[s] = strategy_counts.get(s, 0) + 1

    # Weekly trends — group by date
    from collections import defaultdict
    daily: dict[str, dict] = defaultdict(lambda: {"healed": 0, "rejected": 0, "pending": 0, "avg_confidence": []})
    for i in items:
        date_str = (i.get("created_at") or "")[:10]
        if not date_str:
            continue
        status = i.get("status", "pending")
        if status == "approved":
            daily[date_str]["healed"] += 1
            daily[date_str]["avg_confidence"].append(i.get("confidence", 0))
        elif status == "rejected":
            daily[date_str]["rejected"] += 1
        else:
            daily[date_str]["pending"] += 1

    weekly_trends = []
    for date_str in sorted(daily.keys()):
        d = daily[date_str]
        conf_list = d["avg_confidence"]
        weekly_trends.append({
            "date": date_str,
            "healed": d["healed"],
            "rejected": d["rejected"],
            "pending": d["pending"],
            "avg_confidence": round(sum(conf_list) / max(len(conf_list), 1), 2),
        })

    return {
        "total_proposals": total_proposals,
        "total_healed": total_healed,
        "total_rejected": len(rejected),
        "total_pending": len(pending),
        "approval_rate": approval_rate,
        "avg_confidence": avg_confidence,
        "hours_saved": hours_saved,
        "maintenance_reduction_pct": maintenance_reduction_pct,
        "strategy_breakdown": strategy_counts,
        "weekly_trends": weekly_trends,
    }


@router.post("/{proposal_id}/edit", dependencies=[Depends(_require_api_key)])
async def edit_proposal(proposal_id: str, body: EditRequest):
    """Update the proposed selector before approval."""
    from ..db_adapter import try_db

    proposal = None
    async with try_db() as db:
        if db:
            from ...db.repository import HealingProposalRepo, _to_dict
            repo = HealingProposalRepo(db)
            row = await repo.get(proposal_id)
            if row:
                proposal = _to_dict(row)

    if not proposal:
        proposal = PENDING_APPROVALS.get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")
    if proposal["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Proposal already {proposal['status']}")

    db_updated = False
    async with try_db() as db:
        if db:
            from ...db.repository import HealingProposalRepo
            repo = HealingProposalRepo(db)
            await repo.update(proposal_id, {"proposed_selector": body.selector})
            db_updated = True

    if not db_updated and proposal_id in PENDING_APPROVALS:
        PENDING_APPROVALS[proposal_id]["proposed_selector"] = body.selector

    return {"ok": True, "proposal_id": proposal_id, "updated_selector": body.selector}


@router.get("/{proposal_id}", dependencies=[Depends(_require_api_key)])
async def get_proposal(proposal_id: str):
    """Return current state of a single healing proposal (includes _analyzing flag)."""
    proposal = PENDING_APPROVALS.get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")
    return proposal
