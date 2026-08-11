"""
ARTA Self-Healing Agent — TEA Layer 4 (Repair Loop)
Autonomously repairs automation tests when failures arise from:
  - UI selector changes (element moved, id/class renamed)
  - API schema changes (field renamed, type changed, endpoint moved)
  - Environment instability (flaky infrastructure, timeouts)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import httpx

import anthropic
from anthropic import AsyncAnthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .retry_policy import LLM_RETRYABLE_EXC   # R134.C — single-source-of-truth retry tuple

log = logging.getLogger("arta.self_healing")


@dataclass
class HealingAction:
    test_id: str
    failure_type: Literal["selector", "api_schema", "env", "flaky", "generation", "execution"]
    original_file: str
    healed_content: str
    changes: list[dict] = field(default_factory=list)   # [{old, new, reason}]
    confidence: float = 0.0
    requires_human_review: bool = False
    pr_branch: str = ""


# ── Rule-based classifiers (zero LLM tokens) ───────────────────��─────────────

# Maps a keyword in the error message/type → (root_cause, auto_fixable, action_hint)
# Checked in order; first match wins.
_GENERATION_FAILURE_RULES: list[tuple[str, str, bool, str]] = [
    ("RateLimitError",       "rate_limit",    False, "Claude API rate limited — wait for reset, then run /heal-tests again"),
    ("rate limit",           "rate_limit",    False, "Claude API rate limited — wait for reset, then run /heal-tests again"),
    ("asyncio.TimeoutError", "timeout",       True,  "LLM call timed out — retrying with single-tool generation"),
    ("TimeoutError",         "timeout",       True,  "LLM call timed out — retrying with single-tool generation"),
    ("timed out after",      "timeout",       True,  "LLM call timed out — retrying with single-tool generation"),
    ("No LLM client",        "no_client",     False, "LLM not configured — add Anthropic API key in Settings"),
    ("Invalid API key",      "auth_failure",  False, "Invalid API key — check Anthropic credentials in Settings"),
    ("Not logged in",        "auth_failure",  False, "Claude CLI not authenticated — run: claude login"),
    ("session expired",      "session_expired", True, "CLI session expired — retrying with fresh session"),
    ("empty response",       "empty_response", True, "LLM returned empty output — retrying with single-tool generation"),
    ("short response",       "empty_response", True, "LLM returned minimal output — retrying with single-tool generation"),
]

# Maps a keyword in the execution error_message → (failure_type, auto_fixable)
_EXECUTION_FAILURE_RULES: list[tuple[str, str, bool]] = [
    ("ResizeObserver",   "noise",    True),   # browser noise — auto-patch filter
    ("Non-Error",        "noise",    True),
    ("locator(",         "selector", True),
    ("getByRole",        "selector", True),
    ("getByLabel",       "selector", True),
    ("waiting for",      "selector", True),
    ("no such element",  "selector", True),
    ("strict mode",      "selector", True),
    ("Timeout exceeded", "timeout",  True),
    ("net::ERR_",        "network",  False),
    ("ECONNREFUSED",     "network",  False),
    ("ECONNRESET",       "network",  False),
    ("HTTP 401",         "auth",     False),
    ("HTTP 403",         "auth",     False),
    (" 401 ",            "auth",     False),
    (" 403 ",            "auth",     False),
    # k6 performance test failures
    ("Could not parse k6 output",    "k6_script_error", True),
    ("exit code:",                    "k6_script_error", True),
    ("failed parsing threshold",      "k6_threshold_syntax", True),  # p95<3000 → p(95)<3000
    ("unknown executor",              "k6_executor",     True),
    ("invalid scenario",              "k6_config",       True),
    ("GoError",                       "k6_script_error", True),
    ("dial tcp",                      "network",         False),
]


def _classify_generation_failure(error_type: str, error_msg: str) -> tuple[str, bool, str]:
    """Rule-based classification — returns (root_cause, auto_fixable, action_hint). Zero LLM tokens."""
    combined = f"{error_type} {error_msg}".lower()
    for keyword, root_cause, auto_fixable, hint in _GENERATION_FAILURE_RULES:
        if keyword.lower() in combined:
            return root_cause, auto_fixable, hint
    return "unknown", False, "Unknown generation failure — check backend logs for details"


def _classify_execution_failure(error_msg: str) -> tuple[str, bool]:
    """Rule-based classification — returns (failure_type, auto_fixable). Zero LLM tokens."""
    msg_lower = (error_msg or "").lower()
    for keyword, failure_type, auto_fixable in _EXECUTION_FAILURE_RULES:
        if keyword.lower() in msg_lower:
            return failure_type, auto_fixable
    return "unknown", False


SELECTOR_HEALING_PROMPT = """\
You are an expert test automation engineer repairing a broken Playwright test.

FAILED TEST:
{test_code}

FAILURE:
Error: {error_message}
Selector that failed: {failed_selector}

DOM BEFORE (last passing run — abbreviated):
{dom_before}

DOM AFTER (current — abbreviated):
{dom_after}

INSTRUCTIONS:
1. Identify what changed in the DOM that caused the selector failure
2. Find the best equivalent selector in the new DOM
3. Prefer `data-testid` over CSS/XPath — most stable
4. If no data-testid, use accessible role + name (getByRole, getByLabel)
5. Only change the selector — do NOT alter test logic or assertions
6. Be conservative — if unsure, flag requires_human_review = true

Output ONLY valid JSON:
{{
  "healed_selector": "<new selector expression>",
  "selector_strategy": "data-testid | role | label | text | css",
  "reason": "<what changed in the DOM>",
  "confidence": <0.0–1.0>,
  "requires_human_review": <true|false>,
  "healed_code": "<complete healed test file content>"
}}
"""

API_SCHEMA_HEALING_PROMPT = """\
You are an expert API test automation engineer.

POSTMAN COLLECTION (current — broken):
{collection_json}

API SCHEMA DIFF (what changed):
{schema_diff}

INSTRUCTIONS:
1. Update request bodies to match new field names/types
2. Update test assertions to match new response schema
3. Update environment variable references if endpoint changed
4. Add new required fields with placeholder values
5. Mark removed assertions as skipped (not deleted — preserve history)

Output ONLY valid JSON Postman Collection v2.1. No prose.
"""

FLAKINESS_ANALYSIS_PROMPT = """\
You are a test reliability expert analyzing flaky test patterns.

EXECUTION HISTORY (last 10 runs):
{history_json}

TEST CODE:
{test_code}

Identify:
1. Is this test inherently flaky or environment-dependent?
2. What is the root cause of inconsistency?
3. Should we: add retry, add wait, fix assertion, quarantine, or mark as known-flaky?

Output JSON:
{{
  "flakiness_cause": "race_condition|timing|external_dependency|environment|assertion|data",
  "recommendation": "add_retry|add_wait|fix_assertion|quarantine|mark_known_flaky",
  "fix": "<specific code change to apply>",
  "confidence": <0.0–1.0>,
  "quarantine": <true|false>
}}
"""


class SelfHealingAgent:
    """
    Autonomously repairs broken automation tests.

    Triggered when ExecutionAgent reports failures classified as:
      - ENV: environment instability → add retry
      - TEST: selector failure → heal selector
      - SCHEMA: API field change → update collection
      - FLAKY: non-deterministic → quarantine or add wait
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str = "claude-sonnet-4-6",
        auto_pr: bool = True,
        github_token: str | None = None,
        require_human_approval: bool = False,
        min_confidence_for_queue: float = 0.65,
    ):
        self._client = client
        self._model = model
        self._auto_pr = auto_pr
        self._github_token = github_token
        self._require_human_approval = require_human_approval
        self._min_confidence_for_queue = min_confidence_for_queue

    # ── Public API ────────────────────────────────────────────────────────────

    async def heal_selector_failure(
        self,
        failed_test: dict,
        dom_before: str,
        dom_after: str,
    ) -> HealingAction:
        """
        Repair a Playwright test whose selector stopped matching.
        Compares before/after DOM snapshots to find equivalent element.
        """
        test_code = failed_test.get("script_content", "")
        error_msg = failed_test.get("error_message", "")

        # Extract the failed selector from error message
        selector_match = re.search(
            r"locator\(['\"]([^'\"]+)['\"]\)|getBy\w+\(['\"]([^'\"]+)['\"]\)",
            error_msg,
        )
        failed_selector = selector_match.group(1) or selector_match.group(2) \
                          if selector_match else "unknown"

        prompt = SELECTOR_HEALING_PROMPT.format(
            test_code=self._sanitize_for_prompt(test_code, max_len=3000),
            error_message=self._sanitize_for_prompt(error_msg),
            failed_selector=failed_selector,
            dom_before=self._sanitize_for_prompt(dom_before, max_len=2000),
            dom_after=self._sanitize_for_prompt(dom_after, max_len=2000),
        )

        message = await self._call_llm(
            model=self._model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )

        data = self._parse_json(message.content[0].text)
        healed_code = data.get("healed_code", test_code)
        confidence = float(data.get("confidence", 0.5))

        action = HealingAction(
            test_id=failed_test.get("test_id", ""),
            failure_type="selector",
            original_file=failed_test.get("script_path", ""),
            healed_content=healed_code,
            changes=[{
                "old": failed_selector,
                "new": data.get("healed_selector", ""),
                "reason": data.get("reason", ""),
            }],
            confidence=confidence,
            requires_human_review=data.get("requires_human_review", confidence < 0.7),
        )

        # Route: human approval queue OR auto-PR
        if (
            self._require_human_approval
            and action.confidence >= self._min_confidence_for_queue
        ):
            action.pr_branch = await self._queue_for_approval(action)
        elif self._auto_pr and not action.requires_human_review and self._github_token:
            action.pr_branch = await self._create_healing_pr(action)

        return action

    async def heal_api_schema_change(
        self,
        collection: dict,
        schema_diff: str,
    ) -> HealingAction:
        """
        Update a Newman/Postman collection to match a changed API schema.
        Used when OpenAPI spec is updated and tests break due to field renames.
        """
        prompt = API_SCHEMA_HEALING_PROMPT.format(
            collection_json=json.dumps(collection, indent=2)[:4000],
            schema_diff=schema_diff[:2000],
        )

        message = await self._call_llm(
            model=self._model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        healed_text = self._strip_fences(message.content[0].text)
        try:
            healed_collection = json.loads(healed_text)
            healed_content = json.dumps(healed_collection, indent=2)
            confidence = 0.85
        except json.JSONDecodeError:
            healed_content = json.dumps(collection, indent=2)  # Fallback
            confidence = 0.0

        return HealingAction(
            test_id=collection.get("info", {}).get("name", "unknown"),
            failure_type="api_schema",
            original_file="",
            healed_content=healed_content,
            changes=[{"reason": "API schema changed", "diff": schema_diff[:200]}],
            confidence=confidence,
            requires_human_review=confidence < 0.7,
        )

    async def detect_flaky_selectors(
        self,
        execution_history: list[dict],
    ) -> list[dict]:
        """
        Analyze execution history to identify flaky tests.
        Returns actionable recommendations per test.

        A test is flaky if: passes AND fails across runs with no code change.
        """
        from collections import defaultdict

        # Group results by test_id
        by_test: dict[str, list[dict]] = defaultdict(list)
        for result in execution_history:
            by_test[result["test_id"]].append(result)

        flaky_tests: list[dict] = []
        for test_id, runs in by_test.items():
            if len(runs) < 3:
                continue
            statuses = [r["status"] for r in runs]
            fail_rate = statuses.count("FAIL") / len(statuses)
            # 10–90% failure rate = flaky (not consistently passing or failing)
            if 0.10 < fail_rate < 0.90:
                flaky_tests.append({
                    "test_id": test_id,
                    "fail_rate": round(fail_rate, 2),
                    "run_count": len(runs),
                    "statuses": statuses[-5:],  # Last 5 runs
                })

        if not flaky_tests:
            return []

        # Analyze each flaky test
        analyses: list[dict] = []
        for ft in flaky_tests:
            history_json = json.dumps(ft, indent=2)
            # Find test code from last run
            last_run = next(
                (r for r in reversed(execution_history) if r["test_id"] == ft["test_id"]),
                {},
            )
            test_code = last_run.get("script_content", "# test code not available")

            prompt = FLAKINESS_ANALYSIS_PROMPT.format(
                history_json=history_json,
                test_code=test_code[:2000],
            )
            message = await self._call_llm(
                model=self._model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            analysis = self._parse_json(message.content[0].text)
            analyses.append({**ft, **analysis})

        return analyses

    async def apply_retry_policy(
        self,
        test_file_content: str,
        retry_count: int = 2,
    ) -> str:
        """
        Add retry logic to a Playwright test file for environment-flaky tests.
        Uses Playwright's built-in retries config.
        """
        if "retries:" in test_file_content or "test.describe.configure" in test_file_content:
            return test_file_content  # Already has retry

        # Add retries at describe block level
        retry_block = f"  test.describe.configure({{ retries: {retry_count} }});\n"
        # Insert after first describe( line
        return re.sub(
            r"(test\.describe\([^{]+\{)",
            r"\1\n" + retry_block,
            test_file_content,
            count=1,
        )

    # ── Generation Self-Healing ───────────────────────────────────────────────

    async def heal_generation_failures(
        self,
        failed_tests: list[dict],
        project_id: str,
    ) -> dict:
        """
        Diagnose and re-run LLM generation for tests that fell back to stub templates.

        Cost-optimized: uses rule-based classification (zero tokens) for known error
        types. Only calls Haiku for unknown errors. Calls Sonnet only when retrying
        actual script generation. Deduplicates by (requirement_id, tool) to avoid
        redundant LLM calls.

        Returns: {healed: int, queued: int, failed: int, proposals: list[str]}
        """
        import pathlib as _pathlib

        if not failed_tests:
            return {"healed": 0, "queued": 0, "failed": 0, "proposals": []}

        log.info("Generation healing: %d fallback tests to analyze for project %s", len(failed_tests), project_id)

        healed = 0
        queued = 0
        failed_count = 0
        proposals: list[str] = []

        # Deduplicate: one healing attempt per (requirement_id, tool)
        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for t in failed_tests:
            key = (t.get("requirement_id", ""), t.get("tool", "playwright"))
            if key not in seen:
                seen.add(key)
                deduped.append(t)

        # Separate known vs unknown failures for batching
        unknown_tests: list[dict] = []

        for test in deduped:
            failure = test.get("generation_failure") or {}
            error_type = failure.get("error_type", "Unknown")
            error_msg = failure.get("error_msg", "")
            tool = test.get("tool", "playwright")
            gherkin = test.get("gherkin", "")
            req_id = test.get("requirement_id", test.get("id", ""))

            root_cause, auto_fixable, hint = _classify_generation_failure(error_type, error_msg)

            if root_cause == "unknown":
                unknown_tests.append(test)
                continue

            if auto_fixable and gherkin and gherkin.strip() not in ("# No Gherkin generated", ""):
                # Retry generation via AutomationEngineerAgent.generate_single()
                try:
                    from .automation_engineer import AutomationEngineerAgent
                    auto_agent = AutomationEngineerAgent(self._client)
                    risk = {
                        "requirement_id": req_id,
                        "priority": test.get("priority", "P1"),
                        "risk_score": test.get("risk_score", 5.0),
                        "test_types": [test.get("test_type", "UI")],
                    }
                    script = await auto_agent.generate_single(gherkin, tool, risk)
                    if script and script.content and len(script.content) > 50:
                        # Write script to disk
                        script_path = test.get("script_path", "")
                        if script_path:
                            full_path = _pathlib.Path(script_path)
                            full_path.parent.mkdir(parents=True, exist_ok=True)
                            full_path.write_text(script.content)
                        # Update in-memory test entry
                        test["script_content"] = script.content
                        test["generation_source"] = "healed"
                        test["generation_failure"] = None
                        healed += 1
                    else:
                        pid = await self._queue_generation_proposal(test, hint + " — LLM retry returned empty output")
                        proposals.append(pid)
                        queued += 1
                except Exception as exc:
                    pid = await self._queue_generation_proposal(test, f"{hint} — retry failed: {exc}")
                    proposals.append(pid)
                    failed_count += 1
            else:
                # Not auto-fixable or no gherkin — queue informational proposal (zero LLM)
                pid = await self._queue_generation_proposal(test, hint)
                proposals.append(pid)
                queued += 1

        # Batched Haiku call for unknown failures (one call covers all unknowns)
        if unknown_tests:
            try:
                unknown_summary = "\n".join(
                    f"- req={t.get('requirement_id')} tool={t.get('tool')} "
                    f"error={t.get('generation_failure', {}).get('error_type', '?')}: "
                    f"{t.get('generation_failure', {}).get('error_msg', '')[:100]}"
                    for t in unknown_tests[:10]
                )
                msg = await self._call_llm(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=300,
                    messages=[{"role": "user", "content": (
                        "Classify these test generation failures. "
                        "Reply JSON: {\"root_cause\": \"rate_limit|timeout|auth|config|unknown\", "
                        "\"auto_fixable\": true/false, \"hint\": \"<one sentence action>\"}\n\n"
                        f"Failures:\n{unknown_summary}"
                    )}],
                )
                classification = self._parse_json(msg.content[0].text)
                hint_unknown = classification.get("hint", "Check backend logs for generation errors")
                for t in unknown_tests:
                    pid = await self._queue_generation_proposal(t, f"[LLM-diagnosed] {hint_unknown}")
                    proposals.append(pid)
                    queued += 1
            except Exception:
                for t in unknown_tests:
                    pid = await self._queue_generation_proposal(t, "Generation failure — check backend logs")
                    proposals.append(pid)
                    failed_count += 1

        return {"healed": healed, "queued": queued, "failed": failed_count, "proposals": proposals}

    async def _queue_generation_proposal(self, test: dict, reason: str) -> str:
        """Queue an informational HealingProposal for a generation failure. Zero LLM tokens."""
        import uuid as _uuid
        try:
            from src.api.routers.healing import PENDING_APPROVALS
        except ImportError:
            return "[QUEUED-NO-ROUTER]"

        proposal_id = f"gen-heal-{_uuid.uuid4().hex[:8]}"
        req_id = test.get("requirement_id", test.get("id", "unknown"))
        tool = test.get("tool", "playwright")
        stage = (test.get("generation_failure") or {}).get("stage", "unknown")

        PENDING_APPROVALS[proposal_id] = {
            "id": proposal_id,
            "test_id": test.get("id", req_id),
            "defect_id": None,
            "strategy": "generation",
            "confidence": 0.9,
            "ai_reasoning": reason,
            "current_selector": f"{req_id} / {tool} / stage={stage}",
            "proposed_selector": "Re-run /heal-tests after resolving the issue above",
            "diff_preview": f"Test uses fallback template — LLM generation failed at {stage}",
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        return proposal_id

    # ── Execution Self-Healing ────────────────────────────────────────────────

    async def heal_execution_failures(
        self,
        failed_results: list[dict],
        project_id: str,
    ) -> list[str]:
        """
        Analyze execution failures and queue healing proposals.

        Cost-optimized: rule-based classification (zero tokens) for noise/selector/auth.
        Haiku called only for unknown failures, batched per unique spec file.
        Sonnet used only when actual script patching is needed.

        Returns list of proposal IDs queued.
        """
        log.info("Execution healing: %d failed results to analyze for project %s", len(failed_results), project_id)
        proposals: list[str] = []

        # Group failures by spec file to batch LLM calls per-file
        by_file: dict[str, list[dict]] = {}
        for r in failed_results:
            script_path = r.get("script_path") or r.get("file") or ""
            if not script_path:
                try:
                    from src.api.routers.tests import GENERATED_TESTS
                    for t in GENERATED_TESTS:
                        if t.get("id") == r.get("test_id") or t.get("title") == r.get("title"):
                            script_path = t.get("script_path", "")
                            break
                except ImportError:
                    pass
            key = script_path or r.get("test_id", "unknown")
            by_file.setdefault(key, []).append(r)

        unknown_by_file: dict[str, list[dict]] = {}

        for file_key, file_failures in by_file.items():
            # k6 performance tests — use k6-specific healing
            tool = file_failures[0].get("automation_tool", "playwright")
            if tool == "k6":
                k6_proposals = await self._heal_k6_failures(file_key, file_failures)
                proposals.extend(k6_proposals)
                continue

            failure_types: list[tuple[str, bool]] = []
            for r in file_failures:
                err = r.get("error_message") or r.get("error") or r.get("logs") or ""
                ft, auto_fix = _classify_execution_failure(err)
                failure_types.append((ft, auto_fix))

            # "noise" — auto-patch the spec file (zero LLM tokens)
            if all(ft == "noise" for ft, _ in failure_types):
                patched = await self._patch_noise_filter(file_key, file_failures)
                if patched:
                    continue

            # "selector" — queue for human review via existing healing infrastructure
            # Fix GG: when ARTA_HEAL_SELECTORS_INLINE=1 is set, also attempt a
            # one-shot LLM patch + write to disk before queueing the proposal.
            # Bounded by ARTA_HEAL_MAX_ATTEMPTS (default 5) so a runaway run
            # with hundreds of selector failures doesn't burn LLM budget.
            import os as _os
            # Fix LL: default ON. Opt-out form preserves emergency disable
            # via ARTA_HEAL_SELECTORS_INLINE=0 while making the heal-and-retry
            # fire automatically. Bounded by ARTA_HEAL_MAX_ATTEMPTS=5.
            _heal_inline = _os.environ.get("ARTA_HEAL_SELECTORS_INLINE", "1") != "0"
            _heal_max = int(_os.environ.get("ARTA_HEAL_MAX_ATTEMPTS", "5"))
            _heals_applied = 0
            for r, (ft, _) in zip(file_failures, failure_types):
                if ft == "selector":
                    if _heal_inline and _heals_applied < _heal_max:
                        try:
                            patched = await self._attempt_inline_selector_patch(r, file_key)
                            if patched:
                                _heals_applied += 1
                                log.info("selector heal applied for %s in %s",
                                         r.get("test_id", "?"), file_key)
                        except Exception as _heal_exc:
                            log.warning("selector heal attempt failed: %s", _heal_exc)
                    pid = await self._queue_execution_proposal(r, "selector", file_key)
                    proposals.append(pid)

            # "auth" / "network" — queue informational proposals (zero LLM)
            for r, (ft, _) in zip(file_failures, failure_types):
                if ft in ("auth", "network"):
                    hint = (
                        "Authentication failure — verify credentials in Settings → Environments"
                        if ft == "auth" else
                        "Network error — verify base URL and connectivity in Settings → Environments"
                    )
                    pid = await self._queue_execution_proposal(r, ft, file_key, hint=hint)
                    proposals.append(pid)

            # "unknown" — collect for batched Haiku call
            unknowns = [r for r, (ft, _) in zip(file_failures, failure_types) if ft == "unknown"]
            if unknowns:
                unknown_by_file[file_key] = unknowns

        # Batched Haiku classification for unknown failures (one call per unique file)
        for file_key, unknowns in unknown_by_file.items():
            try:
                err_summary = " | ".join(
                    (r.get("error_message") or r.get("error") or "")[:80]
                    for r in unknowns[:3]
                )
                msg = await self._call_llm(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    messages=[{"role": "user", "content": (
                        "Classify this Playwright test failure in one word "
                        "(selector|auth|network|logic|data|timeout|unknown) "
                        "and give a one-sentence fix hint. "
                        f"JSON: {{\"type\": \"...\", \"hint\": \"...\"}}\n\nErrors: {err_summary}"
                    )}],
                )
                classification = self._parse_json(msg.content[0].text)
                hint = classification.get("hint", "Review test logic and application state")
                for r in unknowns:
                    pid = await self._queue_execution_proposal(r, "unknown", file_key,
                                                               hint=f"[LLM-diagnosed] {hint}")
                    proposals.append(pid)
            except Exception:
                for r in unknowns:
                    pid = await self._queue_execution_proposal(r, "unknown", file_key)
                    proposals.append(pid)

        return proposals

    async def _heal_k6_failures(self, file_key: str, failures: list[dict]) -> list[str]:
        """
        Auto-fix k6 performance test script issues.
        Rule-based fixes for common k6 problems (zero LLM tokens).
        Returns list of proposal IDs queued.
        """
        import pathlib as _pathlib

        proposals: list[str] = []

        # 1. Resolve file path — handle PERF- prefix from test_id
        script_path = _pathlib.Path(file_key)
        if (not script_path.exists() or script_path.suffix != ".js") and file_key.startswith("PERF-"):
            resolved = _pathlib.Path(f"src/automation/k6/{file_key[5:]}.js")
            if resolved.exists():
                script_path = resolved
                log.info("K6 healing: resolved %s → %s", file_key, resolved)

        if not script_path.exists() or script_path.suffix != ".js":
            log.warning("K6 healing: script not found at %s — queuing diagnostic proposal", file_key)
            for r in failures:
                pid = await self._queue_execution_proposal(
                    r, "k6_script_error", file_key,
                    hint=f"K6 script not found on disk: {file_key}. Regenerate tests to create the script.",
                )
                proposals.append(pid)
            return proposals

        if script_path.exists() and script_path.suffix == ".js":
            content = script_path.read_text()
            original = content
            # Fix camelCase executor names → kebab-case (k6 requirement)
            _executor_fixes = {
                "'rampingVUs'":           "'ramping-vus'",
                '"rampingVUs"':           '"ramping-vus"',
                "'constantVUs'":          "'constant-vus'",
                '"constantVUs"':          '"constant-vus"',
                "'rampingArrivalRate'":   "'ramping-arrival-rate'",
                '"rampingArrivalRate"':   '"ramping-arrival-rate"',
                "'constantArrivalRate'":  "'constant-arrival-rate'",
                '"constantArrivalRate"':  '"constant-arrival-rate"',
            }
            for old, new in _executor_fixes.items():
                content = content.replace(old, new)
            # Fix incomplete executor name: 'ramping' → 'ramping-vus'
            content = re.sub(
                r"""executor:\s*['"]ramping['"]""",
                "executor: 'ramping-vus'",
                content,
            )
            # Fix threshold syntax: p95<X → p(95)<X, p99<X → p(99)<X (k6 requires parens)
            content = re.sub(r'\bp95<', 'p(95)<', content)
            content = re.sub(r'\bp99<', 'p(99)<', content)
            content = re.sub(r'\bp90<', 'p(90)<', content)
            content = re.sub(r'\bp50<', 'p(50)<', content)
            if content != original:
                script_path.write_text(content)
                log.info("Auto-fixed k6 script: %s (executor names / threshold syntax)", script_path.name)
                pid = await self._queue_execution_proposal(
                    failures[0], "k6_executor", file_key,
                    hint=f"Auto-fixed k6 script issues (executor names / threshold syntax) in {script_path.name}",
                )
                proposals.append(pid)
                return proposals

        # 2. Analyze failure patterns for remaining issues
        for r in failures:
            err = r.get("error_message") or r.get("error") or ""
            duration = r.get("duration_ms", 0)
            actual = r.get("actual", {}) or {}

            if duration < 500:
                # Ultra-short duration = k6 crashed at validation
                pid = await self._queue_execution_proposal(
                    r, "k6_script_error", file_key,
                    hint=f"k6 crashed in {duration}ms (validation error). Check stderr: {err[:150]}",
                )
                proposals.append(pid)
            elif actual.get("p95_ms") is not None:
                # Threshold breach — k6 ran but SLA was exceeded
                p95 = actual["p95_ms"]
                threshold = (r.get("expected", {}) or {}).get("p95_threshold_ms", 3000)
                pid = await self._queue_execution_proposal(
                    r, "k6_threshold", file_key,
                    hint=f"SLA breach: p95={p95}ms exceeds {threshold}ms threshold. "
                         f"Check app performance or adjust threshold.",
                )
                proposals.append(pid)
            elif all(v is None for v in actual.values()):
                # No metrics at all — k6 never made HTTP requests
                pid = await self._queue_execution_proposal(
                    r, "network", file_key,
                    hint="k6 produced no HTTP metrics — target may be unreachable. "
                         "Check TARGET_BASE_URL in Settings → Environments.",
                )
                proposals.append(pid)

        return proposals

    async def _patch_noise_filter(self, script_path: str, failures: list[dict]) -> bool:
        """
        Auto-patch a Playwright spec file to filter ResizeObserver/Non-Error noise.
        No LLM call — simple string replacement with context-aware indentation.
        Returns True if patch was applied.
        """
        import pathlib as _pathlib
        try:
            path = _pathlib.Path(script_path)
            if not path.exists() or path.suffix != ".ts":
                return False
            content = path.read_text()
            old_listener = "page.on('pageerror', (err) => errors.push(err.message));"
            if old_listener not in content:
                return False
            # Detect indentation of the old line to produce correctly-indented replacement
            for line in content.splitlines():
                stripped = line.lstrip()
                if old_listener in stripped:
                    indent = line[: len(line) - len(stripped)]
                    break
            else:
                indent = "    "
            new_listener = (
                f"page.on('pageerror', (err) => {{\n"
                f"{indent}  if (!err.message.includes('ResizeObserver') && !err.message.includes('Non-Error')) {{\n"
                f"{indent}    errors.push(err.message);\n"
                f"{indent}  }}\n"
                f"{indent}}});"
            )
            content = content.replace(old_listener, new_listener)
            path.write_text(content)
            return True
        except Exception:
            pass
        return False

    async def _attempt_inline_selector_patch(
        self, result: dict, file_key: str
    ) -> bool:
        """Fix GG: attempt a one-shot LLM patch for a selector-class failure.

        Reads the spec file at file_key, sends it + the failing error to
        Haiku with a strict prompt asking for a corrected selector. If the
        LLM returns a sensible patch, write it back to disk (the runner's
        retry will pick up the fixed file on the next iteration).

        Bounded by the caller (ARTA_HEAL_MAX_ATTEMPTS). LLM-cost guard:
        single Haiku call per heal, ~1k tokens.

        Returns True if the spec file was modified, False otherwise.
        """
        from pathlib import Path as _Path
        spec_path = _Path(file_key)
        if not spec_path.is_file():
            return False
        original = spec_path.read_text()
        err = result.get("error_message") or result.get("error") or ""
        if not err:
            return False
        prompt = (
            "You are a Playwright test repair agent. The test below failed "
            "with the error message that follows. Identify the bad selector "
            "in the spec, propose a corrected selector, and emit the FULL "
            "corrected spec file as a single fenced ```typescript code block. "
            "Do not change anything besides the selectors that caused the "
            "failure. If multiple selectors look wrong, fix only the one "
            "implicated by the error.\n\n"
            f"=== FAILING ERROR ===\n{err[:600]}\n\n"
            f"=== SPEC FILE ===\n```typescript\n{original[:8000]}\n```\n"
        )
        try:
            msg = await self._call_llm(
                model="claude-haiku-4-5-20251001",
                max_tokens=8000,
                timeout=60.0,
                messages=[{"role": "user", "content": prompt}],
            )
            text = msg.content[0].text if hasattr(msg, "content") else str(msg)
            import re as _re
            m = _re.search(r"```(?:typescript|ts|javascript|js)?\s*\n(.*?)```",
                          text, _re.DOTALL)
            if not m:
                return False
            patched = m.group(1).strip()
            # Sanity: must still have at least one `test(` and not be
            # smaller than half the original (LLM may have truncated).
            if "test(" not in patched or len(patched) < len(original) * 0.5:
                return False
            spec_path.write_text(patched)
            return True
        except Exception:
            return False

    async def _queue_execution_proposal(
        self, result: dict, failure_type: str, file_key: str, hint: str | None = None
    ) -> str:
        """Queue a HealingProposal for an execution failure. Zero LLM tokens."""
        import uuid as _uuid
        try:
            from src.api.routers.healing import PENDING_APPROVALS
        except ImportError:
            return "[QUEUED-NO-ROUTER]"

        proposal_id = f"exec-heal-{_uuid.uuid4().hex[:8]}"
        err = result.get("error_message") or result.get("error") or ""
        default_hints = {
            "selector": "Element selector failed — UI may have changed. Review DOM and update selector.",
            "auth": "Authentication failure — verify credentials in Settings → Environments.",
            "network": "Network error — verify base URL and connectivity.",
            "timeout": "Test timed out — consider increasing timeout or checking app performance.",
        }
        reasoning = hint or default_hints.get(failure_type, "Test failed — review execution logs for details")

        PENDING_APPROVALS[proposal_id] = {
            "id": proposal_id,
            "test_id": result.get("test_id", "unknown"),
            "defect_id": None,
            "strategy": failure_type,
            "confidence": 0.7,
            "ai_reasoning": reasoning,
            "current_selector": err[:200],
            "proposed_selector": f"See: {file_key}",
            "diff_preview": f"Execution failure in {file_key}: {err[:150]}",
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z",
            # Phase 5.6: this run already failed and we're queueing a heal —
            # the gate must NOT pass on the broken-then-patched-but-not-rerun
            # state. Inline-heal patches the file but the in-flight runner is
            # already done; the only signal that matters is the next run.
            "requires_rerun": True,
        }
        return proposal_id

    # ── Approval Queue ────────────────────────────────────────────────────────

    async def _queue_for_approval(self, action: HealingAction) -> str:
        """
        Push a HealingProposal to the in-memory approval queue in healing.py.
        Returns a proposal_id string ("heal-<hex>").

        In production this would write to Redis so the queue survives restarts.
        The approval API (GET /api/healing/queue, POST /{id}/approve, etc.)
        reads from the same PENDING_APPROVALS dict.
        """
        import uuid as _uuid
        try:
            # Import the shared in-memory store from the healing router
            from src.api.routers.healing import PENDING_APPROVALS
        except ImportError:
            # Fallback: agent running outside FastAPI context
            return "[QUEUED-NO-ROUTER]"

        proposal_id = f"heal-{_uuid.uuid4().hex[:8]}"
        first_change = action.changes[0] if action.changes else {}

        PENDING_APPROVALS[proposal_id] = {
            "id": proposal_id,
            "test_id": action.test_id,
            "defect_id": None,
            "strategy": action.failure_type,
            "confidence": action.confidence,
            "ai_reasoning": first_change.get("reason", "Automated selector repair"),
            "current_selector": first_change.get("old", ""),
            "proposed_selector": first_change.get("new", ""),
            "diff_preview": action.healed_content[:300],
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z",
            # Phase 5.6: an approved selector heal patches the file on disk,
            # but the test the operator clicked Approve on is already failed
            # in this run. requires_rerun=True flags that the gate decision
            # for this build should be deferred until a re-run reflects the
            # patched selectors.
            "requires_rerun": True,
        }
        return proposal_id

    # ── PR Creation ───────────────────────────────────────────────────────────

    async def _create_healing_pr(self, action: HealingAction) -> str:
        """
        Create a GitHub PR with healed test files via GitHub REST API v3.
        Returns the PR branch name, or a "[SKIPPED]" string if env vars absent.

        Steps:
          1. GET  /repos/{owner}/{repo}/git/ref/heads/{default_branch} → HEAD SHA
          2. POST /repos/{owner}/{repo}/git/refs → create new branch from HEAD
          3. PUT  /repos/{owner}/{repo}/contents/{path} → upload healed file
          4. POST /repos/{owner}/{repo}/pulls → open PR
        """
        token = self._github_token or os.environ.get("GITHUB_TOKEN", "")
        repo = os.environ.get("GITHUB_REPO", "")  # e.g. "owner/repo-name"

        if not token or not repo:
            return "[SKIPPED] GITHUB_TOKEN or GITHUB_REPO not configured"

        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        branch = f"arta/self-heal-{action.test_id}-{timestamp}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        base_url = f"https://api.github.com/repos/{repo}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Step 1: Resolve HEAD SHA of the default branch
                r = await client.get(f"{base_url}/git/ref/heads/main", headers=headers)
                if r.status_code == 404:
                    # Try master if main not found
                    r = await client.get(f"{base_url}/git/ref/heads/master", headers=headers)
                r.raise_for_status()
                head_sha = r.json()["object"]["sha"]

                # Step 2: Create healing branch
                r = await client.post(
                    f"{base_url}/git/refs",
                    headers=headers,
                    json={"ref": f"refs/heads/{branch}", "sha": head_sha},
                )
                r.raise_for_status()

                # Step 3: Upload healed file (GET existing SHA if file already exists)
                file_path = action.original_file.lstrip("/")
                existing_sha: str | None = None
                r = await client.get(
                    f"{base_url}/contents/{file_path}",
                    headers=headers,
                    params={"ref": branch},
                )
                if r.status_code == 200:
                    existing_sha = r.json().get("sha")

                encoded = base64.b64encode(action.healed_content.encode()).decode()
                payload: dict = {
                    "message": f"fix(auto-heal): repair selector in {file_path}\n\nARTA Self-Healing Agent — confidence {action.confidence:.0%}",
                    "content": encoded,
                    "branch": branch,
                }
                if existing_sha:
                    payload["sha"] = existing_sha

                r = await client.put(
                    f"{base_url}/contents/{file_path}",
                    headers=headers,
                    json=payload,
                )
                r.raise_for_status()

                # Step 4: Open PR
                changes_summary = "; ".join(
                    f"{c.get('old', '')} → {c.get('new', '')}" for c in action.changes
                )
                r = await client.post(
                    f"{base_url}/pulls",
                    headers=headers,
                    json={
                        "title": f"[ARTA Auto-Heal] {action.test_id}: {action.failure_type} fix",
                        "body": (
                            f"## ARTA Self-Healing PR\n\n"
                            f"**Test ID:** `{action.test_id}`\n"
                            f"**Failure type:** `{action.failure_type}`\n"
                            f"**Confidence:** {action.confidence:.0%}\n\n"
                            f"### Changes\n{changes_summary}\n\n"
                            f"> Auto-generated by ARTA Self-Healing Agent. "
                            f"Please review before merging."
                        ),
                        "head": branch,
                        "base": "main",
                        "draft": action.confidence < 0.85,
                    },
                )
                r.raise_for_status()

        except httpx.HTTPStatusError as exc:
            return f"[ERROR] GitHub API {exc.response.status_code}: {exc.response.text[:200]}"
        except httpx.RequestError as exc:
            return f"[ERROR] GitHub API request failed: {exc}"

        return branch

    # ── Helpers ───────────────────────────────────────────────────────────────

    @retry(
        # R134.C — single-source-of-truth retry tuple covers anthropic.* SDK
        # errors, RuntimeError (ClaudeCLI/Ollama subprocess failures per
        # Gap-2), AND httpx transient network errors.
        retry=retry_if_exception_type(LLM_RETRYABLE_EXC),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _call_llm(self, **kwargs):
        """Wrapper around messages.create() with exponential-backoff retry + circuit breaker."""
        # Gap-2: Route through circuit breaker so degraded provider fails fast.
        from .circuit_breaker import get_breaker
        provider_name = getattr(self._client, "provider", None) or type(self._client).__name__
        breaker = get_breaker(str(provider_name))
        return await breaker.call(self._client.messages.create, **kwargs)

    # ── Phase G2 + G3 — chain-aware heal patterns ─────────────────────────────

    async def auto_apply_heal(self, proposal_id: str) -> bool:
        """Phase K12 — bypass operator approval for high-confidence (≥0.85)
        heal proposals.

        IMPORTANT — wiring contract: the healing router (src/api/routers/healing.py)
        only consumes `status="approved"` for the rerun-executor and the
        gate's pending-heal-rerun check. We set `status="approved"` to
        match that contract, AND stamp an `auto_approved=True` audit flag
        so dashboards / audit trails can distinguish operator-approved
        from system-auto-approved heals.

        Used by `_queue_chain_heal_proposals` (post_run_chain_pipeline.py)
        when `chain_reverify` (default confidence 0.85) carries the
        threshold. Provider-contract heals stay below the threshold by
        design (need operator review of the new jsonpath).

        Operators can disable globally via env `ARTA_K12_AUTO_HEAL=0`.
        """
        try:
            from ..api.routers.healing import PENDING_APPROVALS
        except ImportError:
            return False

        # Global kill-switch for ops emergencies.
        import os as _os
        if _os.environ.get("ARTA_K12_AUTO_HEAL", "1") == "0":
            return False

        proposal = PENDING_APPROVALS.get(proposal_id)
        if not isinstance(proposal, dict):
            return False

        # Phase L5 — circuit breaker. Cap per-(project, run) auto-applies
        # at 3 to prevent infinite heal-rerun-fail loops. Keys live on the
        # PENDING_APPROVALS dict under a reserved `_auto_heal_counters` slot
        # so they survive within the same process / run boundary but don't
        # need a separate persistence layer.
        #
        # Phase M8 — KNOWN LIMITATION (in-memory persistence): the
        # `_auto_heal_counters` slot lives on the PENDING_APPROVALS dict,
        # which is process-local and resets on API restart. A run that
        # hangs mid-heal-loop and is then restarted (operator intervention,
        # crash recovery, deploy) re-enters with a clean counter and could
        # auto-heal up to 3 more times on the SAME run. Worst case: a
        # post-restart run cycles +3 auto-heals before naturally hitting
        # the cap again.
        #
        # Acceptable for v1 because:
        #   - Restart implies operator intervention; they'll see the
        #     prior counter state via `requires_rerun=True` anyway.
        #   - The counter resets, so worst case is bounded (+3, not infinite).
        #   - Persistence to Redis/PG is a follow-up if real-world
        #     incidents prove this insufficient.
        project_id = proposal.get("project_id") or ""
        run_id = proposal.get("run_id") or ""
        counter_key = f"{project_id}:{run_id}"
        counters = PENDING_APPROVALS.setdefault("_auto_heal_counters", {})
        if not isinstance(counters, dict):
            counters = {}
            PENDING_APPROVALS["_auto_heal_counters"] = counters
        prior = counters.get(counter_key, 0)
        if prior >= 3:
            log.warning(
                "L5: auto-heal cap reached (3) for %s — falling back to operator approval. "
                "Repeated heals on the same run usually mean the heal logic itself is wrong; "
                "human review prevents an infinite cycle.",
                counter_key,
            )
            return False
        counters[counter_key] = prior + 1

        proposal["status"] = "approved"   # matches downstream contract
        proposal["auto_approved"] = True
        proposal["auto_approved_at"] = datetime.utcnow().isoformat() + "Z"
        proposal["auto_approved_reason"] = (
            f"K12 auto-heal: confidence={proposal.get('confidence', 0)} ≥ 0.85"
        )

        # R38.4 — for test_gen_regen, K12's selector-based rerun executor
        # is the wrong consumer (no selector to apply). Drop a regen-queue
        # marker per affected test so the next gen cycle picks them up.
        # Mirrors the manual approve path in healing.approve_proposal.
        if proposal.get("type") == "test_gen_regen":
            try:
                from pathlib import Path as _Path
                import json as _json
                queue_dir = _Path(".arta/regen_queue")
                queue_dir.mkdir(parents=True, exist_ok=True)
                affected = proposal.get("affected_tests") or []
                payload_base = {
                    "proposal_id": proposal_id,
                    "triage_category": proposal.get("triage_category"),
                    "status_code": proposal.get("status_code"),
                    "signals": list(proposal.get("signals") or []),
                    "sample_error": proposal.get("sample_error", ""),
                    "queued_at": datetime.utcnow().isoformat() + "Z",
                    "auto_approved": True,
                }
                queued = 0
                for test_id in affected:
                    if not test_id:
                        continue
                    try:
                        (queue_dir / f"{test_id}.json").write_text(
                            _json.dumps({**payload_base, "test_id": test_id}, indent=2),
                        )
                        queued += 1
                    except Exception as _marker_exc:
                        log.debug("R38.4: K12 marker write failed for %s: %s",
                                  test_id, _marker_exc)
                log.info(
                    "R38.4 (K12): test_gen_regen %s auto-approved — "
                    "queued %d/%d regen markers (triage=%s status=%s)",
                    proposal_id, queued, len(affected),
                    proposal.get("triage_category"),
                    proposal.get("status_code"),
                )
            except Exception as _regen_exc:
                log.warning("R38.4 (K12): regen queueing failed for %s: %s",
                            proposal_id, _regen_exc)

        # The requires_rerun=True flag remains; the existing rerun executor
        # will pick up these proposals identically to manually-approved ones.
        return True

    async def propose_chain_reverify(
        self,
        *,
        root_cause_test_id: str,
        affected_tests: list[str],
        via_var: str | None = None,
        approval_window_s: int = 600,
    ) -> str:
        """Phase G2: queue a `chain_reverify` PENDING_APPROVAL.

        Triggered when an upstream provider test heals + the operator
        approves the fix. This proposal asks for permission to auto-rerun
        the cascade-affected tests so the gate can re-evaluate them
        against the patched provider.

        DR-8: snapshots `affected_tests` at approval time so concurrent
        operator edits don't poison the rerun. Caps cascade depth at 3
        (handled by the rerun executor — proposals carry the full list,
        but the executor processes only direct dependents in one pass).
        """
        import uuid as _uuid
        try:
            from src.api.routers.healing import PENDING_APPROVALS
        except ImportError:
            return "[QUEUED-NO-ROUTER]"

        proposal_id = f"chain-reverify-{_uuid.uuid4().hex[:8]}"
        PENDING_APPROVALS[proposal_id] = {
            "id": proposal_id,
            "type": "chain_reverify",
            "test_id": root_cause_test_id,
            "root_cause_test_id": root_cause_test_id,
            "affected_tests": list(affected_tests),
            "affected_count": len(affected_tests),
            "via_var": via_var,
            "strategy": "chain_reverify",
            "confidence": 0.85,
            "ai_reasoning": (
                f"Provider test {root_cause_test_id} healed. "
                f"{len(affected_tests)} downstream tests cascaded from this "
                f"failure (linked via {via_var}). Approve to auto-rerun them "
                f"so the gate can re-evaluate against the patched provider."
            ),
            "diff_preview": (
                f"Will rerun: {', '.join(affected_tests[:5])}"
                + (f" + {len(affected_tests) - 5} more" if len(affected_tests) > 5 else "")
            ),
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "approval_window_s": approval_window_s,
            # Phase 5.6 plumbing — gate must defer until rerun completes.
            "requires_rerun": True,
        }
        return proposal_id

    async def propose_url_drift_heal(
        self,
        *,
        test_id: str,
        old_path: str,
        new_path: str,
        evidence: dict | None = None,
    ) -> str:
        """Phase G2: queue a `url_drift` PENDING_APPROVAL.

        When a Newman test fails 4xx and the captured-endpoint store
        (Phase B/C) shows the same logical endpoint at a different path
        (e.g., `/v1/datasets/{id}` → `/v2/datasets/{id}`), propose a heal
        that updates the consumer's URL template.

        Operator approval triggers the rewrite + chain_reverify cascade.
        """
        import uuid as _uuid
        try:
            from src.api.routers.healing import PENDING_APPROVALS
        except ImportError:
            return "[QUEUED-NO-ROUTER]"

        proposal_id = f"url-drift-{_uuid.uuid4().hex[:8]}"
        PENDING_APPROVALS[proposal_id] = {
            "id": proposal_id,
            "type": "url_drift",
            "test_id": test_id,
            "old_path": old_path,
            "new_path": new_path,
            "strategy": "url_drift",
            "confidence": 0.8,
            "ai_reasoning": (
                f"Test {test_id} called {old_path} and got an error response, "
                f"but the discovery store shows the SUT now serves the same "
                f"logical endpoint at {new_path}. The path appears to have "
                f"been moved (likely an API version bump). Approve to update "
                f"the test."
            ),
            "current_selector": old_path,
            "proposed_selector": new_path,
            "diff_preview": f"{old_path} → {new_path}",
            "evidence": evidence or {},
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "requires_rerun": True,
        }
        return proposal_id

    async def propose_provider_contract_heal(
        self,
        *,
        test_id: str,
        var_name: str,
        old_jsonpath: str,
        new_jsonpath: str | None = None,
        provider_response_shape: dict | None = None,
    ) -> str:
        """Phase G3: queue a `provider_contract_heal` PENDING_APPROVAL.

        When Phase F1 flags a `provider_contract_violation`, this proposes
        the consumer-side fix — usually a jsonpath update from `$.id` to
        the actual response field path. When `new_jsonpath` is None, the
        operator chooses from `provider_response_shape` candidates in the
        UI.
        """
        import uuid as _uuid
        try:
            from src.api.routers.healing import PENDING_APPROVALS
        except ImportError:
            return "[QUEUED-NO-ROUTER]"

        proposal_id = f"contract-heal-{_uuid.uuid4().hex[:8]}"
        PENDING_APPROVALS[proposal_id] = {
            "id": proposal_id,
            "type": "provider_contract_heal",
            "test_id": test_id,
            "var_name": var_name,
            "old_jsonpath": old_jsonpath,
            "new_jsonpath": new_jsonpath,
            "provider_response_shape": provider_response_shape or {},
            "strategy": "provider_contract_heal",
            "confidence": 0.75 if new_jsonpath else 0.5,
            "ai_reasoning": (
                f"The provider for {var_name} passed but its response did "
                f"not contain the expected jsonpath {old_jsonpath}. "
                + (
                    f"Discovery captured a new shape with {new_jsonpath} — "
                    f"approve to update the consumer."
                    if new_jsonpath
                    else "Operator: choose the new path from the captured "
                         "response shape in the UI."
                )
            ),
            "current_selector": old_jsonpath,
            "proposed_selector": new_jsonpath or "[operator-chosen]",
            "diff_preview": f"{old_jsonpath} → {new_jsonpath or '?'}",
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "requires_rerun": True,
        }
        return proposal_id

    async def propose_test_gen_regen(
        self,
        *,
        test_id: str,
        affected_tests: list[str],
        triage_category: str,
        status_code: int | None,
        signals: list[str] | None,
        sample_error: str = "",
        approval_window_s: int = 600,
    ) -> str:
        """R37.1 — queue a `test_gen_regen` PENDING_APPROVAL.

        Triggered when triage classifies a defect as `test_gen_bug`
        (e.g. 415 OpenAPI placement, hallucinated selectors, missing
        imports). Approving the proposal feeds the failure text back to
        the generator as a Tier-2 hint and regenerates the offending
        spec. Confidence 0.85 so the K12 auto-apply threshold fires.
        """
        import uuid as _uuid
        try:
            from src.api.routers.healing import PENDING_APPROVALS
        except ImportError:
            return "[QUEUED-NO-ROUTER]"

        proposal_id = f"test-gen-regen-{_uuid.uuid4().hex[:8]}"
        sig_text = ", ".join((signals or [])[:3]) or "test_gen_bug"
        reason_bits = []
        if status_code:
            reason_bits.append(f"status={status_code}")
        if signals:
            reason_bits.append(f"signals={sig_text}")
        sig_label = "; ".join(reason_bits) or "test_gen_bug"

        PENDING_APPROVALS[proposal_id] = {
            "id": proposal_id,
            "type": "test_gen_regen",
            "test_id": test_id,
            "affected_tests": list(affected_tests),
            "affected_count": len(affected_tests),
            "triage_category": triage_category,
            "status_code": status_code,
            "signals": list(signals or []),
            "sample_error": (sample_error or "")[:300],
            "strategy": "test_gen_regen",
            "confidence": 0.85,
            "ai_reasoning": (
                f"Triage classified {len(affected_tests)} test(s) as "
                f"test_gen_bug ({sig_label}). The failure mode is in the "
                f"GENERATED test, not the SUT. Approve to regenerate the "
                f"spec(s) with the runtime failure text fed back as a "
                f"Tier-2 hint so the generator avoids the same bug."
            ),
            "diff_preview": (
                f"Will regenerate: {', '.join(affected_tests[:3])}"
                + (f" + {len(affected_tests) - 3} more" if len(affected_tests) > 3 else "")
            ),
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "approval_window_s": approval_window_s,
            "requires_rerun": True,
        }
        return proposal_id

    @staticmethod
    def _sanitize_for_prompt(text: str, max_len: int = 4000) -> str:
        """Strip prompt injection patterns and enforce length limit."""
        text = str(text)[:max_len]
        for pattern in ["ignore previous", "disregard above", "new instructions"]:
            text = re.sub(pattern, "[REDACTED]", text, flags=re.IGNORECASE)
        return text

    def _parse_json(self, text: str) -> dict:
        """R134.D — delegates to shared SSoT extractor (fail-soft contract:
        returns {} on extraction failure per existing self_healing
        callers' expectations)."""
        from .json_extract import extract_json_from_llm_output
        return extract_json_from_llm_output(text, default={})

    def _strip_fences(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        return text.strip()
