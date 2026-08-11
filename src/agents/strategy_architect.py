"""
ARTA Strategy Architect Agent — TEA Layer 2: Risk-Based Test Design

Implements the BMAD TEA risk scoring methodology:
  Impact (1-3: Minor / Degraded / Critical)
  Probability (1-3: Unlikely / Possible / Likely)
  Risk Score = Impact × Probability → 1-9
  P0 ≥8 · P1 6-7 · P2 4-5 · P3 ≤3
  Score = 9 → automatic gate FAIL
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .retry_policy import LLM_RETRYABLE_EXC   # R134.C — single-source-of-truth retry tuple

log = logging.getLogger("arta.strategy")


# BMAD TEA risk threshold actions
RISK_THRESHOLDS = {
    "DOCUMENT": (1, 3),   # Score 1-3: document and accept
    "MONITOR":  (4, 5),   # Score 4-5: monitor actively
    "MITIGATE": (6, 8),   # Score 6-8: require mitigation plan
    "AUTO_FAIL": (9, 9),  # Score 9: automatic gate FAIL
}


def classify_priority(score: int) -> str:
    """Classify risk score into BMAD TEA priority tier."""
    if score >= 8:
        return "P0"
    if score >= 6:
        return "P1"
    if score >= 4:
        return "P2"
    return "P3"


def risk_action(score: int) -> str:
    """Return BMAD TEA risk action for a given score."""
    for action, (lo, hi) in RISK_THRESHOLDS.items():
        if lo <= score <= hi:
            return action
    return "DOCUMENT"


# Phase 1.4: Authoritative tool catalogue. Any tool the LLM "recommends" outside
# this set is dropped with a warning so Layer 4 never receives a name it can't
# instantiate. Sourced from TOOL_SELECTION_RULES in automation_engineer; kept as
# a local constant to avoid the cross-module import cycle.
_VALID_TOOLS: frozenset[str] = frozenset({
    "playwright", "selenium", "cypress",
    "newman", "k6", "zap", "axe",
    "appium", "pytest", "preflight",
})


# ── R211 Phase A2 — architecture-grounded test-type refinement ────────────────
# The keyword/priority floor (Phase L1 K9/K10 + M4) is the safety net; this
# REFINES it with REAL architecture signals (mapped endpoints, protocol,
# mutation, live DOM catalog). ADDITIVE only — it never drops a type the floor
# chose, so a wrong architecture signal can't reduce coverage. Pure function
# (no I/O) → unit-testable; the caller supplies the discovery-derived booleans.
def architecture_ground_test_types(
    test_types: list[str],
    *,
    has_api_endpoints: bool = False,
    has_dom_catalog: bool = False,
    is_mutation: bool = False,
) -> list[str]:
    out = list(test_types or [])

    def _add(t: str) -> None:
        if t not in out:
            out.append(t)

    if has_api_endpoints:
        _add("API")            # a real mapped endpoint → contract/api testing applies
    if is_mutation:
        _add("Performance")    # create/upload endpoints warrant load characterization
    if has_dom_catalog:
        _add("UI")             # a real authenticated DOM catalog → UI is reachable
        _add("Accessibility")  # … and an a11y surface exists to assert against
    return out


# Substring families used to tag a profile's protocols from its mapped/captured
# endpoints (the specifics steer the per-protocol generator/helper; the test
# TYPE stays "API"). Kept here so adding a family is a one-line Python change.
_PROTOCOL_PATH_HINTS: dict[str, tuple[str, ...]] = {
    "sse":     ("/stream", "/events", "/sse", "text/event-stream"),
    "graphql": ("/graphql", "/gql"),
    "grpc":    ("/grpc", ".grpc"),
    "ws":      ("/ws", "/websocket", "/socket"),
}


# ── R211 Phase B1 — AC-level mutation intent (declared at the requirement) ────
# A requirement/AC whose behavior CREATES/UPLOADS/CONFIGURES SUT state is an
# action-workflow — its happy path can only be tested by mutating the SUT, which
# R154 default-denies unless the operator opts in (ARTA_R154_ALLOW_DESTRUCTIVE_TESTS
# + SUT_TEST_DATA_NAMESPACE). Declaring the intent HERE (from the AC text) makes
# the policy originate at the requirement, not a downstream code guess. The flag
# only GATES Phase-E action-chain emission (which ALSO requires the R154 env), so
# a conservative over-mark is harmless (no mutation without the env opt-in).
_MUTATION_VERBS = (
    "create", "upload", "configure", "delete", "update", "insert", "remove",
    "register", "provision", "launch", "import", "modify", "submit", "publish",
    "enable", "disable", "assign", "revoke", "generate-upload",
)
# match the verb STEM + common inflections (creates/uploaded/configuring) but
# keep a word boundary so "creative"/"updater" don't false-match.
_MUTATION_VERB_RE = __import__("re").compile(
    r"\b(" + "|".join(v.replace("-", r"\-") for v in _MUTATION_VERBS)
    + r")(?:s|es|ed|ing|d)?\b",
    __import__("re").IGNORECASE)


def detect_mutation_intent(text: str) -> dict:
    """R211 B1 — classify a requirement/AC as state-mutating from its text.
    Returns {destructive: bool, verbs: [...], sandbox_namespace: None}. The
    namespace is filled from SUT_TEST_DATA_NAMESPACE at gen/dispatch (R154)."""
    found = sorted({m.group(1).lower() for m in _MUTATION_VERB_RE.finditer(text or "")})
    return {"destructive": bool(found), "verbs": found, "sandbox_namespace": None}


def derive_protocols(endpoints: list[dict] | None) -> list[str]:
    """R211 A2 — tag the protocol families present in a requirement's mapped
    endpoints (default ['rest'] when endpoints exist but none match a special
    family; [] when there are no endpoints)."""
    eps = endpoints or []
    if not eps:
        return []
    found: list[str] = []
    for e in eps:
        p = str((e or {}).get("path") if isinstance(e, dict) else e or "").lower()
        for fam, hints in _PROTOCOL_PATH_HINTS.items():
            if fam not in found and any(h in p for h in hints):
                found.append(fam)
    if not found:
        found.append("rest")
    return found


_REGULATORY_KEYWORDS: dict[str, list[str]] = {
    "GDPR":  ["gdpr", "personal data", "data subject", "right to erasure", "right to be forgotten",
              "data protection", "consent", "privacy", "pii", "personally identifiable"],
    "SOX":   ["sox", "sarbanes", "financial reporting", "internal controls", "audit trail",
              "material weakness", "revenue recognition"],
    "PCI":   ["pci", "payment card", "cardholder data", "cvv", "pan number", "credit card",
              "debit card", "card number", "pci-dss"],
    "HIPAA": ["hipaa", "protected health", "phi", "health information", "patient data",
              "medical record", "covered entity"],
}


def _detect_regulatory_tags(text: str) -> list[str]:
    """Keyword scan — no LLM. Returns sorted list of detected regulation labels."""
    lower = text.lower()
    return sorted(tag for tag, kws in _REGULATORY_KEYWORDS.items() if any(kw in lower for kw in kws))


async def _historical_failure_rate(req_id: str) -> float | None:
    """Fix OOO + Phase 1.5: per-requirement FAIL rate over the prior 30 days.

    Returns:
        - `float` in [0.0, 1.0] when at least one execution_result was found.
        - `None` when there's no signal (DB unavailable, query error, no test_id
          ever ran). Callers MUST distinguish "no signal" from "0.0 actual rate"
          so a healthy unstoried requirement isn't bumped by the closed-loop.

    Joins execution_results → test_cases → requirements via UUID FKs (per
    db/models.py: er.test_case_id → tc.id, tc.requirement_id → r.id).
    """
    if not req_id:
        return None
    try:
        from sqlalchemy import text as _sql_text
        from ..db.session import async_session_factory
        async with async_session_factory() as db:
            row = (await db.execute(_sql_text("""
                SELECT
                    count(*) FILTER (WHERE er.status::text = 'FAIL') AS fails,
                    count(*) AS total
                FROM execution_results er
                JOIN test_cases tc ON er.test_case_id = tc.id
                JOIN requirements r ON tc.requirement_id = r.id
                WHERE r.req_id = :req
                  AND er.executed_at > NOW() - INTERVAL '30 days'
            """), {"req": req_id})).first()
            if not row or not row.total:
                # No history → no signal. Keep distinct from 0.0 so callers
                # don't conflate "test always passed" with "test never ran".
                return None
            return float(row.fails) / float(row.total)
    except Exception as exc:
        # DB unavailable or query failed — explicit warning so operators see
        # that the closed-loop is silently disabled, then None signal.
        log.warning(
            "Fix OOO: historical-rate lookup unavailable for %s (%s: %s) — "
            "closed-loop bump disabled for this run",
            req_id, type(exc).__name__, exc,
        )
        return None


@dataclass
class RiskProfile:
    requirement_id: str
    priority: str           # P0 / P1 / P2 / P3
    risk_score: int         # 1-9 (Impact × Probability)
    impact: int             # 1-3: Minor / Degraded / Critical
    probability: int        # 1-3: Unlikely / Possible / Likely
    risk_action: str        # DOCUMENT / MONITOR / MITIGATE / AUTO_FAIL
    rationale: str
    test_types: list[str]   # ["UI", "API", "Performance", "Security"]
    coverage_target_pct: int
    recommended_tools: list[str]
    regulatory_tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Phase A3 / A4 (per plan DR-6): degraded RiskProfile signals to the
    # quality gate (`_check_strategy_degraded`) and admin "Strategy review"
    # queue that this profile was salvaged from a malformed LLM payload —
    # operator should regenerate or hand-edit before relying on it.
    degraded: bool = False
    degraded_reason: str = ""
    # ── R211 Phase A — unified Test-Plan fields ──────────────────────────────
    # The RiskProfile IS the single per-requirement plan object (it already
    # flows orchestrator → atdd_designer → automation_engineer). These fields
    # are ENRICHED post-scoring with discovery/architecture context
    # (`enrich_profile_with_architecture`); defaults keep full back-compat so
    # an un-enriched profile behaves exactly as before.
    endpoints: list[dict] = field(default_factory=list)       # [{method, path, score}] — real mapped endpoints
    workflow_chain: list[dict] = field(default_factory=list)  # ordered chain nodes (R200 cm / R195 FK)
    data_needs: dict = field(default_factory=dict)            # {bodies, fixtures, ids}
    mutation: dict = field(default_factory=dict)              # {destructive: bool, sandbox_namespace: str|None}
    protocols: list[str] = field(default_factory=list)        # [rest|sse|grpc|graphql|ws|mq]
    ungroundable: bool = False                                # True → 0 real endpoints match → truthful BLOCK


TEA_RISK_PROMPT = """\
You are a TEA (Test Engineering Architecture) Risk Analyst — expert in \
BMAD methodology risk-based testing.

Analyze this requirement and produce a structured risk assessment.

REQUIREMENT:
{requirement_text}

METADATA:
- Product type: {product_type}
- Regulatory requirements: {regulations}
- Module historical defect rate: {defect_rate}%
- Recent code changes: {recent_changes}
- Integration dependencies: {dependencies}

BMAD TEA RISK SCORING (two dimensions only):

IMPACT (1-3):
  1 = Minor    — cosmetic, workaround exists, <5% users affected
  2 = Degraded — feature impaired, no workaround for some users, revenue impact <$10K
  3 = Critical — total failure, data loss/breach, regulatory violation, >$100K exposure

PROBABILITY (1-3):
  1 = Unlikely — mature code, no recent changes, strong test history
  2 = Possible — moderate complexity, some recent changes, partial test coverage
  3 = Likely   — new/rewritten code, high complexity, external dependencies, poor test history

FORMULA: risk_score = impact × probability → range 1-9

RISK ACTION THRESHOLDS:
  1-3  → DOCUMENT (accept and document)
  4-5  → MONITOR  (actively monitor)
  6-8  → MITIGATE (require mitigation plan + owner)
  9    → AUTO_FAIL (automatic quality gate FAIL — cannot ship)

PRIORITY: P0 ≥8 | P1 6-7 | P2 4-5 | P3 ≤3

Determine ALL applicable test types based on the requirement context. You MUST return an array containing every relevant test type.
- If it mentions endpoints, webhooks, or APIs, include "API" (tool: newman).
- Phase K10: include "Performance" (tool: k6) when ANY of:
  - Requirement mentions SLAs, concurrency, throughput, latency, or speed
  - Requirement involves analytics endpoints, paginated lists, or search queries (these scale with data)
  - Requirement is P0 or P1 (high-impact endpoints deserve perf coverage by default)
  - Requirement involves bulk operations (uploads, exports, batch writes)
- Phase K9: include "Security" (tool: zap) when ANY of:
  - Requirement mentions auth, roles, RBAC, sessions, tokens, JWT, OAuth
  - Requirement involves user-supplied data (forms, uploads, query strings, body params)
  - Requirement involves persistence of user data (DB writes, file storage, S3)
  - Requirement involves analytics, PII, regulated data (PCI/HIPAA/GDPR)
  - Requirement is P0 or P1 (high-impact endpoints deserve security coverage)
- "UI" (tool: playwright) is usually always included for frontend apps.
- F19-4: If it mentions UI elements (forms, buttons, navigation, modals,
  tables, dashboards, embeddable widgets) OR explicitly mentions accessibility,
  WCAG, a11y, ARIA, screen-reader, keyboard-navigation, color-contrast,
  include "Accessibility" (tool: axe). Default for any user-facing UI
  requirement at P0/P1 priority — UI without a11y testing is incomplete.

[CRITICAL] Output ONLY the JSON object below. No text before or after. No markdown code fences (no ```). No "Here is" or "The risk assessment is" preamble. Start your response with `{{` and end with `}}`.
Output ONLY valid JSON — no prose:
{{
  "requirement_id": "{req_id}",
  "impact": <1|2|3>,
  "probability": <1|2|3>,
  "risk_score": <1-9>,
  "priority": "P0|P1|P2|P3",
  "risk_action": "DOCUMENT|MONITOR|MITIGATE|AUTO_FAIL",
  "rationale": "<2–3 sentence justification referencing impact and probability factors>",
  "test_types": ["UI", "API", "Accessibility"],
  "coverage_target_pct": <80|90|95|100>,
  "recommended_tools": ["playwright", "newman", "axe"]
}}
"""


class StrategyArchitectAgent:
    """
    TEA Strategy Architect Agent.
    Produces risk profiles and test strategy for each requirement.
    """

    def __init__(self, client: AsyncAnthropic, model: str = "claude-haiku-4-5-20251001"):
        # B1: Default to Haiku — risk scoring outputs a small structured JSON.
        # Sonnet is overkill (~20x cost). With Ollama, this routes to ARTA_FAST_MODEL.
        self._client = client
        self._model = model

    async def score_risks(self, requirements: list[dict]) -> list[RiskProfile]:
        """Score all requirements concurrently. Logs (not silently drops) failures."""
        import asyncio
        # F7-4: span the whole risk-scoring stage so the BMAD TEA pipeline shows
        # one trace from request → strategy → atdd → automation. No-op when
        # ARTA_TRACING_ENABLED is off.
        from ..observability.tracing import span as _trace_span
        with _trace_span("strategy.score_risks",
                         model=self._model,
                         requirement_count=len(requirements)):
            results = await asyncio.gather(
                *[self._score_single(req) for req in requirements],
                return_exceptions=True,
            )
            # A2: Log each failed requirement so silent drops are visible.
            valid: list[RiskProfile] = []
            for req, result in zip(requirements, results):
                if isinstance(result, RiskProfile):
                    valid.append(result)
                elif isinstance(result, Exception):
                    log.warning(
                        "Risk scoring failed for %s (%s: %s) — excluded from this pass",
                        req.get("id") or req.get("req_id", "?"),
                        type(result).__name__, result,
                    )
                # else: shouldn't happen but skip silently
            return valid

    def persist_strategy(
        self,
        profiles: list[RiskProfile],
        project_id: str | None,
        trace_id: str | None = None,
        prompt_version: str | None = None,
    ) -> str | None:
        """F5-2: Write a strategy artifact to .arta/strategies/ so risk decisions
        are auditable + replayable. Returns the path written, or None on failure.
        """
        from datetime import datetime, timezone
        from pathlib import Path
        import json
        import os
        import uuid

        if not profiles:
            return None

        out_dir = Path(os.environ.get("ARTA_STRATEGIES_DIR", ".arta/strategies"))
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("Could not create strategies dir %s: %s", out_dir, e)
            return None

        tid = trace_id or str(uuid.uuid4())[:8]
        pid = (project_id or "global").replace("/", "_")
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = out_dir / f"{pid}_{ts}_{tid}.json"

        # Risk distribution histogram
        dist: dict[str, int] = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
        top_risks: list[dict] = []
        for p in profiles:
            dist[p.priority] = dist.get(p.priority, 0) + 1
            if p.risk_score >= 6:
                top_risks.append({
                    "requirement_id": p.requirement_id,
                    "priority": p.priority,
                    "risk_score": p.risk_score,
                    "risk_action": p.risk_action,
                    "rationale": p.rationale[:200],
                })

        artifact = {
            "schema_version": "1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project_id": project_id,
            "trace_id": tid,
            "model": self._model,
            "prompt_version": prompt_version,
            "requirements_scored": len(profiles),
            "risk_distribution": dist,
            "top_risks_score_ge_6": sorted(top_risks, key=lambda x: -x["risk_score"]),
            "compliance_markers": sorted({tag for p in profiles for tag in p.regulatory_tags}),
            # Layer 1 deliverables: Testing Pyramid + Tool Chain
            "testing_pyramid": self.build_test_pyramid(profiles),
            "tool_chain": sorted({tool for p in profiles for tool in p.recommended_tools}),
            # Layer 9: per-requirement mitigation plans (consumed by gate's
            # _check_risk_concerns for score 6-8 → CONCERNS).
            "mitigation_plans": {},
            "profiles": [
                {
                    "requirement_id": p.requirement_id,
                    "priority": p.priority,
                    "risk_score": p.risk_score,
                    "impact": p.impact,
                    "probability": p.probability,
                    "risk_action": p.risk_action,
                    "test_types": p.test_types,
                    "coverage_target_pct": p.coverage_target_pct,
                    "recommended_tools": p.recommended_tools,
                    "regulatory_tags": p.regulatory_tags,
                    # F8-4: Stamp trace_id on each profile so cohort queries
                    # (`MATCH (p:RiskProfile {trace_id:$tid})`) work without
                    # joining back through the artifact wrapper.
                    "trace_id": tid,
                }
                for p in profiles
            ],
        }
        try:
            path.write_text(json.dumps(artifact, indent=2))
            log.info("Strategy artifact persisted: %s (%d requirements)", path, len(profiles))
            return str(path)
        except Exception as e:
            log.warning("Failed to write strategy artifact %s: %s", path, e)
            return None

    async def _score_single(self, req: dict) -> RiskProfile:
        prompt = TEA_RISK_PROMPT.format(
            req_id=req.get("id", "UNKNOWN"),
            requirement_text=self._sanitize_for_prompt(
                req.get("description", req.get("title", ""))
            ),
            product_type=req.get("product_type", "SaaS web application"),
            regulations=", ".join(req.get("regulations", [])) or "none",
            defect_rate=req.get("historical_defect_rate_pct", 0),
            recent_changes=req.get("days_since_last_change", "unknown"),
            dependencies=", ".join(req.get("external_dependencies", [])) or "none",
        )

        # Enrich prompt with source code context if available
        code_context = req.get("_code_context", "")
        if code_context:
            prompt += f"\n\n# Source Code Context (from project repositories — use this to identify actual endpoints, patterns, and dependencies):\n{code_context[:4000]}"

        # R215.T (same rationale as defect_intel._analyze_single): risk scoring
        # is a single-shot STRUCTURED-JSON call, but the TEA prompt's "analyze
        # the risk" framing makes Claude Code attempt tool calls with tools
        # available → it burns the whole --max-turns budget across multiple
        # `--continue` turns → each attempt overruns the 120s wait_for in
        # tests.py → TimeoutError → risk_scoring_failed → 0 tests. `--tools
        # none` lets the model answer directly in one turn. Guarded to the CLI
        # transport — the Anthropic/LiteLLM SDK paths reject the unknown kwarg.
        _cli_extra: dict = {}
        if type(getattr(self, "_client", None)).__name__ == "ClaudeCLIClient":
            _cli_extra["disable_tools"] = True

        message = await self._call_llm(
            model=self._model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            **_cli_extra,
        )

        # Phase A4 (per plan DR-6): malformed LLM JSON used to crash the whole
        # `_score_single` and abort the entire pipeline. Now we return a degraded
        # RiskProfile so the pipeline keeps moving and the operator can see the
        # failure in the admin Strategy-review queue + gate `_check_strategy_degraded`.
        try:
            data: dict[str, Any] = self._extract_json(message.content[0].text)
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning(
                "strategy.json_parse_failed req=%s error=%r raw=%r — returning degraded profile",
                req.get("id", "?"), exc, message.content[0].text[:300],
            )
            return RiskProfile(
                requirement_id=req.get("id", ""),
                priority="P2",
                risk_score=4,
                impact=2,
                probability=2,
                risk_action=risk_action(4),
                rationale="DEGRADED: LLM returned malformed JSON; score defaulted to medium pending operator review.",
                test_types=["UI"],
                coverage_target_pct=80,
                recommended_tools=["playwright"],
                regulatory_tags=_detect_regulatory_tags(
                    req.get("description", "") + " " + req.get("title", "")
                ),
                warnings=[f"risk_llm_malformed_json: {type(exc).__name__}"],
                degraded=True,
                degraded_reason="risk_llm_malformed_json",
            )

        impact = max(1, min(3, int(data.get("impact", 2))))
        probability = max(1, min(3, int(data.get("probability", 2))))
        # Phase 1.4: BMAD TEA score is `impact × probability` — computed in code,
        # not trusted from the LLM. Log when the LLM-returned `risk_score`
        # doesn't match (drift detector); the in-code value always wins.
        score = impact * probability
        try:
            llm_score = int(data.get("risk_score", score))
        except (TypeError, ValueError):
            llm_score = score
        if llm_score != score:
            log.warning(
                "LLM risk drift for %s: returned risk_score=%d but impact(%d)*probability(%d)=%d "
                "— using deterministic value",
                req.get("id") or data.get("requirement_id", "?"),
                llm_score, impact, probability, score,
            )

        # Fix OOO — closed-loop risk: requirements that fail in production
        # historically deserve elevated priority regardless of LLM baseline.
        # Phase 1.5: hist_rate is `None` when there's no signal (DB down or
        # no run history) — only bump on a confirmed > 30% rate, never on
        # absence of data.
        rationale_text: str = str(data.get("rationale", ""))
        req_id_for_history = req.get("id") or data.get("requirement_id") or ""
        hist_rate = await _historical_failure_rate(req_id_for_history)
        if hist_rate is not None and hist_rate > 0.3 and score < 9:
            new_score = min(9, score + 1)
            log.info(
                "Fix OOO: bumped risk_score %d→%d for %s due to historical FAIL rate %.0f%%",
                score, new_score, req_id_for_history, hist_rate * 100,
            )
            score = new_score
            rationale_text = (rationale_text + f" Historical FAIL rate {hist_rate:.0%} → bumped +1.").strip()

        # Validate test_types — default to ["UI"] if missing or empty to prevent KeyError cascade.
        # Phase A3 (per DR-6): also stamp `degraded` so the gate + admin Strategy-review queue
        # see this requirement needs operator follow-up — log+continue alone hides the silent
        # downstream effect (only-UI tests) until run time.
        test_types: list[str] = list(data.get("test_types") or [])

        # Phase L1 — deterministic Security/Performance triggers. The LLM
        # complies with K9/K10 prompt language inconsistently (run-89e80da6
        # showed 7/19 ZAP coverage despite the prompt saying "include
        # Security for all P0/P1"). This post-process augment GUARANTEES
        # the floor regardless of LLM behavior.
        priority_str = str(data.get("priority", "") or "P2").upper()
        req_text_full = (
            (req.get("description", "") or "") + " "
            + (req.get("title", "") or "")
        ).lower()
        sec_triggers = (
            "auth", "login", "role", "rbac", "session", "token", "jwt", "oauth", "sso",
            "form", "upload", "submit",
            "persist", "store", "save", "database", "s3",
            "pii", "regulated", "hipaa", "gdpr", "pci",
            "user-supplied", "user input", "permission",
        )
        perf_triggers = (
            "sla", "latency", "throughput", "concurrent", "speed",
            "p95", "p99", "performance",
            "list", "search", "query", "paginat", "analytic",
            "bulk", "batch", "export",
        )
        is_high_priority = priority_str in ("P0", "P1")
        if is_high_priority or any(t in req_text_full for t in sec_triggers):
            if "Security" not in test_types:
                test_types.append("Security")
        if is_high_priority or any(t in req_text_full for t in perf_triggers):
            if "Performance" not in test_types:
                test_types.append("Performance")
        profile_warnings: list[str] = []
        profile_degraded = False
        profile_degraded_reason = ""
        if not test_types:
            log.warning(
                "strategy.test_types_empty req=%s payload_keys=%s — defaulting to ['UI']",
                data.get("requirement_id", "?"),
                sorted(data.keys()) if isinstance(data, dict) else "<not_dict>",
            )
            test_types = ["UI"]
            profile_warnings.append(
                "test_types_defaulted_to_UI: risk scoring LLM returned no test_types — "
                "downstream will only generate UI tests unless this is corrected"
            )
            profile_degraded = True
            profile_degraded_reason = "test_types_empty_from_llm"

        # Phase 1.4: Cross-check against the registered tool catalogue. The LLM
        # occasionally returns "wapiti" / "burp" / "selenium-grid" — Layer 4 has
        # no handler for those, so they'd silently produce zero scripts. Drop
        # unknown tools with a warning the gate can see; default to ["playwright"]
        # only when the entire list is empty/invalid (safe fallback for UI tests).
        raw_tools: list[str] = data.get("recommended_tools") or []
        recommended_tools: list[str] = []
        unknown_tools: list[str] = []
        for tool in raw_tools:
            tname = str(tool).strip().lower()
            if not tname:
                continue
            if tname in _VALID_TOOLS:
                if tname not in recommended_tools:
                    recommended_tools.append(tname)
            else:
                unknown_tools.append(tname)
        if unknown_tools:
            log.warning(
                "LLM recommended unknown tool(s) for %s: %s — dropped (valid: %s)",
                req.get("id") or data.get("requirement_id", "?"),
                unknown_tools, sorted(_VALID_TOOLS),
            )
            profile_warnings.append(
                f"unknown_tools_dropped: {unknown_tools} are not in the registered "
                f"tool catalogue; downstream generators ignore them"
            )
        if not recommended_tools:
            recommended_tools = ["playwright"]

        # Phase M4 — sync recommended_tools with augmented test_types.
        # L1's deterministic augment adds "Security"/"Performance" to
        # test_types AFTER the LLM has emitted recommended_tools.
        # Downstream tool dispatch (`automation_engineer`) reads from
        # recommended_tools; without this sync, L1 promised 17/19 ZAP
        # coverage but delivered 7/19 because adding "Security" never
        # caused "zap" to land in recommended_tools.
        if "Security" in test_types and "zap" not in recommended_tools and "zap" in _VALID_TOOLS:
            recommended_tools.append("zap")
        if "Performance" in test_types and "k6" not in recommended_tools and "k6" in _VALID_TOOLS:
            recommended_tools.append("k6")

        return RiskProfile(
            requirement_id=data.get("requirement_id", ""),
            priority=classify_priority(score),
            risk_score=score,
            impact=impact,
            probability=probability,
            risk_action=risk_action(score),
            rationale=rationale_text,
            test_types=test_types,
            coverage_target_pct=int(data.get("coverage_target_pct", 80)),
            recommended_tools=recommended_tools,
            regulatory_tags=_detect_regulatory_tags(
                req.get("description", "") + " " + req.get("title", "")
            ),
            warnings=profile_warnings,
            degraded=profile_degraded,
            degraded_reason=profile_degraded_reason,
        )

    @retry(
        # R134.C — single-source-of-truth retry tuple covers anthropic.* SDK
        # errors, RuntimeError (ClaudeCLI/Ollama subprocess failures per A1),
        # AND httpx transient network errors.
        retry=retry_if_exception_type(LLM_RETRYABLE_EXC),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _call_llm(self, **kwargs):
        """Wrapper around messages.create() with exponential-backoff retry + circuit breaker."""
        # I8: Route through circuit breaker so a degraded provider fails fast (~50ms)
        # after 5 failures in 60s, instead of grinding through hours of doomed retries.
        from .circuit_breaker import get_breaker
        provider_name = getattr(self._client, "provider", None) or type(self._client).__name__
        breaker = get_breaker(str(provider_name))
        return await breaker.call(self._client.messages.create, **kwargs)

    @staticmethod
    def _sanitize_for_prompt(text: str, max_len: int = 4000) -> str:
        """Strip prompt injection patterns and enforce length limit."""
        text = str(text)[:max_len]
        for pattern in ["ignore previous", "disregard above", "new instructions"]:
            text = re.sub(pattern, "[REDACTED]", text, flags=re.IGNORECASE)
        return text

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """R134.D — delegates to shared SSoT extractor. Adds raise-on-failure
        behavior preserved from the pre-R134.D contract (callers expect a
        dict; raise allows them to surface gen failures truthfully)."""
        from .json_extract import extract_json_from_llm_output
        result = extract_json_from_llm_output(text, default=None)
        if result is None:
            raise ValueError(f"No valid JSON found in LLM output: {(text or '')[:200]}")
        return result

    def build_test_pyramid(self, profiles: list[RiskProfile]) -> dict:
        """
        Derive testing pyramid allocation from risk profiles.
        BMAD TEA 1-9 risk scale drives allocation:
          Score 9 (AUTO_FAIL) → max coverage on all tiers
          Score 6-8 (MITIGATE) → heavy API/Performance/Security
          Score 4-5 (MONITOR) → standard pyramid
          Score 1-3 (DOCUMENT) → unit-heavy, minimal E2E
        """
        auto_fail = sum(1 for p in profiles if p.risk_score == 9)
        mitigate = sum(1 for p in profiles if 6 <= p.risk_score <= 8)
        total = max(len(profiles), 1)
        high_risk_ratio = (auto_fail + mitigate) / total

        return {
            "unit":        {"pct": 30, "owner": "developer"},
            "integration": {"pct": 25, "owner": "developer+arta"},
            "api":         {"pct": 25 + high_risk_ratio * 10, "owner": "arta"},
            "ui_e2e":      {"pct": 10 + (auto_fail / total) * 5, "owner": "arta"},
            "performance": {"pct": 5 + high_risk_ratio * 3, "owner": "arta"},
            "security":    {"pct": 5 + high_risk_ratio * 3, "owner": "arta"},
        }
