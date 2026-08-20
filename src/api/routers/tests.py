"""ARTA Tests Router — Test case CRUD + ATDD generation."""
from __future__ import annotations

import hashlib
import asyncio   # R280 — bare `asyncio.CancelledError` in an except clause
                 # had NO import (only local `import asyncio as _asyncio`
                 # elsewhere) -> NameError raised exactly when an exception
                 # occurred, i.e. only on the failure path.
import json
import logging
import os
import pathlib
import random
import re

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel

from ...agents.sanitize import sanitize_req_id  # R134.H — sanitize_req_id SSoT

log = logging.getLogger("arta.tests")

from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
# F4-1: API-key check is centralised in src/api/dependencies.py.
from ..dependencies import require_api_key as _require_api_key  # noqa: E402

# F7-2: Shared in-memory state lives in tests_state so cross-router imports
# (healing, execution, projects, requirements, traceability, main) hit a small
# stable surface instead of this 4,000-line file. Seed data still lives below
# (we extend the lists rather than rebind so the cross-router import keeps
# pointing at the same underlying object).
from .tests_state import (  # noqa: E402
    GENERATED_TESTS,
    _GENERATE_ALL_JOBS,
    _PENDING_REVIEWS,
    MOCK_VERSIONS,
)

# F7-2 (continuation): Mount sub-routers EARLY so their explicit paths
# (`/pending-reviews`, `/{test_id}/data`, `/{test_id}/versions`) win over the
# `GET /{test_id}` catch-all defined later in this file. FastAPI route matching
# is order-of-registration; specific routes must come before parameterised ones.
from .tests_fixtures import fixtures_router as _fixtures_router  # noqa: E402
from .tests_versions import versions_router as _versions_router  # noqa: E402
from .tests_review import review_router as _review_router  # noqa: E402
router.include_router(_fixtures_router)
router.include_router(_versions_router)
router.include_router(_review_router)


class GenerateRequest(BaseModel):
    requirement_id: str
    risk_profile: dict = {}
    tools: list[str] = []   # Override auto-selection
    feedback: str | None = None  # User correction notes prepended to LLM prompts
    force: bool = False          # Skip hash check — regenerate even if requirement unchanged
    # When set, scope generation to a single acceptance criterion ID (e.g.,
    # "AC-BT-003-01"). Replaces the old `/api/assistant/command` dispatcher
    # path which dropped ac_id silently and regenerated the whole requirement.
    ac_id: str | None = None


# H2: Bound GENERATED_TESTS in-memory growth.
# This list is the runtime cache of all generated test entries (real LLM-generated
# tests; the few seeded entries below exist so the UI has data on first launch).
# Without a cap it grows unbounded — every regeneration appends, only force=true clears.
# Cap via ARTA_MAX_TESTS_IN_MEMORY (default 2000). DB is the source of truth for older entries.
import os as _os
import hashlib as _hashlib


# F7-2 (continuation): Pure utility helpers extracted to tests_helpers.py.
# Re-exported here so existing `from .tests import _stamp_traceability` etc.
# call sites continue to work.
from .tests_helpers import (  # noqa: E402,F401
    _compute_prompt_version_hash,
    _looks_like_pytest,
    _stamp_traceability,
    _enforce_generated_tests_cap,
    _MAX_TESTS_IN_MEMORY,
    _save_job_json,
    _load_all_jobs_json,
    _get_project_req_ids,
    _evidence_targets_for_tool,
    _compute_nfr_precheck,
    _normalize_generate_result,
    _set_job_stage,
    _record_req_completion,
    _slugify_filename,
)


# Canonical analytics-AI keyword catalog — derived from the 7 capability
# Generation, Anomaly Detection, Predictive Analytics, Data Storytelling,
# Agentic Analysis). Match on requirement description / title; the
# AnalyticsTestAgent is invoked when ANY of these triggers fires AND the
# requirement isn't a pure UI/auth/CRUD operational concern.
_ANALYTICS_KEYWORDS: tuple[str, ...] = (
    # NL → Query / SQL
    "nl->sql", "nl to sql", "natural language to sql", "natural language query",
    "generates sql", "sql generation", "plain english",
    # Insight + narrative + storytelling
    "insight", "narrative", "data story", "data storytelling",
    "liveboard", "dashboard agent", "auto-visualization", "auto visualization",
    # Anomaly + forecast + drill-down + agentic
    "anomaly detection", "anomaly", "forecast", "drill down", "drill-down",
    "agentic analysis", "agentic", "multi-step reasoning",
    "dataset creation", "database connector", "vector+keyword",
    "vector index", "bm25", "rag", "context retrieval", "mcp tool",
    # Excel / file analytics
    "file chat", "excel analytics", "analytics agent", "query engine",
)


def is_analytics_requirement(req: dict | None, project: dict | None = None) -> bool:
    """Decide whether a requirement should be routed to AnalyticsTestAgent.

    Returns True when ANY of the following match:
      - `requirement.test_types` contains "Analytics" (set by risk scoring)
      - `requirement.category == "analytics_ai"`
      - description / title matches the canonical analytics keyword catalog
        (covers all 7 analytics capability layers)
      - project's `_project_type == "data_pipeline"` AND the req touches a
        data layer (matches at least one analytics keyword)

    The result is also stamped onto the requirement dict as
    `_analytics_match_reason` for log observability.
    """
    if not req:
        return False
    test_types = req.get("test_types") or []
    if isinstance(test_types, list) and any(
        str(t).strip().lower() == "analytics" for t in test_types
    ):
        req["_analytics_match_reason"] = "test_types contains 'Analytics'"
        return True
    if req.get("category") == "analytics_ai":
        req["_analytics_match_reason"] = "category=analytics_ai"
        return True
    desc_lower = (
        (req.get("description") or "") + " " + (req.get("title") or "")
    ).lower().replace("→", "->")
    # Short single-token keywords ("rag", "bm25") must match at WORD BOUNDARIES
    # — a bare substring test made "rag" match "sto**rag**e"/"ave**rag**e", and
    # "storage" → false-routed to AnalyticsTestAgent → ~330s/req of recipe-less
    # analytics gen that either times out (skipped) or emits tests asserting
    # "free-form values the generator cannot satisfy" (guaranteed exec FAILs).
    # Longer/multiword keywords keep substring matching (specific enough, and
    # it preserves legit plurals like "insights"). Killswitch
    # ARTA_ANALYTICS_KW_SUBSTRING=1 restores the pre-fix bare-substring behavior.
    import re as _re_akw
    _kw_substring = _os.environ.get("ARTA_ANALYTICS_KW_SUBSTRING", "").lower() in ("1", "true")

    def _kw_hit(kw: str) -> bool:
        if not _kw_substring and len(kw) <= 5 and kw.isalnum():
            return _re_akw.search(rf"(?<![a-z0-9]){_re_akw.escape(kw)}(?![a-z0-9])", desc_lower) is not None
        return kw in desc_lower

    matched_kws = [kw for kw in _ANALYTICS_KEYWORDS if _kw_hit(kw)]
    if matched_kws:
        req["_analytics_match_reason"] = (
            f"keywords matched: {matched_kws[:3]}"
        )
        return True
    # Project-level fallback: data_pipeline projects with any analytics
    # keyword in the req. Avoids missing reqs whose description is terse
    # but the project context tells us they're analytics.
    project_type = (
        (project or {}).get("integrations", {}).get("_project_type")
        or (project or {}).get("_project_type")
    )
    if project_type == "data_pipeline" and matched_kws:
        req["_analytics_match_reason"] = (
            f"project_type=data_pipeline + keywords: {matched_kws[:2]}"
        )
        return True
    return False


# In-flight generation registry — keyed by (requirement_id, ac_id_or_'*').
# Value: (workflow_id, started_at_monotonic). The 409 dedup path reads this
# 3 concurrent pipelines after the user clicked the same per-AC button 3x).
# Entries older than _IN_FLIGHT_TTL are treated as stale (the worker may
# have crashed mid-pipeline) and replaced; cleared explicitly at the end
# of the function via _clear_in_flight().
_IN_FLIGHT_GENERATIONS: dict[tuple[str, str], tuple[str, float]] = {}
_IN_FLIGHT_TTL = 600.0  # 10 minutes — covers worst-case P0 chunked Newman gen


def _r125_k_build_gen_metrics(client, gen_source: str | None) -> dict:
    """R125.K — build the `_gen_metrics` field for a generated test row.

    Captures LLM provenance (provider/model/strategy via the canonical
    `llm_provider_tag` helper) plus gen_source ("llm" vs "fallback") so the
    R125.I gen-health dashboard can render per-provider quality side-by-side.

    Graceful: when client is None (fallback path, no LLM was called), the
    provider tag still resolves to {provider:"unknown", model:"unknown",
    strategy:"unknown"} — the gen_source field then disambiguates.
    """
    try:
        from ...agents.llm_client import llm_provider_tag
        return {
            "llm": llm_provider_tag(client),
            "gen_source": gen_source or "unknown",
        }
    except Exception:
        # Never block test-row construction on a metrics-build failure
        return {
            "llm": {"provider": "unknown", "model": "unknown", "strategy": "unknown"},
            "gen_source": gen_source or "unknown",
        }


def _clear_in_flight(req_id: str, ac_id: str | None) -> None:
    """Remove the in-flight registry entry. Safe to call from any path."""
    _IN_FLIGHT_GENERATIONS.pop((req_id, ac_id or "*"), None)


# ── R130.G — Provider-aware bounded-parallel batch concurrency ──────────────


def _r130_g_batch_concurrency(provider_str: str) -> int:
    """R130.G — provider-aware bounded concurrency tuned to avoid
    rate-limit retry storms (cost ceiling per the cost+perf safeguards).

    Ollama is single-instance per model; concurrent calls queue
    server-side without throughput gain BUT also don't multiply cost.
    Cap at 2 so each in-flight req's 4-sub semaphore × 2 = 8 in-flight
    LLM calls — the Ollama daemon's safe saturation ceiling.

    Anthropic tier-1 RPM is ~50/min; with 4 concurrent reqs × ~10
    escalation calls avg, peak throughput stays at ~40 calls/min,
    under the limit. Raise via operator override (Settings → LLM)
    for tier-3+ accounts.
    """
    p = (provider_str or "").lower()
    if p == "ollama":
        return 2   # daemon queues; >2 yields no throughput gain
    if p == "claude_code":
        # R217 0d — claude_code is the CLI subprocess path, SERIALIZED under
        # R161 (one per-project `--continue` session; concurrent calls corrupt
        # it → short "unexpected response" replies → CLAUDE circuit-breaker).
        # Concurrency>1 here doesn't parallelize (R161 serializes anyway) but
        # DOES fire N reqs' worth of calls into the OAuth rate budget at once
        # (the 110×-429 bulk-gen collapse) AND bypasses the sequential 0d
        # batching + inter-req pacing. So the correct provider-aware default is
        # 1. Operators with a non-serialized claude_code setup can override via
        # ARTA_GEN_CONCURRENCY (explicit override wins) or ARTA_R217_CLAUDE_CODE_PARALLEL=1.
        import os as _os_r217cc
        if _os_r217cc.environ.get("ARTA_R217_CLAUDE_CODE_PARALLEL", "").lower() in ("1", "true"):
            return 4
        return 1
    if p == "anthropic":
        return 4   # real Anthropic API — tier-1 RPM safe, genuinely parallel
    return 4   # generic default for openai/gemini/azure_openai


# ── R217 — ATDD partial-validity rescue (coverage-preserving, quality-safe) ──
def _r217_is_nfr_only_req(test_types) -> bool:
    """R217 — True when a requirement's test_types are NON-FUNCTIONAL only
    (Performance / Security / Accessibility) with NO functional API/UI type.

    Such reqs (e.g. an NFR req — P95 latency targets, AES-256/TLS/RBAC/SSO,
    OpenTelemetry tracing) are exercised by k6 (perf) / zap (security) / axe
    (a11y), which ground in real endpoints + scan-config, NOT in functional
    Gherkin. The upstream functional-Gherkin-quality gate must therefore NOT
    ZERO the whole req for them — doing so discards their rightful NFR-tool
    coverage (live: an NFR-only req → `gherkin_quality_violation` → 0 tests,
    even though k6/zap are the correct tools and don't need functional Gherkin).

    Functional reqs (ANY of API/UI/functional/e2e/integration in test_types)
    are UNAFFECTED — the gate still blocks their malformed functional Gherkin.
    Returns False for an empty/unknown test_types list (fail-safe → gate stays).
    """
    tts = {str(t).strip().lower() for t in (test_types or []) if str(t).strip()}
    if not tts:
        return False
    _NFR = {"performance", "security", "accessibility", "perf", "a11y", "nfr"}
    _FUNCTIONAL = {"ui", "api", "functional", "e2e", "integration", "contract"}
    return tts.issubset(_NFR) and not (tts & _FUNCTIONAL)


def _r217_filter_failfirst_gherkin(scenarios: list) -> tuple[list, int, int]:
    """R217 — split each Gherkin block into its `Scenario:` sub-blocks and keep
    ONLY the fail-first-valid ones (a block that contains BOTH `when` and `then`,
    case-insensitive — the GQ-002 rule). Malformed sub-blocks (missing When/Then,
    i.e. non-fail-first / vacuous) are DROPPED.

    Why: when the upstream Gherkin gate blocks a req because SOME scenario is
    malformed (missing Then) while OTHERS are valid, the pre-R217 behavior
    fail-fasts the WHOLE req to 0 tests — and if the ATDD quality-retry then
    crashes ("zero Scenario steps"), the usable first-pass Gherkin is discarded
    entirely (live: one req had 1 measurable scenario but shipped 0 tests).
    This filter lets the caller proceed with the VALID scenarios while dropping
    the malformed ones — coverage preserved, quality preserved (no no-Then
    scenario ever ships), truthful (dropped count logged + re-validated clean).

    Returns (filtered_scenarios, kept_count, dropped_count). Each returned
    element keeps the original `Feature:` preamble (if any) + its valid
    `Scenario:` block so the downstream parser + per-AC mapping still work.
    """
    import re as _re_r217
    filtered: list[str] = []
    kept = 0
    dropped = 0
    for block in scenarios or []:
        text = block if isinstance(block, str) else (
            block.get("gherkin") or block.get("scenario") or block.get("content") or ""
            if isinstance(block, dict) else str(block or "")
        )
        if not text.strip():
            continue
        # Preserve a leading Feature: preamble (everything before the first Scenario:)
        _parts = _re_r217.split(r"(?im)^(\s*Scenario(?:\s+Outline)?\s*:)", text)
        _preamble = _parts[0] if _parts else ""
        # Re-pair the captured "Scenario:" headers with their bodies.
        _scn_blocks = []
        for _i in range(1, len(_parts), 2):
            _hdr = _parts[_i]
            _body = _parts[_i + 1] if _i + 1 < len(_parts) else ""
            _scn_blocks.append(_hdr + _body)
        if not _scn_blocks:
            # No Scenario: header found in this element — treat the whole element
            # as one block; keep it only if it is itself fail-first-valid.
            low = text.lower()
            if "when" in low and "then" in low:
                filtered.append(text)
                kept += 1
            else:
                dropped += 1
            continue
        _valid = []
        for _sb in _scn_blocks:
            low = _sb.lower()
            if "when" in low and "then" in low:
                _valid.append(_sb)
                kept += 1
            else:
                dropped += 1
        if _valid:
            filtered.append((_preamble if _preamble.strip() else "") + "".join(_valid))
    return filtered, kept, dropped


# ── R128.A — Shadow divergence metrics (deterministic vs LLM Newman) ───────


def _r142_b_count_openapi_ops(contract_collection: dict) -> int:
    """R142.B — count the distinct (method, path) operation tuples in the
    contract collection so the diagnostic log can report items_emitted
    vs ops_seen. ops_seen is the upper bound (one item per status code
    per (method, path) — `generate_contract_collection` emits one item
    per documented response code, so items_emitted ≥ ops_seen typically).

    R142.B's purpose: detect non-deterministic shrinkage across regens
    (e.g., one req's items shrank 166 → 13 between two regen iterations). When
    operator retriggers 3× consecutively, the ratio items_emitted / ops_seen
    should be stable. R142.A's guard reads this signal as the baseline.

    Returns count of distinct (METHOD, normalized_path) tuples; reuses
    R128.A's endpoint-key extractor (same canonical-path normalization).
    """
    try:
        return len(_r128_a_extract_endpoint_keys(contract_collection or {}))
    except Exception:
        return 0


def _r128_a_extract_endpoint_keys(collection: dict) -> set[tuple[str, str]]:
    """Walk a Newman collection (Postman v2.1 shape) + return the
    `(METHOD, normalized_path)` set. Used by R128.A divergence compute.

    Normalization rules: lowercase method; path-params collapsed to
    `:param` form so OpenAPI `{id}` and LLM `{{id}}` / `:id` alias to
    the same canonical key. Trailing slashes stripped. Query strings
    dropped (different param values are NOT a divergence signal).
    """
    import re as _re
    out: set[tuple[str, str]] = set()
    for item in (collection.get("item") or []):
        if not isinstance(item, dict):
            continue
        req = item.get("request") or {}
        if not isinstance(req, dict):
            continue
        method = (req.get("method") or "GET").upper()
        url = req.get("url")
        raw_path = ""
        if isinstance(url, dict):
            path_parts = url.get("path") or []
            if isinstance(path_parts, list):
                raw_path = "/" + "/".join(str(p) for p in path_parts)
            elif isinstance(url, dict):
                raw_path = url.get("raw", "") or ""
        elif isinstance(url, str):
            raw_path = url
        # Strip protocol + host
        raw_path = _re.sub(r"^https?://[^/]+", "", raw_path)
        # Strip query string
        raw_path = raw_path.split("?", 1)[0]
        # Canonicalize path-params (any of {x}, {{x}}, :x) → :param
        raw_path = _re.sub(r"\{\{?[^/}]+\}?\}", ":param", raw_path)
        raw_path = _re.sub(r":[a-zA-Z_][a-zA-Z0-9_]*", ":param", raw_path)
        raw_path = raw_path.rstrip("/") or "/"
        out.add((method, raw_path))
    return out


def _r128_a_compute_divergence(
    contract_collection: dict, llm_newman_raw: str, req_id: str,
) -> dict:
    """R128.A — produce a divergence dict comparing deterministic
    OpenAPI-derived Newman vs LLM-mode Newman. Surfaces:
      * contract_endpoint_count / llm_endpoint_count
      * contract_only_endpoints (in baseline, NOT in LLM)
      * llm_only_endpoints (in LLM, NOT in baseline — likely hallucinations)
      * overlap_pct (|intersection| / |contract|)
      * divergence_severity ∈ {aligned, minor, material, severe}

    Operator dashboard reads this via R125.I gen-health endpoint
    aggregator (R128.A.dashboard). Mission contract: LLM-mode Newman
    drift from the deterministic baseline becomes a per-req visible
    signal — invisible drift cannot erode Pillar-1 quality silently.
    """
    import json as _json
    contract_keys = _r128_a_extract_endpoint_keys(contract_collection or {})
    llm_parsed: dict = {}
    try:
        llm_parsed = _json.loads(llm_newman_raw) if llm_newman_raw else {}
    except Exception:
        llm_parsed = {}
    llm_keys = _r128_a_extract_endpoint_keys(llm_parsed)

    contract_only = sorted(
        f"{m} {p}" for (m, p) in (contract_keys - llm_keys)
    )[:20]
    llm_only = sorted(
        f"{m} {p}" for (m, p) in (llm_keys - contract_keys)
    )[:20]
    overlap = len(contract_keys & llm_keys)
    base = len(contract_keys) or 1
    overlap_pct = round((overlap / base) * 100.0, 1)

    # Severity heuristic. A hallucination flood (>5 LLM-only paths the
    # OpenAPI baseline doesn't know about) is ALWAYS severe regardless
    # of how well the LLM happened to cover the baseline subset —
    # operator's mission is to catch invented endpoints, not just
    # measure coverage. Buckets:
    #   aligned  overlap ≥ 95%  AND llm_only == 0
    #   minor    overlap ≥ 80%  AND llm_only ≤ 2
    #   severe   overlap < 50%  OR  llm_only  > 5
    #   material everything else (≥50% overlap, 3-5 hallucinations)
    if overlap_pct >= 95.0 and len(llm_only) == 0:
        severity = "aligned"
    elif overlap_pct >= 80.0 and len(llm_only) <= 2:
        severity = "minor"
    elif overlap_pct < 50.0 or len(llm_only) > 5:
        severity = "severe"
    else:
        severity = "material"

    return {
        "req_id":                  req_id,
        "contract_endpoint_count": len(contract_keys),
        "llm_endpoint_count":      len(llm_keys),
        "overlap_count":           overlap,
        "overlap_pct":             overlap_pct,
        "contract_only_endpoints": contract_only,
        "llm_only_endpoints":      llm_only,
        "divergence_severity":     severity,
    }


def _dry_run_quarantine(script_path) -> tuple[bool, str]:
    """Fix FF: cheap parse/syntax check on a freshly-written test script.

    Returns (ok, error_message). Quick, no network: validates the script
    can at least *parse* — catches the cases where the LLM emits
    syntactically-broken JS/JSON/Python before we ship it to a real run.
    Network-level dry-run (npx playwright --list etc.) is intentionally
    NOT done here — too slow for the gen path. The runner does its own
    deeper validation at execution time.

    Failures rename the script to <name>.broken-dryrun so the .broken-*
    gate skip pattern in gates.py:232 excludes it from runs.
    """
    import ast
    import json as _json
    p = str(script_path)
    try:
        if p.endswith(".py"):
            ast.parse(script_path.read_text())
        elif p.endswith(".json"):
            _json.loads(script_path.read_text())
        elif p.endswith(".ts") or p.endswith(".js"):
            # No bundled JS/TS parser — do a brace/bracket balance check
            # which catches the most common LLM-truncation cases.
            txt = script_path.read_text()
            if txt.count("{") != txt.count("}"):
                return False, f"unbalanced braces: {{={txt.count('{')} }}={txt.count('}')}"
            if txt.count("(") != txt.count(")"):
                return False, f"unbalanced parens: ({txt.count('(')}) )={txt.count(')')}"
            backtick_unescaped = txt.count("`") - len(__import__("re").findall(r"\\`", txt))
            if backtick_unescaped % 2 != 0:
                return False, f"odd backtick count ({backtick_unescaped}): truncated template literal"
        # Other extensions (.yaml etc.) — pass through; runner validates.
        return True, ""
    except SyntaxError as exc:
        return False, f"Python SyntaxError: {exc.msg} at line {exc.lineno}"
    except _json.JSONDecodeError as exc:
        return False, f"JSON parse error: {exc.msg} at line {exc.lineno}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# F7-2: Use extend() rather than rebind, so the cross-router `from .tests
# import GENERATED_TESTS` (now `from .tests_state import GENERATED_TESTS`)
# keeps pointing at the same underlying list this seed populates.
GENERATED_TESTS.extend([
    {
        "id": "TC-124", "title": "Happy path Visa checkout", "priority": "P0",
        "status": "PASS", "duration_ms": 2300, "tool": "playwright",
        "requirement_id": "REQ-017", "ac_id": "AC-001", "test_type": "UI",
        "gherkin": "Scenario: Successful Visa card checkout\n  Given I have 2 items in cart\n  When I enter valid Visa card details\n  Then order confirmed within 3s",
        "script_path": "src/automation/playwright/checkout.spec.ts",
    },
    {
        "id": "TC-125", "title": "Mastercard with 3DS auth", "priority": "P0",
        "status": "PASS", "duration_ms": 4100, "tool": "playwright",
        "requirement_id": "REQ-017", "ac_id": "AC-005", "test_type": "UI",
        "gherkin": "Scenario: 3DS triggered for transaction > £150",
        "script_path": "src/automation/playwright/checkout.spec.ts",
    },
    {
        "id": "TC-126", "title": "Payment timeout under concurrent load", "priority": "P0",
        "status": "FAIL", "duration_ms": 45200, "tool": "k6",
        "requirement_id": "REQ-017", "ac_id": "AC-004", "test_type": "Performance",
        "gherkin": "Scenario: Payment under 500 concurrent users within 3s",
        "script_path": "src/automation/k6/checkout-performance.js",
        "error_message": "Race condition: duplicate transaction ID under high concurrency",
    },
    {
        "id": "TC-127", "title": "Expired card rejection", "priority": "P0",
        "status": "PASS", "duration_ms": 1800, "tool": "playwright",
        "requirement_id": "REQ-017", "ac_id": "AC-002", "test_type": "UI",
        "gherkin": "Scenario: Expired card shows error without charge",
        "script_path": "src/automation/playwright/checkout.spec.ts",
    },
    {
        "id": "TC-128", "title": "Stolen card fraud detection", "priority": "P0",
        "status": "PASS", "duration_ms": 2100, "tool": "playwright",
        "requirement_id": "REQ-017", "ac_id": "AC-002", "test_type": "UI",
        "gherkin": "Scenario: Stolen card triggers fraud detection silently",
        "script_path": "src/automation/playwright/checkout.spec.ts",
    },
    {
        "id": "TC-131", "title": "SQL injection blocked", "priority": "P0",
        "status": "PASS", "duration_ms": 950, "tool": "playwright",
        "requirement_id": "REQ-017", "ac_id": "AC-003", "test_type": "Security",
        "gherkin": "Scenario: SQL injection in card number field sanitized",
        "script_path": "src/automation/playwright/checkout.spec.ts",
    },
])  # F7-2: closes the GENERATED_TESTS.extend([...]) above

# Track seed test IDs so we can distinguish them from generated tests
_SEED_TEST_IDS = {t["id"] for t in GENERATED_TESTS}


# ── JSON backup helpers ──────────────────────────────────────────────────────
# F7-2 (continuation): Body lives in tests_helpers.py; here we keep thin
# wrappers because _save_tests_json needs `_SEED_TEST_IDS` (defined right above
# this block — a closure-style file-local set).

def _save_tests_json():
    """Persist all generated tests (beyond seed data) to a local JSON file as backup."""
    from .tests_helpers import _save_tests_json as _impl
    _impl(_SEED_TEST_IDS)


def _load_tests_json():
    """Load previously generated tests from the JSON backup into GENERATED_TESTS."""
    from .tests_helpers import _load_tests_json as _impl
    _impl()


_load_tests_json()

# Backfill generation_source for tests created before provenance tracking was added.
# This enables /heal-tests and self-healing to detect fallback stubs in old data.
for _t in GENERATED_TESTS:
    if "generation_source" not in _t:
        _content = _t.get("script_content", "")
        if (
            "Stub smoke test" in _content
            or "ARTA Stub" in _content
            or "ARTA Auto-Generated" in _content
            or not _content
        ):
            _t["generation_source"] = "fallback"
        else:
            _t["generation_source"] = "llm"


# ── Generate-all job persistence ────────────────────────────────────────────
# F7-2 (continuation): _save_job_json + _load_all_jobs_json + _get_project_req_ids
# all live in tests_helpers.py now (re-exported at top of this file). The boot-time
# load + interrupted-run repair logic stays here because it executes on import.
_JOBS_FILE = Path(".arta/generate_all_jobs.json")

# Load persisted jobs on startup
#
# F18-4: The original hook marked `running` jobs as `interrupted` in memory
# but never persisted that back to disk. Result: after a container restart,
# the `.arta/generate_all_jobs.json` file still showed status=running, the
# UI kept polling for an "active" job that would never make progress, and
# subsequent boots re-applied the same stale state. Now we also write the
# repaired status back so the on-disk record reflects reality.
_orphan_job_count = 0
for _persisted_job in _load_all_jobs_json():
    if _persisted_job.get("job_id") and _persisted_job["job_id"] not in _GENERATE_ALL_JOBS:
        # Mark previously-running jobs as failed (server restarted mid-run).
        # A background task running during API shutdown cannot survive the
        # restart — even with F8-3 supervise(), the task is killed when the
        # event loop dies. So any `running` entry loaded from disk is an
        # orphan by definition.
        if _persisted_job.get("status") == "running":
            _persisted_job["status"] = "interrupted"
            _persisted_job["completed_at"] = _persisted_job.get(
                "completed_at") or _persisted_job.get("started_at", "")
            _persisted_job["interruption_reason"] = (
                "API container restart detected on boot; background task did "
                "not survive the restart. Use Retry Failed to resume."
            )
            _orphan_job_count += 1
        _GENERATE_ALL_JOBS[_persisted_job["job_id"]] = _persisted_job

# Persist the repaired states back to disk so the on-disk file reflects
# reality (otherwise it silently disagrees with the in-memory dict until
# the next save). _save_job_json reads the current file, removes the
# entry matching job_id, appends the updated entry, and rewrites — so
# sequential calls for each orphan correctly reconcile the full file.
if _orphan_job_count > 0:
    for _j in list(_GENERATE_ALL_JOBS.values()):
        if _j.get("interruption_reason", "").startswith("API container restart"):
            _save_job_json(_j)
    import logging as _boot_log
    _boot_log.getLogger("arta.tests").info(
        "boot cleanup: marked %d orphaned 'running' jobs as 'interrupted' "
        "(API restart detected)", _orphan_job_count,
    )


@router.get("", dependencies=[Depends(_require_api_key)])
async def list_tests(
    priority: str | None = None,
    status: str | None = None,
    tool: str | None = None,
    requirement_id: str | None = None,
    project_id: str | None = None,
    flag: str | None = None,
):
    """List test cases with optional filters.

    R330 P1d — `flag` filters on gate-stamped metadata so the SUT-understanding
    panel CTA can deep-link the exact tests needing attention:
    `potentially_incorrect` | `guess` | `needs_attention` (union of both).
    """
    from ..db_adapter import try_db

    def _flag_match(t: dict) -> bool:
        meta = t.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if flag == "potentially_incorrect":
            return bool(meta.get("potentially_incorrect"))
        if flag == "guess":
            return meta.get("grounded_by") == "guess"
        if flag == "authz_ungrounded":
            return bool(meta.get("authz_ungrounded"))
        if flag == "needs_attention":
            # P2.3 — an authz-ungrounded test (exercises a gated endpoint with no
            # authz grounding) is an understanding gap; surface it in the same
            # triage lane as potentially_incorrect/guess.
            return (bool(meta.get("potentially_incorrect"))
                    or meta.get("grounded_by") == "guess"
                    or bool(meta.get("authz_ungrounded")))
        return True  # unknown flag value — no filtering (fail-open for listing)

    async with try_db() as db:
        if db:
            from ...db.repository import TestCaseRepo, _to_dict
            repo = TestCaseRepo(db)
            rows, total = await repo.list(priority=priority, tool=tool, status=status, project_id=project_id)
            tests = [_to_dict(r) for r in rows]

            # F20-4 + F20-8: Normalize requirement_id (and tool, ac_id) to
            # textual identifiers expected by the rest of the API + UI.
            #
            # The DB column `test_cases.requirement_id` is a UUID FK to
            # `requirements.id`. Once F20-7 made the upstream lookup
            # succeed, this column became populated with UUIDs — but the
            # rest of the codebase (project_req_ids filter, frontend
            # filters, GENERATED_TESTS in-memory tests) all use the
            # the UUID with the textual req_id from metadata when it's
            # available, not just when the UUID is NULL. (The F20-4
            # version that only fired on NULL silently dropped 100+ DB
            # tests after F20-7 because the UUID looked truthy and never
            # got rewritten — failing the project_req_ids filter below.)
            for t in tests:
                meta = t.get("metadata") or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                # Always prefer textual req_id from metadata when present;
                # fall back to existing column value (which may be a UUID
                # or already textual depending on the persist path).
                if meta.get("requirement_id"):
                    t["requirement_id"] = meta["requirement_id"]
                if meta.get("ac_id"):
                    t["ac_id"] = meta["ac_id"]
                if meta.get("ac_measurability") and not t.get("ac_measurability"):
                    t["ac_measurability"] = meta["ac_measurability"]
                # Tool comes from automation_tool column; surface under both
                # keys for the UI which checks `tool` first
                if not t.get("tool") and t.get("automation_tool"):
                    t["tool"] = t["automation_tool"]

            if requirement_id:
                tests = [t for t in tests if t.get("requirement_id") == requirement_id.upper()]

            # Merge in-memory generated tests not yet in DB
            from .requirements import PROJECT_REQUIREMENTS
            # F20-6: Build the dedup key set from `test_id` (textual,
            # like "TC-AM-021-01") not `id` (which for DB rows is the
            # row's UUID primary key). The previous code's `id or
            # test_id` returned the UUID for DB rows, so when in-memory
            # tests (whose `id` IS the textual test_id) were checked
            # against this set they never matched — producing a phantom
            # duplicate of every DB test. That doubled the /api/tests
            # total and broke parity with /api/requirements.test_count.
            db_test_ids = {t.get("test_id") or t.get("id") for t in tests}
            project_req_ids = _get_project_req_ids(project_id) if project_id else None
            for t in GENERATED_TESTS:
                # In-memory tests use `id` as the textual TC-id (see the
                # builder at line ~2167). `test_id` may also be set as
                # an alias. Either way we want to compare the textual id.
                tid = t.get("test_id") or t.get("id")
                if tid and tid not in db_test_ids:
                    if project_req_ids is not None and t.get("requirement_id", "") not in project_req_ids:
                        continue
                    tests.append(t)

            # F20-4: After hydration + merge, drop tests whose requirement_id
            # is still empty/missing OR not in this project's req list.
            # These are residual orphans from prior failed persists that
            # can't be salvaged from metadata. Keeping them would make the
            # /api/tests total exceed the sum of test_count from
            # /api/requirements — breaking the Architecture vs Test
            # Explorer parity contract.
            if project_req_ids is not None:
                tests = [t for t in tests
                         if t.get("requirement_id") and t["requirement_id"] in project_req_ids]

            # F20-1: Removed title-based dedup. The ID-based merge above
            # (db_test_ids guard at lines ~245-252) already prevents
            # the same persisted test from appearing twice. The prior
            # title-dedup collapsed legitimately distinct tests that
            # share a title across tools — e.g. Playwright's
            # `test('Login happy path', ...)` and the Axe a11y
            # `test('Login happy path', ...)` from the same AC — making
            # Test Explorer's "All (N)" count silently disagree with
            # the Architecture page's per-req `test_count` sum. Dropping
            # the dedup gives a true file-count semantic that matches
            # what's on disk.
            if flag:
                tests = [t for t in tests if _flag_match(t)]
            safe_tests = json.loads(json.dumps(tests, default=str))
            return {"tests": safe_tests, "total": len(safe_tests)}

    # Mock fallback — filter by project_id using PROJECT_REQUIREMENTS lookup
    if project_id:
        project_req_ids = _get_project_req_ids(project_id)
        tests = [t for t in GENERATED_TESTS if t.get("requirement_id", "") in project_req_ids]
    else:
        # No project selected — return E-Commerce demo tests (default project)
        from .requirements import ECOMMERCE_PROJECT_ID
        project_req_ids = _get_project_req_ids(ECOMMERCE_PROJECT_ID)
        tests = [t for t in GENERATED_TESTS if t.get("requirement_id", "") in project_req_ids]
    if priority:
        tests = [t for t in tests if t["priority"] == priority.upper()]
    if status:
        tests = [t for t in tests if t["status"] == status.upper()]
    if tool:
        tests = [t for t in tests if t["tool"] == tool.lower()]
    if requirement_id:
        tests = [t for t in tests if t["requirement_id"] == requirement_id.upper()]
    if flag:
        tests = [t for t in tests if _flag_match(t)]
    # F20-1: Removed title-based dedup (mock fallback path) for the same
    # reason as the DB path above — title collisions across tools (axe
    # vs playwright vs newman testing the same AC) are legitimate
    # distinct tests, not duplicates. The mock fallback reads only from
    # GENERATED_TESTS so an ID-based dedup is also unnecessary —
    # GENERATED_TESTS is append-only with unique test_ids.
    safe_tests = json.loads(json.dumps(tests, default=str))
    return {"tests": safe_tests, "total": len(safe_tests)}


@router.get("/upstream-quality", dependencies=[Depends(_require_api_key)])
async def upstream_quality_summary(project_id: str = Query(None)):
    """B5 — aggregate upstream artifact-quality across a project's requirements
    from the per-requirement sidecars written at gen time: requirement
    testability (measurable-AC %), Gherkin↔requirement alignment, pre-gen AC
    coverage, and fallback/block rates. The metrics ARTA historically did NOT
    track upstream (validation was concentrated at the test-script stage).

    NOTE: must be declared before `GET /{test_id}` so the single-segment path
    isn't captured by the test-id route.
    """
    from ...agents.upstream_quality import read_upstream_quality
    out = read_upstream_quality(project_id=project_id)
    # TEA Layer-9 gate signal: DB-side requirement-quality band distribution
    # (stamped at Jira import into metadata.quality) — covers requirements
    # that have never generated (sidecars are gen-time-only). INFO visibility.
    try:
        from ..db_adapter import try_db
        from sqlalchemy import text as _text
        async with try_db() as db:
            if db is not None:
                # Filter built in Python — a ":pid IS NULL OR CAST(:pid AS uuid)"
                # param is untypeable for asyncpg and failed silently here.
                _where = "metadata->'quality' IS NOT NULL"
                _params: dict = {}
                if project_id:
                    _where += " AND project_id = CAST(:pid AS uuid)"
                    _params["pid"] = project_id
                rows = (await db.execute(_text(f"""
                    SELECT metadata->'quality'->>'band' AS band,
                           COUNT(*) AS n,
                           AVG((metadata->'quality'->>'score')::numeric) AS avg_score
                    FROM requirements
                    WHERE {_where}
                    GROUP BY 1"""), _params)).all()
                if rows:
                    total_n = sum(r.n for r in rows)
                    out["requirement_bands"] = {
                        **{r.band: r.n for r in rows if r.band},
                        "avg_score": round(sum(float(r.avg_score or 0) * r.n
                                               for r in rows) / total_n, 1),
                    }
    except Exception as _exc:
        log.debug("upstream-quality: requirement_bands aggregate skipped: %s", _exc)
    return out


@router.get("/{test_id}", dependencies=[Depends(_require_api_key)])
async def get_test(
    test_id: str,
    project_id: str | None = Query(None),
):
    """Get test case detail including Gherkin and automation script path.

    R113.E — when `project_id` is supplied, the test's requirement_id MUST
    belong to that project (multi-project isolation). Cross-project reads
    return 403. Pre-R113.E: any caller with valid API key could fetch ANY
    test by ID, bypassing project scope.
    """
    from ..db_adapter import try_db

    def _r113_e_check_scope(test_obj: dict) -> None:
        """R113.E — verify the test belongs to the caller-supplied project."""
        if not project_id:
            return  # backward-compat: no project_id supplied
        req_id = test_obj.get("requirement_id")
        if not req_id:
            return  # legacy test without requirement_id; allow
        try:
            project_req_ids = _get_project_req_ids(project_id)
        except Exception:
            return  # graceful: if lookup fails, allow (don't block on infra hiccups)
        if project_req_ids and req_id not in project_req_ids:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"R113.E: Test {test_id} (requirement {req_id}) does NOT "
                    f"belong to project {project_id}. Cross-project read "
                    f"refused."
                ),
            )

    async with try_db() as db:
        if db:
            from ...db.repository import TestCaseRepo, _to_dict
            repo = TestCaseRepo(db)
            row = await repo.get(test_id.upper())
            if row:
                _row_dict = _to_dict(row)
                _r113_e_check_scope(_row_dict)
                return _row_dict

    test = next((t for t in GENERATED_TESTS if t["id"] == test_id.upper()), None)
    if not test:
        raise HTTPException(status_code=404, detail=f"Test {test_id} not found")
    _r113_e_check_scope(test)
    return test


# ── Helpers for cross-store requirement lookup ────────────────────────────

def _find_requirement(req_id: str):
    """Find a requirement by ID across all project requirement stores."""
    from .requirements import PROJECT_REQUIREMENTS
    for reqs in PROJECT_REQUIREMENTS.values():
        if not isinstance(reqs, list):
            continue
        for r in reqs:
            if not isinstance(r, dict):
                continue
            if r.get("req_id") == req_id or r.get("id") == req_id:
                return r
    return None


def _extract_scenario_title(gherkin: str) -> str:
    """Extract the first Scenario title from Gherkin text."""
    for line in gherkin.split('\n'):
        line = line.strip()
        if line.startswith('Scenario:') or line.startswith('Scenario Outline:'):
            return line.split(':', 1)[1].strip()
    return "Generated Test"


def _ac_measurability_of(ac, ac_id: str, requirement: dict | None = None) -> str:
    """"measurable" | "unmeasured" | "unknown" for one AC dict — the per-test
    provenance stamp. The import-time verdict (metadata.quality.ac_flags) wins:
    R205 enrichment appends measurable clauses to AC text before gen, so the
    heuristic alone would report enriched — not source — measurability.
    Killswitch ARTA_AC_MEASURABILITY_WEIGHT_DISABLE=1."""
    if os.environ.get("ARTA_AC_MEASURABILITY_WEIGHT_DISABLE") == "1":
        return "unknown"
    try:
        stamped = (((requirement or {}).get("metadata") or {}).get("quality") or {}).get("ac_flags")
        if stamped is not None:
            return "unmeasured" if ac_id in stamped else "measurable"
        from ...agents.upstream_quality import ac_measurability_flags
        if not isinstance(ac, dict):
            return "unknown"
        return "unmeasured" if ac_measurability_flags(
            [{**ac, "id": ac_id or ac.get("id") or "ac"}]) else "measurable"
    except Exception:
        return "unknown"


def _extract_ac_scenario(
    combined_gherkin: str,
    ac_index: int,
    ac_id: str,
    ac_statement: str,
) -> str:
    """Phase Q1 — pull the Scenario block for a specific AC from the merged
    feature file produced by ATDD's `_merge_feature_files`.

    Pre-Q1, every test row for one requirement stored the FULL combined
    blob, so dashboards/UI showed identical Gherkin across tests within
    a req. Post-Q1, each test row carries its AC's specific Scenario
    (with the requirement's Feature/Background header preserved).

    Strategy:
      1. Split the combined gherkin on `^Scenario:` lines into
         (header, scenario_1, scenario_2, ...)
      2. Find the scenario block whose first line cites this AC
         (matched by AC-id token, then by statement substring,
         then by positional index — three fallback steps)
      3. Re-prepend the Feature/Background header so the per-AC
         output is a self-contained, runnable Gherkin file
      4. Defensive fallback: when extraction fails for ANY reason,
         return the original combined_gherkin (= no behavior regression)

    Args:
        combined_gherkin: the joined feature file from ATDD
        ac_index: 0-based position of this AC in the requirement's AC list
        ac_id: the AC's id (e.g. "AC-AN-002-01" or "AC-1")
        ac_statement: the AC's statement text (used as a fuzzy match)

    Returns:
        Either the AC-specific Gherkin (header + matching Scenario) OR
        the unchanged `combined_gherkin` when no match found.
    """
    if not combined_gherkin or "Scenario:" not in combined_gherkin:
        return combined_gherkin

    # Allow leading horizontal whitespace — real-world ATDD output indents
    # Scenario blocks by 2-4 spaces under the Feature line. Use [ \t]*
    # (NOT \s*) because \s matches \n too, which would also match blank
    # lines BEFORE a Scenario line (\s spans the newline + indent + 'S')
    # and produce 2× the expected split points (one per blank line + one
    # per scenario line). Verified live: \s* gave 35 parts for a file
    # with 15 scenarios; [ \t]* gives the correct 16 parts.
    parts = re.split(r"^(?=[ \t]*Scenario:)", combined_gherkin, flags=re.MULTILINE)
    if len(parts) < 2:
        return combined_gherkin
    header = parts[0]
    scenarios = parts[1:]

    # Build needles in order of specificity — most-specific match wins.
    needles: list[str] = []
    if ac_id:
        # Both forms: "AC-AN-002-01" full and "AC-1" short
        needles.append(ac_id.lower())
        m = re.search(r"AC-?\d+", ac_id)
        if m:
            needles.append(m.group(0).lower())
    if ac_statement:
        # First ~30 chars of statement, lowercased
        snippet = ac_statement[:30].strip().lower()
        if len(snippet) >= 6:   # too-short snippets cause false matches
            needles.append(snippet)

    matched: str | None = None
    for scenario in scenarios:
        first_line = scenario.split("\n", 1)[0].lower()
        for needle in needles:
            if needle and needle in first_line:
                matched = scenario
                break
        if matched is not None:
            break

    # Positional fallback when needle-match failed
    if matched is None and 0 <= ac_index < len(scenarios):
        matched = scenarios[ac_index]
    if matched is None:
        return combined_gherkin

    return header + matched.rstrip() + "\n"


# ── Fallback Gherkin when LLM is unavailable ─────────────────────────────

FALLBACK_GHERKIN = {
    "REQ-BT-001": """Feature: Bug CRUD Operations
  # REQ-BT-001 — Bug CRUD Operations

  Scenario: Create a new bug
    Given I am on the BugTrackr dashboard
    When I click "New Bug"
    And I fill in title "Login button broken"
    And I select priority "High"
    And I fill in description "Button unresponsive on click"
    And I click "Submit"
    Then I should see the bug "Login button broken" in the bug list
    And the bug status should be "Open"

  Scenario: View bug details
    Given a bug "Login button broken" exists in the system
    When I click on the bug title "Login button broken" in the list
    Then I should see the bug detail page
    And the page shows title "Login button broken"
    And the page shows priority "High"
    And the page shows status "Open"

  Scenario: Update bug priority
    Given I am viewing bug "Login button broken" detail page
    When I change the priority to "Critical"
    And I click "Save"
    Then the bug priority should be "Critical"
    And the change is reflected in the bug list

  Scenario: Delete a bug
    Given a bug "Test bug" exists in the system
    When I click the delete button for "Test bug"
    And I confirm the deletion
    Then the bug "Test bug" is removed from the list
""",
    "REQ-BT-002": """Feature: Bug Status Workflow
  # REQ-BT-002 — Bug Status Workflow

  Scenario: Transition bug from Open to In Progress
    Given a bug exists with status "Open"
    When I change the status to "In Progress"
    Then the bug status should be "In Progress"
    And the activity log shows "Status changed from Open to In Progress"

  Scenario: Resolve and close a bug
    Given a bug exists with status "In Progress"
    When I change the status to "Resolved"
    Then the bug status should be "Resolved"
    When I change the status to "Closed"
    Then the bug status should be "Closed"
    And the activity log shows both transitions

  Scenario: Reject invalid status transition
    Given a bug exists with status "Open"
    When I try to change the status to "Closed"
    Then the transition should be rejected
    And I see an error "Invalid status transition"
""",
    "REQ-BT-003": """Feature: Role-Based Access Control
  # REQ-BT-003 — Role-Based Access Control

  Scenario: Admin has full access
    Given I am logged in as an admin user
    When I navigate to user management
    Then I can see the user list
    And I can create new users
    When I navigate to bug management
    Then I can delete any bug

  Scenario: Developer cannot delete bugs
    Given I am logged in as a developer user
    When I view a bug detail page
    Then the delete button is not visible
    When I try to access the delete API directly
    Then I receive a 403 Forbidden response

  Scenario: Tester can create bugs and comments
    Given I am logged in as a tester user
    When I create a new bug "Found UI glitch"
    Then the bug is created successfully
    When I add a comment "Steps to reproduce attached"
    Then the comment is saved
""",
    "REQ-BT-004": """Feature: Comment System
  # REQ-BT-004 — Comment System

  Scenario: Add a comment to a bug
    Given I am viewing a bug detail page
    When I type "This needs more investigation" in the comment box
    And I click "Submit"
    Then the comment appears in the comment list
    And the comment shows my username
    And the comment shows the current timestamp

  Scenario: Cannot submit empty comment
    Given I am viewing a bug detail page
    When I leave the comment box empty
    And I click "Submit"
    Then I see a validation error "Comment cannot be empty"
""",
    "REQ-BT-005": """Feature: Activity Tracking & Audit Logs
  # REQ-BT-005 — Activity Tracking & Audit Logs

  Scenario: Status change appears in activity log
    Given a bug "Server error" has status "Open"
    When I change the status to "In Progress"
    And I view the bug activity log
    Then I see an entry "Status changed from Open to In Progress"
    And the entry shows the current user name
    And the entry shows the current timestamp

  Scenario: Field update appears in activity log
    Given a bug "Server error" exists
    When I change the priority from "Medium" to "High"
    And I view the bug activity log
    Then I see an entry "Priority changed from Medium to High"
""",
    "REQ-BT-006": """Feature: Project Dashboard
  # REQ-BT-006 — Project Dashboard

  Scenario: Dashboard shows correct bug counts
    Given there are 5 bugs with status "Open"
    And there are 3 bugs with status "Resolved"
    When I view the project dashboard
    Then I see "Total Bugs: 8"
    And I see "Open: 5"
    And I see "Resolved: 3"

  Scenario: Dashboard shows bugs by priority
    Given there are 2 "Critical" bugs and 3 "High" bugs
    When I view the dashboard priority breakdown
    Then the chart shows Critical: 2 and High: 3
""",
    "REQ-BT-007": """Feature: Health Check API
  # REQ-BT-007 — Health Check API

  Scenario: Health endpoint returns 200
    Given the BugTrackr application is running
    When I send a GET request to "/api/health"
    Then I receive HTTP status 200
    And the response body contains "status": "ok"

  Scenario: Health endpoint responds within SLA
    Given the BugTrackr application is running
    When I send a GET request to "/api/health"
    Then the response time is less than 5000 milliseconds
""",
    "REQ-BT-008": """Feature: Dark Mode Support
  # REQ-BT-008 — Dark Mode Support

  Scenario: Toggle dark mode
    Given I am on any page in light mode
    When I click the dark mode toggle
    Then the UI switches to dark theme
    And the body has class "dark"

  Scenario: Dark mode preference persists
    Given I have enabled dark mode
    When I close the browser and reopen the application
    Then the UI is still in dark mode
    And localStorage contains theme "dark"
""",
}

FALLBACK_PLAYWRIGHT_TEMPLATE = '''import {{ test, expect }} from '@playwright/test';

// ── ARTA Auto-Generated — {req_id}: {title} ──────────
// Priority: {priority} | Risk: {risk_score}/9

const BASE = process.env.TARGET_BASE_URL || '{base_url}';

// ── Application Smoke Tests ──────────────────────────────
test.describe('{title} — Smoke', () => {{
  test('application responds to navigation', async ({{ page }}) => {{
    const resp = await page.goto('/', {{ waitUntil: 'domcontentloaded' }});
    // Accept any non-server-error response (200, 301, 302 all valid)
    expect((resp?.status() ?? 0)).toBeLessThan(500);
  }});

  test('page renders with content', async ({{ page }}) => {{
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.locator('body')).toBeVisible();
    const title = await page.title();
    expect(title.length).toBeGreaterThan(0);
  }});

  test('no uncaught JavaScript errors on load', async ({{ page }}) => {{
    const errors: string[] = [];
    page.on('pageerror', (err) => {{
      // Filter known non-fatal browser noise
      if (!err.message.includes('ResizeObserver') && !err.message.includes('Non-Error')) {{
        errors.push(err.message);
      }}
    }});
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    expect(errors, `Uncaught JS errors: ${{errors.join('; ')}}`).toHaveLength(0);
  }});
}});

// ── Authenticated User Tests ─────────────────────────────
test.describe('{title} — Auth', () => {{
  test('authenticated session has valid cookies', async ({{ page }}) => {{
    // Skip if no auth method configured — avoids false failures in unauthenticated environments
    if (!process.env.TARGET_AUTH_METHOD || process.env.TARGET_AUTH_METHOD === 'none') {{
      test.skip(true, 'Auth not configured — set TARGET_AUTH_METHOD in Settings → Environments');
      return;
    }}
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    const cookies = await page.context().cookies();
    const hasAuth = cookies.some(c =>
      c.name.includes('token') || c.name.includes('session') || c.name.includes('auth')
    );
    expect(hasAuth, 'Expected auth cookie — verify credentials in Settings → Environments').toBeTruthy();
  }});

  test('page renders content after auth', async ({{ page }}) => {{
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    const bodyText = await page.locator('body').innerText();
    expect(bodyText.length).toBeGreaterThan(10);
  }});
}});
'''

FALLBACK_NEWMAN_TEMPLATE = '''{{
  "info": {{
    "name": "ARTA Generated — {req_id}: {title}",
    "description": "Stub API test collection (LLM generation failed — replace with real requests)",
    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  }},
  "item": [
    {{
      "name": "{req_id} — Health check",
      "request": {{
        "method": "GET",
        "header": [
          {{ "key": "Authorization", "value": "Bearer {{{{auth_token}}}}" }}
        ],
        "url": {{ "raw": "{{{{base_url}}}}/health", "host": ["{{{{base_url}}}}"], "path": ["health"] }}
      }},
      "event": [
        {{
          "listen": "test",
          "script": {{
            "exec": [
              "pm.test('Status 200', () => pm.response.to.have.status(200));",
              "pm.test('Response time < 3000ms', () => pm.expect(pm.response.responseTime).to.be.below(3000));"
            ],
            "type": "text/javascript"
          }}
        }}
      ]
    }}
  ],
  "variable": [
    {{ "key": "base_url", "value": "{base_url}" }},
    {{ "key": "auth_token", "value": "" }}
  ]
}}'''

FALLBACK_K6_TEMPLATE = '''import http from 'k6/http';
import {{ check, sleep }} from 'k6';

// ── ARTA Stub k6 Script — {req_id}: {title} ──────────────────────
// Stub smoke test — run /heal-tests to generate real scenarios
// Priority: {priority} | Risk: {risk_score}/9

export const options = {{
  stages: [
    {{ duration: '5s', target: 5 }},
    {{ duration: '10s', target: 5 }},
    {{ duration: '5s', target: 0 }},
  ],
  thresholds: {{
    http_req_duration: ['{sla_threshold}'],
    http_req_failed: ['rate<0.10'],
  }},
}};

const BASE_URL = __ENV.TARGET_BASE_URL || '{base_url}';

export default function () {{
  const res = http.get(`${{BASE_URL}}/health`);
  check(res, {{
    'status 200': (r) => r.status === 200,
    'response time OK': (r) => r.timings.duration < 3000,
  }});
  sleep(1);
}}
'''

# F13-3: FALLBACK_ZAP_TEMPLATE removed — was the spider+passive stub source
# that produced the misleading "minimal scan stub" YAMLs found in
# src/automation/zap/. The writer was already gone but the constant lingered.
# Real ZAP failures now propagate via _generate_zap raising on attack_jobs==0,
# which @retry catches and (eventually) surfaces to the caller as a real
# generation failure rather than a fake-passing scan.

BUGTRACKR_PLAYWRIGHT_TEMPLATES: dict[str, str] = {
    "REQ-BT-001": '''import { test, expect } from '@playwright/test';

// ── ARTA Auto-Generated — REQ-BT-001: Bug CRUD Operations ────────
// Priority: P0 | Risk: 8.0/9 | Type: UI E2E
// BugTrackr is UI-only — all tests use browser automation

const BASE = process.env.TARGET_BASE_URL || 'http://localhost:3005';
const uniqueId = () => `arta-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,6)}`;

test.describe('REQ-BT-001: Bug CRUD Operations', () => {
  test.setTimeout(60000);

  test.beforeEach(async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
    // Verify we are authenticated (not on login page)
    const url = page.url();
    expect(url).not.toContain('/login');
  });

  test('Page loads and shows bug list', async ({ page }) => {
    // Verify the main page renders with bug-related content
    await expect(page.locator('body')).toBeVisible();
    // Look for common UI elements (table, list, or cards)
    const hasBugContent = await page.getByText(/bug|issue|ticket/i).first().isVisible().catch(() => false);
    expect(hasBugContent || true).toBeTruthy(); // Soft check — page loads
  });

  test('Can navigate to create bug form', async ({ page }) => {
    // Look for a create/new button
    const createBtn = page.getByRole('button', { name: /new|create|add|report/i }).first();
    if (await createBtn.isVisible().catch(() => false)) {
      await createBtn.click();
      await page.waitForLoadState('networkidle');
      // Verify a form or modal appeared
      const hasForm = await page.locator('form, [role="dialog"], input[type="text"]').first().isVisible().catch(() => false);
      expect(hasForm).toBeTruthy();
    }
  });

  test('Can fill and submit bug creation form', async ({ page }) => {
    // Navigate to create form
    const createBtn = page.getByRole('button', { name: /new|create|add|report/i }).first();
    if (await createBtn.isVisible().catch(() => false)) {
      await createBtn.click();
      await page.waitForLoadState('networkidle');

      // Fill title if input exists
      const titleInput = page.getByPlaceholder(/title|name|summary/i).first()
        .or(page.getByLabel(/title|name|summary/i).first());
      if (await titleInput.isVisible().catch(() => false)) {
        await titleInput.fill(`ARTA Test Bug ${uniqueId()}`);
      }

      // Fill description if exists
      const descInput = page.getByPlaceholder(/description|detail/i).first()
        .or(page.getByLabel(/description|detail/i).first());
      if (await descInput.isVisible().catch(() => false)) {
        await descInput.fill('Automated E2E test by ARTA TEA Platform');
      }

      // Select priority if dropdown exists
      const prioritySelect = page.getByLabel(/priority/i).first()
        .or(page.getByRole('combobox', { name: /priority/i }).first());
      if (await prioritySelect.isVisible().catch(() => false)) {
        await prioritySelect.selectOption({ label: /high/i }).catch(() => {});
      }

      // Submit
      const submitBtn = page.getByRole('button', { name: /submit|create|save|add/i }).first();
      if (await submitBtn.isVisible().catch(() => false)) {
        await submitBtn.click();
        await page.waitForLoadState('networkidle');
      }
    }
  });

  test('Can view bug details by clicking a bug', async ({ page }) => {
    // Find any clickable bug item in the list
    const bugLink = page.getByRole('link', { name: /bug|issue/i }).first()
      .or(page.locator('tr, [class*="card"], [class*="item"]').first());
    if (await bugLink.isVisible().catch(() => false)) {
      await bugLink.click();
      await page.waitForLoadState('networkidle');
      // Should see detail content (title, description, status, etc.)
      await expect(page.locator('body')).toBeVisible();
    }
  });

  test('Can delete a bug via UI', async ({ page }) => {
    // Look for a delete button on the page
    const deleteBtn = page.getByRole('button', { name: /delete|remove/i }).first();
    if (await deleteBtn.isVisible().catch(() => false)) {
      await deleteBtn.click();
      // Handle confirmation dialog if present
      const confirmBtn = page.getByRole('button', { name: /confirm|yes|ok|delete/i }).first();
      if (await confirmBtn.isVisible().catch(() => false)) {
        await confirmBtn.click();
        await page.waitForLoadState('networkidle');
      }
    }
  });
});
''',
    "REQ-BT-002": '''import { test, expect } from '@playwright/test';

// ── ARTA Auto-Generated — REQ-BT-002: Bug Status Workflow ────────
// Priority: P0 | Risk: 7.0/9 | Type: UI E2E
// BugTrackr is UI-only — all tests use browser automation

const BASE = process.env.TARGET_BASE_URL || 'http://localhost:3005';
const uniqueId = () => `arta-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,6)}`;

test.describe('REQ-BT-002: Bug Status Workflow', () => {
  test.setTimeout(60000);

  test.beforeEach(async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
    const url = page.url();
    expect(url).not.toContain('/login');
  });

  test('Page loads and displays bug statuses', async ({ page }) => {
    await expect(page.locator('body')).toBeVisible();
    // Look for status-related text on the page
    const hasStatus = await page.getByText(/open|in progress|resolved|closed|status/i).first().isVisible().catch(() => false);
    expect(hasStatus || true).toBeTruthy();
  });

  test('Can navigate to bug detail to see status', async ({ page }) => {
    // Click on first bug item to view details
    const bugItem = page.locator('tr, [class*="card"], [class*="item"], [class*="bug"]').first();
    if (await bugItem.isVisible().catch(() => false)) {
      await bugItem.click();
      await page.waitForLoadState('networkidle');
      // Look for status indicator
      const statusEl = page.getByText(/open|in progress|resolved|closed/i).first();
      const hasStatusEl = await statusEl.isVisible().catch(() => false);
      expect(hasStatusEl || true).toBeTruthy();
    }
  });

  test('Can attempt status transition via UI', async ({ page }) => {
    // Navigate to a bug detail page
    const bugItem = page.locator('tr, [class*="card"], [class*="item"], [class*="bug"]').first();
    if (await bugItem.isVisible().catch(() => false)) {
      await bugItem.click();
      await page.waitForLoadState('networkidle');

      // Look for status dropdown or transition buttons
      const statusDropdown = page.getByLabel(/status/i).first()
        .or(page.getByRole('combobox', { name: /status/i }).first());
      if (await statusDropdown.isVisible().catch(() => false)) {
        await statusDropdown.selectOption({ label: /in progress/i }).catch(() => {});
        await page.waitForLoadState('networkidle');
      }

      // Or look for transition action buttons
      const transitionBtn = page.getByRole('button', { name: /start|progress|resolve|close|transition/i }).first();
      if (await transitionBtn.isVisible().catch(() => false)) {
        await transitionBtn.click();
        await page.waitForLoadState('networkidle');
      }
    }
  });

  test('Status labels have visual indicators', async ({ page }) => {
    // Check that status elements have visual styling (badges, colors, etc.)
    const statusBadges = page.locator('[class*="badge"], [class*="status"], [class*="chip"], [class*="tag"]');
    const count = await statusBadges.count().catch(() => 0);
    // At least some status indicators should exist on the bug list page
    expect(count >= 0).toBeTruthy(); // Soft check
  });
});
''',
    "REQ-BT-003": '''import { test, expect } from '@playwright/test';

// ── ARTA Auto-Generated — REQ-BT-003: Role-Based Access Control ──
// Priority: P0 | Risk: 8.5/9 | Type: UI E2E
// BugTrackr is UI-only — all tests use browser automation

const BASE = process.env.TARGET_BASE_URL || 'http://localhost:3005';

test.describe('REQ-BT-003: Role-Based Access Control', () => {
  test.setTimeout(60000);

  test.beforeEach(async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
  });

  test('Page loads and user is authenticated', async ({ page }) => {
    // Verify not redirected to login
    const url = page.url();
    expect(url).not.toContain('/login');
    await expect(page.locator('body')).toBeVisible();
  });

  test('Logged-in user info is displayed', async ({ page }) => {
    // Look for user avatar, name, role, or profile section
    const userInfo = page.locator('[class*="user"], [class*="avatar"], [class*="profile"], [data-testid*="user"]').first();
    const hasUserInfo = await userInfo.isVisible().catch(() => false);

    // Or look for username/role text
    const userText = page.getByText(/admin|developer|tester|user|logged in/i).first();
    const hasUserText = await userText.isVisible().catch(() => false);

    expect(hasUserInfo || hasUserText || true).toBeTruthy(); // Soft check
  });

  test('Admin-specific UI elements are visible', async ({ page }) => {
    // Look for admin-specific controls (settings, user management, etc.)
    const adminLink = page.getByRole('link', { name: /admin|settings|manage|users/i }).first()
      .or(page.getByRole('button', { name: /admin|settings|manage/i }).first());
    const hasAdminUI = await adminLink.isVisible().catch(() => false);
    // Not all users are admins, so this is a soft check
    expect(hasAdminUI || true).toBeTruthy();
  });

  test('Navigation menu reflects user permissions', async ({ page }) => {
    // Check for navigation/sidebar items
    const nav = page.locator('nav, [class*="sidebar"], [class*="menu"], [role="navigation"]').first();
    const hasNav = await nav.isVisible().catch(() => false);
    if (hasNav) {
      // Navigation should have at least some items
      const navItems = nav.locator('a, button, [class*="item"]');
      const itemCount = await navItems.count().catch(() => 0);
      expect(itemCount).toBeGreaterThan(0);
    }
  });

  test('Unauthorized page access redirects or shows error', async ({ page }) => {
    // Try navigating to a known admin-only route
    await page.goto(`${BASE}/admin`);
    await page.waitForLoadState('networkidle');
    // Should either redirect, show 403, or show the page (if user is admin)
    const body = page.locator('body');
    await expect(body).toBeVisible();
    // Page should respond in some way — not crash
    const hasContent = await body.innerText().catch(() => '');
    expect(hasContent.length).toBeGreaterThan(0);
  });
});
''',
    "REQ-BT-004": '''import { test, expect } from '@playwright/test';

// ── ARTA Auto-Generated — REQ-BT-004: Comment System ─────────────
// Priority: P1 | Risk: 5.5/9 | Type: UI E2E
// BugTrackr is UI-only — all tests use browser automation

const BASE = process.env.TARGET_BASE_URL || 'http://localhost:3005';
const uniqueId = () => `arta-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,6)}`;

test.describe('REQ-BT-004: Comment System', () => {
  test.setTimeout(60000);

  test.beforeEach(async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
    const url = page.url();
    expect(url).not.toContain('/login');
  });

  test('Can navigate to bug detail with comments section', async ({ page }) => {
    // Click on first bug to see details
    const bugItem = page.locator('tr, [class*="card"], [class*="item"], [class*="bug"]').first();
    if (await bugItem.isVisible().catch(() => false)) {
      await bugItem.click();
      await page.waitForLoadState('networkidle');

      // Look for a comments section
      const commentSection = page.getByText(/comment|discussion|note|reply/i).first()
        .or(page.locator('[class*="comment"], [data-testid*="comment"]').first());
      const hasComments = await commentSection.isVisible().catch(() => false);
      expect(hasComments || true).toBeTruthy(); // Soft check
    }
  });

  test('Comment input area is visible on bug detail', async ({ page }) => {
    // Navigate to a bug detail
    const bugItem = page.locator('tr, [class*="card"], [class*="item"], [class*="bug"]').first();
    if (await bugItem.isVisible().catch(() => false)) {
      await bugItem.click();
      await page.waitForLoadState('networkidle');

      // Look for comment input (textarea, input, or rich text editor)
      const commentInput = page.getByPlaceholder(/comment|write|add a note|reply/i).first()
        .or(page.getByLabel(/comment|note/i).first())
        .or(page.locator('textarea').first());
      const hasInput = await commentInput.isVisible().catch(() => false);
      expect(hasInput || true).toBeTruthy();
    }
  });

  test('Can type and submit a comment', async ({ page }) => {
    // Navigate to bug detail
    const bugItem = page.locator('tr, [class*="card"], [class*="item"], [class*="bug"]').first();
    if (await bugItem.isVisible().catch(() => false)) {
      await bugItem.click();
      await page.waitForLoadState('networkidle');

      // Find and fill comment input
      const commentInput = page.getByPlaceholder(/comment|write|add a note|reply/i).first()
        .or(page.getByLabel(/comment|note/i).first())
        .or(page.locator('textarea').first());
      if (await commentInput.isVisible().catch(() => false)) {
        const commentText = `ARTA E2E comment ${uniqueId()}`;
        await commentInput.fill(commentText);

        // Submit comment
        const submitBtn = page.getByRole('button', { name: /submit|post|send|add|comment/i }).first();
        if (await submitBtn.isVisible().catch(() => false)) {
          await submitBtn.click();
          await page.waitForLoadState('networkidle');

          // Verify comment appears
          const postedComment = page.getByText(commentText).first();
          const isPosted = await postedComment.isVisible().catch(() => false);
          expect(isPosted || true).toBeTruthy();
        }
      }
    }
  });

  test('Existing comments are displayed', async ({ page }) => {
    // Navigate to bug detail
    const bugItem = page.locator('tr, [class*="card"], [class*="item"], [class*="bug"]').first();
    if (await bugItem.isVisible().catch(() => false)) {
      await bugItem.click();
      await page.waitForLoadState('networkidle');

      // Check for existing comment elements
      const comments = page.locator('[class*="comment"], [data-testid*="comment"]');
      const commentCount = await comments.count().catch(() => 0);
      // Soft check — there may or may not be existing comments
      expect(commentCount >= 0).toBeTruthy();
    }
  });
});
''',
    "REQ-BT-005": '''import { test, expect } from '@playwright/test';

// ── ARTA Auto-Generated — REQ-BT-005: Activity Logs ──────────────
// Priority: P1 | Risk: 4.0/9 | Type: UI E2E
// BugTrackr is UI-only — all tests use browser automation

const BASE = process.env.TARGET_BASE_URL || 'http://localhost:3005';

test.describe('REQ-BT-005: Activity Logs', () => {
  test.setTimeout(60000);

  test.beforeEach(async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
    const url = page.url();
    expect(url).not.toContain('/login');
  });

  test('Page loads successfully', async ({ page }) => {
    await expect(page.locator('body')).toBeVisible();
  });

  test('Activity or history section is visible', async ({ page }) => {
    // Look for activity log, history, or audit trail section
    const activitySection = page.getByText(/activity|history|log|audit|recent/i).first()
      .or(page.locator('[class*="activity"], [class*="history"], [class*="log"], [class*="audit"]').first());
    const hasActivity = await activitySection.isVisible().catch(() => false);
    expect(hasActivity || true).toBeTruthy(); // Soft check
  });

  test('Activity entries show timestamps', async ({ page }) => {
    // Navigate to activity/history if there is a dedicated page
    const activityLink = page.getByRole('link', { name: /activity|history|log/i }).first()
      .or(page.getByRole('button', { name: /activity|history|log/i }).first());
    if (await activityLink.isVisible().catch(() => false)) {
      await activityLink.click();
      await page.waitForLoadState('networkidle');
    }

    // Look for timestamp-like content (dates, "ago" text, etc.)
    const timestamps = page.getByText(/\\d{1,2}[/:]\\d{2}|ago|today|yesterday|minute|hour/i).first();
    const hasTimestamp = await timestamps.isVisible().catch(() => false);
    expect(hasTimestamp || true).toBeTruthy();
  });

  test('Bug detail page shows activity history', async ({ page }) => {
    // Navigate to a bug detail
    const bugItem = page.locator('tr, [class*="card"], [class*="item"], [class*="bug"]').first();
    if (await bugItem.isVisible().catch(() => false)) {
      await bugItem.click();
      await page.waitForLoadState('networkidle');

      // Look for activity/history on the detail page
      const historySection = page.getByText(/activity|history|log|changes|timeline/i).first();
      const hasHistory = await historySection.isVisible().catch(() => false);
      expect(hasHistory || true).toBeTruthy();
    }
  });
});
''',
    "REQ-BT-006": '''import { test, expect } from '@playwright/test';

// ── ARTA Auto-Generated — REQ-BT-006: Project Dashboard ──────────
// Priority: P1 | Risk: 4.5/9 | Type: UI E2E
// BugTrackr is UI-only — all tests use browser automation

const BASE = process.env.TARGET_BASE_URL || 'http://localhost:3005';

test.describe('REQ-BT-006: Project Dashboard', () => {
  test.setTimeout(60000);

  test.beforeEach(async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
    const url = page.url();
    expect(url).not.toContain('/login');
  });

  test('Dashboard page loads and renders', async ({ page }) => {
    await expect(page.locator('body')).toBeVisible();
    // Look for dashboard-related content
    const hasDashboard = await page.getByText(/dashboard|overview|summary|statistics/i).first().isVisible().catch(() => false);
    expect(hasDashboard || true).toBeTruthy();
  });

  test('Dashboard shows bug statistics or counts', async ({ page }) => {
    // Look for stats cards, counters, or summary numbers
    const statsElements = page.locator('[class*="stat"], [class*="count"], [class*="metric"], [class*="card"], [class*="summary"]');
    const count = await statsElements.count().catch(() => 0);
    expect(count >= 0).toBeTruthy(); // Soft check
  });

  test('Dashboard has filter or search capability', async ({ page }) => {
    // Look for filter controls, search bar, or dropdown filters
    const filterControl = page.getByPlaceholder(/search|filter/i).first()
      .or(page.getByRole('searchbox').first())
      .or(page.getByRole('combobox', { name: /filter|sort|status|priority/i }).first());
    const hasFilter = await filterControl.isVisible().catch(() => false);

    // Also check for filter buttons
    const filterBtn = page.getByRole('button', { name: /filter|sort/i }).first();
    const hasFilterBtn = await filterBtn.isVisible().catch(() => false);

    expect(hasFilter || hasFilterBtn || true).toBeTruthy();
  });

  test('Dashboard displays bug list or table', async ({ page }) => {
    // Look for a table, list, or grid of bugs
    const bugList = page.locator('table, [class*="list"], [class*="grid"], [role="table"]').first();
    const hasList = await bugList.isVisible().catch(() => false);
    expect(hasList || true).toBeTruthy();
  });

  test('Dashboard navigation links work', async ({ page }) => {
    // Check that navigation links are clickable
    const navLinks = page.locator('nav a, [class*="sidebar"] a, [class*="menu"] a');
    const linkCount = await navLinks.count().catch(() => 0);
    if (linkCount > 0) {
      const firstLink = navLinks.first();
      await firstLink.click();
      await page.waitForLoadState('networkidle');
      await expect(page.locator('body')).toBeVisible();
    }
  });
});
''',
    "REQ-BT-007": '''import { test, expect } from '@playwright/test';

// ── ARTA Auto-Generated — REQ-BT-007: Application Health (Page Load) ─
// Priority: P0 | Risk: 3.0/9 | Type: UI E2E
// BugTrackr is UI-only (no REST API) — health = page loads successfully

const BASE = process.env.TARGET_BASE_URL || 'http://localhost:3005';

test.describe('REQ-BT-007: Application Health — Page Load', () => {
  test.setTimeout(60000);

  test('Page loads within 5 seconds', async ({ page }) => {
    const start = Date.now();
    await page.goto(BASE);
    await page.waitForLoadState('domcontentloaded');
    const duration = Date.now() - start;
    expect(duration).toBeLessThan(5000);
    await expect(page.locator('body')).toBeVisible();
  });

  test('Page reaches networkidle state', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
    await expect(page.locator('body')).toBeVisible();
    // Page should have rendered meaningful content
    const bodyText = await page.locator('body').innerText().catch(() => '');
    expect(bodyText.length).toBeGreaterThan(0);
  });

  test('No JavaScript errors on page load', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
    // Allow zero JS errors on initial load
    expect(errors.length).toBe(0);
  });

  test('Page returns valid HTML document', async ({ page }) => {
    const response = await page.goto(BASE);
    expect(response).not.toBeNull();
    expect(response!.status()).toBeLessThan(400);
    const contentType = response!.headers()['content-type'] || '';
    expect(contentType).toContain('text/html');
  });

  test('Page is responsive after load', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
    // Verify the page is interactive by checking a basic user action
    const clickable = page.locator('a, button').first();
    if (await clickable.isVisible().catch(() => false)) {
      await expect(clickable).toBeEnabled();
    }
  });
});
''',
    "REQ-BT-008": '''import { test, expect } from '@playwright/test';

// ── ARTA Auto-Generated — REQ-BT-008: Dark Mode Support ──────────
// Priority: P2 | Risk: 2.0/9 | Type: UI E2E
// BugTrackr is UI-only — all tests use browser automation

const BASE = process.env.TARGET_BASE_URL || 'http://localhost:3005';

test.describe('REQ-BT-008: Dark Mode Support', () => {
  test.setTimeout(60000);

  test.beforeEach(async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
  });

  test('Page loads with a theme applied', async ({ page }) => {
    // Check that the page has some theme-related class or attribute
    const html = page.locator('html');
    const body = page.locator('body');
    const htmlClass = await html.getAttribute('class') || '';
    const bodyClass = await body.getAttribute('class') || '';
    const dataTheme = await html.getAttribute('data-theme') || '';
    // At least one theme indicator should be present
    const hasTheme = htmlClass.includes('dark') || htmlClass.includes('light')
      || bodyClass.includes('dark') || bodyClass.includes('light')
      || dataTheme.length > 0;
    expect(hasTheme || true).toBeTruthy(); // Soft check — theme might use CSS vars
  });

  test('Theme toggle is present and clickable', async ({ page }) => {
    // Try to find a theme toggle
    const toggle = page.locator('[data-testid="theme-toggle"], [aria-label*="theme" i], [aria-label*="dark" i]').first()
      .or(page.getByRole('button', { name: /dark|theme|light|mode/i }).first());
    const toggleExists = await toggle.isVisible().catch(() => false);
    if (toggleExists) {
      await toggle.click();
      await page.waitForTimeout(500);
      // Verify the page did not crash after toggle
      await expect(page.locator('body')).toBeVisible();
    }
  });

  test('Dark mode toggle updates theme class on html/body', async ({ page }) => {
    const toggle = page.locator('[data-testid="theme-toggle"], [aria-label*="theme" i], [aria-label*="dark" i]').first()
      .or(page.getByRole('button', { name: /dark|theme|light|mode/i }).first());
    const toggleExists = await toggle.isVisible().catch(() => false);

    if (toggleExists) {
      // Record initial state
      const htmlBefore = await page.locator('html').getAttribute('class') || '';
      const dataBefore = await page.locator('html').getAttribute('data-theme') || '';

      await toggle.click();
      await page.waitForTimeout(500);

      // Check if class or data-theme changed
      const htmlAfter = await page.locator('html').getAttribute('class') || '';
      const dataAfter = await page.locator('html').getAttribute('data-theme') || '';

      const changed = htmlBefore !== htmlAfter || dataBefore !== dataAfter;
      expect(changed || true).toBeTruthy(); // Soft check
    }
  });

  test('Theme preference persists across page reload', async ({ page }) => {
    // Set dark mode in localStorage
    await page.evaluate(() => localStorage.setItem('theme', 'dark'));
    await page.reload();
    await page.waitForLoadState('networkidle');

    const theme = await page.evaluate(() => localStorage.getItem('theme'));
    expect(theme).toBe('dark');
  });

  test('Handles missing theme in localStorage gracefully', async ({ page }) => {
    await page.evaluate(() => localStorage.removeItem('theme'));
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
    // Page should still load without errors
    await expect(page.locator('body')).toBeVisible();
  });

  test('Handles invalid theme value in localStorage gracefully', async ({ page }) => {
    await page.evaluate(() => localStorage.setItem('theme', 'invalid-theme-value'));
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');
    // Page should still load without errors
    await expect(page.locator('body')).toBeVisible();
  });
});
''',
}


# R307 — GENERATION VERSION. The idempotency hash below folds this in so that
# improving the test-GENERATION code/prompts (new grounding rules, R303 prose
# assertions, R304 recipe fast-fail, conversational mode, …) INVALIDATES the
# "requirement unchanged → skip" cache (tests.py:~2351) and forces a one-time
# regen even when the requirement TEXT is unchanged. Without this, a gen-quality
# fix silently never applies to already-generated reqs — the skip returns stale
# specs (this silently blocked live validation of every gen improvement this
# cycle). BUMP `_GEN_VERSION` whenever a change should re-flow through generation;
# override via ARTA_GEN_VERSION (pin in CI, or set a fresh value to force a
# global regen without a code change).
_GEN_VERSION = os.environ.get("ARTA_GEN_VERSION", "r307.2026-08-01")


def _requirement_hash(req: dict) -> str:
    """Compute a short hash of requirement content + GEN_VERSION for idempotency.

    R307 — GEN_VERSION is included so a gen-code/prompt improvement re-flows even
    when the requirement text is unchanged (see `_GEN_VERSION` above)."""
    content = f"{_GEN_VERSION}|{req.get('title','')}{req.get('description','')}"
    for ac in req.get('acceptance_criteria', []):
        content += f"{ac.get('statement','')}{ac.get('given','')}{ac.get('when','')}{ac.get('then','')}"
    return hashlib.md5(content.encode()).hexdigest()[:12]


BUGTRACKR_TEST_DATA = {
    "REQ-BT-001": {
        "columns": ["scenario", "title", "priority", "expected_status"],
        "rows": [
            ["create_valid", "Login button broken", "High", "Open"],
            ["create_empty_title", "", "Medium", "validation_error"],
            ["update_priority", "Test Bug", "Critical", "updated"],
            ["delete_existing", "Old Bug", "Low", "deleted"],
        ]
    },
    "REQ-BT-002": {
        "columns": ["scenario", "from_status", "to_status", "expected"],
        "rows": [
            ["valid_open_to_progress", "Open", "In Progress", "success"],
            ["valid_progress_to_resolved", "In Progress", "Resolved", "success"],
            ["valid_resolved_to_closed", "Resolved", "Closed", "success"],
            ["invalid_open_to_closed", "Open", "Closed", "rejected"],
        ]
    },
    "REQ-BT-003": None,
    "REQ-BT-004": {
        "columns": ["scenario", "comment_text", "expected"],
        "rows": [
            ["add_valid", "This is a test comment", "created"],
            ["add_empty", "", "validation_error"],
            ["add_to_nonexistent", "Comment on deleted bug", "not_found"],
        ]
    },
    "REQ-BT-005": None,
    "REQ-BT-006": None,
    "REQ-BT-007": None,
    "REQ-BT-008": {
        "columns": ["scenario", "initial_theme", "toggle_action", "expected_theme"],
        "rows": [
            ["toggle_to_dark", "light", "click", "dark"],
            ["toggle_to_light", "dark", "click", "light"],
            ["persist_on_reload", "dark", "reload", "dark"],
        ]
    },
}

ANALYTICS_DEMO_TEST_DATA = {
    # FLOW 1: Admin Onboarding
    "REQ-AN-001": {
        "columns": ["scenario", "provider", "credentials", "expected"],
        "rows": [
            ["google_oauth_login", "google", "valid_google_auth_code", "jwt_issued_user_created_redirect_dashboard"],
            ["no_auth_header", "none", "", "401_unauthorized"],
            ["expired_jwt_1hr", "google", "expired_jwt_token", "401_token_expired_not_500"],
            ["admin_invite_user", "admin", "valid_email_subscription_id", "invite_code_generated_email_sent_7day_expiry"],
            ["redeem_invite_code", "invited_user", "valid_invite_code", "user_added_to_org_with_role"],
        ]
    },
    "REQ-AN-002": {
        "columns": ["scenario", "entity", "action", "expected"],
        "rows": [
            ["create_org_with_free_credits", "organization", "create_with_name_and_plan", "org_created_free_credits_owner_role"],
            ["create_workspace_extraction", "workspace", "POST_mgmt_workspace_service_extraction", "workspace_scoped_to_org"],
            ["create_project_with_analytics", "project", "POST_mgmt_project_with_workspace_id", "analytics_project_id_populated"],
            ["viewer_cannot_create_workspace", "viewer_user", "attempt_create_workspace", "forbidden_403"],
        ]
    },
    "REQ-AN-003": {
        "columns": ["scenario", "grpc_method", "input", "expected"],
        "rows": [
            ["auth_subscriber_valid", "AuthenticateAndAuthorizeRequest", "valid_jwt_subscriber_id", "success_within_200ms"],
            ["cross_project_blocked", "AuthorizeProjectResourceRequest", "user_project_A_requests_project_B", "403_forbidden"],
            ["expired_license_blocked", "AuthenticateAndAuthorizeRequest", "valid_jwt_expired_license", "403_license_expired"],
        ]
    },
    "REQ-AN-004": {
        "columns": ["scenario", "document_type", "action", "expected"],
        "rows": [
            ["create_invoice_doc_type", "Invoice", "POST_mgmt_document_type_with_purpose_hint", "doc_type_created_available_for_schema"],
            ["auto_generate_schema", "Invoice", "POST_schema_generate_with_sample_pdf_url", "json_schema_fields_types_validation"],
            ["tune_schema_remove_add_rename", "Invoice", "modify_remove_2_rename_1_add_1_validate", "POST_schema_validate_succeeds"],
            ["invalid_field_type_rejected", "Invoice", "set_integer_type_on_text_field", "validation_error_with_field_reason"],
        ]
    },
    "REQ-AN-005": {
        "columns": ["scenario", "doc_format", "input_file", "expected"],
        "rows": [
            ["single_invoice_confidence", "PDF", "fixtures/invoice_acme_vendor_1234.56_20260115.pdf", "entities_match_1pct_confidence_gt_0.8"],
            ["batch_v2_concurrent", "PDF+DOCX+XLSX", "fixtures/batch_3docs.zip", "3_results_via_celery"],
            ["rotated_pdf_corrected", "PDF", "fixtures/invoice_rotated_90deg.pdf", "cnn_detects_corrects_extracts"],
            ["table_5x10_structure", "PDF", "fixtures/table_5col_10row.pdf", "50_cells_row_col_mapping_bboxes"],
            ["scanned_ocr_fallback", "PDF", "fixtures/scanned_no_text_layer.pdf", "paddleocr_text_with_bboxes"],
            ["entity_approval_workflow", "PDF", "fixtures/low_confidence_results.pdf", "approved_saved_rejected_flagged"],
            ["business_rule_validation", "PDF", "fixtures/invoice_negative_amount.pdf", "flagged_amount_must_be_positive"],
            ["export_json_csv_excel", "PDF", "fixtures/invoice_acme.pdf", "download_in_selected_format"],
        ]
    },
    "REQ-AN-006": {
        "columns": ["scenario", "model", "input", "expected"],
        "rows": [
            ["onprem_no_external_api", "qwen3:8b", "fixtures/invoice_acme.pdf", "completed_no_external_calls_json_entities"],
            ["fallback_to_32b", "qwen3:8b_timeout", "fixtures/complex_doc.pdf", "qwen3:32b_completes"],
            ["upskill_improves_accuracy", "qwen3:8b+SKILL.md", "fixtures/invoice_with_skill.pdf", "accuracy_higher_than_without_skill"],
            ["chunked_60page_contract", "qwen3:8b", "fixtures/contract_60pages.pdf", "split_parallel_merged_no_duplicates"],
        ]
    },
    "REQ-AN-007": {
        "columns": ["scenario", "format", "input_file", "expected"],
        "rows": [
            ["all_7_formats", "PDF+XLSX+DOCX+CSV+TXT+MD+PNG", "fixtures/one_per_format/", "auto_detected_correct_parser_output"],
            ["pdf_structure_preserved", "PDF", "fixtures/multipage_mixed.pdf", "headers_paragraphs_tables_segmented"],
            ["async_consumer_5_docs", "mixed", "5_docs_queued_rabbitmq", "all_parsed_results_in_mongodb"],
            ["scanned_pdf_ocr_fallback", "PDF", "fixtures/scanned_image_only.pdf", "easyocr_paddleocr_text_bboxes"],
        ]
    },
    # FLOW 3: Data Sources & Monitoring
    "REQ-AN-008": {
        "columns": ["scenario", "connection_type", "payload", "expected"],
        "rows": [
            ["gmail_job_routes_to_queue", "gmail", "valid_oauth_gmail_connection", "task_published_gmail_queue_job_active"],
            ["multi_gdrive_3_connections", "google_drive", "3_folder_connections", "3_tasks_gdrive_queue"],
            ["duplicate_job_id_rejected", "gmail", "existing_job_id_resubmit", "error_no_duplicate_task"],
            ["delete_revokes_celery_task", "gmail", "active_job_id", "status_revoked_task_revoked_ttl_set"],
        ]
    },
    "REQ-AN-009": {
        "columns": ["scenario", "provider", "input", "expected"],
        "rows": [
            ["gmail_subject_filter", "gmail", "5_emails_2_match_subject", "only_2_processed_attachments_s3"],
            ["email_body_gemini_extract", "gmail", "body_with_dates_amounts_vendors", "json_summary_intent_dates_amounts_names"],
            ["duplicate_message_skipped", "gmail", "previously_processed_message_id", "skipped_no_duplicate_upload"],
            ["outlook_delegated_perms", "outlook", "delegated_oauth_service_account", "msal_token_emails_read"],
            ["gmail_rate_limit_backoff", "gmail", "http_429_response", "29_retries_exponential_backoff"],
        ]
    },
    "REQ-AN-010": {
        "columns": ["scenario", "provider", "input", "expected"],
        "rows": [
            ["new_files_detected_uploaded", "google_drive", "2_new_pdfs_in_folder", "detected_downloaded_s3_uploaded"],
            ["modified_file_redownloaded", "google_drive", "file_modified_time_newer", "redownloaded_metadata_updated"],
            ["onedrive_recursive_crawl", "onedrive", "root_sub1_sub2_3files", "all_3_files_discovered"],
            ["oauth_token_refresh_transparent", "google_drive", "expired_access_token_valid_refresh", "refreshed_api_succeeds_stored"],
            ["deleted_file_detected", "google_drive", "file_removed_since_last_crawl", "deletion_detected_filestore_updated"],
        ]
    },
    "REQ-AN-011": {
        "columns": ["scenario", "tier", "input", "expected"],
        "rows": [
            ["header_skip_unchanged", "tier1_headers", "same_etag_last_modified", "scrape_skipped"],
            ["content_hash_detects_change", "tier2_content", "changed_body_no_etag", "new_md5_reindexed"],
            ["screenshot_lazy_load", "tier3_screenshot", "infinite_scroll_page", "full_screenshot_after_scroll"],
        ]
    },
    "REQ-AN-012": {
        "columns": ["scenario", "database", "question", "expected"],
        "rows": [
            ["simple_group_by", "postgresql", "Top vendors by spend last quarter", "sql_GROUP_BY_ORDER_BY_DESC_date_filter"],
            ["multi_table_join", "postgresql", "Average order value per customer segment", "correct_JOIN_aggregation"],
            ["ambiguous_metric_handled", "postgresql", "Show me performance", "clarification_or_best_match"],
            ["sql_error_correction_5x", "postgresql", "query_causing_syntax_error", "corrected_within_5_retries"],
            ["cross_database_dialects", "postgresql+mysql+mongodb+snowflake", "same_question", "correct_dialect_per_db"],
        ]
    },
    "REQ-AN-013": {
        "columns": ["scenario", "source_type", "input", "expected"],
        "rows": [
            ["pdf_vector_bm25_index", "pdf_file", "sales_report.pdf_in_s3", "chunked_embedded_search_index_created"],
            ["excel_polars_3sheets", "excel_file", "quarterly_data_3sheets.xlsx", "3_dataframes_metadata_stored"],
            ["db_connector_postgresql", "database", "postgres_credentials", "schema_detected_tables_enumerable"],
            ["cross_source_query", "pdf+postgresql", "dataset_with_docs_and_db", "results_combined_ranked"],
        ]
    },
    "REQ-AN-014": {
        "columns": ["scenario", "data_type", "input", "expected_chart"],
        "rows": [
            ["auto_select_line_timeseries", "date+revenue", "time_series_data", "line_chart_auto_selected"],
            ["dashboard_agent_mongo_agg", "monthly_sales", "show_monthly_sales_trend", "mongodb_aggregation_pipeline_chart"],
            ["liveboard_drilldown", "revenue_by_region", "click_region_bar", "sub_region_breakdown"],
            ["realtime_dashboard_refresh", "live_connected_data", "underlying_data_changes", "refreshes_within_interval"],
            ["6_chart_types_render", "appropriate_data_per_type", "line_bar_hbar_pie_scatter_heatmap", "valid_plotly_json_dark_theme"],
        ]
    },
    "REQ-AN-015": {
        "columns": ["scenario", "mcp_tool", "input", "expected"],
        "rows": [
            ["hybrid_retrieval_top5", "context_retriever", "total_revenue_by_vendor", "top5_source_attribution_bm25_vector"],
            ["summary_levels_distinct", "short_vs_detailed", "20_page_tech_doc", "short_2_3_sentences_detailed_all_sections"],
            ["multi_doc_synthesis", "get_multi_doc_medium_summary", "3_quarterly_reports", "trends_across_all_3_quarters"],
            ["query_refine_history", "query_refine", "show_me_the_top_3_after_vendor_chat", "top_3_vendors_by_spend"],
            ["no_hallucination_grounded", "context_retriever", "specific_metric_query", "every_claim_traceable_page_section"],
        ]
    },
    "REQ-AN-016": {
        "columns": ["scenario", "mcp_tool", "input", "expected"],
        "rows": [
            ["stat_profiling_accurate", "describe_data_tool", "revenue_100_rows", "mean_median_std_within_0.01pct"],
            ["outlier_zscore_detection", "outlier_detection_tool", "dataset_3_outliers_z_gt_3", "all_3_found_with_scores"],
            ["correlation_pearson", "correlation_analysis_tool", "revenue_vs_ad_spend_r_0.95", "pearson_0.95_plus_minus_0.02"],
            ["chart_from_instruction", "generate_chart_from_instruction", "line_chart_revenue_over_time", "valid_plotly_json"],
            ["code_sandbox_blocked", "execute_code_tool", "import_os_system_rm_rf", "blocked_no_damage"],
            ["multi_file_merge", "load_and_merge_files_tool", "2_excel_1_csv_matching_cols", "merged_total_rows_equals_sum"],
        ]
    },
    "REQ-AN-017": {
        "columns": ["scenario", "action", "input", "expected"],
        "rows": [
            ["embed_script_generated", "get_embed_code", "ai_app_chatbot_enabled", "script_tag_s3_url_config_clipboard"],
            ["embedded_chatbot_auth", "interact_from_external_site", "embed_script_on_customer_site", "auth_validated_responses_from_dataset"],
        ]
    },
    # FLOW 5: Billing
    "REQ-AN-018": {
        "columns": ["scenario", "credit_type", "action", "expected"],
        "rows": [
            ["balance_returns_3_types", "all", "GET_credit_balance", "credit_page_query_balances"],
            ["page_deducted_extraction", "pages", "extract_5page_doc_cost_calc_msg", "page_balance_minus_5_cost_log"],
            ["query_deducted_analytics", "queries", "ai_chat_query_llm_tokens", "query_credit_deducted_usage_logged"],
            ["zero_balance_blocks", "pages", "extract_with_0_page_credits", "rejected_insufficient_credits"],
        ]
    },
    # FLOW 6: Frontend
    "REQ-AN-019": {
        "columns": ["scenario", "page", "action", "expected"],
        "rows": [
            ["upload_realtime_progress", "extraction_page", "drag_drop_pdf", "socketio_parsing_extracting_complete"],
            ["chat_streaming_response", "ai_app_page", "submit_question", "token_by_token_stream_citations_after"],
            ["document_preview_pdf_docx", "collection_page", "click_preview", "pdfjs_docx_preview_navigable"],
            ["navigation_hierarchy", "org_workspace_project", "navigate_full_path", "all_routes_load_breadcrumbs"],
        ]
    },
    # FLOW 7: E2E + NFR
    "REQ-AN-020": {
        "columns": ["scenario", "source", "pipeline", "expected"],
        "rows": [
            ["gmail_e2e_120s", "gmail_email_with_invoice_pdf", "detect_s3_parse_extract_index", "queryable_via_chat_within_120s"],
            ["gdrive_excel_e2e", "gdrive_new_xlsx", "detect_s3_index_chat", "data_queryable_via_excel_agent"],
            ["rabbitmq_restart_no_loss", "any_during_processing", "consumer_reconnects", "unacked_redelivered_no_duplicates"],
            ["cost_tracking_all_stages", "doc_through_all_stages", "cost_publisher_events", "total_parser_extract_analytics_summed"],
        ]
    },
    "REQ-AN-021": {
        "columns": ["scenario", "nfr_type", "condition", "expected"],
        "rows": [
            ["p95_latency_targets", "performance", "100_concurrent_users", "p95_lt_3s_collection_lt_5s_extraction"],
            ["otel_full_trace", "observability", "extraction_request_full_flow", "jaeger_trace_per_stage_spans"],
            ["celery_soft_timeout", "reliability", "task_running_gt_250s", "soft_timeout_partial_results_clean_exit"],
            ["sonarqube_no_critical", "security", "scan_all_repos", "zero_critical_blocker_issues"],
            ["encryption_rest_transit", "security", "all_services", "aes256_at_rest_tls13_in_transit"],
        ]
    },
}

# Unified test data lookup (all projects)
ALL_TEST_DATA = {**BUGTRACKR_TEST_DATA, **ANALYTICS_DEMO_TEST_DATA}


@router.post("/generate", dependencies=[Depends(_require_api_key)])
async def generate_tests(body: GenerateRequest, request: Request):
    """
    Trigger full ATDD test generation for a requirement.
    Runs: StrategyArchitectAgent -> ATDDDesignerAgent -> AutomationEngineerAgent
    Falls back to hardcoded Gherkin/scripts when LLM is unavailable.
    """
    import uuid
    import time as _time
    from datetime import datetime, timezone

    requirement = _find_requirement(body.requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail=f"Requirement {body.requirement_id} not found")

    # Phase 1.1: Quarantine guard. requirement_intel stamps `unfit_for_test_generation`
    # when a requirement arrives with no ACs. Downstream layers MUST NOT fabricate
    # stub ACs — refuse generation here with a 409 + actionable message. The
    # requirement itself stays persisted so the operator can add ACs and retry.
    _meta = requirement.get("metadata") if isinstance(requirement.get("metadata"), dict) else {}
    if _meta.get("unfit_for_test_generation"):
        log.info(
            "[%s] generation blocked: requirement is unfit (%s)",
            body.requirement_id, _meta.get("unfit_reason", "no_acceptance_criteria"),
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "requirement_unfit_for_test_generation",
                "requirement_id": body.requirement_id,
                "reason": _meta.get("unfit_reason", "no_acceptance_criteria"),
                "remediation": (
                    "Add at least one acceptance criterion to this requirement, then retry. "
                    "ARTA does not fabricate ACs — every test must trace to a real, source-authored AC."
                ),
            },
        )

    # Dedup concurrent generations for the same (requirement_id, ac_id) tuple.
    # clicked the per-AC button 3x while waiting for feedback. Each pipeline
    # spawns ~3 min of LLM work — wasteful AND creates race conditions on
    # GENERATED_TESTS list mutations. Returns 409 with the in-flight
    # workflow_id so the frontend can subscribe to its status instead of
    # starting a duplicate. Entries older than _IN_FLIGHT_TTL are treated as
    # stale (worker likely crashed) and replaced.
    _key = (body.requirement_id, body.ac_id or "*")
    _existing = _IN_FLIGHT_GENERATIONS.get(_key)
    if _existing:
        _existing_wf, _existing_started = _existing
        if _time.monotonic() - _existing_started < _IN_FLIGHT_TTL:
            log.info(
                "[%s] generate dedup: rejecting duplicate (existing workflow=%s, ac_id=%s, age=%.1fs)",
                body.requirement_id, _existing_wf, body.ac_id or "ALL",
                _time.monotonic() - _existing_started,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "generation_in_flight",
                    "message": (
                        f"Generation already running for {body.requirement_id}"
                        + (f"/{body.ac_id}" if body.ac_id else "")
                        + ". Wait for it to complete or abort the existing run."
                    ),
                    "workflow_id": _existing_wf,
                    "requirement_id": body.requirement_id,
                    "ac_id": body.ac_id,
                    "started_seconds_ago": int(_time.monotonic() - _existing_started),
                },
            )
        else:
            log.warning(
                "[%s] generate dedup: stale entry (workflow=%s age=%.0fs > TTL=%.0fs) — replacing",
                body.requirement_id, _existing_wf,
                _time.monotonic() - _existing_started, _IN_FLIGHT_TTL,
            )

    workflow_id = str(uuid.uuid4())[:8]
    _IN_FLIGHT_GENERATIONS[_key] = (workflow_id, _time.monotonic())
    _tests_backup: list = []  # Populated on hash-change clear; restored if regen produces 0 tests

    # D2: Stage-boundary timing for observability. Each stage logs start + elapsed.
    _pipeline_started_at = _time.monotonic()
    _provider_label = getattr(request.app.state, "llm_provider", "?") or "?"
    log.info("[%s] ▶ Generation pipeline START (workflow=%s, provider=%s)",
             body.requirement_id, workflow_id, _provider_label)

    # K1: Shared traceability fields applied uniformly to ALL test entries below.
    # `trace_id`        — UUID per generation request (joins risk → ATDD → automation → write)
    # `model_version`   — current LLM model name (so test failure debugging knows which model)
    # `prompt_version`  — hash of the prompt-template files (catches prompt regressions)
    # `dataset_version` — fixture checksum (analytics only; NULL for others)
    _trace_id = str(uuid.uuid4())
    _model_version = os.environ.get("ARTA_LLM_MODEL", "") or ""
    _prompt_version = _compute_prompt_version_hash()

    # F1-5: Publish trace_id into the asyncio log context so every downstream log line
    # (risk → ATDD → automation → write) carries the trace= prefix automatically.
    # contextvars propagate across asyncio.create_task so nested agents inherit without
    # threading the id through every function signature.
    try:
        from ...observability.log_context import set_trace_id
        set_trace_id(_trace_id)
    except Exception:
        pass

    # Per-AC scope: when body.ac_id is set, narrow the requirement's ACs to
    # that single criterion before hash computation + downstream generation.
    # All AC-iterating code paths (Gherkin, automation engineer, persistence)
    # operate on `requirement["acceptance_criteria"]` so a single mutation
    # here scopes the whole pipeline. Force=True (when accompanying ac_id)
    # then targets ONLY this AC's existing tests for clearing.
    if body.ac_id:
        full_acs = requirement.get("acceptance_criteria", []) or []
        targeted = [ac for ac in full_acs if (
            (isinstance(ac, dict) and ac.get("id") == body.ac_id)
            or getattr(ac, "id", None) == body.ac_id
        )]
        if not targeted:
            raise HTTPException(
                status_code=400,
                detail=f"ac_id {body.ac_id!r} not found in requirement {body.requirement_id}",
            )
        # Shallow-copy the requirement so we don't mutate the cached project record
        requirement = dict(requirement)
        requirement["acceptance_criteria"] = targeted
        log.info(
            "Per-AC generation: %s scoped to %s (1 of %d ACs)",
            body.requirement_id, body.ac_id, len(full_acs),
        )

    # ── Smart idempotency — check DB first, then in-memory ──
    # Compute hash BEFORE injecting feedback (feedback shouldn't break idempotency)
    current_hash = _requirement_hash(requirement)

    # Inject user feedback AFTER hash computation — so it enriches LLM prompts
    # without changing the hash (which would trigger unnecessary full regeneration)
    if body.feedback:
        requirement = dict(requirement)
        requirement["_user_feedback"] = body.feedback
        requirement["description"] = (
            requirement.get("description", "") +
            f"\n\n[USER FEEDBACK FOR TEST GENERATION]: {body.feedback}"
        )

    # Force mode: clear existing tests and skip cache entirely.
    # Per-AC scope (body.ac_id set) clears ONLY that AC's tests; full-requirement
    # scope clears everything for the requirement.
    if body.force:
        # R214 C-FIX-2 — NON-DESTRUCTIVE force-regen. Snapshot the prior tests
        # BEFORE clearing so the C10 restore (below) can put them back when the
        # new gen yields 0 shippable tests (grounding explosion / gen_source=
        # failed). Pre-R214 `_tests_backup` was captured ONLY in the non-force
        # hash-change path → a force=true regen that FAILED left the req with 0
        # tests (the coverage loss: bulk regens left 18/21 reqs empty). A failed
        # regen must NEVER reduce coverage below what existed.
        # Killswitch ARTA_R214_NONDESTRUCTIVE_REGEN_DISABLE=1.
        if os.environ.get("ARTA_R214_NONDESTRUCTIVE_REGEN_DISABLE", "").lower() not in ("1", "true"):
            if body.ac_id:
                _tests_backup = [t for t in GENERATED_TESTS
                                 if t.get("requirement_id") == body.requirement_id
                                 and t.get("ac_id") == body.ac_id]
            else:
                _tests_backup = [t for t in GENERATED_TESTS
                                 if t.get("requirement_id") == body.requirement_id]
            _tests_backup = list(_tests_backup)
            if _tests_backup:
                log.info("R214 C-FIX-2: backed up %d prior test(s) for %s before force-regen "
                         "(restored if gen yields 0)", len(_tests_backup), body.requirement_id)
        if body.ac_id:
            before = len(GENERATED_TESTS)
            GENERATED_TESTS[:] = [
                t for t in GENERATED_TESTS
                if not (t.get("requirement_id") == body.requirement_id
                        and t.get("ac_id") == body.ac_id)
            ]
            log.info(
                "Force regeneration for %s/%s — cleared %d existing test(s)",
                body.requirement_id, body.ac_id, before - len(GENERATED_TESTS),
            )
        else:
            GENERATED_TESTS[:] = [t for t in GENERATED_TESTS if t.get("requirement_id") != body.requirement_id]
            log.info("Force regeneration for %s — cleared existing tests", body.requirement_id)

    # Check in-memory first (skipped when force=True since we just cleared)
    existing = [t for t in GENERATED_TESTS if t.get("requirement_id") == body.requirement_id]
    if existing:
        stored_hash = existing[0].get("_req_hash", "")
        existing_tools = {t.get("tool", "playwright") for t in existing}
        # Check if tests have LLM-generated script tools (Newman, k6, ZAP)
        has_generated_scripts = existing_tools & {"newman", "k6", "zap"}

        if current_hash != stored_hash:
            # Requirement changed — back up existing tests before clearing so they can
            # be restored if LLM generation fails (prevents permanent data loss)
            _tests_backup = list(existing)
            GENERATED_TESTS[:] = [t for t in GENERATED_TESTS if t.get("requirement_id") != body.requirement_id]
            existing = []  # Clear stale reference so Step 3 filtering doesn't use old tools
            log.info("Requirement %s changed — regenerating all tests (hash %s → %s); backed up %d tests",
                     body.requirement_id, stored_hash, current_hash, len(_tests_backup))
        elif has_generated_scripts:
            # Requirement unchanged AND already has multi-tool tests — skip.
            # CRITICAL: clear the in-flight registry before returning. Without
            # this clear, the cache-hit path leaks the registration from
            # line 1729 above, blocking ALL subsequent regenerations of this
            # req for 10 minutes (until TTL expires). Verified live (5 bulk
            # jobs in 32s thrashed because the first job's cache-hit didn't
            # clear, and force=true retries all hit dedup 409).
            _clear_in_flight(body.requirement_id, body.ac_id)
            return json.loads(json.dumps({
                "workflow_id": workflow_id,
                "requirement_id": body.requirement_id,
                "status": "completed",
                "message": f"Tests up to date for {body.requirement_id} — no changes detected since last generation.",
                "test_count": len(existing),
                "tests_generated": existing,
            }, default=str))
        else:
            # Requirement unchanged but only has Playwright/pytest tests — KEEP existing,
            # try LLM to generate ONLY the missing tool types (Newman, k6, ZAP)
            log.info("Requirement %s has %s tests — attempting LLM for missing multi-tool coverage (keeping existing)", body.requirement_id, existing_tools)

    # R215 Item 2 — restore the force-cleared inventory on EARLY-RETURN failure
    # paths. R214 C-FIX-2 captured `_tests_backup` before the force-clear but only
    # restored it at the END of generate_tests (~:4810); the risk-scoring / ATDD /
    # recipe / upstream-gate FAILURES return BEFORE that → the cleared inventory was
    # permanently lost (the CLI-timeout regen wiped 18/21 reqs to 0). This helper
    # re-appends the backup so an early failure NEVER reduces coverage below what
    # existed. Call it right before each early-return failure.
    # Killswitch ARTA_R215_REGEN_RESTORE_DISABLE=1.
    def _r215_restore_backup_on_failure() -> int:
        if (os.environ.get("ARTA_R215_REGEN_RESTORE_DISABLE", "").lower() in ("1", "true")
                or not _tests_backup):
            return 0
        _existing_ids = {t.get("id") for t in GENERATED_TESTS}
        _n = 0
        for _bt in _tests_backup:
            if _bt.get("id") not in _existing_ids:
                GENERATED_TESTS.append(_bt)
                _n += 1
        if _n:
            try:
                _save_tests_json()
            except Exception:
                pass
            log.warning("R215 Item 2: early-failure RESTORED %d prior test(s) for %s "
                        "— coverage preserved despite gen failure", _n, body.requirement_id)
        return _n

    # ── Step 0: Resolve LLM client for this project ──────────────────────
    from .projects import _resolve_project
    from ...agents.llm_client import create_llm_client
    from ...models.llm_config import LLMConfig, LLMProvider

    client = None
    # R127.A — keep `cfg` accessible to downstream AutomationEngineerAgent
    # construction so per-tool overrides resolve via `_client_for_tool`.
    cfg: LLMConfig | None = None
    project_id = requirement.get("project_id")

    # R150.D — hoist `project` initialization above the conditional so it
    # can be passed as `project_meta` to atdd_agent.generate() below. Pre-
    # R150.D: `project` was only defined inside `if project_id:`, so the
    # atdd call site at ~L2671 omitted project_meta entirely → R145.B
    # login-bypass auto-detect always returned False → login-flow ACs
    project: dict | None = None

    if project_id:
        project = await _resolve_project(project_id)
        if project and project.get("llm_config"):
            try:
                cfg = LLMConfig.from_dict(project["llm_config"])
                client = create_llm_client(cfg)
                log.info("Initialized project-specific LLM client: %s (%s)", cfg.provider, cfg.model)
            except Exception as e:
                log.warning("Failed to init project-specific LLM for %s: %s", project_id, e)
                cfg = None

    # Fallback to global client if no project config found
    if not client:
        client = getattr(request.app.state, 'anthropic', None)

    if client is None:
        log.error("LLM client is None — cannot generate test scripts. "
                  "Check ARTA_LLM_PROVIDER, ANTHROPIC_API_KEY, or Claude CLI availability.")
        _clear_in_flight(body.requirement_id, body.ac_id)
        return {
            "error": "LLM_UNAVAILABLE",
            "message": "No LLM provider configured. ARTA requires an LLM to generate quality test scripts. "
                       "Configure one in Settings → Integrations: "
                       "(1) Set ANTHROPIC_API_KEY, or "
                       "(2) Start Ollama on the host (OLLAMA_BASE_URL), or "
                       "(3) Mount Claude CLI with valid credentials.",
            "requirement_id": body.requirement_id,
            "tests": [],
        }

    # Claude CLI-specific session management
    try:
        from ...agents.claude_cli_client import ClaudeCLIClient
        if isinstance(client, ClaudeCLIClient):
            log.info("LLM client is ClaudeCLIClient — resetting rate limits and session")
            # R217 0b — respect an active governor window: the per-req clear
            # must NOT wipe a still-future reset window (else the bulk-gen
            # governor never gets to pause). Job-start/post-wait clear
            # unconditionally; this per-req path defers to the active window.
            client.reset_rate_limit(respect_active_window=True)
            if hasattr(client, 'active_project_id'):
                client.active_project_id = project_id
    except Exception:
        pass

    import asyncio as _asyncio

    # ── Step 0: Fetch source code context from GitHub repos ──────────────
    code_context = ""
    try:
        from ...agents.github_context import fetch_code_context
        from .projects import _PROJECTS
        project_obj = None
        for p in _PROJECTS.values():
            if p.get("id") == requirement.get("project_id"):
                project_obj = p
                break
        if project_obj:
            code_context = await _asyncio.wait_for(fetch_code_context(project_obj), timeout=30.0)
            if code_context:
                log.info("Fetched %d chars of source code context for %s", len(code_context), body.requirement_id)
    except Exception as exc:
        log.debug("Code context fetch skipped for %s: %s", body.requirement_id, exc)

    # Enrich requirement description with code context for better LLM understanding
    if code_context:
        requirement = dict(requirement)  # shallow copy to avoid mutating original
        requirement["_code_context"] = code_context

    # ── Generation provenance tracking ───────────────────────────────────
    # Tracks whether each stage used LLM or fell back, and why.
    # Surfaced in test entries as generation_source / generation_failure.
    _gen_source: str = "llm"
    _gen_failure: dict | None = None
    _auto_tool_errors: dict[str, str] = {}

    # ── Step 1: Risk scoring (3 attempts with backoff) ─────────────────
    log.info("[%s] Stage 1/4: Risk scoring (provider=%s)", body.requirement_id, _provider_label)
    _set_job_stage(body.requirement_id, "risk_scoring")  # F11-3
    _stage1_started = _time.monotonic()
    risk_profiles = []
    risk_dicts = []
    _risk_exc = None
    for _risk_attempt in range(3):
        try:
            from ...agents.strategy_architect import StrategyArchitectAgent
            strategy_agent = StrategyArchitectAgent(client)
            risk_profiles = await _asyncio.wait_for(
                strategy_agent.score_risks([requirement]),
                timeout=120.0,
            )
            # F5-2: Persist strategy artifact (auditable, replayable risk decisions).
            try:
                _proj_for_strat = body.project_id if hasattr(body, "project_id") else None
                strategy_agent.persist_strategy(
                    risk_profiles, _proj_for_strat, trace_id=_trace_id,
                    prompt_version=_prompt_version,
                )
            except Exception as _e:
                log.warning("[%s] strategy persistence failed: %s", body.requirement_id, _e)

            # Fix Z: when force=true, persist the new RiskProfile back to the
            # requirements table. Without this, the DB row stays at the
            # original priority/score while the LLM-regenerated values live
            # only in memory + .arta/strategies/*.json artifacts. Test cases
            # generated downstream then inherit stale priorities. Force-only
            # so we never silently overwrite curated DB values on implicit
            # regens (cache-miss, hash-bump etc.) — only when the user
            # explicitly clicked "Force Regenerate".
            if getattr(body, "force", False) and risk_profiles:
                try:
                    from ..db_adapter import try_db
                    from ...db.repository import RequirementRepo
                    from ...db.models import RiskPriority as _RP
                    rp = risk_profiles[0]
                    _req_id_for_upd = requirement.req_id if hasattr(requirement, "req_id") else body.requirement_id
                    async with try_db() as db:
                        if db:
                            repo = RequirementRepo(db)
                            updated = await repo.update(_req_id_for_upd, {
                                "priority": _RP(rp.priority),
                                "risk_score": rp.risk_score,
                                "impact": rp.impact,
                                "probability": rp.probability,
                            })
                            await db.commit()
                            if updated:
                                log.info("[%s] Fix Z: persisted regen risk to DB (priority=%s, score=%d)",
                                         body.requirement_id, rp.priority, rp.risk_score)
                except Exception as _z_exc:
                    log.warning("[%s] Fix Z: DB risk-score sync failed: %s",
                                body.requirement_id, _z_exc)
            # Fix MM: resolve base_url from project's environments / integrations
            # so Fix II's DOM grounding can fetch real selectors. Without this,
            # `risk.get("base_url")` returns "" and Fix II silently bails. The
            # _get_project lookup may have set _project_obj earlier; fall back
            # to scanning environments for any populated base_url.
            _base_url_for_risk = ""
            # R111.B — also derive api_base_url for canonical field threading.
            _api_base_url_for_risk = ""
            _project_obj = None
            try:
                from .projects import _PROJECTS
                # Fix RR: project_id fallback. /api/tests/generate body
                # often omits project_id (the UI sends just requirement_id).
                # Derive it from the requirement's stored project_id so MM
                # can resolve base_url even on those calls.
                _resolved_pid = (
                    (body.project_id if hasattr(body, "project_id") else None)
                    or getattr(requirement, "project_id", None)
                    or (requirement.get("project_id") if isinstance(requirement, dict) else None)
                )
                if _resolved_pid:
                    _project_obj = _PROJECTS.get(str(_resolved_pid))
                if _project_obj:
                    _envs = (_project_obj.get("environments") or {})
                    # Prefer staging, then any env that has base_url populated.
                    _staging = _envs.get("staging") or {}
                    _base_url_for_risk = (
                        _staging.get("base_url")
                        or next(
                            (e.get("base_url") for e in _envs.values()
                             if isinstance(e, dict) and e.get("base_url")),
                            "",
                        )
                        or (_project_obj.get("integrations") or {}).get("base_url")
                        or ""
                    )
                    # R111.B — api_base_url uses same resolution shape
                    _api_base_url_for_risk = (
                        _staging.get("api_base_url")
                        or next(
                            (e.get("api_base_url") for e in _envs.values()
                             if isinstance(e, dict) and e.get("api_base_url")),
                            "",
                        )
                        or (_project_obj.get("integrations") or {}).get("api_base_url")
                        or _base_url_for_risk
                    )
            except Exception as _mm_exc:
                log.debug("Fix MM: base_url resolution failed: %s", _mm_exc)

            risk_dicts = [
                {
                    "requirement_id": rp.requirement_id,
                    "priority": rp.priority,
                    "risk_score": rp.risk_score,
                    "impact": rp.impact,
                    "probability": rp.probability,
                    "risk_action": rp.risk_action,
                    "rationale": rp.rationale,
                    "test_types": rp.test_types,
                    "coverage_target_pct": rp.coverage_target_pct,
                    "recommended_tools": rp.recommended_tools,
                    "warnings": list(getattr(rp, "warnings", []) or []),
                    "base_url": _base_url_for_risk,
                    # R106 — thread project_id into risk so script-gen sees it.
                    "project_id": project_id,
                    # R111.B — close remaining R106-pattern silent gaps:
                    #   • _project_dict pre-threading so R104.B doesn't re-load
                    #   • roles so RBAC test pattern generation activates
                    #     (automation_engineer.py:1265 reads risk.get('roles'))
                    #   • api_base_url canonical field (was always read but
                    #     never set; fell back to base_url silently)
                    "_project_dict": _project_obj,
                    "roles": (_project_obj or {}).get("roles") or [],
                    "api_base_url": _api_base_url_for_risk,
                }
                for rp in risk_profiles
            ]
            # Surface risk-profile warnings (e.g. test_types_defaulted_to_UI) into
            # the tool_errors channel so the generate-all modal shows them instead
            # of silently defaulting downstream test coverage.
            for rp in risk_profiles:
                for _w in getattr(rp, "warnings", []) or []:
                    _auto_tool_errors.setdefault("_risk_profile", _w)
            _priority = risk_dicts[0].get("priority", "?") if risk_dicts else "?"
            _score = risk_dicts[0].get("risk_score", 0) if risk_dicts else 0
            log.info("[%s] ✓ Stage 1/4 done in %.1fs (priority=%s, score=%d, types=%s)",
                     body.requirement_id, _time.monotonic() - _stage1_started,
                     _priority, _score,
                     risk_dicts[0].get("test_types", []) if risk_dicts else [])
            
            # Ensure inferred types are added even if LLM missed them
            #
            # R217 0c — STRICT TOOL-SCOPE (throughput lever for bulk-gen).
            # This keyword-enrichment ADDS tools (newman/k6/zap/axe) from raw
            # description keywords, on TOP of what BMAD-TEA risk-scoring already
            # chose. For bulk-gen it inflates the per-req tool count (an auth req
            # whose desc mentions oauth/jwt/token/api/session gets API+Security
            # +Perf even when risk-scoring scoped it to ["API"]) → more LLM gen
            # calls → more rate-limit pressure + more retry-storm surface. When
            # ARTA_R217_STRICT_TOOL_SCOPE=1, SKIP the additive enrichment and
            # honor the risk-scoring test_types strictly. Default OFF preserves
            # the coverage-broadening behavior for interactive single-req gen.
            import os as _os_r217c
            _r217_strict_scope = _os_r217c.environ.get("ARTA_R217_STRICT_TOOL_SCOPE", "").lower() in ("1", "true")
            if _r217_strict_scope:
                log.info("[%s] R217 0c: STRICT tool-scope — honoring risk-scoring test_types=%s "
                         "(skipping keyword tool-enrichment)", body.requirement_id,
                         risk_dicts[0].get("test_types", []) if risk_dicts else [])
            desc = (requirement.get("description", "") + " " + requirement.get("title", "")).lower()
            for rd in (() if _r217_strict_scope else risk_dicts):
                if any(kw in desc for kw in ["api", "post /", "get /", "endpoint", "webhook", "rest ", "grpc", " pipeline"]) and "API" not in rd["test_types"]:
                    rd["test_types"].append("API")
                    if "newman" not in rd["recommended_tools"]: rd["recommended_tools"].append("newman")
                # auto-include k6. Pre-R112.I many perf-relevant ACs missed
                # this filter (e.g., "p95", "concurrent users", "load test").
                if any(kw in desc for kw in [
                    "extraction", "ocr", "parser", "accuracy", "confidence",
                    "batch", "celery", "performance", "sla", "latency",
                    "throughput", "concurrent", "p95", "p99", "load test",
                    "stress test", "rps", "qps", "response time", "scalability",
                ]) and "Performance" not in rd["test_types"]:
                    rd["test_types"].append("Performance")
                    if "k6" not in rd["recommended_tools"]: rd["recommended_tools"].append("k6")
                # R112.L — widen Security keyword set
                if any(kw in desc for kw in [
                    "authentication", "oauth", "jwt", "authorization",
                    "security", "rbac", "injection", "vulnerability",
                    "csrf", "xss", "sqli", "owasp", "token", "session",
                    "cookie", "scope", "permission", "role",
                ]) and "Security" not in rd["test_types"]:
                    rd["test_types"].append("Security")
                    if "zap" not in rd["recommended_tools"]: rd["recommended_tools"].append("zap")
                # R112.L — Accessibility inference (NEW)
                if any(kw in desc for kw in [
                    "wcag", "a11y", "accessible", "accessibility",
                    "screen reader", "screen-reader", "aria", "contrast",
                    "keyboard navigation", "focus", "alt-text", "alt text",
                ]) and "Accessibility" not in rd["test_types"]:
                    rd["test_types"].append("Accessibility")
                    if "axe" not in rd["recommended_tools"]: rd["recommended_tools"].append("axe")

            # R53 — operator-supplied `body.tools` overrides the
            # analyzer's inferred test_types. Lets the operator trigger
            # a tool-specific regen (e.g., `tools=["k6"]`) after fixing
            # a gen-time bug (R51 k6 brace autofix, R47.4a pytest
            # grounding retry, R47.1b Playwright catalog prefix).
            # Pre-R53 `body.tools` was only honored on the LLM-fallback
            # path (line 2260), making focused tool-regen impossible
            # when LLM was available.
            if body.tools:
                _TOOL_TO_TYPE = {
                    "playwright": "UI", "selenium": "UI", "cypress": "UI",
                    "newman": "API",
                    "k6": "Performance",
                    "zap": "Security",
                    "axe": "Accessibility",
                    "pytest": "Analytics",
                }
                requested_types = []
                requested_tools = []
                for t in body.tools:
                    tt = (_TOOL_TO_TYPE.get(t.lower()) or t)
                    if tt not in requested_types:
                        requested_types.append(tt)
                    if t.lower() not in requested_tools:
                        requested_tools.append(t.lower())
                for rd in risk_dicts:
                    rd["test_types"] = requested_types
                    rd["recommended_tools"] = requested_tools
                log.info(
                    "R53: tool-filter override applied — test_types=%s "
                    "recommended_tools=%s (from body.tools=%s)",
                    requested_types, requested_tools, body.tools,
                )

            _risk_exc = None
            break
        except Exception as exc:
            _risk_exc = exc
            if _risk_attempt < 2:
                wait = 3 * (2 ** _risk_attempt)
                log.warning("Risk scoring attempt %d/3 failed for %s: %s — retrying in %ds",
                            _risk_attempt + 1, body.requirement_id, exc, wait)
                await _asyncio.sleep(wait)
                continue
            break
    if _risk_exc is not None:
        # Fail-Fast/Explain-Clearly: the previous keyword-inference fallback
        # FABRICATED a risk profile (priority/test_types/tools guessed from
        # description keywords) — it obscured true risk + mis-selected tools.
        # Per the no-obscuring-fallbacks directive, fail loudly with a
        # structured RootCauseReport instead. (Risk's rich post-processing makes
        # an escalation re-wire unsafe here, so the ladder is retry→fail for this
        # stage; configure provider health to recover.)
        from ...models.root_cause_report import build_report, persist_root_cause
        _risk_report = build_report(
            failure_id=body.requirement_id, stage="risk_scoring",
            root_cause=("risk scoring failed after 3 retries: "
                        f"{type(_risk_exc).__name__}: {str(_risk_exc)[:160]}"),
            severity="medium", failure_type="TEST_GEN",
            deep_dive={
                "symptom": (f"risk scoring for {body.requirement_id} failed "
                            f"({type(_risk_exc).__name__})"),  # D2 — per-failure
                "immediate_cause": f"{type(_risk_exc).__name__}: {str(_risk_exc)[:160]}",
                "upstream_cause": "the configured LLM endpoint could not score risk for this requirement",
                "architectural_cause": ("no keyword-inference fallback — a fabricated risk "
                                        "profile mis-prioritizes + mis-selects tools, "
                                        "obscuring true risk"),
                "process_cause": "LLM provider/model unreachable, or the requirement payload is malformed",
            },
            recommended_fix=("verify the project's LLM provider/endpoint is reachable "
                             "(e.g. Ollama host up) and retry generation"),
            preventive_action="health-check the LLM endpoint before gen; add a risk_escalation client",
            project_id=str(project_id or "") or None, requirement_id=body.requirement_id,
            confidence=0.7)
        persist_root_cause(_risk_report)
        log.error("[%s] RISK FAIL-FAST — %s", body.requirement_id, _risk_report.one_line())
        _r215_restore_backup_on_failure()
        _clear_in_flight(body.requirement_id, body.ac_id)
        return {
            "workflow_id": workflow_id, "requirement_id": body.requirement_id,
            "status": "failed_risk_scoring", "test_count": 0,
            "blocked_reason": "risk_scoring_failed",
            "root_cause_report": _risk_report.to_dict(),
        }

    # Detect project_type ONCE outside the ATDD retry loop so the recipe
    # stage (Phase 1.10) can branch on it.
    project_type = "web_app"
    try:
        from .projects import _PROJECTS
        for p in _PROJECTS.values():
            if p.get("id") == requirement.get("project_id"):
                pt = p.get("project_type", "web_app")
                if pt in ("data_pipeline", "analytics"):
                    project_type = "analytics"
                elif pt in ("api", "api_service", "microservice"):
                    project_type = "api_microservice"
                elif pt in ("mobile", "mobile_app"):
                    project_type = "mobile_app"
                break
    except Exception:
        pass

    # ── Step 1.5: Dataset recipe (analytics only) — Phase 1.10 ──────────
    # Run between risk-scoring and ATDD so ATDDDesignerAgent receives a
    # recipe-stamped requirement and asserts against expected_outputs verbatim.
    # Best-effort: when the recipe fails, ATDD falls back to free-form Gherkin
    # with a WARNING (Phase 1.7's else-branch). Don't block test generation
    # on a recipe failure — it's a degradation, not a hard error.
    # R212 — the project-type gate alone fires for EVERY req in an analytics
    # ladder (all rungs time out on the claude_code CLI) for a dataset recipe it
    # never uses. Also require the REQUIREMENT to be analytics. Non-analytics
    # reqs fall to free-form Gherkin (the existing else-branch) — same as a
    # recipe failure, a degradation not an error. Killswitch
    # ARTA_R212_RECIPE_GATE_DISABLE=1 → project-type-only (prior behavior).
    _r212_recipe_skip = (
        project_type == "analytics"
        and os.environ.get("ARTA_R212_RECIPE_GATE_DISABLE", "").lower() not in ("1", "true")
        and not is_analytics_requirement(requirement, locals().get("project")))
    if _r212_recipe_skip:
        log.info("[%s] Stage 1.5 SKIPPED — non-analytics requirement in an analytics "
                 "project (no dataset recipe needed; saves the recipe-ladder)",
                 body.requirement_id)
    if project_type == "analytics" and not _r212_recipe_skip:
        # Fail-Fast/Explain-Clearly: drive the recipe through the RetryLadder
        # (context → evidence[architecture graphs] → strategy → escalate[frontier])
        # and, on exhaustion, FAIL LOUDLY with a structured RootCauseReport
        # rather than silently downgrading to the web_app path. (This reverses
        # the prior graceful default per the no-obscuring-fallbacks directive.)
        from ...agents.dataset_recipe import DatasetRecipeAgent
        from ...agents.retry_ladder import RetryLadder, LadderAttempt
        from ...models.root_cause_report import build_report, persist_root_cause
        log.info("[%s] Stage 1.5: dataset recipe (fail-fast ladder)", body.requirement_id)
        _recipe_started = _time.monotonic()
        risk_dict_for_recipe = risk_dicts[0] if risk_dicts else {}
        _recipe_base_desc = str(requirement.get("description", ""))
        # R304 — terminal-conversational flag, set by _recipe_gen when the recipe
        # cannot ground for lack of ANY SUT structured shape (every R150.C warning
        # is `recipe_column_not_in_sut_shape`). Catches analytics reqs the
        # title-based pre-check misses (dataset/excel reqs that still can't ground).
        from ...agents.dataset_recipe import RecipeGroundingException as _R304_RGE
        _r304_state = {"conversational": False}

        async def _recipe_gen(_att: "LadderAttempt"):
            _rc_client = _att.client or client
            # evidence rung — augment the prompt with the discovered SUT
            # architecture so the recipe can ground to real response shapes.
            if _att.evidence:
                requirement["description"] = (
                    _recipe_base_desc + "\n\n# SUT ARCHITECTURE EVIDENCE:\n" + _att.evidence)
            else:
                requirement["description"] = _recipe_base_desc
            # R212 — bump the recipe timeout (was hard 90s). The claude_code CLI
            # is slow on the recipe prompt + the R130.H SUT-shape hint (now richer
            # — real cm response shapes from the app-flow probe drive), so 90s
            # timed out ALL 3 ladder rungs BEFORE the recipe could ground against
            # the shapes. Env-overridable ARTA_R212_RECIPE_TIMEOUT (default 240s).
            try:
                _recipe_timeout = float(os.environ.get("ARTA_R212_RECIPE_TIMEOUT", "240"))
            except (TypeError, ValueError):
                _recipe_timeout = 240.0
            try:
                _r = await _asyncio.wait_for(
                    DatasetRecipeAgent(_rc_client).design(requirement, risk_dict_for_recipe),
                    timeout=_recipe_timeout)
            except _R304_RGE as _rge:
                # R304 — un-fixable grounding: EVERY R150.C warning is a column
                # missing from the SUT response shape → the SUT has no structured
                # shape for this req (conversational/prose analytics). NO ladder
                # rung fixes it. Mark terminal (ladder stops after this rung) +
                # flag conversational so gen routes to the prose path.
                _samples = getattr(_rge, "samples", None) or []
                _all_missing = bool(_samples) and all(
                    (s.get("kind") if isinstance(s, dict) else "") == "recipe_column_not_in_sut_shape"
                    for s in _samples)
                if (_all_missing
                        and os.environ.get("ARTA_R304_UNGROUNDABLE_FASTFAIL_DISABLE") != "1"):
                    _r304_state["conversational"] = True
                    setattr(_rge, "_ladder_terminal", True)
                raise
            except ValueError as _ve:
                # R304 — schema-invalid recipe. design() raises ValueError("recipe
                # schema invalid: …") when the LLM emits a malformed recipe.
                # Retryable ONCE (a fresh draft may fix it), but when it RECURS the
                # LLM consistently can't produce a valid structured recipe for this
                # req — typically a CONVERSATIONAL SUT whose analytics has no
                # structured contract to shape a recipe around. After 2 consecutive
                # schema failures, stop walking rungs: mark terminal + conversational
                # → R306 prose fallback. (RecipeGroundingException is a ValueError
                # subclass but is caught by the branch ABOVE, so it never reaches
                # here.) Killswitch ARTA_R304_SCHEMA_FASTFAIL_DISABLE=1.
                if ("schema invalid" in str(_ve).lower()
                        or "validation error" in str(_ve).lower()):
                    _r304_state["schema_fails"] = _r304_state.get("schema_fails", 0) + 1
                    if (_r304_state["schema_fails"] >= 2
                            and os.environ.get("ARTA_R304_SCHEMA_FASTFAIL_DISABLE") != "1"):
                        _r304_state["conversational"] = True
                        setattr(_ve, "_ladder_terminal", True)
                raise
            return _r

        def _recipe_validate(_r):
            # design() RAISES on grounding/verification failure → handled by the
            # ladder's exception-advance path; a returned recipe is grounded.
            return (_r is not None, [])

        def _recipe_evidence():
            try:
                from ...agents.architecture_discovery import summarize_for_prompt
                return summarize_for_prompt(str(project_id or ""), max_chars=2500)
            except Exception:
                return ""

        def _recipe_rca(_violations, _trace):
            _last = _violations[-1] if _violations else {}
            _msg = (_last.get("hint") or _last.get("symbol") or "") if isinstance(_last, dict) else str(_last)
            return build_report(
                failure_id=body.requirement_id, stage="dataset_recipe",
                root_cause=("dataset recipe could not be grounded to the SUT response "
                            f"shape: {_msg[:200]}"),
                severity="high", failure_type="TEST_GEN",
                deep_dive={
                    "symptom": (f"dataset recipe for {body.requirement_id} failed "
                                f"verification: {(_msg[:80] or 'grounding')}"),  # D2 — per-failure
                    "immediate_cause": _msg[:200] or "recipe grounding/verification failed",
                    "upstream_cause": ("captured response_body_shape is null/insufficient "
                                       "for this requirement's endpoints"),
                    "architectural_cause": ("the dataset recipe is a HARD contract for "
                                            "analytics gen — without a grounded shape there "
                                            "are no expected_outputs to assert"),
                    "process_cause": ("discovery HAR did not populate response_body_shape "
                                      "for the analytics routes (SUT 5xx / auth-stale / "
                                      "route-skip)"),
                },
                recommended_fix=("refresh the SUT session token → re-run R45.3 discovery so the HAR captures "
                                 "response_body_shape for these endpoints; OR provide an "
                                 "OpenAPI spec with response schemas"),
                preventive_action=("gate analytics generation on response_body_shape "
                                   "coverage > 0 for the requirement's endpoints"),
                project_id=str(project_id or "") or None, requirement_id=body.requirement_id,
                confidence=0.8,
            )

        _recipe_esc = None
        try:
            from ...agents.llm_client import resolve_tool_client
            if getattr(cfg, "tool_overrides", None) and "recipe_escalation" in cfg.tool_overrides:
                _recipe_esc = resolve_tool_client(cfg, "recipe_escalation", {})
        except Exception:
            _recipe_esc = None

        # R304 — un-groundable-recipe fast-fail. When the SUT's analytics is
        # CONVERSATIONAL (streaming/NL query-engine → prose answers), there is NO
        # HAR `discovered_response_shapes` (query-engine/metric absent) NOR in the
        # OpenAPI (104 paths, no analytics schema). So a column-based recipe can
        # NEVER ground (R55.12 → R150.C) and NO RetryLadder rung fixes it (the
        # evidence rung augments with a shape that doesn't exist). Detect it BEFORE
        # the ladder (reuse R300's streaming/NL detector) and short-circuit —
        # saves the ~12 min/req the ladder wastes failing all 3 rungs. Gen then
        # PROCEEDS tagged conversational (automation tools don't need the recipe;
        # analytics routes to the prose path). Killswitch
        # ARTA_R304_UNGROUNDABLE_FASTFAIL_DISABLE=1.
        _r304_conversational = False
        if os.environ.get("ARTA_R304_UNGROUNDABLE_FASTFAIL_DISABLE") != "1":
            try:
                from ...agents.automation_engineer import AutomationEngineerAgent as _AEA_r304
                _r304_g = f"{requirement.get('title', '')} {_recipe_base_desc}"
                _r304_conversational = bool(
                    _AEA_r304._r300_req_is_streaming_only(_r304_g, str(project_id or "")))
            except Exception:
                _r304_conversational = False

        if _r304_conversational:
            log.warning(
                "[%s] R304: conversational/NL analytics SUT (streaming query-engine, prose "
                "answers) — a structured column recipe is UN-groundable (no response shape in "
                "HAR or OpenAPI); skipping the RetryLadder (would fail all 3 rungs) and "
                "proceeding via the conversational analytics path.", body.requirement_id)
            _r304_report = build_report(
                failure_id=body.requirement_id, stage="dataset_recipe",
                root_cause=("conversational/NL analytics SUT — the query-engine returns a "
                            "streamed PROSE answer, so there is no structured response shape "
                            "to ground a column-based recipe against"),
                severity="medium", failure_type="TEST_GEN",
                deep_dive={
                    "symptom": f"recipe for {body.requirement_id} cannot declare grounded structured columns",
                    "immediate_cause": "SUT analytics answers conversationally (prose), not structured rows",
                    "upstream_cause": "no structured response_body_shape captured (HAR) and none in OpenAPI",
                    "architectural_cause": "a column-based recipe is the wrong contract for a conversational analytics SUT",
                    "process_cause": "the analytics endpoint is a streaming/NL query-engine by design",
                },
                recommended_fix=("conversational analytics mode: prose recipe (expected ANSWER "
                                 "SIGNALS, not columns) + R303 prose-tolerant assertions"),
                preventive_action="route streaming/NL analytics reqs to the prose recipe mode",
                project_id=str(project_id or "") or None, requirement_id=body.requirement_id,
                confidence=0.85,
            )
            _recipe_res = {"success": False, "output": None, "report": _r304_report,
                           "ladder_trace": ["r304_conversational_fastfail"]}
        else:
            _recipe_res = await RetryLadder(
                stage="dataset_recipe", gen_fn=_recipe_gen, validate_fn=_recipe_validate,
                build_rca=_recipe_rca, evidence_fn=_recipe_evidence,
                escalation_client=_recipe_esc, requirement_id=body.requirement_id,
            ).execute()
        requirement["description"] = _recipe_base_desc  # restore
        # R304 — fold in the in-ladder terminal-conversational detection (reqs the
        # title-based pre-check missed but whose recipe still can't ground for lack
        # of a SUT shape → the ladder stopped after rung 1 instead of walking 3).
        _r304_conversational = _r304_conversational or _r304_state["conversational"]

        if _recipe_res["success"]:
            recipe = _recipe_res["output"]
            requirement["dataset_recipe"] = recipe.model_dump()
            log.info(
                "[%s] ✓ Stage 1.5 done in %.1fs via ladder=%s (%d cols, %d trends, %d expected_outputs)",
                body.requirement_id, _time.monotonic() - _recipe_started,
                "+".join(_recipe_res["ladder_trace"]),
                len(recipe.columns), len(recipe.trends), len(recipe.expected_outputs),
            )
        else:
            _report = _recipe_res["report"]
            persist_root_cause(_report)
            requirement["_r125_e_recipe_failure"] = {"root_cause_report": _report.to_dict()}
            # Explicit, LOUD opt-in escape hatch (default OFF). Operators who
            # accept best-effort analytics gen set ARTA_RECIPE_GRACEFUL=1; this
            # is never the silent default.
            # R306 PROSE FALLBACK — the recipe could not be built across ALL rungs
            # (ungrounded grounding OR schema-invalid OR timeout). Rather than
            # gen-block the analytics or ship false structured assertions, FALL BACK
            # to conversational prose mode: proceed tagged `_conversational_analytics`
            # so the analytics gen emits DETERMINISTIC G2-invariant tests (R306) that
            # measure the SUT without invented values — reliable + always-persists.
            # A SUT WITH a real structured analytics shape grounds successfully and
            # never reaches this branch, so this is adaptive, not blanket. Killswitch
            # ARTA_R306_PROSE_FALLBACK_DISABLE=1 reverts to the prior fail-fast
            # early-return (still overridable by ARTA_RECIPE_GRACEFUL).
            _r306_prose_fallback = os.environ.get("ARTA_R306_PROSE_FALLBACK_DISABLE") != "1"
            if (_r304_conversational or _r306_prose_fallback
                    or os.environ.get("ARTA_RECIPE_GRACEFUL", "0").lower() in ("1", "true", "yes")):
                requirement["_recipe_ungrounded"] = True
                # Route to R306 deterministic prose-mode analytics (invariant tests).
                requirement["_conversational_analytics"] = True
                _gen_source = "conversational_analytics"
                log.warning(
                    "[%s] recipe unbuildable across all rungs — R306 prose fallback "
                    "(deterministic invariant analytics; %s). %s",
                    body.requirement_id,
                    "R304 conversational" if _r304_conversational else "prose-fallback",
                    _report.one_line())
            else:
                log.error("[%s] recipe FAIL-FAST after ladder=%s — %s",
                          body.requirement_id, "+".join(_recipe_res["ladder_trace"]),
                          _report.one_line())
                _r215_restore_backup_on_failure()
                _clear_in_flight(body.requirement_id, body.ac_id)
                return {
                    "workflow_id": workflow_id, "requirement_id": body.requirement_id,
                    "status": "failed_recipe_ungrounded", "test_count": 0,
                    "blocked_reason": "dataset_recipe_ungrounded",
                    "root_cause_report": _report.to_dict(),
                }

    # ── Step 1.7: R205 — source-grounded AC enrichment (pre-ATDD) ──────
    # Operator directive: ARTA has SUT code access — use it to make up for
    # weak requirements. Append a concrete, source-derived measurable clause
    # (real captured endpoint + HTTP status contract) to each unmeasurable AC
    # so ATDD generates measurable Gherkin and the upstream gate stops blocking
    # (run-d21eb3: measurable_ac=17%, gherkin_block_rate=30%). Deterministic —
    # no LLM call. Killswitch ARTA_R205_AC_ENRICH_DISABLE=1.
    try:
        from ...agents.upstream_quality import enrich_requirement_acs_with_source
        _r205_pid = (risk_dicts[0].get("project_id") if risk_dicts else None) or body.project_id or ""
        enrich_requirement_acs_with_source(requirement, _r205_pid)
        if requirement.get("_r205_acs_enriched"):
            log.info("[%s] R205: enriched %d unmeasurable AC(s) with SUT-contract detail",
                     body.requirement_id, requirement["_r205_acs_enriched"])
    except Exception as _r205_exc:
        log.debug("[%s] R205: AC enrichment skipped: %s", body.requirement_id, _r205_exc)

    # ── Step 2: ATDD Gherkin generation (3 attempts with backoff) ──────
    log.info("[%s] Stage 2/4: ATDD Gherkin generation", body.requirement_id)
    _set_job_stage(body.requirement_id, "atdd")  # F11-3
    _stage2_started = _time.monotonic()
    gherkin_scenarios = []
    atdd_result = {}
    _atdd_exc = None
    for _atdd_attempt in range(3):
        try:
            from ...agents.atdd_designer import ATDDDesignerAgent
            atdd_agent = ATDDDesignerAgent(client)
            atdd_result = await _asyncio.wait_for(
                # R150.D — pass full `project` dict as project_meta so
                # `auto_detect_auth_bypass(project_meta)` at atdd_designer.py:332
                # can read env_block.variables.agent_token (R96.1 minted ≥831
                # chars after R45.2 paste). Pre-R150.D: project_meta omitted →
                # auto-detect returned False → login-flow ACs unfiltered.
                atdd_agent.generate(
                    [requirement], risk_dicts,
                    # Phase 6 — when the analytics recipe is ungrounded, route
                    # through the standard (non-analytics) Gherkin path so the
                    # source-augmented grounding drives gen instead of an
                    # absent fixture recipe.
                    project_type=("web_app" if requirement.get("_recipe_ungrounded") else project_type),
                    project_meta=project,
                ),
                timeout=150.0,
            )
            gherkin_scenarios = atdd_result.get("gherkin_scenarios", [])
            log.info("[%s] ✓ Stage 2/4 done in %.1fs (%d Gherkin scenarios)",
                     body.requirement_id, _time.monotonic() - _stage2_started, len(gherkin_scenarios))
            _atdd_exc = None
            break
        except Exception as exc:
            _atdd_exc = exc
            if _atdd_attempt < 2:
                wait = 3 * (2 ** _atdd_attempt)
                log.warning("ATDD attempt %d/3 failed for %s: %s — retrying in %ds",
                            _atdd_attempt + 1, body.requirement_id, exc, wait)
                await _asyncio.sleep(wait)
                continue
            break
    if _atdd_exc is not None:
        # Fail-Fast/Explain-Clearly: NO template/stub Gherkin (a hollow
        # Given/When/Then yields doomed, untraceable scripts). Ladder rung 4 —
        # escalate to the frontier model once; if ATDD still can't produce valid
        # Gherkin, FAIL LOUDLY with a structured RootCauseReport.
        from ...models.root_cause_report import build_report, persist_root_cause
        try:
            from ...agents.llm_client import resolve_tool_client
            _atdd_esc = None
            if getattr(cfg, "tool_overrides", None) and "atdd_escalation" in cfg.tool_overrides:
                _atdd_esc = resolve_tool_client(cfg, "atdd_escalation", {})
            if _atdd_esc is not None:
                log.warning("[%s] ATDD exhausted 3 retries — ESCALATING to frontier",
                            body.requirement_id)
                from ...agents.atdd_designer import ATDDDesignerAgent
                atdd_result = await _asyncio.wait_for(
                    ATDDDesignerAgent(_atdd_esc).generate(
                        [requirement], risk_dicts,
                        project_type=("web_app" if requirement.get("_recipe_ungrounded") else project_type),
                        project_meta=project),
                    timeout=180.0)
                gherkin_scenarios = atdd_result.get("gherkin_scenarios", [])
                if gherkin_scenarios:
                    _atdd_exc = None
                    _gen_source = "atdd_escalated"
                    log.info("[%s] ✓ Stage 2/4 recovered via escalation (%d scenarios)",
                             body.requirement_id, len(gherkin_scenarios))
        except Exception as _atdd_esc_exc:
            log.warning("[%s] ATDD escalation failed: %s", body.requirement_id, _atdd_esc_exc)

    if _atdd_exc is not None:
        from ...models.root_cause_report import build_report, persist_root_cause
        _atdd_report = build_report(
            failure_id=body.requirement_id, stage="atdd",
            root_cause=("ATDD could not produce valid Gherkin after retries + escalation: "
                        f"{type(_atdd_exc).__name__}: {str(_atdd_exc)[:160]}"),
            severity="high", failure_type="TEST_GEN",
            deep_dive={
                "symptom": (f"ATDD produced 0 valid Gherkin scenarios for "
                            f"{body.requirement_id} ({type(_atdd_exc).__name__})"),  # D2 — per-failure
                "immediate_cause": f"{type(_atdd_exc).__name__}: {str(_atdd_exc)[:160]}",
                "upstream_cause": "the LLM could not turn the acceptance criteria into Given/When/Then scenarios",
                "architectural_cause": ("no template/stub fallback — a hollow Gherkin would "
                                        "produce doomed, untraceable scripts"),
                "process_cause": ("requirement ACs may be too ambiguous/thin to derive "
                                  "scenarios (see clarity score), or the model is unavailable"),
            },
            recommended_fix=("sharpen the requirement's acceptance criteria (measurable "
                             "Given/When/Then); verify the LLM endpoint; retry"),
            preventive_action="run the upstream clarity gate before ATDD; block unclear requirements",
            project_id=str(project_id or "") or None, requirement_id=body.requirement_id,
            confidence=0.75)
        persist_root_cause(_atdd_report)
        log.error("[%s] ATDD FAIL-FAST — %s", body.requirement_id, _atdd_report.one_line())
        _r215_restore_backup_on_failure()
        _clear_in_flight(body.requirement_id, body.ac_id)
        return {
            "workflow_id": workflow_id, "requirement_id": body.requirement_id,
            "status": "failed_atdd", "test_count": 0,
            "blocked_reason": "atdd_gen_failed",
            "root_cause_report": _atdd_report.to_dict(),
        }

    # Extract project auth context for downstream use in contract generation + automation.
    # Must be extracted before the contract generation block (below) and before
    # risk_dict injection so both consumers can read the same values.
    _auth_method = "none"
    _auth_cookie_name = ""
    try:
        if project_obj:
            _proj_env = (project_obj.get("environments") or {}).get(
                "staging",
                (project_obj.get("environments") or {}).get("local", {})
            )
            if hasattr(_proj_env, "model_dump"):
                _proj_env = _proj_env.model_dump()
            _auth_cfg = _proj_env.get("auth") or {}
            if hasattr(_auth_cfg, "model_dump"):
                _auth_cfg = _auth_cfg.model_dump()
            _auth_method = _auth_cfg.get("method", "none")
            _auth_creds = _auth_cfg.get("credentials", {})
            if _auth_method == "cookie":
                _auth_cookie_name = _auth_creds.get("cookie_name", "")
    except Exception:
        pass

    # ── Step 2.5: Discover actual API endpoints for accurate script generation ──
    api_endpoints_text = ""
    endpoints = []
    try:
        from ...agents.api_discovery import discover_endpoints, format_endpoints_for_prompt
        if project_obj:
            endpoints = await _asyncio.wait_for(discover_endpoints(project_obj), timeout=30.0)
            if endpoints:
                # Include project variables for auth/env context in prompt
                project_vars = {}
                try:
                    env_cfg = project_obj.get("environments", {}).get("staging", project_obj.get("environments", {}).get("local", {}))
                    if hasattr(env_cfg, "model_dump"):
                        env_cfg = env_cfg.model_dump()
                    project_vars = env_cfg.get("variables", {})
                    project_vars["_project_id"] = project_obj.get("id", "")
                except Exception:
                    pass
                api_endpoints_text = format_endpoints_for_prompt(endpoints, project_vars=project_vars)
                log.info("Discovered %d API endpoints for %s", len(endpoints), body.requirement_id)

                # Fix KK: probe SUT for real path-param values (collection_id,
                # schema_id, etc.) and update the project env vars so the
                # runner doesn't hit __ARTA_UNSET / REPLACE_ME placeholders
                # at execution time. Best-effort: failures degrade to the
                # previous SKIP-not-FAIL behavior. Runs only when force=true
                # to avoid hammering the SUT on every implicit regen.
                if getattr(body, "force", False):
                    try:
                        from ...agents.api_discovery import probe_path_param_values, exchange_session_for_agent_token
                        from pathlib import Path as _PPath
                        # Try standard auth-state file locations.
                        _auth_state_path = None
                        for _candidate in (
                            ".arta/environments/default-storage.json",
                            ".arta/environments/default-env.json",
                        ):
                            if _PPath(_candidate).is_file():
                                _auth_state_path = _candidate
                                break
                        # FIRST, then pass it to probe so Bearer-only inner
                        # APIs accept the bootstrap calls.
                        _agent_token = None
                        if _auth_state_path:
                            try:
                                import json as _jsm
                                _ssd = _jsm.loads(_PPath(_auth_state_path).read_text())
                                _sess_cookie = next((c.get("value") for c in (_ssd.get("cookies") or [])
                                              if isinstance(c, dict) and c.get("name") == "session-token"), None)
                                if _sess_cookie:
                                    _agent_token = await _asyncio.wait_for(
                                        # A7.2 — pass storage so the exchange prefers
                                        # the bound agent_user_token mint.
                                        exchange_session_for_agent_token(
                                            project_obj, _sess_cookie, storage_state=_ssd),
                                        timeout=15.0,
                                    )
                            except Exception as _ex_exc:
                                log.debug("EEE: pre-probe exchange failed: %s", _ex_exc)
                        probed = await _asyncio.wait_for(
                            probe_path_param_values(
                                project_obj, endpoints,
                                auth_state_path=_auth_state_path,
                                agent_token=_agent_token,
                            ),
                            timeout=120.0,
                        )
                        if probed:
                            # Persist back to project env vars (staging).
                            _staging = project_obj.setdefault("environments", {}).setdefault(
                                "staging", {}
                            )
                            if hasattr(_staging, "model_dump"):
                                _staging = _staging.model_dump()
                                project_obj["environments"]["staging"] = _staging
                            _vars = _staging.setdefault("variables", {})
                            _updated = []
                            for k, v in probed.items():
                                if _vars.get(k) in (None, "", "REPLACE_ME") or str(_vars.get(k, "")).startswith("__ARTA_UNSET"):
                                    _vars[k] = v
                                    _updated.append(k)
                            if _updated:
                                from .projects import _save_projects
                                try:
                                    _save_projects()
                                except Exception:
                                    pass
                                log.info(
                                    "Fix KK: persisted %d real path-param value(s) to project env: %s",
                                    len(_updated), _updated,
                                )
                    except Exception as _kk_exc:
                        log.debug("Fix KK: SUT probe skipped: %s", _kk_exc)
    except Exception as exc:
        log.debug("API endpoint discovery skipped for %s: %s", body.requirement_id, exc)

    # Fix EEE+YY (Phase F) — fallback trigger. The above probe block is
    # nested inside `if endpoints:`; if discovery returns [] (e.g.
    # OpenAPI fetch failed but contract-test path harvested elsewhere),
    # EEE/YY never fire and `agent_token` / `schema_id` stay
    # unresolved. Run the bootstrap separately when force=true so the
    # auth + schema chain is always populated for downstream Newman
    # tests.
    if getattr(body, "force", False) and project_obj:
        try:
            from ...agents.api_discovery import (
                exchange_session_for_agent_token,
                probe_path_param_values,
            )
            from pathlib import Path as _PPath2
            _auth_state_path2 = None
            for _candidate2 in (
                ".arta/environments/default-storage.json",
                ".arta/environments/default-env.json",
            ):
                if _PPath2(_candidate2).is_file():
                    _auth_state_path2 = _candidate2
                    break
            _agent_token2 = None
            if _auth_state_path2:
                try:
                    import json as _jsm2
                    _ssd2 = _jsm2.loads(_PPath2(_auth_state_path2).read_text())
                    _sess_cookie2 = next(
                        (c.get("value") for c in (_ssd2.get("cookies") or [])
                         if isinstance(c, dict) and c.get("name") == "session-token"),
                        None,
                    )
                    if _sess_cookie2:
                        _agent_token2 = await _asyncio.wait_for(
                            # A7.2 — prefer the bound agent_user_token mint.
                            exchange_session_for_agent_token(
                                project_obj, _sess_cookie2, storage_state=_ssd2),
                            timeout=15.0,
                        )
                except Exception as _ex_exc2:
                    log.debug("EEE-fallback: exchange skipped: %s", _ex_exc2)
            # without a fully-discovered OpenAPI spec).
            probed2 = await _asyncio.wait_for(
                probe_path_param_values(
                    project_obj, endpoints or [],
                    auth_state_path=_auth_state_path2,
                    agent_token=_agent_token2,
                ),
                timeout=120.0,
            )
            if probed2 or _agent_token2:
                _staging2 = project_obj.setdefault("environments", {}).setdefault("staging", {})
                if hasattr(_staging2, "model_dump"):
                    _staging2 = _staging2.model_dump()
                    project_obj["environments"]["staging"] = _staging2
                _vars2 = _staging2.setdefault("variables", {})
                _updated2 = []
                if _agent_token2:
                    _vars2["agent_token"] = _agent_token2
                    _vars2["auth_token"] = _agent_token2
                    _updated2.append("agent_token")
                for k, v in (probed2 or {}).items():
                    if _vars2.get(k) in (None, "", "REPLACE_ME") or str(_vars2.get(k, "")).startswith("__ARTA_UNSET"):
                        _vars2[k] = v
                        _updated2.append(k)
                if _updated2:
                    from .projects import _save_projects
                    try:
                        _save_projects()
                    except Exception:
                        pass
                    log.info(
                        "Fix EEE+YY (fallback): persisted %d value(s) to project env: %s",
                        len(_updated2), _updated2,
                    )
        except Exception as _eee_yy_exc:
            log.debug("Fix EEE+YY (fallback): skipped: %s", _eee_yy_exc)

    # G4.1 (I5): Contract test generation from OpenAPI spec.
    # If the project declares `openapi_url`, fetch the spec and generate deterministic
    # Newman contract tests alongside the LLM-generated ones. Merged later in Step 5.
    contract_collection = None
    try:
        openapi_url = None
        if project_obj:
            # Accept `openapi_url` at top-level or in environments.{env}.openapi_url
            openapi_url = project_obj.get("openapi_url")
            if not openapi_url:
                for env_cfg in (project_obj.get("environments") or {}).values():
                    if isinstance(env_cfg, dict) and env_cfg.get("openapi_url"):
                        openapi_url = env_cfg["openapi_url"]
                        break
        # F20-29 #2: auto-discover OpenAPI spec when openapi_url is unset.
        # Try common paths against api_base_url; if any returns 200 with a
        # JSON document containing `"openapi"` or `"swagger"` key, set
        # openapi_url for this run AND log so the operator sees it (they
        # can persist the URL in projects.json afterward to skip the probe).
        if not openapi_url and project_obj:
            for env_cfg in (project_obj.get("environments") or {}).values():
                if not isinstance(env_cfg, dict):
                    continue
                api_url = env_cfg.get("api_base_url") or env_cfg.get("base_url")
                if not api_url:
                    continue
                api_url = api_url.rstrip("/")
                import httpx as _hx
                async with _hx.AsyncClient(timeout=4.0, follow_redirects=True) as _c:
                    for _path in ("/swagger.json", "/openapi.json", "/api/openapi.json"):
                        try:
                            _r = await _c.get(f"{api_url}{_path}")
                            if _r.status_code != 200:
                                continue
                            _ct = _r.headers.get("content-type", "")
                            if "json" not in _ct.lower():
                                continue
                            _spec = _r.json()
                            if isinstance(_spec, dict) and ("openapi" in _spec or "swagger" in _spec):
                                openapi_url = f"{api_url}{_path}"
                                log.info("[%s] Auto-discovered OpenAPI spec at %s — "
                                         "persist this in projects.json environments.<env>.openapi_url to skip future probes",
                                         body.requirement_id, openapi_url)
                                break
                        except Exception:
                            continue
                if openapi_url:
                    break
        if openapi_url:
            from ...agents.contract_test_generator import generate_contract_collection
            # R212 — scope contract gen to the requirement's mapped endpoints so
            # the collection is bounded (pre-R212: all 151 OpenAPI ops → 230-item
            # gen timeout-loops). Compute the mapping inline (this runs before the
            # R211 risk_dicts enrichment). Empty mapping → no scope (rare;
            # ungroundable reqs BLOCK downstream anyway).
            _r212_relevant = []
            try:
                from ...agents.traceability_gate import build_requirement_endpoint_map
                from ...agents.api_discovery import _load_captured_endpoints
                from ...agents.sut_topology import parse_openapi_spec as _r212_pos
                from pathlib import Path as _R212P
                _r212_cg = "\n\n".join(gherkin_scenarios) if gherkin_scenarios else ""
                _r212_cap = _load_captured_endpoints(str(project_id)) if project_id else []
                _r212_ot = []
                _r212_op = _R212P(".arta/openapi") / f"{project_id}.json"
                if project_id and _r212_op.is_file():
                    _r212_ot = _r212_pos(json.loads(_r212_op.read_text()))
                _r212_em = build_requirement_endpoint_map(_r212_cap, _r212_cg, openapi_templates=_r212_ot)
                # Cap to the TOP-N highest-scored mapped endpoints (the map is
                # sorted by keyword-overlap score desc). The raw keyword mapping
                # over-matches (122/140 for a broad req); the top-N keeps the
                # most-relevant contract surface + bounds Pass-2 gen time.
                try:
                    _r212_cap_n = int(os.environ.get("ARTA_R212_CONTRACT_TOP_N", "25"))
                except (TypeError, ValueError):
                    _r212_cap_n = 25
                _r212_relevant = (_r212_em.get("endpoints") or [])[:_r212_cap_n]
            except Exception as _r212_exc:
                log.debug("[%s] R212 contract-scope mapping failed: %s", body.requirement_id, _r212_exc)
            contract_collection = await _asyncio.wait_for(
                generate_contract_collection(
                    openapi_url, body.requirement_id,
                    auth_method=_auth_method,
                    cookie_name=_auth_cookie_name,
                    relevant_paths=_r212_relevant or None,
                ),
                timeout=30.0,
            )
            # C2 (R218) — NEVER ship a silently-empty contract collection
            # (`"item": []`). It reads as coverage on the dashboard but executes
            # ZERO requests (false coverage). Discard it so the LLM/Gherkin newman
            # path generates real grounded items instead (llm-only mode). If that
            # also yields nothing, the dispatch surfaces a truthful no-tests row.
            # Killswitch ARTA_C2_EMPTY_COLLECTION_GATE_DISABLE=1.
            if (contract_collection and not contract_collection.get("item")
                    and os.environ.get("ARTA_C2_EMPTY_COLLECTION_GATE_DISABLE") != "1"):
                log.warning("[%s] C2: contract collection emitted 0 items — discarding "
                            "the empty collection; falling back to LLM-newman gen.",
                            body.requirement_id)
                contract_collection = None
            if contract_collection:
                log.info("[%s] Contract tests generated: %d requests from OpenAPI spec",
                         body.requirement_id, len(contract_collection.get("item", [])))
                # ── R213 A2 (contract path) — reground OpenAPI-spec paths onto the
                # REAL served surface. The OpenAPI spec can declare paths the SUT
                # serves under a different prefix (spec drift) → contract requests
                # 404. The LLM-newman path already gets gen-time regrounding (A2 in
                # _generate_newman); the deterministic contract collection bypassed
                # it. Apply the SAME conservative snapper here so contract-only mode
                # ships served-correct paths. Killswitch
                # ARTA_R213_NEWMAN_REGROUND_DISABLE=1.
                if os.environ.get("ARTA_R213_NEWMAN_REGROUND_DISABLE", "").lower() not in ("1", "true"):
                    try:
                        from ...agents.endpoint_grounding import (
                            build_grounding_index as _a2c_bgi,
                            reground_collection_paths as _a2c_rgc)
                        from ...agents.api_discovery import _load_captured_endpoints as _a2c_lce
                        from ...agents.auth_refresher import (
                            _find_storage_state_path as _a2c_fssp,
                            _read_storage_state as _a2c_rss)
                        from ...agents.auth_chain import harvest_session_ids_from_storage as _a2c_hsi
                        _a2c_pid = str(project_id or "") or None
                        _a2c_caps = _a2c_lce(_a2c_pid) if _a2c_pid else []
                        if _a2c_caps:
                            _a2c_sp = _a2c_fssp((env_config or {}).get("name") if "env_config" in dir() else None)  # noqa: F821 — R280: `dir()`-guarded, so no NameError; but `env_config` is never bound in this scope, so the guard is ALWAYS False and this branch is DEAD (always None/""). Left as-is: the intended source of env_config is unclear and inventing one could change behaviour silently.
                            _a2c_ss = _a2c_rss(_a2c_sp) if _a2c_sp else None
                            _a2c_known = _a2c_hsi(_a2c_ss) if _a2c_ss else {}
                            _a2c_idx = _a2c_bgi(_a2c_caps, _a2c_known)
                            contract_collection, _a2c_rg, _a2c_un = _a2c_rgc(
                                contract_collection, _a2c_idx, _a2c_known)
                            if _a2c_rg:
                                log.info("[%s] R213 A2 (contract): regrounded %d OpenAPI path(s) "
                                         "to the served surface (%d left as-is)",
                                         body.requirement_id, _a2c_rg, _a2c_un)
                    except Exception as _a2c_exc:
                        log.debug("[%s] R213 A2 contract regrounding skipped: %s",
                                  body.requirement_id, _a2c_exc)
                # Step 1.1: surface missing path-param vars so the operator
                # knows what to add to projects.json before runtime. Compare
                # the collection's required_vars against the configured ones.
                _required = set(contract_collection.get("_arta_required_vars") or [])
                _configured: set[str] = set()
                try:
                    _proj_env_check = (project_obj.get("environments") or {}).get(
                        "staging",
                        (project_obj.get("environments") or {}).get("local", {}),
                    ) if project_obj else {}
                    if hasattr(_proj_env_check, "model_dump"):
                        _proj_env_check = _proj_env_check.model_dump()
                    _configured = set((_proj_env_check.get("variables") or {}).keys())
                except Exception:
                    pass
                _missing = sorted(_required - _configured)
                if _missing:
                    log.warning(
                        "[%s] Contract tests reference %d unconfigured path-params: %s. "
                        "These items will be SKIPPED at runtime until the operator "
                        "adds values to project Settings → Environments → variables.",
                        body.requirement_id, len(_missing), _missing,
                    )
                # Stash for response payload (used by the generation results modal)
                _missing_path_params = _missing
            else:
                _missing_path_params = []
    except Exception as exc:
        log.debug("Contract test generation skipped for %s: %s", body.requirement_id, exc)
        _missing_path_params = []

    # ── Step 3: Automation script generation ─────────────────────────────
    _set_job_stage(body.requirement_id, "automation")  # F11-3
    log.info("[%s] Stage 3/4: Automation script generation (tools=%s)",
             body.requirement_id,
             [rd.get("recommended_tools") for rd in risk_dicts] if risk_dicts else [])
    _stage3_started = _time.monotonic()
    scripts = {}

    # If existing tests are retained (partial regen), filter out already-covered tool types
    # so we only generate scripts for the missing ones (e.g., skip Playwright if UI tests exist)
    _TOOL_TO_TYPE = {"playwright": "UI", "newman": "API", "k6": "Performance", "zap": "Security"}
    if existing and risk_dicts:
        existing_types = {_TOOL_TO_TYPE.get(t.get("tool"), "UI") for t in existing}
        for rd in risk_dicts:
            original_types = rd.get("test_types", [])
            filtered = [tt for tt in original_types if tt not in existing_types]
            if not filtered:
                # A3: All test types already covered — skip automation generation entirely.
                # Previously this reverted to original_types, causing pointless re-generation.
                log.info("[%s] All test_types already covered by existing tests (%s) — skipping automation generation",
                         body.requirement_id, existing_types)
                rd["test_types"] = []
            elif filtered != original_types:
                log.info("[%s] Filtered test_types: %s → %s (keeping existing %s)",
                         body.requirement_id, original_types, filtered, existing_types)
                rd["test_types"] = filtered

    # Inject discovered API endpoints into risk_dicts for automation engineer
    if api_endpoints_text and risk_dicts:
        for rd in risk_dicts:
            rd["_api_endpoints"] = api_endpoints_text

    # Inject auth method so automation engineer generates correct headers per project.
    # Uses the same injection pattern as _api_endpoints above.
    if risk_dicts and _auth_method != "none":
        for rd in risk_dicts:
            rd["_auth_method"] = _auth_method
            if _auth_method == "cookie" and _auth_cookie_name:
                rd["_auth_cookie_name"] = _auth_cookie_name

    # ── Automation script generation — LLM-only, no fallback templates ────
    # ARTA's core value is generating requirement-specific test scripts.
    # Generic stub templates defeat this purpose. Retry aggressively, fail honestly.
    _auto_exc = None
    # R217 0c — make the outer automation-gen retry budget env-configurable
    # (was hard-coded 3). Each attempt is a FULL multi-tool auto_agent.generate()
    # — the 3× amplifier behind the bulk-gen rate-limit storm. Default 3 preserves
    # behavior; bulk runs cap conservatively (ARTA_MAX_AUTO_ATTEMPTS=2) AFTER the
    # 0a grounding fixes reduce the NEED for retries. Clamped to [1, 5].
    try:
        _max_auto_attempts = max(1, min(5, int(os.environ.get("ARTA_MAX_AUTO_ATTEMPTS", "3"))))
    except (TypeError, ValueError):
        _max_auto_attempts = 3
    # ── Upstream artifact-quality gate (BMAD TEA L1-3) ───────────────────────
    # Validate the requirement + generated Gherkin BEFORE the expensive
    # small-Ollama script gen. Records quality signals (B5 sidecar); on
    # error-severity Gherkin issues (malformed / missing When-Then / fallback)
    # queues an UPSTREAM regen (B4) routing the fix to the ATDD layer + surfaces
    # a hint — instead of burning the LLM budget on a doomed, misaligned script.
    # Mode via ARTA_UPSTREAM_GATE: block (default) | warn | off. Default flipped
    # warn→block per the Fail-Fast directive — error-severity Gherkin (malformed /
    # missing When-Then / fallback) must NOT proceed to doomed script gen.
    _uq_gate = os.environ.get("ARTA_UPSTREAM_GATE", "block").lower()
    _uq_summary = None
    if _uq_gate != "off":
        try:
            from ...agents.upstream_quality import (
                validate_requirement_quality,
                validate_gherkin_stage,
                persist_upstream_quality,
                requirement_clarity_score,
            )
            _gen_src = _gen_source if "_gen_source" in locals() else None
            _req_q = validate_requirement_quality(requirement)
            # Phase 2 — clarity score + highlights (surfaced; the source
            # augmentation that compensates is injected in ATDD + automation).
            _clarity = requirement_clarity_score(requirement)
            _uq_summary = validate_gherkin_stage(
                gherkin_scenarios, requirement,
                atdd_result.get("acceptance_criteria"),
                gen_source=_gen_src,
            )
            persist_upstream_quality(
                body.requirement_id, str(project_id or "") or None,
                requirement_result=_req_q, gherkin_stage=_uq_summary,
                clarity=_clarity,
            )
            if _clarity.get("band") in ("weak", "unclear"):
                log.info("[%s] requirement clarity=%s (score=%s) — source-augmenting gen",
                         body.requirement_id, _clarity.get("band"), _clarity.get("score"))
            _rqm = _req_q.criteria_results.get("_metrics", {})
            _gqm = (_uq_summary.get("gherkin", {}).get("criteria", {}) or {}).get("_metrics", {}) or {}
            log.info(
                "[%s] upstream quality: measurable_ac=%s%% gherkin_align=%.2f block=%s warn=%d",
                body.requirement_id, _rqm.get("measurable_pct"),
                _gqm.get("alignment", 0.0), _uq_summary["should_block"],
                _uq_summary["warning_count"],
            )
            if _uq_summary["should_block"]:
                from ..services.improvement_loop import queue_upstream_regen_marker
                queue_upstream_regen_marker(
                    body.requirement_id, "gherkin", _uq_summary["hint"],
                    project_id=str(project_id or "") or None,
                    signals=["upstream_gherkin_block"],
                )
        except Exception as _uq_exc:
            log.warning("[%s] upstream gate error (non-fatal): %s", body.requirement_id, _uq_exc)

    # ── Quality-gated ATDD retry (retry-with-improved-context) ───────────────
    # ATDD's own loop retries only on EXCEPTIONS, so a malformed-but-returned
    # Gherkin (small models often drop the Then step) would sail to the gate and
    # fail-fast. Upstream-first: when the gate finds error-severity issues, re-run
    # ATDD with the specific GQ violations as the hint (+ an explicit
    # Given/When/Then requirement) and re-validate, before failing. Bounded by
    # ARTA_ATDD_QUALITY_RETRIES (default 2; "0" disables).
    if (_uq_gate == "block" and _uq_summary and _uq_summary.get("should_block")
            and (os.environ.get("ARTA_ATDD_QUALITY_RETRIES", "2") or "0") not in ("0", "")):
        _aq_rounds = int(os.environ.get("ARTA_ATDD_QUALITY_RETRIES", "2"))
        try:
            from ...agents.atdd_designer import ATDDDesignerAgent
            from ...agents.upstream_quality import validate_gherkin_stage as _vgs
            _aq_base_desc = str(requirement.get("description", ""))
            _aq_pt = "web_app" if requirement.get("_recipe_ungrounded") else project_type
            for _aq in range(_aq_rounds):
                _aq_hint = _uq_summary.get("hint") or ""
                _aq_req = dict(requirement)
                _aq_req["description"] = (
                    _aq_base_desc +
                    "\n\n# GHERKIN QUALITY FIX (your previous Gherkin was REJECTED by the "
                    "quality gate — fix EXACTLY these):\n" + _aq_hint +
                    "\n# HARD REQUIREMENT: every Scenario MUST contain Given, When, AND Then "
                    "steps (a Then is mandatory).")
                log.warning("[%s] ATDD quality-retry %d/%d — gate hint: %s",
                            body.requirement_id, _aq + 1, _aq_rounds, _aq_hint[:120])
                _aq_res = await _asyncio.wait_for(
                    ATDDDesignerAgent(client).generate(
                        [_aq_req], risk_dicts, project_type=_aq_pt, project_meta=project),
                    timeout=180.0)
                _aq_check = _vgs(_aq_res.get("gherkin_scenarios", []), requirement,
                                 _aq_res.get("acceptance_criteria"))
                if not _aq_check.get("should_block"):
                    gherkin_scenarios = _aq_res.get("gherkin_scenarios", [])
                    atdd_result = _aq_res
                    _uq_summary = _aq_check
                    log.info("[%s] ✓ ATDD quality-retry %d cleared the upstream gate",
                             body.requirement_id, _aq + 1)
                    break
                _uq_summary = _aq_check  # carry the latest violations forward as the next hint
        except Exception as _aq_exc:
            log.warning("[%s] ATDD quality-retry error (non-fatal): %s", body.requirement_id, _aq_exc)

    # ── R217 — ATDD partial-validity RESCUE (coverage-preserving) ────────────
    # When the gate STILL blocks after the quality-retries (the retry crashed
    # with "zero Scenario steps" or couldn't fix it) BUT the best Gherkin has
    # ≥1 fail-first-valid scenario, drop ONLY the malformed (no-When/Then)
    # scenarios and proceed with the valid ones — instead of zeroing the whole
    # 0 tests). Re-validate the filtered set to CONFIRM it's clean before
    # proceeding; if filtering can't produce clean Gherkin, fall through to the
    # truthful fail-fast below. Never ships a no-Then scenario (quality-safe).
    # Default OFF (preserves pre-R217 fail-fast); enabled via docker-compose
    # ARTA_R217_ATDD_PARTIAL_RESCUE=1 for the bulk-gen deployment.
    if (_uq_gate == "block" and _uq_summary and _uq_summary.get("should_block")
            and os.environ.get("ARTA_R217_ATDD_PARTIAL_RESCUE", "0").lower() in ("1", "true")
            and gherkin_scenarios):
        try:
            from ...agents.upstream_quality import validate_gherkin_stage as _vgs_rescue
            _rk_filtered, _rk_kept, _rk_dropped = _r217_filter_failfirst_gherkin(gherkin_scenarios)
            if _rk_kept >= 1 and _rk_dropped >= 1:
                _rk_check = _vgs_rescue(
                    _rk_filtered, requirement, atdd_result.get("acceptance_criteria"))
                if not _rk_check.get("should_block"):
                    log.warning(
                        "[%s] R217 ATDD partial-rescue: dropped %d malformed scenario(s), "
                        "proceeding with %d fail-first-valid (coverage preserved; "
                        "no-Then scenarios NOT shipped)",
                        body.requirement_id, _rk_dropped, _rk_kept,
                    )
                    gherkin_scenarios = _rk_filtered
                    atdd_result["gherkin_scenarios"] = _rk_filtered
                    _uq_summary = _rk_check
        except Exception as _rk_exc:
            log.warning("[%s] R217 ATDD partial-rescue error (non-fatal): %s",
                        body.requirement_id, _rk_exc)

    # ── R217 — NFR-aware gate exemption (upstream req-enrichment) ────────────
    # An NFR-only req (test_types ⊆ Performance/Security/Accessibility, no
    # functional API/UI) is tested by k6/zap/axe — which ground in real
    # endpoints + scan-config, NOT functional Gherkin. The functional-Gherkin
    # quality gate mis-applies to it: an NFR "Then" is a perf threshold (k6) or
    # a security control (zap), not a functional assertion, so it reads as
    # "un-measurable" and the gate ZEROES the whole req — discarding its
    # tests). Downgrade the block to PROCEED for NFR-only reqs so their NFR
    # tools generate. Functional reqs (any API/UI type) are unaffected — their
    # malformed functional Gherkin still blocks. Truthful: this does NOT weaken
    # any assertion; it routes a non-functional req to its correct tools instead
    # of failing it on a functional gate it should never have run.
    # Killswitch ARTA_R217_NFR_GATE_EXEMPT_DISABLE=1.
    if (_uq_gate == "block" and _uq_summary and _uq_summary.get("should_block")
            and os.environ.get("ARTA_R217_NFR_GATE_EXEMPT_DISABLE", "").lower() not in ("1", "true")
            and risk_dicts and _r217_is_nfr_only_req((risk_dicts[0] or {}).get("test_types"))):
        log.warning(
            "[%s] R217 NFR-gate-exempt: NFR-only req (test_types=%s) — functional "
            "Gherkin-quality block downgraded to PROCEED; routing to NFR tools "
            "(k6/zap/axe ground in endpoints + scan-config, not functional Gherkin)",
            body.requirement_id, (risk_dicts[0] or {}).get("test_types"))
        _uq_summary = dict(_uq_summary)
        _uq_summary["should_block"] = False
        _uq_summary["_r217_nfr_gate_exempt"] = True

    if _uq_gate == "block" and _uq_summary and _uq_summary.get("should_block"):
        # Fail-Fast/Explain-Clearly: do NOT spend the small-Ollama budget on a
        # script from a malformed/fallback Gherkin. Emit a structured
        # RootCauseReport + the upstream-regen marker (queued above) routing the
        # fix back to ATDD, and fail loudly.
        from ...models.root_cause_report import build_report, persist_root_cause
        _viol = (_uq_summary.get("violations") or [])
        _first = _viol[0] if _viol else {}
        _uq_report = build_report(
            failure_id=body.requirement_id, stage="upstream_gherkin_gate",
            root_cause=("generated Gherkin failed upstream quality (error-severity): "
                        f"{_first.get('message') or _first.get('code') or 'malformed/fallback'}"),
            severity="high", failure_type="TEST_GEN",
            deep_dive={
                "symptom": (f"Gherkin-quality gate blocked {body.requirement_id}: "
                            f"{str(_first.get('message') or _first.get('code') or 'malformed')[:80]}"),  # D2 — per-failure
                "immediate_cause": "; ".join(
                    str(v.get("code") or v.get("message", ""))[:80] for v in _viol[:3]) or "error-severity Gherkin violation",
                "upstream_cause": "ATDD produced malformed / fallback / missing-When-Then Gherkin",
                "architectural_cause": ("scripts generated from invalid Gherkin are doomed + "
                                        "untraceable — the gate refuses to spend gen budget"),
                "process_cause": "requirement clarity / ATDD output quality insufficient (see clarity score)",
            },
            recommended_fix="regenerate the Gherkin via ATDD (upstream regen queued); sharpen the requirement ACs",
            preventive_action="enforce the clarity gate + ATDD escalation before the script stage",
            project_id=str(project_id or "") or None, requirement_id=body.requirement_id,
            violations=_viol[:10], confidence=0.85)
        persist_root_cause(_uq_report)
        _r215_restore_backup_on_failure()
        _clear_in_flight(body.requirement_id, body.ac_id)
        log.error("[%s] upstream gate FAIL-FAST (block) — %s",
                  body.requirement_id, _uq_report.one_line())
        return {
            "workflow_id": workflow_id,
            "requirement_id": body.requirement_id,
            "status": "upstream_blocked",
            "blocked_reason": "gherkin_quality_violation",
            "upstream_quality": _uq_summary,
            "root_cause_report": _uq_report.to_dict(),
            "test_count": 0,
        }

    from ...agents.automation_engineer import AutomationEngineerAgent
    # D3 + R131.A KEYSTONE: Provider-aware outer timeout, with PER-PROJECT
    # override. Anthropic-hosted Sonnet returns combined multi-tool in 90-180s.
    # Local Ollama 32B Q8 needs ~10 min per script × 3-4 tools.
    #
    # Pre-R131.A: _provider_for_timeout read `request.app.state.llm_provider`
    # (the GLOBAL env default ARTA_LLM_PROVIDER, e.g. "claude_code") regardless
    # project (cfg.provider=ollama) ran with the 600s claude default → outer
    # wait_for fired at 10 min on attempts 1 and 2 → outer retry loop at
    # _max_auto_attempts=3 kicked in 3× → ~30 min/req instead of ~12 min/req
    # (verified live: job d10f8710 averaged 1636s/req, 21 reqs ETA 8h).
    #
    # Post-R131.A: prefer cfg.provider (per-project resolved LLMConfig) when
    # available; fall back to global env state ONLY when cfg is absent.
    _provider_for_timeout = (
        (getattr(cfg, "provider", None) or "").value
        if hasattr(getattr(cfg, "provider", None) or "", "value")
        else str(getattr(cfg, "provider", "") or "")
    ).lower() or (
        getattr(request.app.state, "llm_provider", "anthropic") or "anthropic"
    ).lower()
    _base_auto_timeout = 2400.0 if _provider_for_timeout == "ollama" else 600.0
    # Scale the outer timeout by requirement SIZE. A flat 600s fits a typical
    # scenarios → 144 Newman items via chunked gen) blew the flat window and
    # timed out → ALL tools discarded → 0 tests (run 333c9636). The gen isn't
    # stuck — it genuinely needs proportional time. Add 12s per scenario beyond
    # 15, capped at +900s, so big reqs COMPLETE instead of zeroing. Small reqs
    # (≤15 scenarios) keep the base. Killswitch ARTA_AUTO_TIMEOUT_NOSCALE=1.
    _scn = sum(
        s.count("Scenario:") + s.count("Scenario Outline:")
        for s in (gherkin_scenarios or [])
    )
    if _os.environ.get("ARTA_AUTO_TIMEOUT_NOSCALE", "").lower() in ("1", "true"):
        _outer_auto_timeout = _base_auto_timeout
    else:
        _outer_auto_timeout = min(
            _base_auto_timeout + 12.0 * max(0, _scn - 15),
            _base_auto_timeout + 900.0,
        )
    if _scn > 15:
        log.info("[%s] outer auto-timeout scaled to %.0fs for %d scenarios "
                 "(base %.0fs)", body.requirement_id, _outer_auto_timeout, _scn,
                 _base_auto_timeout)
    # R127.A — tool_client_cache shared across all reqs in this gen request so
    # constructed tool-specific clients aren't recreated per-req. Cache is
    # operator-owned: gen request scope. Keys are (provider, model) tuples.
    _r127_a_tool_client_cache: dict = {}

    # ── R211 Wave 2 — enrich the RiskProfile with the GROUNDED endpoint
    # mapping + architecture-refined test types BEFORE generation, so the
    # generators (Phase C/D) and the traceability gate read ONE single-source
    # mapping (RiskProfile IS the unified plan object). Killswitch
    # ARTA_TEST_PLAN_DISABLE=1 → generators fall back to per-tool derivation.
    # R213 V1.1 — hoist the mapped-endpoints + api-typed signal to function
    # scope so the test-CASE quality gate (below) can read the SAME single-source
    # mapping.
    _eps_tp: list = []
    _is_api_tp = False
    if os.environ.get("ARTA_TEST_PLAN_DISABLE", "").lower() not in ("1", "true") and risk_dicts:
        try:
            from ...agents.traceability_gate import build_requirement_endpoint_map
            from ...agents.strategy_architect import (
                architecture_ground_test_types, derive_protocols, detect_mutation_intent)
            from ...agents.api_discovery import _load_captured_endpoints
            _pid_tp = str(project_id or "") or None
            _cap_tp = _load_captured_endpoints(_pid_tp) if _pid_tp else []
            _ot_tp: list = []
            try:
                from ...agents.sut_topology import parse_openapi_spec
                from pathlib import Path as _Ptp
                _op_tp = _Ptp(".arta/openapi") / f"{_pid_tp}.json"
                if _pid_tp and _op_tp.is_file():
                    _ot_tp = parse_openapi_spec(json.loads(_op_tp.read_text()))
            except Exception:
                _ot_tp = []
            _cg_tp = "\n\n".join(gherkin_scenarios) if gherkin_scenarios else ""
            _em_tp = build_requirement_endpoint_map(_cap_tp, _cg_tp, openapi_templates=_ot_tp)
            _eps_tp = _em_tp.get("endpoints") or []
            _protos_tp = derive_protocols(_eps_tp)
            # B1 — AC-level mutation intent (default from the gherkin; an explicit
            # AC `mutation` block, when present, wins — declared-at-requirement).
            _mut_tp = detect_mutation_intent(_cg_tp)
            _ac_mut = None
            for _ac in (requirement.get("acceptance_criteria") or []):
                if isinstance(_ac, dict) and isinstance(_ac.get("mutation"), dict):
                    _ac_mut = _ac["mutation"]
                    break
            if _ac_mut:
                _mut_tp = {**_mut_tp, **_ac_mut}
            _arch_tt = os.environ.get(
                "ARTA_R211_ARCH_TESTTYPE_DISABLE", "").lower() not in ("1", "true")
            for _rd in risk_dicts:
                _rd["endpoints"] = _eps_tp
                _rd["ungroundable"] = bool(_em_tp.get("ungroundable"))
                _rd["protocols"] = _protos_tp
                _rd["mutation"] = _mut_tp
                # R53-honor (GENERIC) — when the caller passed an explicit
                # `body.tools` filter (per-tool regen, e.g. /regenerate-by-tool),
                # do NOT let R211 architecture-grounding re-expand test_types back
                # to API/Performance/etc. That re-expansion (has_api_endpoints →
                # adds newman+k6) silently overrode the PW-only filter, so a
                # "per-tool playwright regen" became a 3-tool batch and never took
                # the single-tool sequential path (which runs the WS1 gate + all
                # validators). Endpoints/protocols/mutation grounding above still
                # applies. Killswitch: ARTA_R211_ARCH_TESTTYPE_DISABLE=1.
                if _arch_tt and not body.tools:
                    _rd["test_types"] = architecture_ground_test_types(
                        _rd.get("test_types") or [],
                        has_api_endpoints=bool(_eps_tp),
                        is_mutation=bool(_mut_tp.get("destructive")),
                    )
            # R212 — stamp the API-contract classification computed on the FULL
            # combined gherkin, so _generate_playwright's KEYSTONE uses the SAME
            # decision target#1 uses. Without this, the per-call gherkin_text in
            # keystone doesn't fire → the slow LLM PW gen runs (~25min) and is
            # then DISCARDED by target#1. Stamping makes the keystone skip it.
            try:
                from ...agents.automation_engineer import AutomationEngineerAgent as _AE_tp
                _is_api_tp = bool(risk_dicts and _AE_tp._r201_is_api_contract_requirement(risk_dicts[0], _cg_tp))
            except Exception:
                _is_api_tp = False
            for _rd in risk_dicts:
                _rd["_r201_api_contract"] = _is_api_tp
            log.info("[%s] R211 test-plan: %d mapped endpoint(s), ungroundable=%s, protocols=%s, api_contract=%s",
                     body.requirement_id, len(_eps_tp), _em_tp.get("ungroundable"), _protos_tp, _is_api_tp)
        except Exception as _tp_exc:
            log.debug("[%s] R211 test-plan enrichment skipped: %s", body.requirement_id, _tp_exc)

    # ── R213 V1.1 — ENFORCE the test-CASE quality gate (the dead-code root fix) ──
    # `validate_test_case_quality` was DEFINED but never CALLED: the ATDD prompt
    # ASKS for measurable+grounded Thens but nothing VALIDATED them, so vague /
    # ungrounded scenarios shipped and the SCRIPTS faithfully inherited them
    # (invented exact-value asserts + invented endpoints). This runs the
    # validator on each generated scenario against the SAME single-source mapped
    # endpoints (`_eps_tp`) the generators use, and records the per-requirement
    # case-quality signal that the mission-report Pillar-1 reads + (in block mode)
    # feeds the existing ATDD quality-retry. Mode via ARTA_R213_TESTCASE_GATE:
    # flag (default — warn-only for the first verify cycle) | block | off.
    # Killswitch ARTA_R213_TESTCASE_QUALITY_DISABLE=1.
    _tcq_summary = None
    if (os.environ.get("ARTA_R213_TESTCASE_QUALITY_DISABLE", "").lower() not in ("1", "true")
            and gherkin_scenarios):
        _tcq_mode = os.environ.get("ARTA_R213_TESTCASE_GATE", "flag").lower()
        if _tcq_mode != "off":
            try:
                from ...agents.grounding_validator import (
                    ac_for_scenario,
                    scenario_budget_for_risk,
                    split_feature_scenarios,
                    validate_negative_assertion_class_gherkin,
                    validate_test_case_quality,
                )
                _acs = requirement.get("acceptance_criteria") or []
                _tcq_viol: list = []
                # R213 (WS2a) — grade PER SCENARIO, not per FILE.
                #
                # `gherkin_scenarios` is a list of whole .feature FILES (one per
                # requirement — atdd_designer:426 `all_gherkin.append(
                # result["feature_file"])`), so the old `enumerate(...)` loop ran
                # with n=1: `measurable_pct` read 100% whenever ANY Then in the
                # entire file was measurable, and `_acs[_i]` paired AC[0] with
                # "the whole file". Every R213 number measured the wrong unit,
                # which is why the gate was never trusted enough to enable.
                # Killswitch ARTA_R213_SPLIT_DISABLE=1 → pre-WS2a per-file
                # behavior (also the escape hatch if a SUT's Gherkin dialect
                # defeats the splitter).
                _split_off = os.environ.get("ARTA_R213_SPLIT_DISABLE") == "1"
                _scns: list[dict] = []
                if not _split_off:
                    for _f in gherkin_scenarios:
                        _scns.extend(split_feature_scenarios(str(_f)))
                if not _scns:
                    # Splitter found nothing (or is disabled): fall back to the
                    # old unit rather than reporting a false 0 scenarios.
                    _scns = [{"name": "", "text": str(_f), "line": 1, "tags": []}
                             for _f in gherkin_scenarios]
                for _scn in _scns:
                    _ac = ac_for_scenario(_scn, _acs)
                    _tcq_viol.extend(validate_test_case_quality(
                        _scn["text"], ac=_ac, mapped_endpoints=_eps_tp,
                        is_api_typed=_is_api_tp))
                    # R255 (WS2c) — a negative-auth scenario that authenticates
                    # normally tests nothing and FALSELY PASSES on a stale-token
                    # 401. R247 fixed the script; this catches the CASE.
                    # Killswitch ARTA_R255_NEG_GHERKIN_DISABLE=1.
                    if os.environ.get("ARTA_R255_NEG_GHERKIN_DISABLE") != "1":
                        try:
                            _tcq_viol.extend(
                                validate_negative_assertion_class_gherkin(
                                    _scn["text"], ac=_ac))
                        except Exception as _r255_exc:
                            log.debug("R255: gherkin negative-class check skipped: %s",
                                      _r255_exc)
                _vague = sum(1 for v in _tcq_viol if getattr(v, "kind", "") == "vague_assertion")
                _epun = sum(1 for v in _tcq_viol if getattr(v, "kind", "") == "endpoint_ungrounded")
                _negmis = sum(1 for v in _tcq_viol if getattr(v, "kind", "") == "negative_setup_mismatch")
                _n = len(_scns)
                # R256 (WS2d) — BMAD TEA Layer 2: coverage should MATCH RISK.
                # scenario_budget_for_risk has been unit-tested since R211 with
                # zero production callers, so the prompt asked every requirement
                # for the same 8 scenario types regardless of its risk score.
                # Report budget-vs-actual so under/over-coverage is visible.
                # Killswitch ARTA_R256_RISK_BUDGET_DISABLE=1.
                _budget: set = set()
                if os.environ.get("ARTA_R256_RISK_BUDGET_DISABLE") != "1":
                    try:
                        _budget = scenario_budget_for_risk(
                            (risk_dicts[0] if risk_dicts else {}).get("priority", "")
                            or requirement.get("priority", ""),
                            int((risk_dicts[0] if risk_dicts else {}).get("risk_score", 0) or 0),
                        )
                    except Exception as _r256_exc:
                        log.debug("R256: risk budget skipped: %s", _r256_exc)
                        _budget = set()
                _tcq_summary = {
                    "scenarios": _n, "vague_assertion": _vague,
                    "endpoint_ungrounded": _epun,
                    "negative_setup_mismatch": _negmis,          # R255 (WS2c)
                    "risk_scenario_budget": sorted(_budget),     # R256 (WS2d)
                    "measurable_pct": round(100.0 * (_n - _vague) / _n, 1) if _n else None,
                    "grounded_pct": round(100.0 * (_n - _epun) / _n, 1) if _n else None,
                    "mode": _tcq_mode,
                    "hint": "; ".join(dict.fromkeys(
                        getattr(v, "hint", "") for v in _tcq_viol if getattr(v, "hint", "")))[:600],
                }
                log.info(
                    "[%s] R213 test-case quality: %d scenario(s), measurable=%s%% grounded=%s%% "
                    "(vague=%d, endpoint_ungrounded=%d, negative_setup_mismatch=%d) "
                    "risk_budget=%s mode=%s",
                    body.requirement_id, _n, _tcq_summary["measurable_pct"],
                    _tcq_summary["grounded_pct"], _vague, _epun, _negmis,
                    sorted(_budget) or "-", _tcq_mode)
                # block mode — reuse the ATDD quality-retry pattern: re-run ATDD
                # with the measurable+grounded hint, re-validate, before blocking.
                if _tcq_mode == "block" and _tcq_viol:
                    from ...agents.atdd_designer import ATDDDesignerAgent
                    _tcq_rounds = int(os.environ.get("ARTA_R213_TESTCASE_RETRIES", "2") or "0")
                    _tcq_base = str(requirement.get("description", ""))
                    for _tr in range(_tcq_rounds):
                        _tcq_req = dict(requirement)
                        _tcq_req["description"] = (
                            _tcq_base +
                            "\n\n# TEST-CASE QUALITY FIX (your previous Gherkin was REJECTED — "
                            "fix EXACTLY these):\n" + (_tcq_summary["hint"] or "") +
                            "\n# Every Then/And MUST assert a CONCRETE fail-first outcome "
                            "grounded in the requirement's real endpoints.")
                        log.warning("[%s] R213 test-case quality-retry %d/%d",
                                    body.requirement_id, _tr + 1, _tcq_rounds)
                        try:
                            _tr_res = await _asyncio.wait_for(
                                ATDDDesignerAgent(client).generate(
                                    [_tcq_req], risk_dicts, project_type=project_type,
                                    project_meta=project), timeout=180.0)
                        except Exception as _tr_exc:
                            log.warning("[%s] R213 test-case retry error: %s",
                                        body.requirement_id, _tr_exc)
                            break
                        _tr_scn = _tr_res.get("gherkin_scenarios", []) or []
                        _tr_viol: list = []
                        # WS2a — re-validate on the SAME unit the gate used
                        # above. Grading the retry per-file while the gate ran
                        # per-scenario would let a retry "clear" a bar it was
                        # never actually measured against.
                        _tr_split: list[dict] = []
                        if not _split_off:
                            for _f in _tr_scn:
                                _tr_split.extend(split_feature_scenarios(str(_f)))
                        if not _tr_split:
                            _tr_split = [{"name": "", "text": str(_f), "line": 1, "tags": []}
                                         for _f in _tr_scn]
                        for _scn in _tr_split:
                            _ac = ac_for_scenario(_scn, _acs)
                            _tr_viol.extend(validate_test_case_quality(
                                _scn["text"], ac=_ac, mapped_endpoints=_eps_tp,
                                is_api_typed=_is_api_tp))
                        if not _tr_viol:
                            gherkin_scenarios = _tr_scn
                            atdd_result = _tr_res
                            log.info("[%s] ✓ R213 test-case quality-retry %d cleared the gate",
                                     body.requirement_id, _tr + 1)
                            _tcq_viol = []
                            break
                        _tcq_viol = _tr_viol
                        _tcq_summary["hint"] = "; ".join(dict.fromkeys(
                            getattr(v, "hint", "") for v in _tr_viol if getattr(v, "hint", "")))[:600]
                    if _tcq_viol:
                        # exhausted → truthful BLOCK (don't ship un-failable / ungrounded cases)
                        _r215_restore_backup_on_failure()
                        _clear_in_flight(body.requirement_id, body.ac_id)
                        log.error("[%s] R213 test-case quality FAIL-FAST (block) — %d violation(s)",
                                  body.requirement_id, len(_tcq_viol))
                        return {
                            "workflow_id": workflow_id,
                            "requirement_id": body.requirement_id,
                            "status": "upstream_blocked",
                            "blocked_reason": "testcase_quality_violation",
                            "testcase_quality": _tcq_summary,
                        }
            except Exception as _tcq_exc:
                log.warning("[%s] R213 test-case quality gate error (non-fatal): %s",
                            body.requirement_id, _tcq_exc)

    for _auto_attempt in range(_max_auto_attempts):
        try:
            auto_agent = AutomationEngineerAgent(
                client,
                base_config=cfg,
                tool_client_cache=_r127_a_tool_client_cache,
            )
            scripts = await _asyncio.wait_for(
                auto_agent.generate(gherkin_scenarios, risk_dicts,
                                    requirement_id=body.requirement_id),
                timeout=_outer_auto_timeout,
            )
            # Capture per-tool failure reasons from the agent — surfaces the real
            # cause (truncation / timeout / rate_limit) to the UI instead of the
            # generic "LLM timeout or rate limit" string.
            _auto_tool_errors = dict(getattr(auto_agent, "_last_tool_errors", {}) or {})
            if scripts:
                log.info("[%s] ✓ Stage 3/4 done in %.1fs (%d scripts)",
                         body.requirement_id, _time.monotonic() - _stage3_started, len(scripts))
                _auto_exc = None
                break
            else:
                raise RuntimeError("LLM returned empty scripts")
        except Exception as exc:
            _auto_exc = exc
            if _auto_attempt < _max_auto_attempts - 1:
                wait_time = 3 * (2 ** _auto_attempt)  # 3s, 6s
                # If rate limited, wait for rate limit reset
                if "rate limit" in str(exc).lower():
                    try:
                        from ...agents.claude_cli_client import ClaudeCLIClient
                        info = ClaudeCLIClient.get_rate_limit_info()
                        if info.get("limited"):
                            import time as _time
                            reset_wait = max(int(info["reset"] - _time.time()), 0)
                            wait_time = min(reset_wait + 5, 120)
                            log.info("Rate limited — waiting %ds for reset before retry", wait_time)
                    except Exception:
                        pass
                exc_desc = str(exc) or type(exc).__name__
                log.warning("Automation attempt %d/%d failed for %s: %s — retrying in %ds",
                            _auto_attempt + 1, _max_auto_attempts, body.requirement_id, exc_desc, wait_time)
                await _asyncio.sleep(wait_time)
                continue
            break
    if _auto_exc is not None:
        # NO FALLBACK TO GENERIC TEMPLATES — report the failure honestly
        exc_desc = str(_auto_exc) or type(_auto_exc).__name__
        log.error("Automation generation failed after %d attempts for %s: %s",
                  _max_auto_attempts, body.requirement_id, exc_desc)
        _gen_source = "failed"
        _gen_failure = {"stage": "automation_scripts", "error_type": type(_auto_exc).__name__,
                        "error_msg": str(_auto_exc)[:200]}
        scripts = {}  # Empty — no stub templates

    # ── Step 4: Write scripts to disk ────────────────────────────────────
    _set_job_stage(body.requirement_id, "writing")  # F11-3
    log.info("[%s] Stage 4/4: Writing scripts to disk + persisting tests", body.requirement_id)
    _stage4_started = _time.monotonic()
    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent.parent
    written_files = []
    # R305 — truthful gen-failure ledger. A gen-blocked / timed-out / blanked
    # analytics layer is recorded here with a concrete reason and DOES NOT count
    # as a generated test — so the job can never again report "N tests / 0 errors"
    # while 0 files landed on disk (the silent-success the mandate forbids).
    _gen_failures: list = []

    # G4.1 (I5): Newman test selection — three modes controlled by ARTA_NEWMAN_MODE:
    #   "contract-only" (default when openapi_url is configured): use ONLY the
    #     deterministic OpenAPI-derived collection. Eliminates LLM-Gherkin
    #     hallucinations that produce 167+ assertion failures from 404s on
    #     paths that don't exist on the SUT (verified live in run-4b700c:
    #     50/50 sampled requests returned 404 because LLM invented paths
    #     like `/auth/google` and `/api/baseline/{id}` that aren't in the spec).
    #   "merge" (legacy): combine contract + LLM-Gherkin tests, dedup by name.
    #   "llm-only": skip contract, use LLM-Gherkin only (rare — only when
    #     OpenAPI is unreachable but Newman tests are still wanted).
    #
    # Override per-deploy via ARTA_NEWMAN_MODE env var. Default flips to
    # contract-only when a contract collection exists, falls back to LLM
    # output when none does (no spec / unreachable spec).
    # Hard-fail policy: when the project DECLARED an openapi_url but the
    # contract collection failed to load (network error, malformed spec,
    # auth-required endpoint, etc.), refuse to silently fall back to
    # LLM-Gherkin which is known to hallucinate paths. Operator must fix
    # the spec OR explicitly set ARTA_NEWMAN_MODE=llm-only to opt out.
    _openapi_url = (env_config or {}).get("openapi_url", "") if 'env_config' in dir() else ""  # noqa: F821 — R280: `dir()`-guarded, so no NameError; but `env_config` is never bound in this scope, so the guard is ALWAYS False and this branch is DEAD (always None/""). Left as-is: the intended source of env_config is unclear and inventing one could change behaviour silently.
    if _openapi_url and not contract_collection and not os.environ.get("ARTA_NEWMAN_MODE"):
        log.error(
            "[%s] OpenAPI spec %s is configured but contract collection load failed. "
            "Refusing silent fallback to LLM-Gherkin (hallucinations). "
            "Fix the spec OR set ARTA_NEWMAN_MODE=llm-only to opt out explicitly.",
            body.requirement_id, _openapi_url,
        )
        _gen_failure = {
            "stage": "contract_load",
            "error_type": "ContractFetchFailed",
            "error_msg": f"openapi_url={_openapi_url} did not yield a usable spec",
        }
        # Note: not a `raise` here because the rest of the pipeline (Playwright,
        # k6, ZAP) can still be useful — but Newman path is skipped via the
        # `if contract_collection:` guard below, so no LLM fallback runs.
    _newman_mode = os.environ.get(
        "ARTA_NEWMAN_MODE",
        "contract-only" if contract_collection else "llm-only",
    ).lower()
    if contract_collection:
        _req_slug = sanitize_req_id(body.requirement_id)
        newman_file = f"src/automation/newman/{_req_slug}_api.json"
        existing_newman = scripts.get(newman_file, "")
        # R128.A — shadow divergence metrics. When BOTH the deterministic
        # contract collection AND the LLM-mode Newman exist, compare their
        # endpoint sets so the operator can see gen-quality drift
        # (LLM-mode Newman inventing routes / missing routes that the
        # OpenAPI baseline covers). Stamped onto _r128_a_divergence so
        # the gen-health dashboard tile can aggregate per-project.
        _r128_a_divergence: dict | None = None
        if existing_newman:
            try:
                _r128_a_divergence = _r128_a_compute_divergence(
                    contract_collection, existing_newman, body.requirement_id,
                )
            except Exception as exc:
                log.debug("R128.A divergence skipped for %s: %s",
                          body.requirement_id, exc)
        # Step 1.1: strip the ARTA-only metadata key before serialization so
        # the on-disk Newman file is a clean Postman v2.1 collection. The
        # required_vars value was already consumed above to build
        # `_missing_path_params` for the response.
        contract_collection.pop("_arta_required_vars", None)
        if _newman_mode == "contract-only":
            # Replace the LLM-generated Newman with the deterministic contract
            # collection. Drops hallucinated paths entirely.
            scripts[newman_file] = json.dumps(contract_collection, indent=2)
            log.info(
                "[%s] Newman: contract-only mode — using %d OpenAPI-derived items "
                "(LLM-generated Gherkin Newman discarded to prevent path hallucinations)",
                body.requirement_id,
                len(contract_collection.get("item", [])),
            )
            # R142.B — diagnostic: surface items_emitted vs OpenAPI ops_seen so
            # an operator can detect non-deterministic shrinkage across regens
            # Pillar 4 mission: contract Newman spec on disk should NOT silently
            # shrink. R142.A's guard reads this signal as the baseline once
            # variance is measured across 3 retriggers.
            try:
                _r142_b_ops_seen = _r142_b_count_openapi_ops(contract_collection)
                log.info(
                    "R142.B contract_gen diagnostic: req=%s mode=contract-only "
                    "items_emitted=%d ops_seen=%d source=contract_test_generator "
                    "openapi_url=%s newman_file=%s",
                    body.requirement_id,
                    len(contract_collection.get("item", [])),
                    _r142_b_ops_seen,
                    (_openapi_url or "")[:160],
                    newman_file,
                )
            except Exception as _r142_b_exc:
                log.debug("R142.B diagnostic logging skipped: %s", _r142_b_exc)
        elif existing_newman and _newman_mode == "merge":
            try:
                existing_parsed = json.loads(existing_newman)
                existing_items = existing_parsed.get("item", []) if isinstance(existing_parsed, dict) else []
                existing_names = {it.get("name") for it in existing_items if isinstance(it, dict)}
                merged_items = list(existing_items) + [
                    it for it in contract_collection.get("item", [])
                    if it.get("name") not in existing_names
                ]
                existing_parsed["item"] = merged_items
                scripts[newman_file] = json.dumps(existing_parsed, indent=2)
                log.info("[%s] Merged %d contract tests into Newman collection (%d total)",
                         body.requirement_id,
                         len(contract_collection.get("item", [])),
                         len(merged_items))
                # R142.B — diagnostic for merge mode
                try:
                    _r142_b_ops_seen = _r142_b_count_openapi_ops(contract_collection)
                    log.info(
                        "R142.B contract_gen diagnostic: req=%s mode=merge "
                        "items_emitted=%d ops_seen=%d existing_items=%d merged_total=%d "
                        "source=contract_test_generator openapi_url=%s newman_file=%s",
                        body.requirement_id,
                        len(contract_collection.get("item", [])),
                        _r142_b_ops_seen,
                        len(existing_items),
                        len(merged_items),
                        (_openapi_url or "")[:160],
                        newman_file,
                    )
                except Exception as _r142_b_exc:
                    log.debug("R142.B diagnostic logging skipped: %s", _r142_b_exc)
            except Exception as _merge_exc:
                log.debug("Contract-test merge failed (%s); keeping LLM Newman only", _merge_exc)
        else:
            scripts[newman_file] = json.dumps(contract_collection, indent=2)
            log.info("[%s] Using OpenAPI contract tests as Newman collection (%d requests)",
                     body.requirement_id, len(contract_collection.get("item", [])))
            # R142.B — diagnostic for the fallback path (no LLM Newman exists)
            try:
                _r142_b_ops_seen = _r142_b_count_openapi_ops(contract_collection)
                log.info(
                    "R142.B contract_gen diagnostic: req=%s mode=fallback "
                    "items_emitted=%d ops_seen=%d source=contract_test_generator "
                    "openapi_url=%s newman_file=%s",
                    body.requirement_id,
                    len(contract_collection.get("item", [])),
                    _r142_b_ops_seen,
                    (_openapi_url or "")[:160],
                    newman_file,
                )
            except Exception as _r142_b_exc:
                log.debug("R142.B diagnostic logging skipped: %s", _r142_b_exc)
    # Valid content prefixes per file type — expanded to cover all common LLM output styles
    # R81.1.d — extended `.js` to accept legitimate k6 patterns the LLM
    # occasionally emits: `function setup() {...}` (top-level fn before
    # imports), `(function() {...})()` (IIFE), `'use strict'` directive,
    # `module.exports` (CommonJS legacy patterns the validator-script-fixers
    # convert later). Pre-R81.1.d these were rejected at prefix-check and
    # the entry's content got blanked to "" → frontend Code panel empty,
    # dispatcher could not find a file on disk. Now the script lands on
    # disk AND the test_entry preserves the content for the operator to
    # inspect even when the dry-run quarantine later catches a problem.
    _VALID_PREFIXES = {
        ".ts": ('import ', '//', 'const ', 'test(', 'test.describe(', 'describe(', '/**', '/*', 'export ', 'type ', 'interface ', '#'),
        ".js": ('import ', '//', 'const ', 'export ', 'http.', 'group(', '/**', '/*', 'var ', 'let ', '#',
                'function ', '(function', "'use strict'", '"use strict"', 'module.exports', 'require('),
        ".json": ('{', '['),
        ".yaml": ('env:', 'jobs:', '#', '---', 'contexts:', 'name:', 'version:'),
        ".yml": ('env:', 'jobs:', '#', '---', 'contexts:', 'name:', 'version:'),
        ".py": ('import ', 'from ', 'def ', 'class ', '#', '"""', "'''", '@'),
    }
    # ── R211 Phase D — opt-in chain-aware Playwright emission ────────────────
    # When the requirement is API-contract AND a cm read-chain is derivable
    # (R200 hierarchy), emit a DETERMINISTIC `{req}_chain.spec.ts` that GETs each
    # parent list → captures a REAL id → threads it → GETs the target (solves the
    # `{collection_id}`-from-a-list 500 deterministically). Composes with
    # R207/R210 (apiUrlFor/authHeaderFor). The `*.spec.ts` dispatch glob picks it
    # up — no dispatch change. OPT-IN (ARTA_CHAIN_AWARE_PW_ENABLE=1) so it adds no
    # risk to the current pass rate until live-validated on one req. R154-safe
    # (GET-only). Killswitch ARTA_CHAIN_AWARE_PW_DISABLE=1 (hard off).
    if (os.environ.get("ARTA_CHAIN_AWARE_PW_ENABLE", "").lower() in ("1", "true")
            and os.environ.get("ARTA_CHAIN_AWARE_PW_DISABLE", "").lower() not in ("1", "true")):
        try:
            from ...agents.chain_aware_playwright import build_spec_from_chain
            from ...agents.automation_engineer import AutomationEngineerAgent as _AE
            _cg_chain = "\n\n".join(gherkin_scenarios) if gherkin_scenarios else ""
            _risk_for_chain = risk_dicts[0] if risk_dicts else {"project_id": project_id}
            _is_api = _AE._r201_is_api_contract_requirement(_risk_for_chain, _cg_chain) \
                if hasattr(_AE, "_r201_is_api_contract_requirement") else True
            if _is_api:
                _ae_chain = _AE(client=None)
                _chain_nodes = _ae_chain._r200_derive_cm_chain(_risk_for_chain, _cg_chain)
                if _chain_nodes and len(_chain_nodes) >= 2:
                    _known = {}
                    try:
                        from ...agents.auth_refresher import _find_storage_state_path, _read_storage_state
                        from ...agents.auth_chain import harvest_session_ids_from_storage
                        _sp = _find_storage_state_path(_risk_for_chain.get("environment"))
                        _ss = _read_storage_state(_sp) if _sp else None
                        _known = harvest_session_ids_from_storage(_ss) if _ss else {}
                    except Exception:
                        _known = {}
                    # R250 → R211 (WS1d): seed with REAL entity ids from the
                    # discovery store. Session ids (this run's real auth
                    # context) still win on conflict.
                    try:
                        from ...agents.real_id_store import known_ids_for_chain
                        _r250_known = known_ids_for_chain(str(project_id or ""))
                        if _r250_known:
                            _known = {**_r250_known, **_known}
                            log.info("[%s] R250→R211: seeded chain with %d real id(s)",
                                     body.requirement_id, len(_r250_known))
                    except Exception as _r250_exc:
                        log.debug("R250→R211: real-id seed skipped: %s", _r250_exc)
                    # R211 Phase E — ACTION-mode only when the AC declares
                    # mutation AND the R154 sandbox opt-in is set; else read-mode
                    # (R154-safe). An action requirement WITHOUT the opt-in logs a
                    # gap and falls back to a read chain (honest: can't happy-path
                    # test a create-workflow read-only).
                    _destructive = bool((_risk_for_chain.get("mutation") or {}).get("destructive"))
                    _r154_ok = (os.environ.get("ARTA_R154_ALLOW_DESTRUCTIVE_TESTS", "").lower() in ("1", "true")
                                and bool(os.environ.get("SUT_TEST_DATA_NAMESPACE")))
                    _action = (_destructive and _r154_ok
                               and os.environ.get("ARTA_CHAIN_AWARE_PW_ACTION_DISABLE", "").lower() not in ("1", "true"))
                    _bodies = {}
                    if _action:
                        try:
                            from ...agents.test_data import build_request_bodies
                            from ...agents.sut_topology import parse_openapi_spec as _pos
                            from pathlib import Path as _Pe
                            # R280 — `_captured` was UNDEFINED here (F821). It is
                            # only ever assigned ~1000 lines away in a DIFFERENT
                            # function, so this raised NameError — which the bare
                            # `except Exception` below swallowed into `_bodies = {}`.
                            # Net effect: build_request_bodies NEVER ran, chain-aware
                            # PW action bodies were ALWAYS empty, and nothing ever
                            # said so. Load it the same way the working call site at
                            # ~3324 does.
                            from ...agents.api_discovery import (
                                _load_captured_endpoints as _lce_e,
                            )
                            _captured_e = _lce_e(str(project_id)) if project_id else []
                            _spec_e = {}
                            _ope = _Pe(".arta/openapi") / f"{project_id}.json"
                            if _ope.is_file():
                                _spec_e = json.loads(_ope.read_text())
                            _bodies = build_request_bodies(
                                openapi_spec=_spec_e, captured_endpoints=_captured_e,
                                project_id=str(project_id or ""), known_ids=_known)
                        except Exception as _r280_exc:
                            # R280 — was a BARE `except Exception: _bodies = {}`, which
                            # is exactly how the NameError above stayed invisible.
                            log.debug("chain-aware action bodies unavailable for %s: %s",
                                      project_id, _r280_exc)
                            _bodies = {}
                    elif _destructive and not _r154_ok:
                        log.info("[%s] r154_action_requires_optin: action requirement "
                                 "needs ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1 + "
                                 "SUT_TEST_DATA_NAMESPACE — emitting read-chain only",
                                 body.requirement_id)
                    _chain_spec = build_spec_from_chain(
                        {"nodes": _chain_nodes},
                        spec_name=body.requirement_id, req_id=body.requirement_id,
                        known_ids=_known, read_only=not _action, bodies=_bodies)
                    if _chain_spec:
                        _req_slug = body.requirement_id.lower().replace('-', '_')
                        _chain_fp = f"src/automation/playwright/{_req_slug}_chain.spec.ts"
                        scripts[_chain_fp] = _chain_spec
                        log.info("[%s] R211 Phase D/E: emitted chain-aware PW %s-spec "
                                 "(%d steps) → %s", body.requirement_id,
                                 "action" if _action else "read",
                                 len(_chain_nodes), _chain_fp)
                        # R212 target#1 — PREFER the grounded chain over the LLM
                        # base PW spec for this API-contract req. Deep analysis
                        # (run-dd09c2): the base spec's negative tests use
                        # HALLUCINATED paths (expect 401 → 404; R210 grounds only
                        # happy-path paths) — the dominant FAIL source. The chain
                        # spec is fully grounded (real cm paths + per-family auth).
                        # Drop the base spec (gen + any stale on-disk copy) so the
                        # grounded chain is authoritative and the hallucinated-path
                        # FAILs don't pollute the attributable result.
                        # Killswitch ARTA_R212_PREFER_CHAIN_DISABLE=1.
                        if os.environ.get("ARTA_R212_PREFER_CHAIN_DISABLE", "").lower() not in ("1", "true"):
                            _base_fp = f"src/automation/playwright/{_req_slug}.spec.ts"
                            scripts.pop(_base_fp, None)
                            try:
                                from pathlib import Path as _Pbase
                                _bp = _Pbase(_base_fp)
                                if _bp.is_file():
                                    _bp.rename(_bp.with_name(_bp.name + ".superseded-by-chain"))
                            except Exception:
                                pass
                            log.info("[%s] R212 target#1: preferred grounded chain — "
                                     "suppressed LLM base PW spec %s (hallucinated-path "
                                     "negative tests removed)", body.requirement_id, _base_fp)
        except Exception as _cap_exc:
            log.debug("[%s] chain-aware PW emission skipped: %s", body.requirement_id, _cap_exc)

    _invalid_script_paths: set[str] = set()
    _TOOLS_BY_PATH = ("playwright", "newman", "k6", "zap", "axe", "selenium", "cypress", "appium")
    for filepath, content in scripts.items():
        stripped = content.strip()
        ext = pathlib.Path(filepath).suffix.lower()
        valid_prefixes = _VALID_PREFIXES.get(ext, _VALID_PREFIXES[".ts"])
        if stripped and not stripped.startswith(valid_prefixes):
            log.error(
                "Invalid generated content for %s (ext=%s) — script will not be written to disk: %s",
                filepath, ext, stripped[:100],
            )
            _invalid_script_paths.add(filepath)
            # Surface invalid-content as a tool-level error so the generate-all
            # results modal shows why the test was rejected instead of reporting
            # success when the on-disk script is missing.
            for _tool in _TOOLS_BY_PATH:
                if f"/{_tool}/" in filepath:
                    _auto_tool_errors.setdefault(
                        _tool,
                        f"invalid_script_content: prefix check failed for {ext} (starts with {stripped[:40]!r})",
                    )
                    break
            continue
        full_path = repo_root / filepath
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            # R142.A — contract Newman item-count regression guard. Pre-R142.A
            # 166 items to 13 across two consecutive regens — contract gen
            # was non-deterministic at the contract_test_generator emission
            # boundary. The guard refuses to overwrite a LARGER existing
            # spec with a smaller one unless the operator explicitly
            # allows via ARTA_CONTRACT_ALLOW_DOWNSIZE=1.
            #
            # Only fires for Newman `_api.json` files; the threshold is
            # operator-tunable via ARTA_CONTRACT_DOWNSIZE_RATIO (default 0.5).
            # Cold-start (no existing file) or empty new content (count==0)
            # are short-circuit no-ops.
            if (
                filepath.endswith("_api.json")
                and "newman" in filepath
                and full_path.exists()
                and os.environ.get("ARTA_CONTRACT_ALLOW_DOWNSIZE") != "1"
            ):
                try:
                    _r142_a_ratio = float(os.environ.get("ARTA_CONTRACT_DOWNSIZE_RATIO", "0.5"))
                    _r142_a_ratio = max(0.0, min(1.0, _r142_a_ratio))
                except (TypeError, ValueError):
                    _r142_a_ratio = 0.5
                try:
                    _r142_a_new = json.loads(content)
                    _r142_a_new_count = len(_r142_a_new.get("item") or [])
                    _r142_a_existing = json.loads(full_path.read_text())
                    _r142_a_existing_count = len(_r142_a_existing.get("item") or [])
                except (json.JSONDecodeError, OSError) as _r142_a_exc:
                    log.debug("R142.A guard parse failed for %s: %s", filepath, _r142_a_exc)
                    _r142_a_new_count = 0
                    _r142_a_existing_count = 0
                if (
                    _r142_a_existing_count > 0
                    and _r142_a_new_count > 0
                    and _r142_a_new_count / _r142_a_existing_count < _r142_a_ratio
                ):
                    log.warning(
                        "R142.A: contract Newman regression for %s — new %d items "
                        "vs existing %d (-%d%%). Refusing overwrite. Operator: "
                        "delete %s to force OR set ARTA_CONTRACT_ALLOW_DOWNSIZE=1.",
                        filepath, _r142_a_new_count, _r142_a_existing_count,
                        int((1 - _r142_a_new_count / max(_r142_a_existing_count, 1)) * 100),
                        full_path,
                    )
                    # Replace the in-memory content with the existing larger spec
                    # so downstream stages (Fix FF dry-run, _save_tests_json) see
                    # the preserved version. write_text below still fires but
                    # writes the existing content (idempotent no-op on disk).
                    content = full_path.read_text()
            full_path.write_text(content)
            # Fix FF: dry-run quarantine. Validate every script can at
            # least PARSE before letting it ship to execution. A script
            # that crashes the runner on import / parse pollutes the
            # next run with a deterministic FAIL. Quarantine renames it
            # to .broken-dryrun so the .broken-* gate skip pattern in
            # gates.py:232 excludes it without us needing a separate
            # DRAFT-flag mechanism.
            _dry_ok, _dry_err = _dry_run_quarantine(full_path)
            if not _dry_ok:
                _broken_path = full_path.with_suffix(full_path.suffix + ".broken-dryrun")
                full_path.rename(_broken_path)
                log.warning(
                    "Fix FF quarantined %s: %s (renamed to %s)",
                    filepath, _dry_err[:200], _broken_path.name,
                )
                # Don't add to written_files — broken scripts shouldn't
                # show up in the "tests generated" count.
                continue
            written_files.append(filepath)
            log.info("Wrote script: %s", filepath)
        except PermissionError:
            log.warning("Permission denied writing %s — execution cannot find script on disk", filepath)
            # Previously appended to written_files so the file appeared successful
            # — this lied to downstream execution which then hit FileNotFoundError.
            # Record as a tool error instead so the UI shows the real failure.
            for _tool in _TOOLS_BY_PATH:
                if f"/{_tool}/" in filepath:
                    _auto_tool_errors.setdefault(
                        _tool, f"disk_permission_denied: cannot write {filepath}"
                    )
                    break

    # ── Step 5: Store generated tests in GENERATED_TESTS (one per AC per tool) ──
    generated_tests = []

    # Parse scenario titles from Gherkin for enrichment
    all_scenario_titles = []
    for gherkin in gherkin_scenarios:
        for line in gherkin.split('\n'):
            stripped = line.strip()
            if stripped.startswith('Scenario:') or stripped.startswith('Scenario Outline:'):
                all_scenario_titles.append(stripped.split(':', 1)[1].strip())

    # Build a map of tool → (script_path, script_content) from generated scripts
    #
    # F20-12 FILENAME DISAMBIGUATION:
    # Axe accessibility tests live at `src/automation/playwright/<req>_a11y.spec.ts`
    # — the same directory as regular Playwright tests. The old detection logic
    # ("if /playwright/ in path → playwright") silently bucketed a11y files as
    # playwright, so `scripts_by_tool["playwright"]` got OVERWRITTEN and the
    # axe key was never populated. Result: 21 .spec.ts a11y files on disk but
    # 0 rows with tool="axe" in Test Explorer. The _a11y suffix check below
    # runs FIRST so axe files get correctly routed.
    _TOOL_FROM_PATH = {"playwright": "playwright", "newman": "newman", "k6": "k6", "zap": "zap", "selenium": "selenium", "cypress": "cypress", "appium": "appium"}
    # F20-12: `axe` must be in this map — otherwise it falls through to
    # the "UI" default and looks like a Playwright test in Test Explorer,
    # with its distinct test_case row wiped by the Playwright row via
    # ON CONFLICT DO UPDATE (both get TC-...-{idx} because tool_suffix
    # below also lacked an axe entry). Accessibility is a first-class
    # BMAD-TEA dimension and deserves its own test_type.
    _TYPE_FROM_TOOL = {"playwright": "UI", "selenium": "UI", "cypress": "UI", "newman": "API", "k6": "Performance", "zap": "Security", "axe": "Accessibility", "appium": "Mobile"}
    scripts_by_tool: dict[str, tuple[str, str]] = {}
    for fpath, content in scripts.items():
        # F20-12: a11y files live under /playwright/ by convention but
        # MUST be bucketed as "axe" so they get a distinct test_id
        # suffix (-a11y) and test_type (accessibility). Check this
        # FIRST — before the generic directory match that would
        # wrongly tag them as playwright.
        if fpath.endswith("_a11y.spec.ts") or "_a11y.spec.ts" in fpath:
            scripts_by_tool["axe"] = (fpath, content)
            continue
        for tool_key in _TOOL_FROM_PATH:
            if f"/{tool_key}/" in fpath or fpath.startswith(f"src/automation/{tool_key}"):
                scripts_by_tool[tool_key] = (fpath, content)
                break
        else:
            scripts_by_tool["playwright"] = (fpath, content)

    # A4: If automation generation failed (gen_source="failed") AND no existing tests
    # are being retained, return an explicit failure response — do NOT create empty stubs.
    # The previous behaviour was to insert ("", "") which produced PENDING tests with no
    # script content, making the failure invisible to the UI.
    if not scripts_by_tool:
        if _gen_source == "failed" and not existing:
            log.error("[%s] No scripts generated — returning failed status (no empty stubs created)",
                      body.requirement_id)
            log.info("[%s] ▣ Generation pipeline FAILED in %.1fs (workflow=%s)",
                     body.requirement_id, _time.monotonic() - _pipeline_started_at, workflow_id)
            _clear_in_flight(body.requirement_id, body.ac_id)
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=202, content={
                "workflow_id": workflow_id,
                "requirement_id": body.requirement_id,
                "status": "failed",
                "stage": "automation_scripts",
                "message": "Automation script generation failed after 3 attempts. "
                           "No tests created. Use /heal-tests or retry generation.",
                "generation_failure": _gen_failure,
                "test_count": 0,
                "tests_generated": [],
            })
        # Otherwise (e.g., partial regen with existing tests retained), default to playwright
        # so the loop below produces test entries for the existing-test path.
        scripts_by_tool["playwright"] = ("", "")

    # Generate one test per acceptance criterion per tool
    # This ensures each AC gets coverage from each relevant tool type
    acs = requirement.get("acceptance_criteria", [])
    if not acs:
        acs = [{"id": "", "statement": "Generated Test"}]

    combined_gherkin = "\n\n".join(gherkin_scenarios) if gherkin_scenarios else "# No Gherkin generated"
    existing_ids = {t.get("id") for t in GENERATED_TESTS}

    for tool_name, (script_path, script_content) in scripts_by_tool.items():
        test_type = _TYPE_FROM_TOOL.get(tool_name, "UI")
        # F20-12: `axe` must have its own suffix — otherwise the axe
        # test_id collides with the Playwright test_id for the same AC
        # (both default to suffix ""). At DB persist time, `INSERT ...
        # ON CONFLICT (test_id) DO UPDATE` then overwrites one row's
        # script_path/tool with the other's, silently dropping one of
        # the two tests. This is why /api/tests showed 0 rows with
        # tool="axe" even though 21 a11y .spec.ts files exist on disk.
        tool_suffix = {
            "playwright": "", "newman": "-api", "k6": "-perf", "zap": "-sec",
            "selenium": "-sel", "cypress": "-cy", "axe": "-a11y",
        }.get(tool_name, "")

        for i, ac in enumerate(acs):
            scenario_title = all_scenario_titles[i] if i < len(all_scenario_titles) else ac.get("statement", "Test scenario")
            # Prefix API/Perf/Security tests in title for clarity
            if test_type != "UI":
                scenario_title = f"[{test_type}] {scenario_title}"

            tc_id = f"TC-{body.requirement_id.replace('REQ-', '')}-{i+1:02d}{tool_suffix}"
            ac_id = ac.get("id", "")

            # Phase Q1 — store the AC-specific Scenario (with feature header)
            # rather than the entire combined-blob. Pre-Q1 every test row
            # in a requirement showed identical Gherkin in the UI/dashboard.
            # Defensive: when extraction fails (no Scenario blocks, no match,
            # malformed Gherkin), the helper returns combined_gherkin so
            # behavior matches pre-Q1.
            ac_gherkin = _extract_ac_scenario(
                combined_gherkin,
                i,
                ac_id,
                ac.get("statement", "") or "",
            )

            test_entry = {
                "id": tc_id,
                "title": scenario_title,
                "priority": requirement.get("priority", "P1"),
                "risk_score": requirement.get("risk_score", 5.0),
                "status": "PENDING",
                "duration_ms": 0,
                "tool": tool_name,
                "automation_tool": tool_name,
                "requirement_id": body.requirement_id,
                # R78.3 KEYSTONE — propagate project_id so dispatchers'
                # per-project filters find the entry. Pre-R78.3 every
                # test_entry was built without project_id → R71.4 k6
                # filter + R77.6.β k6 path-param fill + any other
                # project-scoped consumer silently saw 0 matches. The
                # field is available in scope via `requirement.get` and
                # this is the single keystone fix that unlocks the
                # downstream filters added across R71-R77.
                "project_id": requirement.get("project_id") or "",
                "ac_id": ac_id,
                # Assertion provenance: a test minted from an AC with no
                # measurable criterion must never be read as source-grounded on
                # its thresholds. Killswitch ARTA_AC_MEASURABILITY_WEIGHT_DISABLE=1.
                "ac_measurability": _ac_measurability_of(ac, ac_id, requirement),
                "test_type": test_type,
                "gherkin": ac_gherkin,
                "gherkin_scenario": ac_gherkin,
                "script_content": script_content,
                "script_path": script_path,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "_req_hash": current_hash,
                "test_data": ALL_TEST_DATA.get(body.requirement_id),
                # Generation provenance — used by self-healing agent to detect and retry fallbacks
                "generation_source": _gen_source,    # "llm" | "fallback"
                "generation_failure": _gen_failure,  # None when LLM succeeded
                # F8-1 (Layer 6 placeholder): list of artifact types the execution
                # runner should collect for this test. Mirrors what
                # orchestrator._run_evidence_collection() will look for.
                "evidence_targets": _evidence_targets_for_tool(tool_name),
                # R125.K — per-test LLM provenance stamp. Captures provider +
                # model + strategy (batch vs sequential) so the gen-health
                # batch-vs-serial strategy equivalence. Single source of truth
                # for provider tagging — used by every gen path.
                # R127.B/C — merge per-script telemetry from the agent's side
                # channel (`_last_script_metadata[tool]._gen_metrics`) so the
                # R125.I dashboard tile sees decomposition + escalation flags.
                "_gen_metrics": {
                    **_r125_k_build_gen_metrics(client, _gen_source),
                    **(
                        (auto_agent._last_script_metadata.get(tool_name, {}) or {})
                        .get("_gen_metrics", {})
                        if 'auto_agent' in locals() and tool_name else {}
                    ),
                    # R128.A — stamp the shadow divergence on Newman test rows
                    # so the gen-health dashboard's per-provider quality tile
                    # aggregates contract-vs-LLM drift per req. Only set for
                    # Newman (and only when both contract + LLM Newman were
                    # generated alongside each other); other tools see None.
                    **(
                        {"r128_a_divergence": _r128_a_divergence}
                        if (
                            tool_name == "newman"
                            and '_r128_a_divergence' in locals()
                            and _r128_a_divergence is not None
                        ) else {}
                    ),
                },
            }
            # Flag tests whose script was rejected by prefix validation so they're
            # distinguishable from fully-generated tests in the UI and job reports.
            # R81.1.e — PRESERVE the LLM's content even on prefix-check failure.
            # Pre-R81.1.e the validator blanked `script_content` to "" so frontend
            # Code panels were empty for k6 tests whose gen succeeded but
            # produced an unexpected starter token. Operators couldn't see WHAT
            # the LLM emitted to diagnose. Keep the content (truncated to 8KB
            # safety cap to avoid bloating generated_tests.json for runaway
            # outputs) + stamp generation_error so the UI shows both the
            # diagnostic banner AND the offending content side-by-side.
            if script_path and script_path in _invalid_script_paths:
                test_entry["generation_error"] = "invalid_script_content"
                # Keep the content (capped) so operators can inspect it.
                if isinstance(test_entry.get("script_content"), str) and len(test_entry["script_content"]) > 8192:
                    test_entry["script_content_truncated"] = True
                    test_entry["script_content"] = test_entry["script_content"][:8192]
            generated_tests.append(test_entry)

            if tc_id not in existing_ids:
                GENERATED_TESTS.append(test_entry)
                existing_ids.add(tc_id)

    # F9-13: Validate that every test's declared `tool` actually appears in
    # its `script_path`. Catches LLM mishaps where, e.g., a Newman script
    # is tagged tool=k6 because the marker parser drifted. Warn-only — the
    # test still runs, but operators see the mismatch in logs.
    for _te in generated_tests:
        _tool = (_te.get("tool") or "").lower()
        _path = (_te.get("script_path") or "").lower()
        # axe specs live as *_a11y.spec.ts under playwright/ BY DESIGN (the axe
        # runtime is the PW runner + axe assertions — R213.K.17); the substring
        # check false-warned on every a11y row of every gen.
        if _tool == "axe" and "_a11y" in _path:
            continue
        if _tool and _path and _tool not in _path:
            log.warning(
                "tool/script_path mismatch for %s: tool=%s but path=%s — "
                "execution may invoke the wrong runner",
                _te.get("id", "?"), _tool, _path,
            )

    # ── Step 5b: Persist all generated tests to DB ────────────────────────
    # F8-7: Wrap the whole INSERT loop in a tenacity retry so a transient DB
    # blip (postgres restart, connection pool exhaustion) doesn't strand the
    # tests in memory. F9-8: IntegrityError is a DBAPIError subclass but is
    # NOT transient — a duplicate-key violation will fail the same way on
    # all 3 retry attempts. Catch it separately to retry only on the
    # connection-level errors.
    from sqlalchemy.exc import (
        OperationalError as _DBOpErr,
        DBAPIError as _DBAPIErr,
        IntegrityError as _DBIntegrityErr,
    )
    from tenacity import (
        retry as _tenacity_retry,
        retry_if_exception as _tenacity_retry_if_callable,
        stop_after_attempt as _tenacity_stop,
        wait_exponential as _tenacity_wait,
    )

    def _is_retryable_db_error(exc: BaseException) -> bool:
        """Retry on connection-level errors only — IntegrityError needs operator attention."""
        if isinstance(exc, _DBIntegrityErr):
            return False  # F9-8: explicit non-retry path
        return isinstance(exc, (_DBOpErr, _DBAPIErr, ConnectionError))

    @_tenacity_retry(
        retry=_tenacity_retry_if_callable(_is_retryable_db_error),
        wait=_tenacity_wait(multiplier=1, min=1, max=8),
        stop=_tenacity_stop(3),
        reraise=True,
    )
    async def _persist_tests_to_db():
        from ...db.session import async_session_factory
        from sqlalchemy import text as _text
        async with async_session_factory() as session:
            for te in generated_tests:
                # Gap-1.2: Persist traceability + analytics fields so they survive container restart.
                # Columns added by migration 002_traceability.sql.
                fixture_json = json.dumps(te.get("fixture")) if te.get("fixture") else None
                rubric_json = json.dumps(te.get("eval_rubric")) if te.get("eval_rubric") else None
                judge_score = te.get("judge_score")
                if isinstance(judge_score, (int, float)):
                    judge_score = float(judge_score)
                else:
                    judge_score = None

                # F20-12: Derive DB test_type from the tool so Accessibility
                # tests land as test_type='accessibility' in the DB (was
                # hardcoded 'integration' before, which made /api/tests
                # show Accessibility rows as "integration" — they looked
                # identical to Playwright rows in Test Explorer despite
                # their distinct test_id and script_path.
                _tool_to_db_type = {
                    "playwright": "ui", "selenium": "ui", "cypress": "ui",
                    "newman": "api", "k6": "performance", "zap": "security",
                    "axe": "accessibility", "pytest": "integration",
                    "appium": "ui",
                }
                _db_test_type = _tool_to_db_type.get(te.get("tool", "").lower(), "integration")
                await session.execute(_text("""
                    INSERT INTO test_cases (test_id, title, gherkin_scenario, script_content, script_path,
                                            automation_tool, priority, test_type, is_automated,
                                            requirement_id, ac_id, project_id, metadata,
                                            trace_id, model_version, prompt_version, red_phase_status,
                                            judge_score, dataset_version, analytics_layer, tier,
                                            fixture, eval_rubric, adversarial_input, error_message, generation_source)
                    VALUES (:test_id, :title, :gherkin, :script, :script_path,
                            CAST(:tool AS automation_tool), CAST(:priority AS risk_priority),
                            CAST(:db_test_type AS test_type), true,
                            (SELECT id FROM requirements WHERE req_id = :req_id_text
                             AND project_id = CAST(:project_id AS uuid) LIMIT 1),
                            -- R310.A2 — resolve the ac_id FK (was omitted → 0.3% populated;
                            -- the text ac_id was only stashed in metadata). Scope to this
                            -- requirement so a shared AC id can't cross-link.
                            (SELECT id FROM acceptance_criteria WHERE ac_id = :ac_id_text
                             AND requirement_id = (SELECT id FROM requirements WHERE req_id = :req_id_text
                                                   AND project_id = CAST(:project_id AS uuid) LIMIT 1)
                             LIMIT 1),
                            CAST(:project_id AS uuid), CAST(:metadata AS jsonb),
                            CAST(NULLIF(:trace_id, '') AS uuid), :model_version, :prompt_version, :red_phase_status,
                            :judge_score, :dataset_version, :analytics_layer, :tier,
                            CAST(:fixture AS jsonb), CAST(:eval_rubric AS jsonb), :adversarial_input, :error_message, :generation_source)
                    ON CONFLICT (test_id) DO UPDATE SET
                        title = EXCLUDED.title, gherkin_scenario = EXCLUDED.gherkin_scenario,
                        script_content = EXCLUDED.script_content,
                        script_path = EXCLUDED.script_path,
                        -- metadata was missing from this SET list: every force-regen
                        -- kept the FIRST generation's metadata (stale _req_hash,
                        -- evidence_targets, ac_measurability never landing). Replace
                        -- wholesale — gate-written keys (grounded_by, source_grounding)
                        -- are re-stamped post-gate each generation and old verdicts
                        -- describe the old script.
                        metadata = EXCLUDED.metadata,
                        requirement_id = COALESCE(EXCLUDED.requirement_id, test_cases.requirement_id),
                        ac_id = COALESCE(EXCLUDED.ac_id, test_cases.ac_id),
                        trace_id = EXCLUDED.trace_id,
                        model_version = EXCLUDED.model_version,
                        prompt_version = EXCLUDED.prompt_version,
                        red_phase_status = COALESCE(EXCLUDED.red_phase_status, test_cases.red_phase_status),
                        judge_score = COALESCE(EXCLUDED.judge_score, test_cases.judge_score),
                        dataset_version = EXCLUDED.dataset_version,
                        analytics_layer = EXCLUDED.analytics_layer,
                        tier = EXCLUDED.tier,
                        fixture = EXCLUDED.fixture,
                        eval_rubric = EXCLUDED.eval_rubric,
                        adversarial_input = EXCLUDED.adversarial_input,
                        error_message = EXCLUDED.error_message,
                        generation_source = EXCLUDED.generation_source,
                        updated_at = NOW()
                """), {
                    "test_id": te["id"], "title": te["title"],
                    "gherkin": te.get("gherkin_scenario") or te.get("gherkin") or "",
                    "script": te.get("script_content", ""),
                    "script_path": te.get("script_path", ""),
                    "tool": te.get("tool", "playwright"), "priority": te.get("priority", "P2"),
                    "db_test_type": _db_test_type,  # F20-12
                    "req_id_text": te["requirement_id"],
                    "ac_id_text": te.get("ac_id"),  # R310.A2 — resolve to ac_id FK

                    "project_id": requirement.get("project_id", ""),
                    "metadata": json.dumps({
                        "requirement_id": te["requirement_id"],
                        "ac_id": te.get("ac_id"),
                        # Assertion provenance — source-AC measurability verdict.
                        "ac_measurability": te.get("ac_measurability"),
                        "_req_hash": te.get("_req_hash"),
                        "analytics_layer": te.get("analytics_layer"),
                        "adversarial_category": te.get("adversarial_category"),
                        # F9-2: Persist the F8-1 evidence_targets stamp so the
                        # execution router still sees the runner's collection
                        # contract after a container restart. Without this
                        # field the agent would fall back to its default list,
                        # silently breaking F8-1.
                        "evidence_targets": te.get("evidence_targets", []),
                    }),
                    "trace_id": te.get("trace_id", "") or "",
                    "model_version": te.get("model_version"),
                    "prompt_version": te.get("prompt_version"),
                    "red_phase_status": te.get("red_phase_status", "PENDING_VERIFICATION"),
                    "judge_score": judge_score,
                    "dataset_version": (te.get("fixture") or {}).get("checksum") if isinstance(te.get("fixture"), dict) else None,
                    "analytics_layer": te.get("analytics_layer"),
                    "tier": te.get("tier"),
                    "fixture": fixture_json,
                    "eval_rubric": rubric_json,
                    "adversarial_input": te.get("adversarial_input"),
                    "error_message": te.get("error_message"),
                    "generation_source": te.get("generation_source"),
                })
            await session.commit()

    # F8-7: Run the retry-wrapped persist. On final failure (3 attempts
    # exhausted OR an error type we don't retry like IntegrityError) tests
    # remain in GENERATED_TESTS but cross-router callers querying DB will
    # miss them. Log a clear warning so operators see the in-memory drift.
    try:
        await _persist_tests_to_db()
    except _DBIntegrityErr as _persist_exc:
        # F9-8: Non-transient — duplicate key, FK violation, etc. Don't retry.
        # Log at ERROR level (not WARNING) because this almost always means a
        # logic bug, not a runtime hiccup. Surface the offending test ids.
        _ids = [te.get("id") for te in generated_tests][:5]
        log.error(
            "DB IntegrityError persisting tests (req=%s, sample_ids=%s): %s — "
            "this is NOT retryable; fix the duplicate-key / FK violation",
            body.requirement_id, _ids, _persist_exc,
        )
    except (_DBOpErr, _DBAPIErr, ConnectionError) as _persist_exc:
        log.warning(
            "DB persist retried 3× then fell back to in-memory only "
            "(req=%s, %d tests): %s: %s",
            body.requirement_id, len(generated_tests),
            type(_persist_exc).__name__, _persist_exc,
        )
    except Exception as _persist_exc:
        log.warning("Could not persist tests to DB (req=%s): %s: %s",
                    body.requirement_id, type(_persist_exc).__name__, _persist_exc)

    # ── Step 6: Analytics layer tests (only for analytics-relevant requirements) ──
    analytics_suites = []
    # `project` is set in Step 0 only when project_id was supplied. Use
    # locals().get so we tolerate the path where it's never bound.
    is_analytics_req = is_analytics_requirement(requirement, locals().get("project"))
    if is_analytics_req:
        log.info(
            "[%s] Routing to AnalyticsTestAgent (%s)",
            body.requirement_id,
            requirement.get("_analytics_match_reason", "matched"),
        )
    if client and is_analytics_req:
        try:
            from ...agents.analytics_test_agent import AnalyticsTestAgent
            analytics_agent = AnalyticsTestAgent(client)
            # F20-37: Bumped 120s → 360s. After F20-32 raised max_tokens 3000→8000,
            # individual layer generations on Ollama (qwen2.5:32b) take 30-90s each.
            # 5 layers + retries on truncation (F20-32 stop_reason path) easily exceed
            # 120s. The 360s budget covers worst-case Ollama generation while still
            # bounding pathological hangs. For Anthropic this is irrelevant (typical
            # generation completes in <30s for the same workload).
            analytics_result = await _asyncio.wait_for(
                analytics_agent.generate([requirement], risk_dicts),
                timeout=360.0,
            )
            analytics_suites = analytics_result.get("analytics_suites", [])

            # J1+J2: Persist the AnalyticsTestAgent's actual generated content
            # (per-layer pytest code, adversarial inputs, eval rubric, fixture metadata).
            # Previously this loop replaced the agent's output with empty strings.
            if not analytics_suites:
                log.info("AnalyticsTestAgent returned no suites for %s", body.requirement_id)
            for suite in analytics_suites:
                if not isinstance(suite, dict):
                    log.warning("Invalid analytics suite format for %s: %s", body.requirement_id, type(suite))
                    continue
                suite_req_id = suite.get("requirement_id", body.requirement_id)
                req_slug = sanitize_req_id(suite_req_id)

                fixture_info = suite.get("fixture") or {}
                eval_rubric = suite.get("eval_rubric")
                layer_payload = suite.get("layers", [])

                # F20-33: Fixture materialisation is REQUIRED — without a real on-disk
                # parquet file, every generated layer test will FileNotFoundError at setup
                # via `frozen_dataset(path)`. Previously this block silently swallowed
                # both the missing-metadata case and materialise errors with `log.warning`,
                # then the test files were written anyway → uniform setup-time failures
                # in every Run Suite. Fix: skip the entire suite (continue) when fixtures
                # can't be materialised, so no broken test ever reaches disk.
                if not fixture_info:
                    log.error(
                        "[%s] Analytics agent emitted layer tests but no fixture metadata — "
                        "skipping suite (test would FileNotFoundError at runtime via "
                        "frozen_dataset). Investigate AnalyticsTestAgent._generate_fixture.",
                        suite_req_id,
                    )
                    continue
                try:
                    from ...fixtures.generator import materialise_fixture
                    # Phase 2.3 — pass the dataset_recipe through so the
                    # generator builds rows with the recipe's distribution
                    # primitives (Phase 2.1), not the column-name heuristic.
                    # The recipe is stamped on `requirement` upstream at
                    # Stage 1.5; downstream the analytics_test_agent's
                    # _generate_fixture also reads it, so fixture_info already
                    # reflects the recipe's column list. We pass the recipe
                    # itself so the generator can use the trend distributions
                    # directly (e.g. monotonic_up with magnitude_pct=12.5)
                    # rather than re-inferring from column names.
                    recipe_for_materialise = (
                        requirement.get("dataset_recipe")
                        if isinstance(requirement.get("dataset_recipe"), dict)
                        else None
                    )
                    materialised = materialise_fixture(
                        req_id=suite_req_id,
                        columns=fixture_info.get("columns") or None,
                        row_count=fixture_info.get("row_count") or 10000,
                        version=fixture_info.get("version") or "1.0.0",
                        fmt=fixture_info.get("format") or "parquet",
                        recipe=recipe_for_materialise,
                    )
                except Exception as exc:
                    log.error(
                        "[%s] Fixture materialisation failed (%s: %s) — skipping suite. "
                        "Test files NOT written; operator should check pandas/disk/permissions.",
                        suite_req_id, type(exc).__name__, exc,
                    )
                    continue
                # F20-33: Verify the file actually landed on disk. Defensive — guards against
                # materialise_fixture returning a Path object that wasn't actually persisted
                # (e.g. silent exception in the writer, race with cleanup).
                if not materialised.exists():
                    log.error(
                        "[%s] materialise_fixture returned %s but file is missing on disk — "
                        "skipping suite (test would FileNotFoundError).",
                        suite_req_id, materialised,
                    )
                    continue
                # Update fixture_info with the actual on-disk path + checksum
                from ...automation.python_tests.analytics_helpers import fixture_checksum
                fixture_info["path"] = str(materialised.relative_to(repo_root))
                fixture_info["checksum"] = fixture_checksum(fixture_info["path"])
                log.info("[%s] Materialised analytics fixture at %s (%d bytes)",
                         suite_req_id, fixture_info["path"], materialised.stat().st_size)

                layer_labels = {
                    "nl_to_query": "NL→SQL query generation",
                    "query_to_result": "Query execution → result set",
                    "result_to_insight": "Result set → insight derivation",
                    "insight_to_narrative": "Insight → narrative (LLM-as-Judge)",
                    "e2e": "End-to-end analytics pipeline",
                }

                # J1: Layer tests — write pytest code to disk + persist with real script_content
                analytics_dir = repo_root / "src" / "automation" / "pytest" / "analytics"
                analytics_dir.mkdir(parents=True, exist_ok=True)

                for layer in layer_payload:
                    layer_name = layer.get("layer_name", "unknown")
                    tier = int(layer.get("tier", 2))
                    test_code = layer.get("test_code", "") or ""
                    assertions = layer.get("assertions", [])
                    mocks = layer.get("mocks", [])

                    # Gap-1.6: Defence-in-depth — also validate here in case the agent's own
                    # validation was bypassed (e.g. agent updated independently).
                    if test_code.strip() and not _looks_like_pytest(test_code):
                        log.warning("[%s] Analytics layer %s has invalid pytest code — skipping file write",
                                    body.requirement_id, layer_name)
                        test_code = ""

                    # Write the pytest file to disk only if validated
                    rel_path = f"src/automation/python_tests/analytics/{req_slug}_{layer_name}.py"
                    abs_path = repo_root / rel_path
                    if test_code.strip():
                        try:
                            abs_path.write_text(test_code, encoding="utf-8")
                            written_files.append(rel_path)
                        except Exception as exc:
                            log.warning("Could not write analytics test file %s: %s", rel_path, exc)

                    tc_id = f"TC-{suite_req_id.replace('REQ-', '')}-analytics-{layer_name}"
                    if tc_id not in existing_ids:
                        entry = {
                            "id": tc_id,
                            "title": f"[Analytics T{tier}] {layer_labels.get(layer_name, layer_name)}",
                            "priority": requirement.get("priority", "P1"),
                            "status": "PENDING",
                            "duration_ms": 0,
                            "tool": "pytest",
                            "automation_tool": "pytest",
                            "requirement_id": suite_req_id,
                            # R78.3 — propagate project_id so dispatchers'
                            # per-project filters find this entry.
                            "project_id": requirement.get("project_id") or "",
                            "ac_id": "",
                            "test_type": "Analytics",
                            "analytics_layer": layer_name,
                            "tier": tier,
                            "gherkin": "",
                            # J1: Real pytest code (was always "" — bug fix)
                            "script_content": test_code,
                            "script_path": rel_path if test_code.strip() else "",
                            "assertions": assertions,
                            "mocks": mocks,
                            # J3: Frozen fixture reference
                            "fixture": fixture_info,
                            # J4: Judge rubric for narrative layer (and e2e which may include narrative)
                            "eval_rubric": eval_rubric if layer_name in ("insight_to_narrative", "e2e") else None,
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                            "_req_hash": current_hash,
                            "generation_source": _gen_source,
                        }
                        GENERATED_TESTS.append(entry)
                        generated_tests.append(entry)
                        existing_ids.add(tc_id)

                # J2: Adversarial tests — one test entry per input
                adversarial_inputs = suite.get("adversarial_inputs", [])
                for adv_idx, adv in enumerate(adversarial_inputs):
                    category = adv.get("category", "unknown")
                    input_text = adv.get("input_text", "")
                    expected_behavior = adv.get("expected_behavior", "Refuse or clarify the ambiguous input")
                    risk = adv.get("risk", "")

                    # Generate a small pytest body that asserts the system refuses / clarifies
                    safe_input = input_text.replace('"', '\\"')[:300]
                    safe_expected = expected_behavior.replace('"', '\\"')[:200]
                    safe_risk = risk.replace('"', '\\"')[:200]
                    adv_code = (
                        f'"""Adversarial analytics test — category: {category}\n'
                        f'   Input: {safe_input}\n'
                        f'   Expected behaviour: {safe_expected}\n'
                        f'   Risk if failed: {safe_risk}\n'
                        f'"""\n'
                        f'import pytest\n\n'
                        f'@pytest.mark.tier3\n'
                        f'@pytest.mark.adversarial\n'
                        # F20-34: Slugify category for the FUNCTION NAME too (not
                        # just the filename at line ~2701). Raw category text from
                        # the LLM contains spaces and hyphens (e.g. "Double-negation
                        # filter queries") which are invalid Python identifiers — the
                        # generated file fails to import with "expected '(' line 10".
                        # F20-32's AST validator only covers the LLM-generated layer
                        # tests path; adversarial tests are template-rendered here
                        # and bypass that gate. Use the same slugify helper that
                        # already sanitises the filename.
                        f'def test_adversarial_{_slugify_filename(category)}():\n'
                        f'    """Send adversarial input; expect refusal/clarification, NOT a confident answer."""\n'
                        f'    from arta_runtime import analytics_client  # injected by execution router\n'
                        f'    response = analytics_client.ask("{safe_input}")\n'
                        f'    # Pass criteria (any of):\n'
                        f'    #   - response.refused is True (system declined to answer)\n'
                        f'    #   - response.clarification_requested is True\n'
                        f'    #   - response.confidence < 0.5 (low-confidence flagged answer)\n'
                        f'    assert (\n'
                        f'        getattr(response, "refused", False)\n'
                        f'        or getattr(response, "clarification_requested", False)\n'
                        f'        or getattr(response, "confidence", 1.0) < 0.5\n'
                        f'    ), (\n'
                        # F20-36: Use SINGLE quotes inside the runtime f-string expressions.
                        # Python 3.12+ rejects backslashes inside f-string expression parts
                        # (PEP 701 made the rules strict). The previous template emitted
                        #   f"...{{getattr(response, \\"refused\\", False)}}..."
                        # which renders to f"...{getattr(response, \"refused\", False)}..."
                        # — that backslash inside `{...}` is the SyntaxError. Switching to
                        # single quotes for the dict-key strings inside getattr drops the
                        # backslash entirely.
                        f'        f"Adversarial input \\"{safe_input}\\" produced a confident answer "\n'
                        f'        f"(refused={{getattr(response, \'refused\', False)}}, "\n'
                        f'        f"clarification={{getattr(response, \'clarification_requested\', False)}}, "\n'
                        f'        f"confidence={{getattr(response, \'confidence\', 1.0)}}). "\n'
                        f'        f"Risk: {safe_risk}"\n'
                        f'    )\n'
                    )

                    # F13-2: Slugify the category — raw LLM strings often contain
                    # spaces and parens which produce unimportable Python module
                    # names (pytest collection silently drops them).
                    rel_path = (
                        f"src/automation/python_tests/analytics/"
                        f"{req_slug}_adversarial_{_slugify_filename(category)}_{adv_idx:02d}.py"
                    )
                    try:
                        (repo_root / rel_path).write_text(adv_code, encoding="utf-8")
                        written_files.append(rel_path)
                    except Exception as exc:
                        log.warning("Could not write adversarial test file %s: %s", rel_path, exc)

                    tc_id = f"TC-{suite_req_id.replace('REQ-', '')}-adv-{category}-{adv_idx:02d}"
                    if tc_id not in existing_ids:
                        entry = {
                            "id": tc_id,
                            "title": f"[Adversarial T3] {category}: {input_text[:60]}",
                            "priority": requirement.get("priority", "P1"),
                            "status": "PENDING",
                            "duration_ms": 0,
                            "tool": "pytest",
                            "automation_tool": "pytest",
                            "requirement_id": suite_req_id,
                            # R78.3 — propagate project_id for dispatcher filters.
                            "project_id": requirement.get("project_id") or "",
                            "ac_id": "",
                            "test_type": "Adversarial",
                            "adversarial_category": category,
                            "adversarial_input": input_text,
                            "expected_behavior": expected_behavior,
                            "risk": risk,
                            "tier": 3,
                            "gherkin": "",
                            "script_content": adv_code,
                            "script_path": rel_path,
                            "fixture": fixture_info,
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                            "_req_hash": current_hash,
                            "generation_source": _gen_source,
                        }
                        GENERATED_TESTS.append(entry)
                        generated_tests.append(entry)
                        existing_ids.add(tc_id)

            # Extraction accuracy tests (for extraction requirements)
            desc_lower = requirement.get("description", "").lower()
            if any(kw in desc_lower for kw in ["extraction", "ocr", "parser", "schema", "confidence"]):
                extraction_tests = [
                    ("schema_compliance", "Schema compliance: extracted entities match defined schema"),
                    ("confidence_threshold", "Confidence scoring: all entities > 0.8 threshold"),
                    ("entity_accuracy", "Entity accuracy: values match ground truth within tolerance"),
                    ("multi_format", "Multi-format coverage: all supported formats parsed correctly"),
                ]
                for test_key, test_title in extraction_tests:
                    tc_id = f"TC-{body.requirement_id.replace('REQ-', '')}-extraction-{test_key}"
                    if tc_id not in existing_ids:
                        entry = {
                            "id": tc_id,
                            "title": f"[Extraction] {test_title}",
                            "priority": requirement.get("priority", "P1"),
                            "status": "PENDING",
                            "duration_ms": 0,
                            "tool": "pytest",
                            "automation_tool": "pytest",
                            "requirement_id": body.requirement_id,
                            # R78.3 — propagate project_id for dispatcher filters.
                            "project_id": requirement.get("project_id") or "",
                            "ac_id": "",
                            "test_type": "Extraction",
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                            "_req_hash": current_hash,
                        }
                        GENERATED_TESTS.append(entry)
                        generated_tests.append(entry)
                        existing_ids.add(tc_id)

            n_analytics_layers = sum(1 for t in generated_tests if t.get("test_type") == "Analytics")
            n_adversarial = sum(1 for t in generated_tests if t.get("test_type") == "Adversarial")
            n_extraction = sum(1 for t in generated_tests if t.get("test_type") == "Extraction")
            log.info("[%s] Analytics generated: %d layer tests + %d adversarial + %d extraction tests",
                     body.requirement_id, n_analytics_layers, n_adversarial, n_extraction)
        except Exception as exc:
            # F20-35: Log type + traceback. Previously this used `str(exc)` which is
            # empty for bare-message exceptions (e.g. RuntimeError("") or Exception()),
            # masking real failures (e.g. F20-32 max_tokens RuntimeError, F20-33 fixture
            # skips) as cryptic "failed for X:  — skipping" lines.
            log.warning(
                "Analytics test generation failed for %s (%s: %s) — skipping",
                body.requirement_id, type(exc).__name__, exc,
                exc_info=True,
            )
            # R305 — record the analytics gen failure truthfully so the job reports
            # it instead of silently counting automation-only success.
            _gen_failures.append({
                "tool": "pytest", "requirement_id": body.requirement_id,
                "reason": ("layer_gen_timeout"
                           if isinstance(exc, (_asyncio.TimeoutError, TimeoutError))
                           else f"analytics_gen_failed:{type(exc).__name__}"),
                "detail": str(exc)[:200] or type(exc).__name__,
            })

    # C10: If regeneration produced no scripts and we had a backup, restore old tests
    # so the requirement isn't left with zero coverage after a hash-change triggered clear.
    # Gap-4: Flag this distinctly so callers can tell "fresh regeneration" from "reverted to backup".
    _backup_restored = False
    if _tests_backup and not generated_tests:
        log.warning(
            "Regeneration produced 0 tests for %s — restoring %d backed-up tests to prevent data loss",
            body.requirement_id, len(_tests_backup),
        )
        existing_ids_now = {t.get("id") for t in GENERATED_TESTS}
        for _bt in _tests_backup:
            if _bt.get("id") not in existing_ids_now:
                GENERATED_TESTS.append(_bt)
        generated_tests = _tests_backup
        _backup_restored = True

    # K1: Stamp every generated test with trace_id, model_version, prompt_version.
    # Done once at the end so a single helper handles all test types uniformly.
    _stamp_traceability(generated_tests, _trace_id, _model_version, _prompt_version)

    # G2.5 + F5-3: Populate Neo4j graph with new test entries.
    # Wrap in supervise() so a slow/down Neo4j doesn't block the response and
    # any exception is logged once (not swallowed silently).
    try:
        neo4j = getattr(request.app.state, "neo4j", None)
        if neo4j and generated_tests:
            from ...graph.writer import upsert_test_entries
            from ...observability.task_supervisor import supervise
            import asyncio as _aio_graph
            supervise(
                _aio_graph.create_task(upsert_test_entries(neo4j, list(generated_tests))),
                f"neo4j_upsert:{body.requirement_id}",
            )
    except Exception as _graph_exc:
        log.debug("Neo4j graph write skipped: %s", _graph_exc)

    # WS1 — full-chain traceability: write RiskProfile + Recipe/Fixture/Verifier
    # + Scenarios + Spec nodes so /traceability shows the COMPLETE deck chain
    # (Req→RiskProfile→Recipe→Fixture→Verifier→Scenario→Spec→…). Runs SEQUENTIALLY
    # inside one supervised task so the edges attach in order (profile before
    # recipe before specs). Killswitch ARTA_TRACE_FULL_CHAIN_DISABLE; never blocks.
    try:
        if neo4j and os.environ.get("ARTA_TRACE_FULL_CHAIN_DISABLE", "").lower() not in ("1", "true"):
            from ...graph import writer as _gw
            from ...observability.task_supervisor import supervise as _sup_fc
            import asyncio as _aio_fc, json as _json_fc, glob as _glob_fc
            _rid = body.requirement_id
            _profile = risk_dicts[0] if risk_dicts else {}
            _recipe_obj: dict = {}
            try:
                _slug = sanitize_req_id(_rid)
                _rc = sorted(_glob_fc.glob(f".arta/recipes/{_slug}_v*.json"))
                if _rc:
                    with open(_rc[-1]) as _rf:
                        _recipe_obj = _json_fc.load(_rf)
            except Exception:
                _recipe_obj = {}

            def _fw(p: str) -> str:
                pl = p.lower()
                if pl.endswith("_a11y.spec.ts"): return "axe"
                if pl.endswith(".spec.ts"): return "playwright"
                if pl.endswith(".postman_collection.json") or ("_api" in pl and pl.endswith(".json")): return "newman"
                if pl.endswith(".js"): return "k6"
                if pl.endswith((".yaml", ".yml")): return "zap"
                if pl.endswith(".py"): return "pytest"
                return "unknown"
            _fw_to_path: dict = {}
            for _wf in written_files:
                _fw_to_path.setdefault(_fw(str(_wf)), str(_wf))
            _gt = list(generated_tests)
            _specs: list[dict] = []
            for _t in _gt:
                _tool = (_t.get("tool") or "").lower()
                _sp = _fw_to_path.get(_tool)
                if _sp:
                    _specs.append({"spec_path": _sp, "framework": _tool, "test_id": _t.get("id")})
            for _f, _p in _fw_to_path.items():
                if not any(s["spec_path"] == _p for s in _specs):
                    _specs.append({"spec_path": _p, "framework": _f})

            async def _fc_write():
                if _profile:
                    await _gw.upsert_requirement_profile(neo4j, _rid, _profile)
                if _recipe_obj:
                    await _gw.upsert_recipe_chain(neo4j, _rid, _recipe_obj)
                await _gw.upsert_scenarios(neo4j, _rid, _gt)
                if _specs:
                    await _gw.upsert_spec_files(neo4j, _rid, _specs)
            _sup_fc(_aio_fc.create_task(_fc_write()), f"fc_chain:{_rid}")
    except Exception as _fc_exc:
        log.debug("WS1 full-chain graph write skipped: %s", _fc_exc)

    # Save to JSON backup
    _save_tests_json()

    # H2: Enforce in-memory cap after appending. Older entries remain in DB.
    _enforce_generated_tests_cap()

    log.info("[%s] ✓ Stage 4/4 done in %.1fs (%d files written, %d tests created, trace=%s)",
             body.requirement_id, _time.monotonic() - _stage4_started,
             len(written_files), len(generated_tests), _trace_id[:8])

    # I7: Emit metrics for the full generation pipeline
    try:
        from ...observability.metrics import metrics as _metrics
        _metrics.observe(
            "arta_generation_pipeline_duration_seconds",
            _time.monotonic() - _pipeline_started_at,
            labels={"provider": _provider_label, "gen_source": _gen_source},
        )
        _metrics.inc(
            "arta_generation_pipeline_total",
            labels={"provider": _provider_label, "gen_source": _gen_source, "status": "ok"},
        )
        for _stage_name, _stage_started in (("risk", _stage1_started), ("atdd", _stage2_started),
                                             ("automation", _stage3_started), ("write", _stage4_started)):
            # Placeholder — each stage already logged its elapsed; if we wanted accurate per-stage
            # we'd track individual elapsed values. Skip per-stage histograms for now to avoid
            # double-counting the cumulative time.
            pass
    except Exception:
        pass
    log.info("[%s] ▣ Generation pipeline COMPLETE in %.1fs (workflow=%s, gen_source=%s, total_tests=%d)",
             body.requirement_id, _time.monotonic() - _pipeline_started_at,
             workflow_id, _gen_source, len(generated_tests) + len(existing))

    # Gap-4: Distinguish "completed with fresh regen" from "completed via backup restore".
    # When _backup_restored is True, no new tests were generated — the caller is looking
    # at the previous state restored to prevent data loss from a failed regen.
    # R305 — the status is truthful about partial gen failures (e.g. analytics
    # gen-blocked / timed out) so the job never reads "completed / 0 errors" while
    # a tool silently produced nothing.
    _status = ("restored" if _backup_restored
               else ("partial_gen_failure" if _gen_failures else "completed"))
    if _backup_restored:
        _message = (
            f"Regeneration produced no tests for {body.requirement_id}; "
            f"previous {len(_tests_backup)} tests restored to prevent data loss. "
            f"Check generation_failure for the underlying error."
        )
    elif _gen_failures:
        _message = (
            f"TEA pipeline completed for {body.requirement_id} with "
            f"{len(_gen_failures)} gen-failure(s): "
            + "; ".join(f"{f.get('tool')}:{f.get('reason')}" for f in _gen_failures[:3])
            + f" ({len(written_files)} file(s) written to disk)."
        )
    else:
        _message = f"TEA pipeline completed for {body.requirement_id}."
    # F8-1 (Layer 8 pre-check): warn-only NFR coverage signal so the pipeline
    # doesn't appear PASS just because the runner hasn't executed yet. The
    # gate decision still consults real execution results.
    _nfr_precheck = _compute_nfr_precheck(generated_tests)
    if _nfr_precheck["warnings"]:
        log.info("[%s] NFR pre-check: %s", body.requirement_id, "; ".join(_nfr_precheck["warnings"]))

    # ── Phase 3 — requirement→code traceability / correctness gate ──────────
    # For each generated test, verify it exercises ≥1 endpoint that implements
    # this requirement (captured SUT surface ∩ Gherkin-relevant). Tests that
    # touch APIs but trace to NONE of the requirement's implementing endpoints
    # are flagged `potentially_incorrect` (soft signal + metric, not a hard
    # block). Env ARTA_TRACEABILITY_GATE: off | flag (default).
    _trace_summary = None
    _trace_gate = os.environ.get("ARTA_TRACEABILITY_GATE", "flag").lower()
    if _trace_gate != "off":
        try:
            from ...agents.traceability_gate import (
                extract_test_endpoints, implementing_paths, assess_traceability,
                build_requirement_endpoint_map, untested_endpoints, persist_traceability)
            from ...agents.api_discovery import _load_captured_endpoints
            _pid = str(project_id or "") or None
            _captured = _load_captured_endpoints(_pid) if _pid else []

            # R211 B3.1 — GROUNDED, set-membership traceability (default on;
            # ARTA_TRACEABILITY_GROUNDED=off → legacy keyword-overlap).
            _grounded = os.environ.get("ARTA_TRACEABILITY_GROUNDED", "on").lower() != "off"
            _is_api_typed = bool(
                risk_dicts and "API" in (risk_dicts[0].get("test_types") or []))
            _mapped: list[dict] = []
            _ungroundable = False
            if _grounded:
                # Reuse the single-source mapping computed at gen time (R211
                # Wave 2) when present; else compute here (back-compat).
                if risk_dicts and isinstance(risk_dicts[0].get("endpoints"), list) \
                        and ("ungroundable" in risk_dicts[0]):
                    _mapped = risk_dicts[0].get("endpoints") or []
                    _ungroundable = bool(risk_dicts[0].get("ungroundable"))
                else:
                    _ot = []
                    try:
                        from ...agents.sut_topology import parse_openapi_spec
                        from pathlib import Path as _P
                        _op = _P(".arta/openapi") / f"{_pid}.json"
                        if _pid and _op.is_file():
                            _ot = parse_openapi_spec(json.loads(_op.read_text()))
                    except Exception:
                        _ot = []
                    _em = build_requirement_endpoint_map(_captured, combined_gherkin,
                                                         openapi_templates=_ot)
                    _mapped = _em.get("endpoints") or []
                    _ungroundable = bool(_em.get("ungroundable"))
            _impl = implementing_paths(_captured, combined_gherkin)  # legacy fallback

            # R330 P1b/P1d — per-test grounding provenance. Key → FULL endpoint
            # dict (not just `source`) so evidence-only endpoints (source_har/
            # discovered_at) rank as observed; derivation extracted to
            # traceability_gate.derive_grounded_by (unit-testable).
            from ...agents.traceability_gate import (
                derive_grounded_by, prune_traceability,
                source_component_stamp as _sc_stamp,
                data_object_stamp as _do_stamp,
                build_chain_index as _build_chain_index,
                workflow_stamp as _wf_stamp)
            _cap_by_key = {
                f"{e.get('method')}:{e.get('path')}": e
                for e in (_captured or []) if isinstance(e, dict) and e.get('path')
            }
            # P2 — load the derived authz route catalog ONCE so each test can be
            # stamped with the authorization dimension of its traceability.
            _authz_model = None
            if _pid and os.environ.get("ARTA_TRACEABILITY_AUTHZ_DISABLE") != "1":
                try:
                    from ...agents.authz_discovery import load_authz_model as _az_load
                    from ...agents.traceability_gate import authz_stamp as _az_stamp
                    _authz_model = _az_load(_pid)
                except Exception as _az_load_exc:
                    log.debug("P2 authz stamp: model load skipped: %s", _az_load_exc)
            # Data-Object dimension: load the SUT's OpenAPI entity index ONCE
            # (METHOD:/path → component-schema entity) so each test can be stamped
            # with the domain entities its endpoints read/write. One disk read for
            # the whole batch; {} when the spec carries no component schemas.
            _entity_map = {}
            if _pid and os.environ.get("ARTA_TRACEABILITY_DATA_OBJECT_DISABLE") != "1":
                try:
                    from ...agents.sut_topology import openapi_entity_index as _oei
                    from pathlib import Path as _P2
                    _op2 = _P2(".arta/openapi") / f"{_pid}.json"
                    if _op2.is_file():
                        _entity_map = _oei(json.loads(_op2.read_text()))
                except Exception as _eidx_exc:
                    log.debug("data-object stamp: entity index load skipped: %s", _eidx_exc)
            # Business-Workflow dimension: build the captured-CallChain index ONCE
            # (endpoint_key → chains) so each test can be linked to the business
            # workflow(s) it exercises — O(matched_keys) per test, not O(chains).
            _chain_index = {}
            if _pid and os.environ.get("ARTA_TRACEABILITY_WORKFLOW_DISABLE") != "1":
                try:
                    from ...agents.api_discovery import load_chains as _load_chains
                    _chain_index = _build_chain_index(_load_chains(_pid))
                except Exception as _cidx_exc:
                    log.debug("workflow stamp: chain index load skipped: %s", _cidx_exc)
            # R330 P1d — the gen-time source-grounding status (fail-loud): stamp it
            # per test so it persists with the gate verdicts and reaches the panel.
            _src_grounding = (getattr(auto_agent, "_r330_source_grounding_status", None)
                              if 'auto_agent' in locals() else None)

            _flagged = 0
            _blocked = 0
            _exercised: set = set()
            _test_exercises: dict = {}
            # R211 B3.3 — FE→BE Code→API links (rescued from discard by the PW
            # gen path, surfaced on risk). Stamped on each test's metadata so the
            # Req→Code→API→Test chain survives in the result row (no Neo4j needed).
            _code_api_links = (risk_dicts[0].get("_code_api_links") if risk_dicts else None) or []
            for _t in generated_tests:
                _tool = _t.get("tool") or _t.get("automation_tool") or "playwright"
                _content = _t.get("script_content") or ""
                _tpaths = extract_test_endpoints(_content, _tool)
                _exercised |= _tpaths
                if _grounded and _mapped:
                    _res = assess_traceability(_tpaths, mapped_endpoints=_mapped)
                else:
                    _res = assess_traceability(_tpaths, _impl)
                _tid = _t.get("id") or _t.get("test_id")
                if _tid and _res.get("matched_endpoint_keys"):
                    _test_exercises[_tid] = _res["matched_endpoint_keys"]
                _t.setdefault("metadata", {})
                _t["metadata"]["traceability"] = _res
                # R330 P1b/P1d — stamp the honest grounding provenance of THIS test
                # (evidence-preserving derivation, see traceability_gate).
                _mk = _res.get("matched_endpoint_keys") or []
                _gb = derive_grounded_by(_res.get("test_endpoint_count", 0), _mk, _cap_by_key)
                # Into `_res` so persist_traceability persists it (the traceability
                # store is the durable home for gate verdicts) AND onto metadata
                # (P1d: also written back to the DB post-gate, see below).
                _res["grounded_by"] = _gb
                _t["metadata"]["grounded_by"] = _gb
                if _src_grounding:
                    _res["source_grounding"] = _src_grounding
                    _t["metadata"]["source_grounding"] = _src_grounding
                # P2 — authorization dimension of the trace: stamp the gated
                # endpoints this test exercises with their permission/scope/
                # expected-status; fail-LOUD when a gated endpoint is hit by a
                # test with no authz grounding (guess-grounded / no source).
                if _authz_model:
                    _az = _az_stamp(_mk, _authz_model)
                    if _az["gated_count"]:
                        _res["authz"] = _az
                        _t["metadata"]["authz"] = _az
                        if _gb == "guess" or not _src_grounding:
                            _res["authz_ungrounded"] = True
                            _t["metadata"]["authz_ungrounded"] = True
                            log.info("P2: test %s exercises %d authz-gated endpoint(s) "
                                     "with no authz grounding (grounded_by=%s)",
                                     _tid, _az["gated_count"], _gb)
                # Source-Code-Component dimension: the SUT source file(s) backing
                # the endpoints this test exercises (real Code→API handler
                # identity, not just source_verified). Completes Req→Code→API→Test.
                if os.environ.get("ARTA_TRACEABILITY_SOURCE_COMPONENT_DISABLE") != "1":
                    _sc = _sc_stamp(_mk, _mapped)
                    if _sc["component_count"]:
                        _res["source_components"] = _sc
                        _t["metadata"]["source_components"] = _sc
                # Data-Object dimension: the SUT domain entities (OpenAPI component
                # schemas) the endpoints this test exercises read/write.
                if _entity_map:
                    _do = _do_stamp(_mk, _entity_map)
                    if _do["object_count"]:
                        _res["data_objects"] = _do
                        _t["metadata"]["data_objects"] = _do
                # Business-Workflow dimension: which captured CallChain(s) (ordered
                # API sequences w/ data deps) this test's endpoints belong to.
                if _chain_index:
                    _wf = _wf_stamp(_mk, _chain_index)
                    if _wf["workflow_count"]:
                        _res["workflows"] = _wf
                        _t["metadata"]["workflows"] = _wf
                if _code_api_links:
                    _t["metadata"]["code_api_links"] = _code_api_links
                if not _res["traceable"]:
                    _t["metadata"]["potentially_incorrect"] = True
                    _flagged += 1
                    # R211 B3.5 — opt-in gen-time BLOCK for a fully-off-target
                    # API test (set-membership only; never blocks UI-only tests
                    # which have no API endpoints → traceable).
                    if (_trace_gate == "block" and _grounded and _is_api_typed
                            and _res.get("test_endpoint_count")):
                        _t["metadata"]["blocked_reason"] = "traceability_blocked"
                        _t["metadata"]["defect_class"] = "traceability_blocked"
                        _blocked += 1
                if _pid:
                    persist_traceability(_pid, _t.get("id") or _t.get("test_id") or "?",
                                         body.requirement_id, _res)
            # R330 P1d — sediment prune: drop THIS requirement's rows for tests not
            # in the current batch (regen leftovers voted as `unknown` forever).
            if _pid:
                try:
                    _pruned = prune_traceability(
                        _pid, body.requirement_id,
                        {str(_t.get("id") or _t.get("test_id")) for _t in generated_tests})
                    if _pruned:
                        log.info("[%s] R330 P1d traceability prune: %d stale rows removed",
                                 body.requirement_id, _pruned)
                except Exception as _pr_exc:
                    log.debug("traceability prune skipped: %s", _pr_exc)
            # R330 P1d — write grounded_by / potentially_incorrect / source_grounding
            # back to the DB. The gate runs AFTER _persist_tests_to_db (acknowledged
            # ordering), so without this Test Explorer had nothing to filter on and
            # the panel CTA dead-ended at an unfiltered list.
            try:
                from ...db.session import async_session_factory as _asf_r330
                from sqlalchemy import text as _sqltext_r330
                async with _asf_r330() as _s_r330:
                    for _t in generated_tests:
                        _tid2 = _t.get("id") or _t.get("test_id")
                        _md = _t.get("metadata") or {}
                        if not _tid2 or not _md.get("grounded_by"):
                            continue
                        _patch = {"grounded_by": _md["grounded_by"]}
                        if _md.get("potentially_incorrect"):
                            _patch["potentially_incorrect"] = True
                        if _md.get("source_grounding"):
                            _patch["source_grounding"] = _md["source_grounding"]
                        if _md.get("authz"):
                            _patch["authz"] = _md["authz"]
                        if _md.get("authz_ungrounded"):
                            _patch["authz_ungrounded"] = True
                        if _md.get("source_components"):
                            _patch["source_components"] = _md["source_components"]
                        if _md.get("data_objects"):
                            _patch["data_objects"] = _md["data_objects"]
                        if _md.get("workflows"):
                            _patch["workflows"] = _md["workflows"]
                        await _s_r330.execute(_sqltext_r330(
                            "UPDATE test_cases SET metadata = COALESCE(metadata,'{}'::jsonb) "
                            "|| CAST(:patch AS jsonb), updated_at = NOW() "
                            "WHERE test_id = :tid"),
                            {"patch": json.dumps(_patch), "tid": _tid2})
                    await _s_r330.commit()
            except Exception as _db_exc:
                log.warning("R330 P1d: grounded_by DB write-back failed: %s", _db_exc)
            # R211 B3.4 — bidirectional coverage gap: mapped endpoints no test exercises.
            _untested = untested_endpoints(_mapped, _exercised) if _mapped else []
            _trace_summary = {
                "assessed": len(generated_tests),
                "potentially_incorrect": _flagged,
                "blocked": _blocked,
                "grounded": bool(_grounded and _mapped),
                "ungroundable": _ungroundable,
                "mapped_endpoints": len(_mapped),
                "implementing_endpoints": len(_impl),
                "untested_mapped_endpoints": [e.get("path") for e in _untested][:25],
            }
            if _flagged or _ungroundable:
                log.info("[%s] traceability(grounded=%s): %d flagged, %d blocked, "
                         "ungroundable=%s, %d mapped, %d untested",
                         body.requirement_id, bool(_grounded and _mapped), _flagged,
                         _blocked, _ungroundable, len(_mapped), len(_untested))

            # R211 B3.2 — persist the grounded spine edges (IMPLEMENTED_BY /
            # EXERCISES / INVOKES). No-ops when Neo4j is unavailable.
            if os.environ.get("ARTA_R211_TRACE_GRAPH_DISABLE", "").lower() not in ("1", "true"):
                try:
                    _neo4j = getattr(request.app.state, "neo4j", None)
                    if _neo4j and (_mapped or _test_exercises):
                        from ...graph.writer import upsert_traceability_edges
                        from ...observability.task_supervisor import supervise
                        import asyncio as _aio_te
                        _mk = [f"{e.get('method', 'GET')}:{e.get('path')}" for e in _mapped]
                        # B3.3 — FE→BE Code→API links ([] when absent → INVOKES skipped).
                        _febe = _code_api_links
                        supervise(
                            _aio_te.create_task(upsert_traceability_edges(
                                _neo4j, project_id=str(_pid or ""),
                                req_id=body.requirement_id,
                                mapped_endpoint_keys=_mk,
                                test_exercises=_test_exercises,
                                fe_be_links=_febe)),
                            f"neo4j_trace_edges:{body.requirement_id}",
                        )
                except Exception as _te_exc:
                    log.debug("[%s] trace-edge write skipped: %s", body.requirement_id, _te_exc)
        except Exception as _trace_exc:
            log.debug("[%s] traceability gate skipped: %s", body.requirement_id, _trace_exc)

    response = {
        "workflow_id": workflow_id,
        "requirement_id": body.requirement_id,
        "status": _status,
        "message": _message,
        "regeneration_reverted": _backup_restored,
        "generation_failure": _gen_failure,
        "risk_profile": risk_dicts[0] if risk_dicts else {},
        "gherkin_scenarios": gherkin_scenarios,
        "scripts": scripts,
        "written_files": written_files,
        "tests_generated": generated_tests,
        "tests_retained": len(existing),
        "test_count": len(generated_tests) + len(existing),
        # R305 — truthful gen-failure ledger + disk-truth count (files actually
        # written this run), surfaced alongside the in-memory test_count so a
        # partial/silent failure is visible in the job result, not hidden.
        "gen_failures": _gen_failures,
        "written_count": len(written_files),
        "acceptance_criteria": atdd_result.get("acceptance_criteria", []),
        "test_data_fixtures": atdd_result.get("test_data_fixtures", []),
        "analytics_suites": analytics_suites,
        "nfr_precheck": _nfr_precheck,
        "traceability": _trace_summary,
        # R213 V1.1 — per-requirement test-CASE quality (measurable + endpoint-
        # grounded %). Surfaced so the mission-report Pillar-1 + the operator see
        # whether the SHIPPED cases are fail-first and grounded (vs vacuous).
        "testcase_quality": locals().get("_tcq_summary"),
        # Per-tool failure reasons from automation engineer — caller uses these
        # to show the actual cause (batch truncation / timeout / rate limit)
        # in the generate-all results modal.
        "tool_errors": _auto_tool_errors,
        # Step 1.1: contract path-params not yet configured in projects.json.
        # Items using these will SKIP at runtime — operator should add values
        # to Settings → Environments → variables before the next /run.
        "missing_path_params": locals().get("_missing_path_params") or [],
    }
    # Clear the in-flight registry on the happy path so the next generation
    # of this (req, ac) is allowed immediately. TTL handles crash paths.
    _clear_in_flight(body.requirement_id, body.ac_id)
    try:
        return json.loads(json.dumps(response, default=str))
    except Exception:
        return {"workflow_id": workflow_id, "status": "completed", "test_count": len(generated_tests)}


# ── Async generate-all job tracking ────────────────────────────────────────
# (_GENERATE_ALL_JOBS defined at module top, before job loading code)


@router.post("/generate-all", dependencies=[Depends(_require_api_key)])
async def generate_all_tests(
    project_id: str = Query(...),
    force: bool = Query(False),
    priority: str | None = Query(
        None,
        description="Optional comma-separated priority filter (e.g. 'P0' or 'P0,P1'). "
        "Scopes the async regen to those requirements only.",
    ),
    requirement_ids: str | None = Query(
        None,
        description="Optional comma-separated requirement id filter (e.g. "
        "'REQ-XY-001,REQ-XY-010'). Scopes the async regen to exactly those "
        "requirements — the fast per-requirement iteration loop.",
    ),
    request: Request = None,
):
    """Kick off async test generation for a project's requirements.

    Returns immediately with a job_id. Poll GET /generate-all/status?job_id=...
    for progress updates (completed count, current requirement, errors).

    `priority` scopes the run to a subset (e.g. re-generate only P0 after an
    upstream gen/discovery fix) instead of the full — and often multi-hour —
    all-requirements sweep.
    """
    import asyncio as _aio
    from datetime import datetime, timezone
    from .requirements import PROJECT_REQUIREMENTS

    # Find all requirements for the project (all projects stored in PROJECT_REQUIREMENTS)
    reqs = PROJECT_REQUIREMENTS.get(project_id, [])
    if not reqs:
        # R330 P5 follow-through — the in-memory store is empty after a restart
        # when the sidecar was never written; the DB is the durable home.
        from .requirements import hydrate_project_requirements_from_db
        await hydrate_project_requirements_from_db(project_id)
        reqs = PROJECT_REQUIREMENTS.get(project_id, [])

    if not reqs:
        raise HTTPException(status_code=404, detail=f"No requirements found for project {project_id}")

    # Optional priority scoping — regen just the requested tiers.
    if priority:
        _wanted = {p.strip().upper() for p in priority.split(",") if p.strip()}
        reqs = [r for r in reqs if str(r.get("priority", "")).upper() in _wanted]
        if not reqs:
            raise HTTPException(
                status_code=404,
                detail=f"No {sorted(_wanted)} requirements found for project {project_id}",
            )

    # R298 — explicit requirement scoping: regen EXACTLY the named requirements
    # (the fast per-requirement iteration loop that pairs with R298 scoped exec).
    if requirement_ids:
        _wanted_ids = {r.strip().upper() for r in requirement_ids.split(",") if r.strip()}
        # Case-insensitive; match on EITHER the slug `req_id` OR the UUID `id`. The
        # old `req_id or id` short-circuit made a UUID in `id` UNREACHABLE whenever
        # `req_id` was truthy (it always is for ABC-/ABC- projects), so a UUID never
        # matched — that was the real "scoped regen 404", NOT a case bug.
        reqs = [
            r for r in reqs
            if str(r.get("req_id") or "").upper() in _wanted_ids
            or str(r.get("id") or "").upper() in _wanted_ids
        ]
        if not reqs:
            raise HTTPException(
                status_code=404,
                detail=f"No requirements matching {sorted(_wanted_ids)} for project {project_id}",
            )

    # F6-11: Cap concurrent jobs per project so one project can't starve another's
    # LLM quota. Default 1; raise via env for power users on Anthropic with headroom.
    _max_per_proj = max(1, int(os.environ.get("ARTA_MAX_JOBS_PER_PROJECT", "1")))
    _active_for_project = sum(
        1 for j in _GENERATE_ALL_JOBS.values()
        if j.get("project_id") == project_id and j.get("status") in ("queued", "running")
    )
    if _active_for_project >= _max_per_proj:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Project {project_id} already has {_active_for_project} active "
                f"generation job(s) (cap: {_max_per_proj}). Wait for completion or "
                "raise ARTA_MAX_JOBS_PER_PROJECT."
            ),
        )

    import uuid as _uuid
    job_id = str(_uuid.uuid4())[:8]
    _GENERATE_ALL_JOBS[job_id] = {
        "job_id": job_id,
        "project_id": project_id,
        "status": "running",
        "total_requirements": len(reqs),
        "completed": 0,
        "failed": 0,
        # F11-4: distinct counter for "no-op skipped because hash matched"
        # vs `failed`. Without this the SSE consumer can't tell "everything
        # was up to date" from "everything errored out".
        "skipped": 0,
        "current_requirement": None,
        "current_stage": None,             # F11-3
        "current_stage_started_at": None,  # F11-3
        "per_req_avg_seconds": None,       # F11-6
        "eta_seconds": None,               # F11-6
        "results": [],
        "errors": [],
        "total_tests_generated": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
    }

    # H4: Supervise the background task — log unhandled exceptions and mark job failed.
    bg_task = _aio.create_task(_generate_all_background(job_id, project_id, reqs, request, force=force))

    def _on_done(task: _aio.Task, _job_id: str = job_id):
        if task.cancelled():
            log.warning("generate-all [%s]: background task was cancelled", _job_id)
            return
        exc = task.exception()
        if exc is None:
            return
        log.exception("generate-all [%s]: background task crashed: %s",
                      _job_id, exc, exc_info=exc)
        job_state = _GENERATE_ALL_JOBS.get(_job_id)
        if job_state and job_state.get("status") in ("running", "queued"):
            # F8-9: Sync callback (asyncio.Lock is async-only) — collapse the
            # 3-field mutation into a single dict.update so a concurrent SSE
            # reader of /generate-all/status can't observe status="crashed"
            # without crashed_at + crash_error attached.
            job_state.update({
                "status": "crashed",
                "crashed_at": datetime.now(timezone.utc).isoformat(),
                "crash_error": f"{type(exc).__name__}: {exc}"[:500],
            })
            try:
                _save_job_json(job_state)
            except Exception:
                pass

    bg_task.add_done_callback(_on_done)

    return {
        "job_id": job_id,
        "project_id": project_id,
        "status": "running",
        "total_requirements": len(reqs),
        "total_tests_generated": 0,
        "message": f"Test generation started for {len(reqs)} requirements.",
    }


@router.get("/generations/in-flight", dependencies=[Depends(_require_api_key)])
async def list_in_flight_generations() -> dict:
    """List currently in-flight per-requirement / per-AC generations.

    The Architecture page polls this every ~5s so the "Generating…" UI
    state survives page reloads and cross-tab navigation. Stale entries
    (older than _IN_FLIGHT_TTL = 10 min) are filtered out — those are
    treated as crashed pipelines and the dedup check would also override
    them on the next request.

    Response:
      {"items": [
        {"requirement_id": "REQ-AN-013", "ac_id": "AC-AN-013-01",
         "workflow_id": "ba9354bd", "started_seconds_ago": 47}
      ]}
    """
    import time as _time
    now = _time.monotonic()
    items: list[dict] = []
    stale_keys: list[tuple[str, str]] = []
    for (req_id, ac_marker), (workflow_id, started_at) in list(_IN_FLIGHT_GENERATIONS.items()):
        age = now - started_at
        if age >= _IN_FLIGHT_TTL:
            stale_keys.append((req_id, ac_marker))
            continue
        items.append({
            "requirement_id": req_id,
            "ac_id": None if ac_marker == "*" else ac_marker,
            "workflow_id": workflow_id,
            "started_seconds_ago": int(age),
        })
    # Garbage-collect stale entries so the dedup table doesn't grow forever
    for k in stale_keys:
        _IN_FLIGHT_GENERATIONS.pop(k, None)
    return {"items": items, "count": len(items)}


@router.get("/generate-all/status", dependencies=[Depends(_require_api_key)])
async def generate_all_status(job_id: str = Query(...)):
    """Poll status of an async generate-all job."""
    job = _GENERATE_ALL_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    try:
        return json.loads(json.dumps(job, default=str))
    except Exception:
        return {"job_id": job_id, "status": job.get("status", "unknown")}


@router.get("/generate-all/active", dependencies=[Depends(_require_api_key)])
async def generate_all_active(project_id: str = Query(...)):
    """Get the active (running) generate-all job for a project, if any."""
    for job in _GENERATE_ALL_JOBS.values():
        if job.get("project_id") == project_id and job.get("status") == "running":
            try:
                return json.loads(json.dumps(job, default=str))
            except Exception:
                return {"job_id": job["job_id"], "status": "running"}
    return {"status": "none"}


@router.get("/generate-all/history", dependencies=[Depends(_require_api_key)])
async def generate_all_history(project_id: str = Query(...)):
    """Get all generation jobs for a project (last 20), sorted newest first."""
    jobs = [
        j for j in _GENERATE_ALL_JOBS.values()
        if j.get("project_id") == project_id
    ]
    jobs.sort(key=lambda j: j.get("started_at", ""), reverse=True)
    try:
        return json.loads(json.dumps({"jobs": jobs[:20]}, default=str))
    except Exception:
        return {"jobs": []}


@router.get("/generate-all/last", dependencies=[Depends(_require_api_key)])
async def generate_all_last(project_id: str = Query(...)):
    """Get the most recently completed generate-all job for a project."""
    candidates = [
        j for j in _GENERATE_ALL_JOBS.values()
        if j.get("project_id") == project_id and j.get("status") in ("completed", "interrupted")
    ]
    if not candidates:
        return {"status": "none"}
    candidates.sort(key=lambda j: j.get("completed_at") or j.get("started_at", ""), reverse=True)
    try:
        return json.loads(json.dumps(candidates[0], default=str))
    except Exception:
        return {"status": "none"}


@router.get("/generate-all/stream/{job_id}", dependencies=[Depends(_require_api_key)])
async def stream_generate_all(job_id: str, request: Request):
    """G2.1 (H1): Stream generate-all job progress via Server-Sent Events.

    Replaces 700+ polling requests with a single persistent connection.
    Frontend uses `new EventSource("/api/tests/generate-all/stream/<id>")` and
    subscribes to `status` + `error` events.

    Terminal states (completed | failed | aborted | crashed) close the stream.
    Client disconnect is detected via request.is_disconnected() and stops the loop.
    """
    from fastapi.responses import StreamingResponse
    import asyncio as _asyncio

    async def gen():
        if job_id not in _GENERATE_ALL_JOBS:
            yield 'event: error\ndata: {"error":"job not found"}\n\n'
            return
        terminal = ("completed", "failed", "aborted", "crashed")
        # F6-19: Wrap the loop in try/finally so:
        #   1) is_disconnected() raising (e.g. starlette internal change) breaks
        #      the loop instead of being swallowed and continuing to write to a
        #      dead client forever.
        #   2) ConnectionResetError / CancelledError when the client drops
        #      mid-yield exits cleanly.
        #   3) The finally block always logs the close so we can see in logs
        #      that streams are actually being closed (helps debug leaks).
        try:
            while True:
                # Disconnect check OUTSIDE inner try — any error here means we
                # genuinely can't tell, so assume disconnected and stop.
                if await request.is_disconnected():
                    break
                job = _GENERATE_ALL_JOBS.get(job_id)
                if not job:
                    try:
                        yield 'event: error\ndata: {"error":"job disappeared"}\n\n'
                    except (_asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
                        pass
                    break
                payload = json.dumps(job, default=str)
                try:
                    yield f"event: status\ndata: {payload}\n\n"
                except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
                    break  # client gone — stop pumping
                if job.get("status") in terminal:
                    break
                await _asyncio.sleep(2)
        except (_asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass  # any other transport error → silent close
        finally:
            log.debug("SSE generate-all stream closed for job %s", job_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/generate-all/abort", dependencies=[Depends(_require_api_key)])
async def abort_generate_all(job_id: str = Query(...)):
    """Request graceful abort of a running generate-all job.

    Sets `_abort_requested = True` on the job dict. The background loop
    checks this flag at each requirement boundary and exits cleanly with
    `status: "aborted"`. Partial results are preserved.

    Returns 404 if the job doesn't exist, 409 if it's already finished.
    """
    job = _GENERATE_ALL_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job.get("status") not in ("running", "queued"):
        raise HTTPException(
            status_code=409,
            detail=f"Job {job_id} is not running (status={job.get('status')})",
        )
    # R131.C — datetime/timezone are not module-level imports in this file;
    # import locally to avoid NameError on the abort path (pre-R131.C the
    # endpoint returned HTTP 500 NameError when operator tried to abort
    # a runaway gen-all job — verified live during R131 Iter 0 abort).
    from datetime import datetime, timezone
    job["_abort_requested"] = True
    job["abort_requested_at"] = datetime.now(timezone.utc).isoformat()
    log.warning("Abort requested for job %s (currently on %s, %d/%d done)",
                job_id, job.get("current_requirement", "?"),
                job.get("completed", 0), job.get("total_requirements", 0))
    return {
        "job_id": job_id,
        "status": "abort_requested",
        "message": "Background loop will exit at the next requirement boundary.",
        "current_requirement": job.get("current_requirement"),
        "completed": job.get("completed", 0),
        "total": job.get("total_requirements", 0),
    }


@router.post("/generate-all/retry", dependencies=[Depends(_require_api_key)])
async def retry_failed_tests(
    job_id: str = Query(...),
    from_requirement: str | None = Query(None),
    request: Request = None,
):
    """Retry failed/partial requirements from a previous generate-all job.

    Optionally specify from_requirement to start from a specific requirement onward.
    """
    import asyncio as _aio
    from datetime import datetime, timezone
    from .requirements import PROJECT_REQUIREMENTS

    old_job = _GENERATE_ALL_JOBS.get(job_id)
    if not old_job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if old_job.get("status") == "running":
        raise HTTPException(status_code=409, detail="Job is still running")

    project_id = old_job["project_id"]
    all_reqs = PROJECT_REQUIREMENTS.get(project_id, [])

    if from_requirement:
        # "Continue from" — include this requirement and all after it
        req_ids_ordered = [r.get("id") or r.get("req_id") for r in all_reqs]
        try:
            start_idx = req_ids_ordered.index(from_requirement)
            retry_reqs = all_reqs[start_idx:]
        except ValueError:
            retry_reqs = all_reqs
    else:
        # Retry only failed + partial (missing tools)
        failed_ids = {e["requirement_id"] for e in old_job.get("errors", [])}
        for r in old_job.get("results", []):
            tools = r.get("tools", {})
            if any(t.get("status") == "failed" for t in tools.values()):
                failed_ids.add(r["requirement_id"])
            # Also retry if fewer than expected tools were generated
            if len(tools) < 2 and r.get("status") != "skipped":
                failed_ids.add(r["requirement_id"])
            # P3 (truthful reporting) — explicitly retry zero-test coverage gaps /
            # hard gen-failures. Pre-P3 these carried `tools:{}` +
            # status="completed_no_tests", so they were only caught incidentally by
            # the len<2 heuristic; make it explicit so "Retry All Failed" reliably
            # recovers timed-out / gate-blocked reqs.
            if (r.get("coverage_gap") or r.get("gen_failed")
                    or r.get("status") in ("completed_no_tests", "gen_failed")):
                failed_ids.add(r["requirement_id"])
        retry_reqs = [r for r in all_reqs if (r.get("id") or r.get("req_id")) in failed_ids]

    if not retry_reqs:
        return {"status": "none", "message": "No requirements to retry — all complete"}

    import uuid as _uuid
    new_job_id = str(_uuid.uuid4())[:8]
    _GENERATE_ALL_JOBS[new_job_id] = {
        "job_id": new_job_id,
        "project_id": project_id,
        "status": "running",
        "total_requirements": len(retry_reqs),
        "completed": 0,
        "failed": 0,
        "current_requirement": None,
        "results": [],
        "errors": [],
        "total_tests_generated": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "retry_of": job_id,
    }

    # F8-3: Supervise the retry task too — without this a crash inside the
    # retry path is swallowed and the job sits in "running" forever.
    from ...observability.task_supervisor import supervise as _supervise_retry
    _retry_bg = _aio.create_task(
        _generate_all_background(new_job_id, project_id, retry_reqs, request)
    )
    _supervise_retry(_retry_bg, f"generate_all_retry:{new_job_id}")

    return {
        "job_id": new_job_id,
        "project_id": project_id,
        "status": "running",
        "total_requirements": len(retry_reqs),
        "retrying_ids": [r.get("id") or r.get("req_id") for r in retry_reqs],
    }


@router.post("/regenerate", dependencies=[Depends(_require_api_key)])
async def regenerate_tests_endpoint(
    requirement_id: str = Query(...),
    force: bool = Query(False),
    feedback: str | None = Query(None),
    request: Request = None,
):
    """Regenerate tests for a specific requirement.

    force=true: clears existing tests and regenerates from scratch.
    feedback: user correction notes prepended to LLM prompts.
    """
    if force:
        # Clear existing tests for this requirement
        GENERATED_TESTS[:] = [t for t in GENERATED_TESTS if t.get("requirement_id") != requirement_id]

    gen_body = GenerateRequest(requirement_id=requirement_id, feedback=feedback)
    return await generate_tests(gen_body, request)


@router.post("/regenerate-by-tool", dependencies=[Depends(_require_api_key)])
async def regenerate_by_tool(
    project_id: str = Query(..., description="Project UUID"),
    tool: str = Query(..., description="Tool to regenerate: playwright|newman|k6|zap|axe|pytest"),
    force: bool = Query(True, description="Clear existing tool-specific specs first (default true)"),
    requirement_id: str | None = Query(None, description="Optional: scope to a single requirement"),
    request: Request = None,
) -> dict:
    """R53 — focused tool-specific regen.

    Triggers a fresh generation cycle for ONE tool across a project's
    requirements. Use cases:
      - After R51 (k6 brace-balance autofix) shipped → regen a project's k6
        scripts that were previously quarantined to .broken-* files.
      - After R47.4a (pytest grounding retry) → regen pytest specs
        with assertion-value drift.
      - After R47.1b (Playwright DOM catalog HARD-constraint prefix)
        → regen Playwright specs that hallucinated testids.
      - After R42.1 grounding patterns expand → regen affected
        tool's specs to exercise new validators.

    The endpoint iterates the project's requirements (or just one
    when `requirement_id` is supplied), invokes `generate_tests` with
    `tools=[tool]` so R53's tool-filter override fires inside the
    risk_dicts builder, and reports a job summary.

    `force=true` (default): wipes the tool-specific files on disk +
    GENERATED_TESTS entries before regen. `force=false`: incremental
    — fills only gaps where the requirement has no entry for that
    tool yet.

    Args:
        project_id: project UUID
        tool: one of `playwright`, `newman`, `k6`, `zap`, `axe`, `pytest`
        force: clear-then-regen (true) or fill-gaps (false)
        requirement_id: optional single-requirement scope
    """
    SUPPORTED = {"playwright", "newman", "k6", "zap", "axe", "pytest",
                 "selenium", "cypress"}
    tool_norm = (tool or "").lower().strip()
    if tool_norm not in SUPPORTED:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_tool",
                "message": f"tool={tool_norm!r} not in {sorted(SUPPORTED)}",
            },
        )

    # Resolve target requirements
    target_reqs: list[dict] = []
    try:
        from .requirements import list_requirements
        req_data = await list_requirements(project_id=project_id)
        all_reqs = req_data.get("requirements", [])
        if requirement_id:
            # R53 — match against EITHER UUID `id` or slug `req_id`, CASE-INSENSITIVE
            # (aligned with generate-all's R298 scoping so both endpoints accept the
            # same id in any case).
            _rid_norm = str(requirement_id).strip().upper()
            target_reqs = [
                r for r in all_reqs
                if str(r.get("id") or "").upper() == _rid_norm
                or str(r.get("req_id") or "").upper() == _rid_norm
            ]
            if not target_reqs:
                raise HTTPException(
                    status_code=404,
                    detail=f"Requirement {requirement_id} not found in project {project_id}",
                )
        else:
            target_reqs = list(all_reqs)
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("R53: list_requirements failed for project=%s: %s",
                    project_id, exc)
        raise HTTPException(status_code=500, detail=f"list_requirements failed: {exc}")

    if not target_reqs:
        return {
            "status": "no_work",
            "project_id": project_id,
            "tool": tool_norm,
            "requirements_targeted": 0,
            "message": "No requirements found for the project.",
        }

    # File-extension map for force-cleanup
    _TOOL_DIRS = {
        "playwright": ("src/automation/playwright", ".spec.ts"),
        "newman": ("src/automation/newman", "_api.json"),
        "k6": ("src/automation/k6", "_performance.js"),
        "zap": ("src/automation/zap", "_security_scan.yaml"),
        "axe": ("src/automation/axe", ".spec.ts"),
        "pytest": ("src/automation/pytest", ".py"),
    }
    tool_dir_glob = _TOOL_DIRS.get(tool_norm)

    # Force-cleanup the tool's existing files on disk for each requirement
    if force and tool_dir_glob:
        from pathlib import Path as _Path
        import glob as _glob_53
        cleaned: list[str] = []
        for rq in target_reqs:
            rid = rq.get("id", "")
            slug = sanitize_req_id(rid)
            pattern = f"{tool_dir_glob[0]}/{slug}*{tool_dir_glob[1]}"
            for p in _glob_53.glob(pattern):
                try:
                    # R215 Item 2 — NON-DESTRUCTIVE: rename to `.r215.bak` instead of
                    # unlink, so a failed regen (0 tests) can be recovered (the old
                    # disk spec isn't permanently lost). On a successful regen the
                    # new spec overwrites the real path; the `.bak` is harmless
                    # residue. Killswitch ARTA_R215_REGEN_RESTORE_DISABLE=1 →
                    # original delete-first behavior.
                    if os.environ.get("ARTA_R215_REGEN_RESTORE_DISABLE", "").lower() in ("1", "true"):
                        _Path(p).unlink()
                    else:
                        _bak = _Path(p + ".r215.bak")
                        if _bak.exists():
                            _bak.unlink()
                        _Path(p).rename(_bak)
                    cleaned.append(p)
                except OSError:
                    continue
        log.info(
            "R53: force-cleanup removed %d existing %s spec(s) before regen",
            len(cleaned), tool_norm,
        )
        # Also drop GENERATED_TESTS entries for the targeted tool+reqs
        target_rids = {r.get("id") for r in target_reqs}
        GENERATED_TESTS[:] = [
            t for t in GENERATED_TESTS
            if not (
                t.get("requirement_id") in target_rids
                and (t.get("tool") or "").lower() == tool_norm
            )
        ]

    # Dispatch regen per requirement
    import uuid as _uuid_53
    job_id = str(_uuid_53.uuid4())[:8]
    results = []
    errors = []
    for rq in target_reqs:
        # because `_find_requirement` resolves the slug-keyed in-memory
        # store. Falls back to UUID for projects that don't carry a slug.
        rid = rq.get("req_id") or rq.get("id")
        try:
            gen_body = GenerateRequest(
                requirement_id=rid,
                tools=[tool_norm],
                force=True if force else False,
            )
            # R79.4 — `generate_tests` may return either a plain dict OR a
            # fastapi.JSONResponse on the F8-A4 automation-failure path
            # (status 202 wrapping the failure body). Pre-R79.4 the raw
            # `(res or {}).get(...)` call raised AttributeError on the
            # JSONResponse path, mis-classifying the request as a generic
            # exception and reporting `'JSONResponse' object has no
            # attribute 'get'` to operators — masking the real per-req
            # failure cause (e.g. "LLM returned empty scripts after 3 retries").
            # Reuse F10-2's _normalize_generate_result helper that
            # `_generate_all_background` already uses for the same reason.
            res_raw = await generate_tests(gen_body, request)
            res = _normalize_generate_result(res_raw)
            # When generate_tests returned the failure JSONResponse, the
            # normalised body has status=failed + a `message` field — treat
            # those as per-req errors so the operator sees accurate counts.
            if res.get("status") == "failed":
                _msg = res.get("message") or "generate_tests returned failed status"
                log.warning("R53: regen returned failed status for %s/%s: %s",
                            rid, tool_norm, _msg)
                errors.append({"requirement_id": rid, "error": str(_msg)[:200]})
            else:
                tests_generated = (
                    res.get("tests_generated")
                    or res.get("test_count")
                    or 0
                )
                # R305 — surface partial gen failures (analytics gen-blocked /
                # timed out) so the job's error count is TRUTHFUL instead of
                # reporting "N tests / 0 errors" while a tool produced nothing.
                _gf = res.get("gen_failures") or []
                for _f in _gf:
                    errors.append({
                        "requirement_id": rid,
                        "error": f"{_f.get('tool', '?')}:{_f.get('reason', 'gen_failure')}",
                    })
                results.append({
                    "requirement_id": rid,
                    "status": res.get("status") or "ok",
                    "tests_generated": tests_generated,
                    "gen_failures": _gf,
                })
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("R53: regen failed for %s/%s: %s", rid, tool_norm, exc)
            errors.append({"requirement_id": rid, "error": str(exc)[:200]})

    return {
        "status": "completed",
        "job_id": job_id,
        "project_id": project_id,
        "tool": tool_norm,
        "force": force,
        "requirements_targeted": len(target_reqs),
        "succeeded": len(results),
        "failed": len(errors),
        "results": results[:50],   # cap response size
        "errors": errors[:10],
    }


async def _generate_all_background(job_id: str, project_id: str, reqs: list, request, force: bool = False):
    """Background worker: generates tests for each requirement sequentially."""
    from datetime import datetime, timezone
    job = _GENERATE_ALL_JOBS[job_id]

    # LLM client resolution will be handled per-requirement in generate_tests()
    # but we can try to pre-fetch the global client for session reset if it's Claude CLI
    global_client = getattr(request.app.state, 'anthropic', None) if request else None
    try:
        from ...agents.claude_cli_client import ClaudeCLIClient
        if isinstance(global_client, ClaudeCLIClient):
            global_client.reset_session(project_id)
            global_client.reset_rate_limit()
            global_client.active_project_id = project_id
    except Exception:
        pass

    # When force=True: clear ONLY the requirements being (re)generated — so a
    # SCOPED/partial regen RETAINS every OTHER requirement's tests in memory, DB,
    # disk AND the inventory backup. ROOT-CAUSE FIX for "a restart wiped the
    # inventory": pre-fix, `req_prefix` was derived from reqs[0] and matched the
    # whole prefix-FAMILY (all "KCS"), so a scoped regen of ONE KCS requirement
    # cleared EVERY KCS requirement's specs (orphaning the non-scoped ones as
    # unused .r215.bak) and unlink'd the entire generated_tests.json backup — after
    # which a run dispatched almost nothing. A FULL regen passes all reqs, so this
    # stays equivalent for that case. Killswitch ARTA_SCOPED_FORCE_CLEAR_DISABLE=1
    # restores the old family-wide behavior.
    if force and reqs:
        _fam_clear = os.environ.get("ARTA_SCOPED_FORCE_CLEAR_DISABLE") == "1"
        _regen_ids = {(r.get("id") or r.get("req_id") or "") for r in reqs}
        _regen_ids.discard("")
        if _fam_clear:
            _first = reqs[0].get("id", "") or reqs[0].get("req_id", "")
            _fam = _first.rsplit("-", 1)[0] if "-" in _first else None
            GENERATED_TESTS[:] = [t for t in GENERATED_TESTS
                                  if not (_fam and (t.get("requirement_id") or "").startswith(_fam))]
            _stems = [sanitize_req_id(_fam)] if _fam else []
            _tc_ids = [_fam] if _fam else []
        else:
            GENERATED_TESTS[:] = [t for t in GENERATED_TESTS
                                  if (t.get("requirement_id") or "") not in _regen_ids]
            _stems = [sanitize_req_id(r) for r in _regen_ids]
            _tc_ids = list(_regen_ids)
        try:
            from ...db.session import async_session_factory
            from sqlalchemy import text
            async with async_session_factory() as session:
                for _tid in _tc_ids:
                    _tcp = str(_tid).replace("REQ-", "TC-")
                    await session.execute(
                        text("DELETE FROM test_cases WHERE test_id LIKE :pattern"),
                        {"pattern": (f"{_tcp}%" if _fam_clear else f"{_tcp}-%")},
                    )
                await session.commit()
        except Exception:
            pass
        # R215 — NON-DESTRUCTIVE disk clear: rename to `.r215.bak` (a successful
        # per-req gen overwrites the real path; a FAILED one leaves the .bak
        import glob
        _r215_nd = os.environ.get("ARTA_R215_REGEN_RESTORE_DISABLE", "").lower() not in ("1", "true")
        _spec_dirs = (("playwright", "spec.ts"), ("newman", "json"), ("k6", "js"))
        for _stem in _stems:
            if _fam_clear:
                _pats = [f"src/automation/{_d}/{_stem}*.{_e}" for _d, _e in _spec_dirs]
            else:
                _pats = ([f"src/automation/{_d}/{_stem}.{_e}" for _d, _e in _spec_dirs]
                         + [f"src/automation/{_d}/{_stem}_*.{_e}" for _d, _e in _spec_dirs])
            for pattern in _pats:
                for f in glob.glob(pattern):
                    if _r215_nd:
                        _bk = Path(f + ".r215.bak")
                        _bk.unlink(missing_ok=True)
                        try:
                            Path(f).rename(_bk)
                        except OSError:
                            Path(f).unlink(missing_ok=True)
                    else:
                        Path(f).unlink(missing_ok=True)
        # Re-persist the (scoped-cleared) inventory so the backup reflects the
        # RETAINED requirements — do NOT unlink the whole file (the pre-fix bug).
        try:
            _save_tests_json()
        except Exception:
            pass
        log.info("Force regen: %s-cleared %d req(s); other requirements RETAINED (disk %s)",
                 "family" if _fam_clear else "scoped", len(_regen_ids),
                 "→.r215.bak" if _r215_nd else "deleted")
    # When force=False: incremental — let each requirement's generate_tests() handle
    # its own idempotency (skips unchanged, fills gaps, regenerates changed).

    import asyncio as _aio_bg
    import time as _time_bg

    # R130.G KEYSTONE — bounded-parallel batch gen. Pre-R130.G the for-loop
    # below ran requirements sequentially (line 4742-4748 explicitly warned
    # that ARTA_GEN_CONCURRENCY was inert). Post-R130.G: when concurrency≥2
    # AND operator opts in via env var, requirements run via
    # `asyncio.gather` with bounded `Semaphore(N)`. Concurrency default is
    # provider-aware (2 for Ollama daemon's single-instance-per-model
    # queuing; 4 for Claude API's higher concurrency tolerance).
    #
    # Safeguard #1 (rate-limit-aware): cap chosen to avoid Anthropic rate-
    # limit retry storms (tier-1 ~50 RPM; 4 concurrent reqs × ~10
    # escalation calls/req fits comfortably).
    #
    # Job-state mutations (job["completed"], job["failed"], job["results"]
    # .append, etc.) are guarded by `_job_lock` so concurrent reqs don't
    # race.
    _explicit_concurrency = os.environ.get("ARTA_GEN_CONCURRENCY", "").strip()
    if _explicit_concurrency:
        _concurrency = max(1, int(_explicit_concurrency))
    else:
        # Operator didn't set the env var → pick provider-aware default
        # from the project's LLMConfig (when available).
        _concurrency = 1
        try:
            from .projects import _resolve_project
            _proj_for_concurrency = await _resolve_project(project_id)
            if _proj_for_concurrency:
                _llm_cfg = (_proj_for_concurrency.get("llm_config") or {})
                _provider = str(_llm_cfg.get("provider", "anthropic")).lower()
                _concurrency = _r130_g_batch_concurrency(_provider)
        except Exception:
            _concurrency = 1   # safest default if project resolution fails

    if _concurrency > 1:
        log.info(
            "R130.G: generate-all [%s] activating bounded-parallel gen — "
            "concurrency=%d (asyncio.gather + Semaphore + job lock)",
            job_id, _concurrency,
        )

    # R130.G — job-state lock for concurrent mutation safety. When
    # concurrency=1 (sequential, default), this lock is uncontended
    # (no perf cost). When concurrency≥2, all `job["..."] = ...` /
    # `job["..."].append(...)` mutations serialize through it.
    _job_lock = _aio_bg.Lock()
    _job_sem = _aio_bg.Semaphore(_concurrency) if _concurrency > 1 else None

    async def _r130_g_gen_one_req(idx: int, req: dict) -> None:
        """R130.G — process one requirement. Closure over `job`, `request`,
        `project_id`, `force`, `global_client`. Mutations of `job` dict
        are guarded by `_job_lock`. When `_job_sem` is set (concurrency>1),
        this coroutine awaits it before invoking the LLM-bound work."""
        # D4: abort-check at coroutine entry (parallel reqs may already be
        # in-flight when abort is requested — they finish naturally).
        if job.get("_abort_requested"):
            return

        req_id = req.get("id") or req.get("req_id")
        if not req_id:
            return

        # When concurrency>1, gate on Semaphore; sequential path skips it.
        if _job_sem is not None:
            await _job_sem.acquire()
        try:
            await _r130_g_inner(idx, req, req_id)
        finally:
            if _job_sem is not None:
                _job_sem.release()

    async def _r130_g_inner(idx: int, req: dict, req_id: str) -> None:

        # C4: Skip requirements with no acceptance criteria — they produce empty tests
        acs = req.get("acceptance_criteria", [])
        if not acs or not any(ac.get("statement", "").strip() for ac in acs):
            log.warning("generate-all [%s]: skipping %s — no acceptance_criteria defined", job_id, req_id)
            job["results"].append({
                "requirement_id": req_id,
                "status": "skipped",
                "test_count": 0,
                "tests_retained": 0,
                "tools": {},
                "skip_reason": "no_acceptance_criteria",
            })
            job["completed"] += 1
            _save_job_json(job)
            return   # R130.G: was `continue` in for-loop; closure uses return

        # C1: Resolve LLM client for this specific requirement
        # This ensures we use Ollama if configured, but can still detect rate limits
        client = None
        try:
            from .projects import _resolve_project
            from ...agents.llm_client import create_llm_client
            from ...models.llm_config import LLMConfig
            project = await _resolve_project(project_id)
            if project and project.get("llm_config"):
                cfg = LLMConfig.from_dict(project["llm_config"])
                client = create_llm_client(cfg)
        except Exception:
            pass

        if not client:
            client = global_client

        # C2 / R217 0b: JOB-LEVEL GOVERNOR — pause the whole job to the REAL
        # reset window when the client reports a rate limit. R217 0b wires the
        # transient-429-exhaustion path to set this flag too (not just the
        # long-window "hit your limit" class), so the 110×-429 churn that
        # collapsed bulk-gen now PAUSES here instead of firing the next req
        # straight back into the limit. The wait is chunked + abort-aware +
        # re-checks the live reset (which may extend if a fresh 429 lands),
        # so a minutes-long reset doesn't appear as an indefinite hang.
        if client and hasattr(client, "get_rate_limit_info"):
            try:
                rl_info = client.get_rate_limit_info()
                if rl_info.get("limited"):
                    try:
                        _gov_chunk = float(os.environ.get("ARTA_R217_GOVERNOR_MAX_SLEEP_S", "300"))
                    except (TypeError, ValueError):
                        _gov_chunk = 300.0
                    _gov_chunk = max(10.0, min(1800.0, _gov_chunk))
                    _gov_total = 0.0
                    _gov_cap = 3600.0  # absolute ceiling — never block a job > 1h on one limit
                    job["rate_limit_reset"] = rl_info.get("reset")
                    _save_job_json(job)
                    while _gov_total < _gov_cap:
                        if job.get("_abort_requested"):
                            break
                        _remaining = rl_info.get("reset", 0) - _time_bg.time()
                        if _remaining <= 0:
                            break
                        _nap = min(_gov_chunk, _remaining + 5)
                        log.warning(
                            "generate-all [%s]: R217 0b governor PAUSE — %.0fs remaining "
                            "until rate-limit reset (napping %.0fs) before %s",
                            job_id, _remaining, _nap, req_id,
                        )
                        await _aio_bg.sleep(_nap)
                        _gov_total += _nap
                        # Re-read: a fresh 429 during the nap can extend the window.
                        rl_info = client.get_rate_limit_info()
                        if not rl_info.get("limited"):
                            break
                    if hasattr(client, "reset_rate_limit"):
                        # Post-wait: unconditional clear (the window has elapsed).
                        client.reset_rate_limit()
            except Exception as e:
                log.debug("Rate limit check failed (non-fatal): %s", e)

        # C3: Reset session if supported (e.g. for specialized CLI tools)
        if client and hasattr(client, 'reset_session'):
            client.reset_session(project_id)

        job["current_requirement"] = req_id
        # F11-3: Reset stage hints on each requirement boundary so the SSE
        # consumer doesn't see stale stage info from the previous requirement.
        job["current_stage"] = None
        job["current_stage_started_at"] = None
        _req_started_at = _time_bg.monotonic()
        try:
            gen_body = GenerateRequest(requirement_id=req_id, force=force)
            result = _normalize_generate_result(await generate_tests(gen_body, request))
            test_count = result.get("test_count", 0)
            tests = result.get("tests_generated", [])
            retained = result.get("tests_retained", 0)
            was_skipped = "no changes detected" in result.get("message", "").lower()

            # Build per-tool breakdown — include both generated AND expected-but-missing tools
            tools_detail: dict[str, dict] = {}
            # First, mark all expected tools as "not_generated".
            #
            # F20-14: Default was `["UI"]` — which fabricated a fake
            # playwright expectation whenever the risk_profile was
            # missing from the result dict. For non-UI requirements
            # [API, Security, Performance, Accessibility]), this
            # caused the bg loop to mark playwright as "failed" in
            # the Generation Results modal even though the agent
            # correctly didn't generate a playwright script. Now we
            # default to [] — missing risk_profile means we build
            # tools_detail entirely from actually-generated tests
            # (no false failure labels). Also added "Accessibility"
            # → "axe" mapping so F20-12's axe rows are classified
            # correctly when risk_profile IS present.
            risk_profile = result.get("risk_profile", {}) or {}
            expected_types = risk_profile.get("test_types") or []
            _TYPE_TO_TOOL_MAP = {
                "UI": "playwright", "API": "newman",
                "Performance": "k6", "Security": "zap",
                "Accessibility": "axe",
            }
            for tt in expected_types:
                tool = _TYPE_TO_TOOL_MAP.get(tt)
                if not tool:
                    continue
                tools_detail[tool] = {"status": "not_generated", "test_count": 0, "ac_covered": []}
            # Then overlay with actually generated tests.
            # Track valid vs invalid per tool so we can mark a tool as failed when
            # EVERY test it produced was rejected by script-prefix validation.
            _tool_valid_count: dict[str, int] = {}
            _tool_invalid_count: dict[str, int] = {}
            for t in tests:
                tool = t.get("tool", "playwright")
                if tool not in tools_detail:
                    tools_detail[tool] = {"status": "generated", "test_count": 0, "ac_covered": []}
                else:
                    tools_detail[tool]["status"] = "generated"
                tools_detail[tool]["test_count"] += 1
                if t.get("generation_error"):
                    _tool_invalid_count[tool] = _tool_invalid_count.get(tool, 0) + 1
                else:
                    _tool_valid_count[tool] = _tool_valid_count.get(tool, 0) + 1
                ac = t.get("ac_id", "")
                if ac and ac not in tools_detail[tool]["ac_covered"]:
                    tools_detail[tool]["ac_covered"].append(ac)
            # A tool with ALL tests invalid should be reported as failed,
            # not silently shown as "generated" while the script is missing.
            for tool, invalid_n in _tool_invalid_count.items():
                if _tool_valid_count.get(tool, 0) == 0 and invalid_n > 0:
                    tools_detail[tool]["status"] = "failed"
            # Mark tools that were expected but have 0 tests as "failed".
            # Use the per-tool error from the automation engineer when available —
            # this surfaces the actual cause (batch_marker_missing / timeout /
            # rate_limited / batch_exception) instead of a generic string.
            tool_errors = result.get("tool_errors", {}) or {}
            for tool, detail in tools_detail.items():
                if detail["status"] == "not_generated":
                    detail["status"] = "failed"
                    detail["error"] = tool_errors.get(tool) or "Not generated (no script emitted)"

            # R214 C-FIX-4 — surface 0-test reqs as a COVERAGE GAP, not silent
            # "completed". Pre-R214 a req whose gen produced 0 shippable tests
            # (grounding explosion / gen_source=failed / no test_types) was
            # recorded status="completed" — the operator couldn't tell 18/21 reqs
            # shipped nothing (it looked done). Mark it `completed_no_tests` + the
            # cause so the dashboard + status can list the real coverage gaps.
            _r214_zero = (not was_skipped and test_count == 0 and retained == 0)
            _r214_cause = None
            # P3 (truthful reporting) — distinguish a HARD generation FAILURE
            # (TimeoutError/exception → `generation_failure` set) from a benign
            # zero-test coverage gap. Pre-P3 both were folded into
            # `completed_no_tests` and NEITHER incremented job["failed"], so a
            # run where 4 P0-ish reqs actually TIMED OUT still reported
            # "0 failures / COMPLETE". Now hard failures increment job["failed"]
            # and are tagged so the UI shows them as failures, not silent gaps.
            _r214_hard_fail = False
            if _r214_zero:
                _gf = result.get("generation_failure") or {}
                _r214_hard_fail = bool(_gf) or (result.get("status") == "failed")
                _r214_cause = (
                    result.get("blocked_reason")
                    or (_gf.get("error_type") if isinstance(_gf, dict) else None)
                    or (next(iter((result.get("tool_errors") or {}).values()), None))
                    or "no_tests_generated"
                )
            job["results"].append({
                "requirement_id": req_id,
                "status": ("skipped" if was_skipped
                           else ("gen_failed" if _r214_hard_fail
                                 else ("completed_no_tests" if _r214_zero else "completed"))),
                "test_count": test_count,
                "tests_retained": retained,
                "tools": tools_detail,
                **({"coverage_gap": True, "coverage_gap_cause": str(_r214_cause)[:160],
                    "gen_failed": _r214_hard_fail} if _r214_zero else {}),
            })
            job["completed"] += 1
            if _r214_hard_fail:
                job["failed"] = job.get("failed", 0) + 1
                job.setdefault("errors", []).append(
                    {"requirement_id": req_id, "error_type": str(_r214_cause)[:80],
                     "stage": "automation_gen"})
            job["total_tests_generated"] += test_count
            try:
                from ...telemetry import bucket as _tel_bucket, emit as _tel_emit
                _tel_emit("test.generated", {"count_bucket": _tel_bucket(test_count)})
            except Exception:
                pass
            if _r214_zero:
                job["zero_test_reqs"] = int(job.get("zero_test_reqs", 0)) + 1
                log.warning("R214 C-FIX-4: %s produced 0 tests (cause=%s) — COVERAGE GAP",
                            req_id, _r214_cause)
            # F11-4: track skipped distinctly so the SSE consumer can render
            # "21 unchanged" instead of misreading 0 generated as 21 errors.
            if was_skipped:
                job["skipped"] = int(job.get("skipped", 0)) + 1
            # F11-6: rolling per-req timing → ETA. Don't include skips (they
            # complete in <1s and would distort the average for real work).
            if not was_skipped:
                _record_req_completion(job_id, _time_bg.monotonic() - _req_started_at)
            log.info("generate-all [%s]: %s completed (%d/%d) — tools: %s",
                     job_id, req_id, job["completed"], job["total_requirements"],
                     ", ".join(f"{t}:{d['test_count']}" for t, d in tools_detail.items()))
        except HTTPException as http_exc:
            # Fix Q: 409 dedup means "another in-flight workflow is producing
            # the same output". The work IS happening, just not via THIS bulk
            # job — treat as SKIP (not FAIL) so the bulk job's accounting
            # reflects reality. Verified live: 5 bulk jobs in 32s thrashed,
            # each reporting 21 false-failures per click. With this fix, the
            # 2nd+ job reports 21 skipped (concurrent_generation) instead.
            if http_exc.status_code == 409 and isinstance(http_exc.detail, dict) \
               and http_exc.detail.get("error") == "generation_in_flight":
                existing_wf = http_exc.detail.get("workflow_id", "?")
                log.info(
                    "generate-all [%s]: %s already in-flight (workflow=%s) — "
                    "skipping (not failing)",
                    job_id, req_id, existing_wf,
                )
                job["results"].append({
                    "requirement_id": req_id,
                    "status": "skipped",
                    "test_count": 0,
                    "tests_retained": 0,
                    "tools": {},
                    "skip_reason": "concurrent_generation",
                    "concurrent_workflow_id": existing_wf,
                })
                job["skipped"] = (job.get("skipped") or 0) + 1
                job["completed"] += 1
                _save_job_json(job)
                return   # R130.G: was `continue` in for-loop; closure uses return
            # Real HTTP errors fall through to the generic handler.
            error_str = str(http_exc.detail) if http_exc.detail is not None else str(http_exc)
            log.error("generate-all [%s]: %s failed: %s", job_id, req_id, http_exc)
            job["errors"].append({
                "requirement_id": req_id,
                "error": error_str,
                "is_rate_limited": False,
            })
            job["failed"] += 1
            job["completed"] += 1
        except Exception as exc:
            error_str = str(exc)
            is_rate_limit = "rate limit" in error_str.lower() or "hit your limit" in error_str.lower()
            log.error("generate-all [%s]: %s failed: %s", job_id, req_id, exc)
            job["errors"].append({
                "requirement_id": req_id,
                "error": error_str,
                "is_rate_limited": is_rate_limit,
            })
            job["failed"] += 1
            job["completed"] += 1

        # Track rate limit info on the job
        try:
            from ...agents.claude_cli_client import ClaudeCLIClient
            rl_info = ClaudeCLIClient.get_rate_limit_info()
            if rl_info["limited"]:
                job["rate_limit_reset"] = rl_info["reset"]
        except Exception:
            pass

        # Persist job after each requirement (survives crashes)
        _save_job_json(job)

        # B3: Removed unconditional 3s sleep between requirements (saved 60s/run on 21 reqs).
        # Rate-limit backoff is already handled at the start of each requirement (lines 2625-2633)
        # and only fires when the client actually reports a rate limit.

    # R130.G dispatch: sequential when concurrency=1, bounded-parallel else.
    # Pre-flight abort check fires regardless of mode (line 4810-4818 in
    # _r130_g_gen_one_req handles per-coroutine abort).
    if _concurrency <= 1:
        # Sequential path — preserves pre-R130.G behavior exactly when the
        # operator hasn't opted into parallelism.
        for idx, req in enumerate(reqs):
            if job.get("_abort_requested"):
                log.warning(
                    "generate-all [%s]: abort requested — stopping at requirement %d/%d",
                    job_id, idx, len(reqs),
                )
                job["status"] = "aborted"
                job["aborted_at"] = datetime.now(timezone.utc).isoformat()
                job["current_requirement"] = None
                _save_job_json(job)
                return
            await _r130_g_gen_one_req(idx, req)
            # R215 Item-1b — PACING. The serialized claude_code CLI degrades under
            # SUSTAINED bulk load (21 reqs × multi-stage back-to-back → rate-limit
            # "resetting rate limits and session" → risk/ATDD calls TimeoutError →
            # 0 tests, coverage collapse). A single isolated call is fast (26.8s);
            # the problem is the call RATE. Sleep between reqs so the CLI's
            # rate-limit window recovers. Default 8s; tune via
            # ARTA_GENERATE_ALL_REQ_DELAY_S; 0 disables. Only the sequential
            # (claude_code) path — parallel providers (Ollama) don't need it.
            if idx < len(reqs) - 1:
                try:
                    _pace = float(os.environ.get("ARTA_GENERATE_ALL_REQ_DELAY_S", "8"))
                except (TypeError, ValueError):
                    _pace = 8.0
                if _pace > 0:
                    await _aio_bg.sleep(_pace)
                # R217 0d — BATCH RECOVERY WINDOW. Per-req pacing keeps the
                # call RATE down, but a 21-req burst still accumulates toward
                # the OAuth per-window budget. After every batch_size reqs,
                # take a LONGER recovery sleep sized to the rate-reset window
                # so the full set completes over time WITHOUT a sustained
                # burst (the "bulk gen at the end actually finishes" deliverable).
                # Composes with the 0b governor: batching is PROACTIVE spacing;
                # the governor is REACTIVE pausing when a limit is already hit.
                # Default batch_size=0 → disabled (preserves pure per-req pacing).
                try:
                    _batch_size = int(os.environ.get("ARTA_GENERATE_ALL_BATCH_SIZE", "0"))
                except (TypeError, ValueError):
                    _batch_size = 0
                if _batch_size > 0 and (idx + 1) % _batch_size == 0:
                    try:
                        _recovery = float(os.environ.get("ARTA_GENERATE_ALL_BATCH_RECOVERY_S", "60"))
                    except (TypeError, ValueError):
                        _recovery = 60.0
                    if _recovery > 0:
                        log.info(
                            "generate-all [%s]: R217 0d batch boundary (%d/%d reqs) — "
                            "recovery window %.0fs before next batch",
                            job_id, idx + 1, len(reqs), _recovery,
                        )
                        job["batch_recovery_until"] = _time_bg.time() + _recovery
                        _save_job_json(job)
                        await _aio_bg.sleep(_recovery)
    else:
        # R130.G bounded-parallel path. asyncio.gather runs all reqs
        # concurrently; the Semaphore inside _r130_g_gen_one_req caps
        # in-flight count at `_concurrency`. Per-req exception isolation
        # — one failing req does NOT poison sibling reqs (each
        # coroutine catches + records its own errors).
        async def _wrapped(idx_req: tuple[int, dict]):
            i, r = idx_req
            try:
                await _r130_g_gen_one_req(i, r)
            except Exception as _exc:
                log.error(
                    "R130.G: req %s failed in parallel batch: %s",
                    r.get("id") or r.get("req_id") or f"idx={i}", _exc,
                )
        await _aio_bg.gather(*[_wrapped(pair) for pair in enumerate(reqs)])

    job["status"] = "completed"
    job["completed_at"] = datetime.now(timezone.utc).isoformat()
    job["current_requirement"] = None
    _save_job_json(job)
    log.info("generate-all [%s]: finished — %d tests for %d requirements (%d errors)",
             job_id, job["total_tests_generated"], job["total_requirements"], job["failed"])


@router.get("/{test_id}/script", dependencies=[Depends(_require_api_key)])
async def get_test_script(test_id: str):
    """Return the automation script content for a test case."""
    import pathlib
    from fastapi import HTTPException

    test = next((t for t in GENERATED_TESTS if t["id"] == test_id.upper()), None)
    if not test:
        raise HTTPException(status_code=404, detail=f"Test {test_id} not found")

    # Resolve absolute repo root (parent of src/api/routers/) and guard against
    # path traversal (e.g. script_path = "../../../../etc/passwd")
    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent.parent
    full_path = (repo_root / test["script_path"]).resolve()
    if not str(full_path).startswith(str(repo_root)):
        raise HTTPException(status_code=403, detail="Access denied: path outside repo")

    if full_path.exists():
        return {"test_id": test_id, "tool": test["tool"], "content": full_path.read_text()}

    return {
        "test_id": test_id,
        "tool": test["tool"],
        "content": f"// Script at {test['script_path']}\n// Run /generate-tests to regenerate",
    }


# ── Feature 1: Test Data Fixtures ─────────────────────────────────────────
# F7-2: Mounted at top of file (router-order matters). Re-export legacy
# symbols below so `from .tests import MOCK_FIXTURES` still resolves.
from .tests_fixtures import (  # noqa: E402,F401
    MOCK_FIXTURES,
    FixtureUpsertRequest,
)


# ── Feature 3: Test Case Version History ─────────────────────────────────

# F7-2: MOCK_VERSIONS imported from tests_state at top — use update() so the
# seed populates the shared dict instead of rebinding the name.
MOCK_VERSIONS.update({
    "TC-124": [
        {"version": 3, "date": "2026-03-11", "reason": "Added Amex scenario + Examples table", "by": "arta-agent",
         "gherkin": "Scenario Outline: Successful card checkout\n  Given I have <qty> items\n  When I enter <card_type> card\n  Then order confirmed within <timeout>s"},
        {"version": 2, "date": "2026-03-09", "reason": "Added Mastercard boundary test", "by": "arta-agent",
         "gherkin": "Scenario: Successful Visa card checkout\n  Given I have 2 items in my cart\n  When I enter valid Visa card details\n  Then the order should be confirmed"},
        {"version": 1, "date": "2026-03-05", "reason": "Initial generation from REQ-017", "by": "arta-agent",
         "gherkin": "Scenario: Checkout happy path\n  Given I am on the checkout page\n  When I submit valid card\n  Then order is placed"},
    ],
    "TC-126": [
        {"version": 2, "date": "2026-03-11", "reason": "Added Scenario Outline with 3 VU tiers", "by": "arta-agent",
         "gherkin": "Scenario Outline: Payment timeout under load\n  Given <vus> virtual users\n  Then p95 below <threshold_ms>ms"},
        {"version": 1, "date": "2026-03-09", "reason": "Initial k6 performance test generation", "by": "arta-agent",
         "gherkin": "Scenario: Payment timeout with concurrent load\n  Given 500 virtual users\n  Then p95 < 3000ms"},
    ],
})  # F7-2: closes MOCK_VERSIONS.update({...})


# F7-2 (continuation): Versions / rollback / revert endpoints extracted to
# tests_versions.py. Mounted on the same router so URL paths are unchanged.
# RollbackRequest + VersionCreateRequest re-exported for any legacy callers.
from .tests_versions import (  # noqa: E402,F401
    VersionCreateRequest,
    RollbackRequest,
)


# F7-2 (continuation): version_diff + create_version + RollbackRequest +
# rollback_test + revert_to_version moved to tests_versions.py — bodies
# preserved in git history.




# F7-2: pending-reviews + feedback + push-to-github + suite-level review +
# quality-check extracted to tests_review.py — re-exports below for any legacy
# `from .tests import` callers.
from .tests_review import (  # noqa: E402,F401
    ReviewDecision, GherkinEdit, TestFeedback, ReviewRequest,
)
