"""
ARTA Defect Intelligence Agent
Autonomous root cause analysis, fix suggestions, and Jira auto-filing.
"""
from __future__ import annotations

import json
import os
import re

import anthropic
from anthropic import AsyncAnthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .retry_policy import LLM_RETRYABLE_EXC   # R134.C — single-source-of-truth retry tuple


RCA_PROMPT = """\
You are an expert software debugging engineer performing root cause analysis.

FAILED TEST:
- Test ID: {test_id}
- Test Title: {test_title}
- Test Type: {test_type}
- Requirement: {requirement_id}
- Risk Priority: {priority}

FAILURE EVIDENCE:
Error Message: {error_message}
Stack Trace:
{stack_trace}

Browser/API Logs (last 20 lines):
{logs}

Last Passing Commit: {last_passing_commit}
Current Commit: {current_commit}
Changed Files: {changed_files}

ANALYSIS TASK:
1. Identify the root cause (be specific — file, line, function if possible)
2. Classify the failure type:
   - BUG (code defect)
   - ENV (environment/infrastructure issue)
   - TEST (flaky test or wrong assertion)
   - REGRESSION (working feature broken by recent change)
   - PERFORMANCE (SLA violation)
   - SECURITY (vulnerability found)
3. Determine impact radius (what else could be affected)
4. Suggest a specific fix with code snippet
5. Recommend regression test additions to prevent recurrence
6. Produce a 5-level root-cause deep-dive. Each level MUST name a DIFFERENT layer —
   NOT a restatement of the one above:
   - symptom:            what the operator/runtime observed (the failure surface)
   - immediate_cause:    the direct failing call / assertion / response
   - upstream_cause:     why that component produced it (the true origin)
   - architectural_cause:the design / contract / missing abstraction that allowed it
   - process_cause:      how it escaped earlier validation (gen gate / review / test gap)

Output ONLY valid JSON:
{{
  "root_cause": "<specific description with file/function if possible>",
  "failure_type": "BUG|ENV|TEST|REGRESSION|PERFORMANCE|SECURITY",
  "confidence": <0.0–1.0>,
  "impacted_files": ["path/to/file.ts:line", ...],
  "impacted_features": ["feature name", ...],
  "suggested_fix": "<code snippet or description>",
  "fix_effort": "minutes|hours|days",
  "regression_tests_needed": ["test description", ...],
  "title": "<concise defect title>",
  "severity": "P0|P1|P2|P3",
  "deep_dive": {{
    "symptom": "<observed failure surface>",
    "immediate_cause": "<direct failing call/assert/response>",
    "upstream_cause": "<why that component produced it>",
    "architectural_cause": "<design/contract that allowed it>",
    "process_cause": "<how it escaped earlier validation>"
  }},
  "preventive_action": "<one concrete change that stops this class of failure recurring>"
}}
"""


# D1 — system prompt for the single-defect RCA. The CLI transport (Claude Code)
# defaults to role-playing an investigation ("Let me examine the source files…"
# + a ```python``` block) BEFORE the JSON, which defeats JSON extraction. This
# pins the response to exactly one bare JSON object.
_RCA_JSON_ONLY_SYSTEM = (
    "You are a defect root-cause analyzer. You have NO filesystem access and NO "
    "tools — do NOT attempt to read, open, or examine any files; reason ONLY from "
    "the failure context in the user message. Respond with EXACTLY ONE JSON object "
    "matching the requested schema and NOTHING else: no preamble, no explanation, "
    "no markdown code fences, no trailing commentary."
)


# ── R127.D.6.F — violation-kind to defect_subclass mapping ──────────────────
#
# When R102.A stamps a spec with a violation kind in `metadata.violation_kinds`
# (via R111.E aggregator at execution.py:3366-3368), R127.D.6.F surfaces the
# kind on the defect row as `defect_subclass` so the operator dashboard can
# group/filter rows by the specific gen-quality failure mode. The parent
# `defect_class` stays `"grounding_blocked"` (R118.G unchanged) so the
# existing auto-heal flow + UI tile copy continue to fire — subclass is
# purely additive.
#
# First-match-wins on iteration order; subclass is single-valued.
_R127_D6_F_KIND_TO_SUBCLASS: dict[str, str] = {
    # R127.D.6.A — PW post-merge paren/brace imbalance
    "merged_paren_imbalance":        "merge_paren_imbalance",
    "merged_brace_imbalance":        "merge_brace_imbalance",
    # R127.D.6.D — pytest AST-validation failures
    "pytest_syntax_error_indent":    "pytest_indent_error",
    "pytest_syntax_error_eof":       "pytest_unclosed_string_or_paren",
    "pytest_syntax_error_generic":   "pytest_syntax_error",
    # R127.D.7.A — per-sub paren imbalance caught at gen attempt 3
    # (triggers R127.C escalation). The fact that the violation appears
    # in metadata.violation_kinds AT ALL means the escalation also failed
    # to produce a clean spec — operator should investigate this sub
    # specifically.
    "per_sub_paren_imbalance":       "per_sub_imbalance_escalated",
    # R127.D.7.C — aggregated safety-net summary (R127.D.7.B skipped
    # ≥1 sub at merge entry because Claude Code escalation also produced
    # ±1 imbalance). Different from `per_sub_imbalance_escalated` —
    # this signals the safety net fired downstream of the upstream
    # escalation, indicating Claude Code may need prompt tuning for
    # the specific Gherkin scenario shape.
    "subs_paren_imbalance":          "per_sub_imbalance_skipped",
}


def _r127_d6_f_compute_subclass(metadata: dict | None) -> str | None:
    """R127.D.6.F — compute `defect_subclass` from a failure's
    `metadata.violation_kinds` (populated by R102.C dispatch reader +
    R111.E aggregator at execution.py:3366-3368).

    Returns the mapped subclass label (e.g., "merge_paren_imbalance"),
    OR `None` when no recognized kind is present. First-match-wins on
    iteration order through `metadata.violation_kinds` keys.

    Single source of truth: R118.G's grounding-blocked branch calls
    this helper; the unit tests at test_r127_d6_f_defect_subclass.py
    exercise the same function directly.
    """
    if not isinstance(metadata, dict):
        return None
    kinds = metadata.get("violation_kinds")
    if not isinstance(kinds, dict):
        return None
    for vk in kinds.keys():
        mapped = _R127_D6_F_KIND_TO_SUBCLASS.get(vk)
        if mapped:
            return mapped
    return None


# ── Deterministic charter RCA — impact / preventive_action / 5-level deep-dive ──
#
# The charter requires EVERY issue to carry {root_cause, impact, severity,
# recommended_fix, preventive_action} + a 5-level deep-dive (symptom → immediate
# → upstream → architectural → process). The LLM RCA path (analyze_failures →
# RCA_PROMPT) produces all of that — but it runs ONLY for `sut_regression` (by
# design: the deterministically-triaged majority — test_gen_bug / grounding_blocked
# / sut_contract_change / operator_review — is built WITHOUT an LLM for token/cost
# efficiency). Those LLM-free defects already carry root_cause + fix + severity;
# they were MISSING impact + preventive_action + deep_dive.
#
# This taxonomy fills exactly those three, DETERMINISTICALLY (the root cause of
# each known class IS known — no LLM needed), so the charter contract holds for
# every defect while preserving the efficiency mandate. Keyed on `defect_class`;
# `{signals}` is interpolated from the defect's triage_signals. Single source of
# truth for the deterministic branch. Killswitch ARTA_DETERMINISTIC_RCA_DISABLE.
_DETERMINISTIC_RCA: dict[str, dict] = {
    "grounding_blocked": {
        "impact": "This spec was BLOCKED at dispatch and never executed against "
                  "the SUT — the requirement's coverage for this scenario is "
                  "UNVERIFIED until a clean regen ships. SUT quality is not implicated.",
        "preventive_action": "Strengthen the gen-time grounding constraint (DOM "
                             "catalog / captured-endpoint / valid-role injection) so "
                             "the LLM cannot emit the violating pattern; the R57.1 "
                             "retry-with-hint loop should converge within 3 attempts.",
        "deep_dive": {
            "symptom": "Spec surfaced a BLOCKED row with 0 SUT execution.",
            "immediate_cause": "R102.C dispatch gate read the R102.A grounding-"
                               "violation stamp on the spec and skipped execution.",
            "upstream_cause": "Gen emitted a hallucinated selector / endpoint / role "
                              "that the grounding validator rejected ({signals}).",
            "architectural_cause": "The SUT's real DOM/endpoint/role inventory was "
                                   "not fully injected into the gen prompt, or the "
                                   "LLM did not honour the injected HARD CONSTRAINT.",
            "process_cause": "The R57.1 retry-with-hint loop exhausted its attempts "
                             "without converging on a grounded spec.",
        },
    },
    "test_gen_bug": {
        "impact": "ARTA-side artifact defect — SUT quality is NOT implicated. The "
                  "affected scenario's coverage is deferred to the self-heal regen queue.",
        "preventive_action": "Add or tighten the gen-time validator / deterministic "
                             "rewriter for this pattern so it is corrected AT GEN, "
                             "not caught at execution.",
        "deep_dive": {
            "symptom": "Test failed on a flaw in the ARTA-generated artifact.",
            "immediate_cause": "The generated script/collection carried a gen-quality "
                               "flaw ({signals}).",
            "upstream_cause": "The LLM emitted a pattern the grounding/rewriter layer "
                              "did not fully constrain.",
            "architectural_cause": "A gen-quality gate gap for this pattern class.",
            "process_cause": "The flaw passed the gen gate and surfaced only at "
                             "execution instead of being blocked/healed at gen.",
        },
    },
    "sut_contract_change": {
        "impact": "A consumer's extraction/assertion is stale; chained tests that "
                  "depend on it may cascade until the contract is re-grounded.",
        "preventive_action": "Re-run response-shape discovery (R305/R212) so the "
                             "captured shape reflects the SUT's current contract, then "
                             "regenerate assertions from the refreshed shape.",
        "deep_dive": {
            "symptom": "Assertion/extraction failed on a response-shape mismatch.",
            "immediate_cause": "The SUT returned a different field/structure than the "
                               "test expects ({signals}).",
            "upstream_cause": "The SUT's response contract changed (rename/reorg) "
                              "since the shape was captured.",
            "architectural_cause": "The test was grounded on a now-stale captured "
                                   "response_body_shape.",
            "process_cause": "Discovery/capture was not refreshed before this run.",
        },
    },
    "operator_review": {
        "impact": "Unclassified — the failure's SUT-vs-ARTA attribution is UNKNOWN "
                  "until an operator triages it; do not read it as a SUT finding yet.",
        "preventive_action": "Extend the deterministic triage rules (R258/R300) to "
                             "recognize this failure shape so future occurrences "
                             "classify automatically instead of falling to review.",
        "deep_dive": {
            "symptom": "Test failed with a pattern the deterministic triage did not match.",
            "immediate_cause": "The failure evidence matched no known triage rule "
                               "({signals}).",
            "upstream_cause": "Unknown until triaged — could be SUT, test-gen, or env.",
            "architectural_cause": "A coverage gap in the deterministic triage taxonomy.",
            "process_cause": "The failure shape is novel, or the evidence is "
                             "insufficient for automatic classification.",
        },
    },
}


def deterministic_rca_fields(defect_class: str | None,
                             triage_signals: list | None = None) -> dict:
    """The charter's impact + preventive_action + 5-level deep_dive for a KNOWN
    deterministic defect class — no LLM (the root cause of each class is known).
    Returns {} for an unknown class or when disabled (fail-open). `triage_signals`
    are interpolated into the `{signals}` slots. Deterministic; single-source for
    the LLM-free defect branch."""
    if os.environ.get("ARTA_DETERMINISTIC_RCA_DISABLE") == "1":
        return {}
    tmpl = _DETERMINISTIC_RCA.get(str(defect_class or ""))
    if not tmpl:
        return {}
    sig = ", ".join(str(s) for s in (triage_signals or [])) or "no specific signal recorded"
    dd = {k: v.replace("{signals}", sig) for k, v in tmpl["deep_dive"].items()}
    return {
        "impact": tmpl["impact"],
        "preventive_action": tmpl["preventive_action"],
        "deep_dive": dd,
    }


# ── R258.B — runner-agnostic status expectation parsing ──────────────────────
# Pre-R258.B every call site used a bare `expected\s*:?\s*200` regex. That
# matches Playwright's `Expected: 200 / Received: 404` but NOT Newman's
# `expected response to have status code 200 but got 404`. Consequence
# (run-0c19e6): the SAME defect classified differently BY RUNNER — PW 404s hit
# the 4xx-on-expected-200 rule → sut_regression; Newman 404s fell through to
# the operator_review fallback at confidence 0.0. Runner shape is not a
# property of the SUT, so it must not steer triage.
_R258_B_EXPECTED_RE = re.compile(
    r"expected(?:\s+response)?(?:\s+to\s+have)?(?:\s+status(?:\s+code)?)?\s*:?\s*(\d{3})\b",
    re.IGNORECASE,
)
_R258_B_RECEIVED_RE = re.compile(
    r"(?:received|(?:but\s+)?got|but\s+was|actual)\s*:?\s*(\d{3})\b",
    re.IGNORECASE,
)

# R300.C — the set of ACTUAL HTTP status codes. Used to reject bare 3-digit
# numbers (p95=519ms, expected=577, port/socket numbers) that the un-anchored
# `[45]\d\d` fallback would otherwise misread as HTTP 5xx/4xx → FALSE
# sut_regression accusations. Covers the real 4xx + 5xx codes only (1xx/2xx/3xx
# never reach the failure classifier).
_VALID_HTTP_STATUS = frozenset({
    400, 401, 402, 403, 404, 405, 406, 407, 408, 409, 410, 411, 412, 413, 414,
    415, 416, 417, 418, 421, 422, 423, 424, 425, 426, 428, 429, 431, 451,
    500, 501, 502, 503, 504, 505, 506, 507, 508, 510, 511,
})

# Shape detector for R258 branch 2. Deliberately mirrors grounding_validator's
# _R190_ID_SHAPED_RE semantics rather than importing it: that module is a
# gen-time dependency and defect_intel runs at report time.
_R258_ID_SHAPED_RE = re.compile(
    r"^(?:"
    r"\d+|"                                          # 42
    r"[0-9a-f]{8}-[0-9a-f-]{8,}|"                    # uuid
    r"[0-9a-f]{16,}|"                                # long hex / mongo oid
    r"[A-Za-z]{2,}[-_][A-Za-z0-9-]*\d[A-Za-z0-9-]*"  # ACC-9F31A2, ASSET-VEH-2468
    r")$",
    re.IGNORECASE,
)


def parse_status_expectation(error_message: str) -> tuple[int | None, int | None]:
    """R258.B — extract (expected, received) HTTP status from a runner's error
    message, runner-agnostically.

    Handles all three shapes ARTA dispatches:
      • Playwright: "Expected: 200\\nReceived: 404"
      • Newman:     "expected response to have status code 200 but got 404"
      • k6:         "expected status 200, got 404"

    Returns (None, None) when the message carries no status expectation.
    """
    if not error_message:
        return (None, None)
    _m_exp = _R258_B_EXPECTED_RE.search(error_message)
    _m_rcv = _R258_B_RECEIVED_RE.search(error_message)

    def _to_int(m):
        if not m:
            return None
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return None

    return (_to_int(_m_exp), _to_int(_m_rcv))


def _r258_skel(p: str, *, aggressive: bool = False) -> str:
    """R258 — path skeleton for 404 triage.

    Delegates to `api_discovery._r212_skel` (single source of truth for the
    strict form, and how `captured_keys` are built). `aggressive=True`
    additionally collapses BUSINESS-shaped ids (`ACC-OTP-57291`) that
    `_r212_skel` deliberately leaves literal, so a fabricated business id can
    be told apart from a genuinely unknown endpoint.
    """
    from .api_discovery import _r212_skel

    base = _r212_skel(p or "")
    if not aggressive:
        return base
    out = [
        ("*" if _R258_ID_SHAPED_RE.match(s) else s)
        for s in base.split("/") if s
    ]
    return "/" + "/".join(out)


_R296_BODYLESS_CACHE: dict[str, set] = {}


def _r296_bodyless_post_skeletons(project_id: str) -> set:
    """R296 — path skeletons of POST/PUT/PATCH endpoints for which ARTA has NO
    non-empty request body (no OpenAPI requestBody, no captured shape, no
    probe-captured body, and — once it ships — no DTO-from-source shape).

    A 500 on one of these is ARTA's missing-body gap, NOT a SUT regression: the
    request could not be constructed correctly, so its result is not a credible
    statement about SUT quality. Cached per project (the sources are per-run
    stable). Empty set on any failure → the caller keeps the current (SUT)
    classification: R296 only ever DOWNGRADES a confident body-gap, never hides
    an unexplained 500.
    """
    if project_id in _R296_BODYLESS_CACHE:
        return _R296_BODYLESS_CACHE[project_id]
    skels: set = set()
    try:
        from pathlib import Path as _P296
        from .api_discovery import _load_captured_endpoints
        from .test_data import build_request_bodies
        caps = _load_captured_endpoints(project_id) or []
        spec = {}
        _ope = _P296(".arta/openapi") / f"{project_id}.json"
        if _ope.is_file():
            spec = json.loads(_ope.read_text())
        bodies = build_request_bodies(openapi_spec=spec, captured_endpoints=caps,
                                      project_id=project_id) or {}

        def _nonempty(b):
            f = b[0] if isinstance(b, list) and b else b
            return isinstance(f, dict) and bool(f)

        for ep in caps:
            if not isinstance(ep, dict):
                continue
            m = str(ep.get("method") or "").upper()
            p = ep.get("path") or ""
            if m not in ("POST", "PUT", "PATCH") or not p:
                continue
            if not _nonempty(bodies.get(f"{m} {p}") or {}):
                skels.add(_r258_skel(p))
    except Exception:
        skels = set()
    _R296_BODYLESS_CACHE[project_id] = skels
    return skels


def _r258_decompose_404(
    *,
    path: str,
    error_message: str,
    real_ids: set[str] | None = None,
    captured_keys: set[str] | None = None,
    real_id_store_available: bool = False,
    seeded_ids: set[str] | None = None,
) -> dict:
    """R258 — DECOMPOSE the 404 cluster, mirroring R111.H's 5xx decomposition.

    Pre-R258 every 4xx-on-expected-200 (404 included) flowed to
    `sut_regression` conf 0.65 + `create_defect`. Live evidence (run-0c19e6:
    520 of 813 failures = 404) shows nearly all of them are ARTA-side: the LLM
    hardcoded a FABRICATED entity id (33 of 63 specs) or called an endpoint the
    SUT never served. Filing those as SUT defects poisons the operator's Jira
    queue and inverts the mission — ARTA reports its own bugs as the SUT's.

    R111.H did this for 5xx; the 404 cluster never got the same treatment.

    Ordering is most-specific-first:
      1. path skeleton NOT in captured_keys  → test_gen_bug  (wrong endpoint)
      2. id-shaped literal not in real_ids   → test_gen_bug  (fabricated data)
      3. captured path + known-real id       → sut_regression (GENUINE signal)
      4. otherwise                           → not_assessed  (honest abstention)

    `real_id_store_available` is the honesty term: until WS1a's store is
    populated, ARTA cannot PROVE an id is fabricated, so branch 2 reports a
    lower confidence and branch 3 abstains rather than crediting the SUT.
    """
    signals: list[str] = []
    real_ids = real_ids or set()
    captured_keys = captured_keys or set()

    # Two skeletons, because they answer two different questions:
    #   • strict  (_r212_skel) — normalizes only numeric/hex/uuid ids to '*',
    #     matching how captured_keys were built.
    #   • loose   — ALSO normalizes business-shaped ids (ACC-OTP-57291,
    #     ASSET-VEH-2468) that _r212_skel leaves literal.
    # If strict misses but loose hits, the ENDPOINT is real and the mismatch is
    # the id → that is branch 2, not branch 1. Without this split, every
    # fabricated business id would be misreported as an invented endpoint and
    # WS1's fabricated-id metric would read zero.
    strict = _r258_skel(path or "", aggressive=False)
    loose = _r258_skel(path or "", aggressive=True)
    _endpoint_known = bool(captured_keys) and (
        strict in captured_keys or loose in captured_keys)

    # Branch 1 — the endpoint itself was never served by the SUT. A 404 on a
    # path ARTA invented says nothing about SUT quality.
    if captured_keys and not _endpoint_known:
        signals.append(f"unknown_endpoint_404_{strict}")
        return {
            "triage_category": "test_gen_bug",
            "triage_confidence": 0.85,
            "triage_signals": signals,
            "recommended_action": "regenerate_test_with_endpoint_grounding",
            "test_gen_bug_subtype": "unknown_endpoint",
        }

    # Branch 2 — the endpoint is real but an id-shaped literal in the path is
    # not one the SUT ever served → fabricated test data (the R170 principle,
    # applied to the LLM's output rather than ARTA's own synthesizer).
    # Read ids off the ORIGINAL path: real_ids are case-sensitive, the
    # skeletons are lowercased.
    _id_literals = [
        s for s in (path or "").split("?")[0].split("/")
        if s and not s.startswith("{") and _R258_ID_SHAPED_RE.match(s)
    ]
    _unknown_ids = [s for s in _id_literals if s not in real_ids]
    if _id_literals and _unknown_ids:
        signals.append(f"fabricated_id_404_{_unknown_ids[0]}")
        return {
            "triage_category": "test_gen_bug",
            # Honesty term: without the real-id store we cannot PROVE the id is
            # fabricated (it may be a legitimately chained id we never
            # recorded), so report lower confidence rather than a false 0.85.
            "triage_confidence": 0.85 if real_id_store_available else 0.60,
            "triage_signals": signals,
            "recommended_action": "regenerate_test_with_real_data_grounding",
            "test_gen_bug_subtype": "fabricated_id",
        }

    # Branch 3 — captured endpoint, and every id in the path is known-real.
    # A 404 here is a CREDIBLE SUT signal: ARTA asked a valid question.
    # Gated on the store being populated — otherwise ARTA would be blaming the
    # SUT over ids it never actually verified.
    if _endpoint_known and real_id_store_available and not _unknown_ids:
        # R254.SEED — EXCEPT when the id is one ARTA CREATED (opt-in sandbox
        # seeding). A 404 on ARTA's own fixture means the fixture is wrong or
        # was cleaned up — ARTA's defect, not the SUT's. Filing it as
        # sut_regression is precisely the misreporting this workstream exists
        # to stop, so seeded ids can never reach a SUT accusation.
        _seeded_hit = [s for s in _id_literals if s in (seeded_ids or set())]
        if _seeded_hit:
            signals.append(f"seeded_id_404_{_seeded_hit[0]}")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.75,
                "triage_signals": signals,
                "recommended_action": "verify_seeded_fixture",
                "test_gen_bug_subtype": "seeded_fixture_missing",
            }
        signals.append(f"sut_404_on_grounded_request_{strict}")
        return {
            "triage_category": "sut_regression",
            "triage_confidence": 0.70,
            "triage_signals": signals,
            "recommended_action": "create_defect",
        }

    # Branch 4 — ARTA does not know. Say so, rather than guessing either way.
    signals.append("not_assessed_404")
    return {
        "triage_category": "not_assessed",
        "triage_confidence": 0.0,
        "triage_signals": signals,
        "recommended_action": "operator_review",
    }


def _r303_c_decompose_4xx(
    *,
    status_code: int,
    path: str,
    method: str = "POST",
    request_body_raw: str | None = None,
    project_id: str | None = None,
    captured_keys: set[str] | None = None,
    captured_endpoints: list | None = None,
    source_verified: bool = False,
) -> dict | None:
    """R303.C — extend the 404 endpoint-existence grounding (`_r258_decompose_404`) to
    the REST of the 4xx cluster (400/409/422), plus request-body contract grounding for
    400. Pre-R303.C these hit the blind LAYER-1B rule (`sut_regression` conf 0.65), which
    the R259 fidelity gate then DEMOTED to `unknown` (< 0.7) — a major feeder of the
    "82% unattributable" rate. Returns a grounded verdict, or None to fall through
    (honest: only classify when there IS evidence). Killswitch ARTA_R303_C_DISABLE=1.

    Grounding, most-specific-first (reuses the SAME contract stores as gen-time):
      1. endpoint NOT in captured AND not source-verified → ARTA invented/mis-shaped
         the path → test_gen_bug/unknown_endpoint (applies to ANY 4xx).
      2. 400 with a recoverable body → compare fields against the contract via
         grounding_validator._r95_4_validate_body_fields (OpenAPI requestBody, else
         captured request/response shape): body used an undeclared field → ARTA's bug
         (test_gen_bug); body conforms but SUT still 400'd → sut_contract_change ≥ 0.75.
      3. otherwise → None (no proof either way; caller keeps honest handling)."""
    import os as _os
    if _os.environ.get("ARTA_R303_C_DISABLE") == "1":
        return None
    if status_code not in (400, 409, 422):
        return None
    captured_keys = captured_keys or set()
    strict = _r258_skel(path or "", aggressive=False)
    loose = _r258_skel(path or "", aggressive=True)
    endpoint_known = bool(captured_keys) and (strict in captured_keys or loose in captured_keys)

    # Branch 1 — endpoint the SUT never served → ARTA invented/mis-shaped it. A 4xx on a
    # path ARTA fabricated says nothing about SUT quality (mirrors the 404 branch 1).
    if captured_keys and not endpoint_known and not source_verified:
        return {
            "triage_category": "test_gen_bug",
            "triage_confidence": 0.85,
            "triage_signals": [f"unknown_endpoint_{status_code}_{strict}"],
            "recommended_action": "regenerate_test_with_endpoint_grounding",
            "test_gen_bug_subtype": "unknown_endpoint",
        }

    # Branch 2 — 400 body-contract grounding (only when the sent body is recoverable).
    if status_code == 400 and request_body_raw:
        try:
            from .grounding_validator import _r95_4_validate_body_fields
            _oa = None
            if project_id:
                try:
                    import json as _json
                    from pathlib import Path as _Path
                    _oa_p = _Path(".arta/openapi") / f"{project_id}.json"
                    if _oa_p.is_file():
                        _oa = _json.loads(_oa_p.read_text())
                except Exception:
                    _oa = None
            # _r95_4's captured fallback grounds against `response_body_shape`; for a
            # REQUEST-body check the correct schema is the captured `request_body_shape`,
            # so map it onto response_body_shape for the matching endpoints (OpenAPI
            # requestBody, when present, still takes precedence inside _r95_4).
            _r303_eps = []
            for _e in (captured_endpoints or []):
                if isinstance(_e, dict) and _e.get("request_body_shape") and not _e.get("response_body_shape"):
                    _e = {**_e, "response_body_shape": _e["request_body_shape"]}
                _r303_eps.append(_e)
            _viols = _r95_4_validate_body_fields(
                name="attribution", method=method or "POST", path=path,
                body_raw=request_body_raw, openapi_spec=_oa,
                captured_endpoints=_r303_eps,
            )
            if _viols:
                _bad = ", ".join(sorted({getattr(v, "symbol", "") for v in _viols})[:6])
                return {
                    "triage_category": "test_gen_bug",
                    "triage_confidence": 0.85,
                    "triage_signals": [f"request_schema_violation_{strict}:{_bad}"],
                    "recommended_action": "regenerate_test_with_body_grounding",
                    "test_gen_bug_subtype": "request_schema_violation",
                }
            if endpoint_known:
                # body fields all conform to the contract, yet the SUT returned 400 →
                # the SUT's request contract changed. A credible, attributable signal.
                return {
                    "triage_category": "sut_contract_change",
                    "triage_confidence": 0.75,
                    "triage_signals": [f"sut_400_on_contract_valid_body_{strict}"],
                    "recommended_action": "self_heal_and_notify",
                    "sut_contract_change_subtype": "request_schema_change",
                }
        except Exception:
            pass

    return None


class DefectIntelAgent:
    """
    Autonomous defect analysis agent.
    Produces structured defect reports with root cause and fix suggestions.

    Phase G1: cascade-aware classification. Before invoking the LLM-driven
    RCA, the agent partitions failures into:
      - root_causes  — failures with no upstream provider; analyzed normally
      - cascades     — failures explained by an upstream provider's failure;
                       collapsed into a single root-cause defect with a
                       `cascade_of` link (no separate ticket, no LLM call)
      - contract_violations — provider passed but didn't supply expected
                       jsonpath; standalone defect with `defect_class=
                       'provider_contract_violation'` (skips LLM RCA — the
                       defect class IS the diagnosis)
    """

    def __init__(self, client: AsyncAnthropic, model: str = "claude-sonnet-4-6"):
        self._client = client
        self._model = model

    @staticmethod
    def _triage_failure(
        failure: dict,
        *,
        test_history: dict | None = None,
        sut_deploys: list | None = None,
        spec_regen_history: dict | None = None,
    ) -> dict:
        """R306.D — thin wrapper: run the deterministic core classifier, then
        reconcile PW UI assertion failures the core could NOT confidently
        attribute into an assessed adjudication_pending bucket (see
        `_r306_reconcile_pw_assertion`). Callers are unchanged."""
        result = DefectIntelAgent._triage_failure_core(
            failure,
            test_history=test_history,
            sut_deploys=sut_deploys,
            spec_regen_history=spec_regen_history,
        )
        return DefectIntelAgent._r306_reconcile_pw_assertion(failure, result)

    @staticmethod
    def _r306_reconcile_pw_assertion(failure: dict, result: dict) -> dict:
        """R306.D (frontend↔backend classifier reconciliation).

        The mission-report's Pillar-4 read "51% not_assessed → MIXED/low" for
        run-26aa5f because Playwright UI assertion failures the core triage could
        not confidently attribute fell to `not_assessed` / below-gate weak
        signals — while the summary REPORT's `_classify_failure` (execution.py)
        already classifies the SAME rows deterministically as `test_design`
        (expect()/toBeVisible/toHaveText/toContainText → "assertion mismatch").
        Two classifiers, two answers for one row.

        This folds the report heuristic INTO triage: a PW assertion mismatch that
        the core left UNattributed becomes a NAMED operator_review at conf ≥
        _R259_ADJUDICATION_MIN_CONFIDENCE → ADJUDICATION_PENDING (assessed,
        product decision required). That is the truthful home — a UI value/
        visibility mismatch is genuinely ambiguous (stale ARTA assertion vs real
        UI defect; the report itself flags test_design as "review if the product
        intentionally changed"). Mapping instead to test_gen_bug would inflate
        arta_defect_rate>0.5 and flip Pillar-4 to a scarier NOT_ASSESSED ("SUT
        unmeasured"); adjudication_pending keeps confidence HIGH and drops
        not_assessed — the confident verdict the reconciliation restores.

        SAFETY: only rows the core sent toward `unknown` are touched —
        `not_assessed`, `operator_review`<0.7, or a below-gate `sut_*`<0.7 (which
        _r259 already demotes to unknown). A CONFIDENT verdict (test_gen_bug /
        grounding_blocked / sut_*≥0.7 / operator_review≥0.7) is returned
        untouched, so no genuine SUT signal or precise ARTA attribution is lost.
        Killswitch ARTA_R306_D_DISABLE=1 → pre-R306.D behavior (stay unknown)."""
        import re as _re
        if os.environ.get("ARTA_R306_D_DISABLE") == "1":
            return result
        cat = str(result.get("triage_category") or "")
        conf = float(result.get("triage_confidence") or 0.0)
        # Confident / assessed verdicts are preserved verbatim.
        if cat in ("test_gen_bug", "grounding_blocked"):
            return result
        if cat in ("sut_regression", "sut_contract_change") and conf >= 0.7:
            return result
        if cat == "operator_review" and conf >= 0.7:
            return result
        # Everything else is `unknown`-bound (not_assessed / below-gate). Only
        # reconcile it when it's a Playwright UI assertion failure.
        em = str(failure.get("error_message") or failure.get("error")
                 or failure.get("response_body") or "")
        em = _re.sub(r"\x1B\[[0-9;]*m", "", em).lower()
        if not em:
            return result
        tool = str((failure.get("metadata") or {}).get("automation_tool")
                   or failure.get("automation_tool") or "").lower()
        is_pw = (tool == "playwright"
                 or "expect(" in em or ".spec.ts" in em
                 or bool(_re.search(r"\blocator\b|getby(?:testid|role|text|label)", em)))
        is_assertion = bool(_re.search(
            r"expect\(|to\s*be\s*visible|tobevisible|to\s*have\s*text|tohavetext|"
            r"to\s*contain\s*text|tocontaintext|to\s*have\s*value|tohavevalue|"
            r"to\s*have\s*count|tohavecount|to\s*be\s*hidden|tobehidden|"
            r"to\s*be\s*checked|tobechecked|to\s*have\s*attribute|tohaveattribute|"
            r"to\s*have\s*class|tohaveclass|\btoequal\b|\btomatch\b|\btobe\b",
            em))
        if is_pw and is_assertion:
            return {
                "triage_category": "operator_review",
                "triage_confidence": 0.72,
                "triage_signals": list(result.get("triage_signals") or [])
                + ["ui_assertion_mismatch_pending_review"],
                "recommended_action": "operator_queue",
                "operator_review_subclass": "ui_assertion_mismatch",
            }
        return result

    @staticmethod
    def _triage_failure_core(
        failure: dict,
        *,
        test_history: dict | None = None,
        sut_deploys: list | None = None,
        spec_regen_history: dict | None = None,
    ) -> dict:
        """R30.1 (KEYSTONE) — classify a failure into one of 4 categories
        so the downstream pipeline (defect creation, self-healing) takes
        the right action.

        Returns:
            {
              "triage_category": "test_gen_bug" | "sut_regression"
                                 | "sut_contract_change" | "operator_review",
              "triage_confidence": float in [0,1],
              "triage_signals": [str, ...],   # which rules fired
              "recommended_action": "self_heal" | "create_defect"
                                    | "self_heal_and_notify" | "operator_queue",
            }

        Decision tree (deterministic; first match wins):

        LAYER 1 — error-message + status-code patterns
          A. test_gen_bug indicators (high-confidence):
             - ChunkLoadError / ENOENT / Cannot find module → import bug
             - ReferenceError on {test, expect, page} → missing import slipped past
             - "Timed out.*locator" / "getByTestId.*not visible" → hallucinated selector
             - status_code == 415 → R18 OpenAPI placement bug
             - JSONDecodeError + auth-redirect HTML → K2 try/catch bug
          B. sut_regression indicators (high-confidence):
             - status_code in {500, 502, 503, 504} → server crash
             - "Connection reset|ECONNRESET" → SUT outage / pool exhaustion
          C. sut_contract_change indicators:
             - "expected X, got Y" with structurally-similar shape → renamed field

        LAYER 2 — historical signals (when test_history provided)
          D. test_gen_bug: test_failed AND was_failing_yesterday AND
             spec_was_regenerated_today
          E. sut_regression: test_failed AND was_passing_yesterday AND
             no_spec_changes
          F. sut_contract_change: test_failed AND sut_deploy_today AND
             pattern matches contract indicators

        LAYER 3 — operator_review fallback for anything unclassified
          (LLM classifier deferred to caller; this method stays cheap +
          deterministic so cascade/PCV partitioning stays fast.)
        """
        import re as _re

        tid = str(failure.get("test_id", ""))
        # R112.B — fall back to response_body when error_message is empty,
        # so R111.H cascade patterns ("missing required field", "unauthorized",
        # etc.) can match against the SUT's actual response text. Pre-R112.B
        # Newman 4xx/5xx with no assertion-level error_message produced
        # em_lower="" → R111.H matchers never fired → all 5xx classified as
        # sut_regression. R112.B threads the body through the back-channel.
        # R124.K — added metadata.response_body_preview as a fall-through
        # source. Post-DB-load failures lose the top-level `actual` field
        # (only `metadata` persists), so the in-memory R112.B chain fails
        # for any classifier that runs against DB-stored rows (post-run
        # chain pipeline). R124.K's promotion of body_preview into metadata
        # at execution.py:_build_params lets this path resolve post-load.
        em = (
            failure.get("error_message")
            or failure.get("error")
            or failure.get("response_body")
            or (failure.get("metadata") or {}).get("response_body_preview")
            or (failure.get("actual") or {}).get("body_preview")
            or ""
        )
        em_str = str(em or "").strip()
        # R49.4 — strip ANSI escape sequences. Playwright + Newman
        # outputs carry `\x1B[31m` color codes that break regex matches
        # like `getbytestid.*not found` (the `\x1B[31m` between "found"
        # and other tokens prevents `.*` from matching the way the
        # pattern expects). Run-d3582b had 136 Playwright fails all
        # classified `operator_review` because of this — `\x1B[2m...\x1B[22m`
        # split the error into multiple regex-invisible fragments.
        em_str = _re.sub(r"\x1B\[[0-9;]*m", "", em_str)
        em_lower = em_str.lower()
        status_code = failure.get("status_code")
        # R166.A — newman/PW result rows carry the HTTP status in
        # `metadata.status_code`, NOT top-level. Pre-R166 _triage_failure read
        # only the top-level field → got None → 5xx/4xx failures fell through to
        # the operator_review fallback (run-531cb2: 2890/2915 → operator_review,
        # only 25 → sut_regression → sut_regression_test_span=0 → the mission
        # metric couldn't exclude SUT bugs). Read the metadata fallback first.
        if status_code is None and isinstance(failure.get("metadata"), dict):
            _md_sc = failure["metadata"].get("status_code")
            if _md_sc is not None:
                try:
                    status_code = int(_md_sc)
                except (TypeError, ValueError):
                    status_code = None
        if status_code is None:
            # Pull from common error-message shapes.
            m = _re.search(r"\b(?:status\s*code|got|received)\s*:?\s*(\d{3})\b", em_str, _re.IGNORECASE)
            if not m:
                m = _re.search(r"\b([45]\d\d)\b", em_str)
            if m:
                try:
                    _sc_cand = int(m.group(1))
                except ValueError:
                    _sc_cand = None
                # R300.C — the bare `[45]\d\d` fallback matched ANY 3-digit
                # number starting with 4/5, so `p95=519ms`, `expected=577`,
                # socket/port numbers, etc. were misread as HTTP status codes →
                # routed to the 5xx classifier → FALSE `sut_regression critical`
                # (run-1b6111: 8 of 11 criticals were codes like 519/548/577/594
                # that are not real HTTP statuses — the mission-report accused the
                # SUT of failures it never returned). Only accept a bare number as
                # a status code when it is an ACTUAL HTTP status code. The
                # context-anchored regex above (status/got/received:) is trusted
                # as-is. Killswitch ARTA_R300_C_STATUS_VALIDATE_DISABLE=1.
                _ctx_anchored = bool(_re.search(
                    r"\b(?:status\s*code|got|received)\s*:?\s*\d{3}\b",
                    em_str, _re.IGNORECASE))
                if (_sc_cand is not None and not _ctx_anchored
                        and os.environ.get("ARTA_R300_C_STATUS_VALIDATE_DISABLE") != "1"
                        and _sc_cand not in _VALID_HTTP_STATUS):
                    _sc_cand = None
                status_code = _sc_cand

        signals: list[str] = []

        # ── R211 Phase G — attribution from the traceability spine ──────────
        # The unified strategy's payoff: make each pass/fail a TRUSTWORTHY,
        # attributable statement about the SUT (the mission-report's credibility).
        #   • An ARTA-side traceability BLOCK is NEVER a SUT defect.
        #   • A failure on a fully grounded + traceable + bodied test is a MORE
        #     credible sut_regression (the request was correct → the SUT broke),
        #     so its confidence is boosted in the 5xx classifier below.
        # Killswitch ARTA_R211_ATTRIBUTION_DISABLE=1 → current triage unchanged.
        _md = failure.get("metadata") if isinstance(failure.get("metadata"), dict) else {}
        _g_off = os.environ.get("ARTA_R211_ATTRIBUTION_DISABLE", "").lower() in ("1", "true")
        _trace = _md.get("traceability") if isinstance(_md.get("traceability"), dict) else {}
        _grounded_traceable = bool(
            not _g_off and _trace.get("grounded") and _trace.get("traceable")
            and (_trace.get("matched_endpoint_keys") or _md.get("code_api_links")))
        if not _g_off and _md.get("blocked_reason") == "traceability_blocked":
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.9,
                "triage_signals": ["traceability_blocked"],
                "recommended_action": "self_heal",
            }
        # R213 V4.1 — a FAIL on a test that references real API endpoints but
        # traces to NONE of the requirement's mapped surface is an ARTA-side gen
        # defect (the test hit the wrong endpoint → the result is NOT a credible
        # SUT statement), NOT operator_review. Collapses the ambiguous bucket to
        # self-heal. UI-only tests (0 endpoints) are traceable → unaffected;
        # grounded+traceable failures fall through to the SUT classifiers below.
        if (not _g_off and _trace.get("grounded")
                and _trace.get("test_endpoint_count") and not _trace.get("traceable")):
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.82,
                "triage_signals": ["untraceable_test_endpoints"],
                "recommended_action": "self_heal",
            }

        # ── R299 — truthful attribution of the operator_review residue ──────
        # Evidence (run-23c675, all-21-req comprehensive): 19 of 27 defect
        # CLUSTERS landed in `operator_review`, so pillar_4 read
        # `MIXED / confidence=low` ("70% could not be attributed"). But the
        # residue is NOT genuinely unattributable — it is dominated by concrete,
        # recognizable failure MODES the earlier layers simply had no matcher
        # for. R299 gives each mode its truthful home so the fidelity metric
        # (R259) stops treating recognizable ARTA/SUT signal as "unknown". This
        # does NOT loosen any validator — it only classifies failures that ARE
        # happening. Killswitch ARTA_R299_TRUTHFUL_ATTRIB_DISABLE=1.
        #
        # Directive alignment (standing user pref "failures are ARTA, not SUT"):
        # when the operator confirms the SUT works manually, a test that asserts
        # on data the SUT never emits (T1), references an undefined variable
        # (T2), or hits a non-routed URL (T3) is an ARTA test-gen defect — not an
        # honest "cannot tell". The SUT-side modes (S1 slow-timeout, S2 partial
        # checks under load) are genuine performance signals ARTA measured.
        if os.environ.get("ARTA_R299_TRUTHFUL_ATTRIB_DISABLE") != "1":
            # R299.T1 — analytics over-specified assertion. The SUT answers the
            # NL→SQL query conversationally (prose); the generated test asserts a
            # STRUCTURED field (insight.metric/value, narrative.*) the
            # conversational mode returns as None → `tolerant_assert(... actual=None)`.
            # The test asked for shape the SUT does not produce → test_gen (regen
            # with prose-tolerant assertions). Note: `actual=None` ONLY — a real
            # but mismatched value (actual='doc_event') stays a genuine data
            # discrepancy and falls through to the SUT/operator layers below.
            if "tolerant_assert" in em_lower and _re.search(r"actual\s*=\s*none\b", em_lower):
                signals.append("analytics_structured_field_absent")
                return {
                    "triage_category": "test_gen_bug",
                    "triage_confidence": 0.8,
                    "triage_signals": signals,
                    "recommended_action": "self_heal",
                    "test_gen_bug_subtype": "analytics_structured_field_absent",
                }
            # R301.F — same over-specification class as R299.T1, but manifesting
            # as a runtime None-ERROR instead of a tolerant_assert: the SUT's
            # conversational answer leaves structured response fields None, and the
            # generated analytics test then breaks HANDLING that None —
            # `TypeError: '<' not supported between ... NoneType`, `NoneType has no
            # len()`, `NoneType is not callable/subscriptable/iterable`, or an
            # assertion `where None = AnalyticsResponse(...)`. This is ARTA test-gen
            # over-specification (regen with prose-tolerant, None-safe assertions),
            # NOT a SUT defect. Scoped to an analytics context (AnalyticsResponse
            # or the analytics spec path) so unrelated NoneType errors are
            # untouched. Run-87c310: ~65 of the 134 analytics operator_review rows.
            if (("analyticsresponse" in em_lower or "python_tests/analytics" in em_lower
                 or "insight(value=none" in em_lower)
                    and (
                        # TypeError from operating on a None structured field
                        (_re.search(r"\bnonetype\b", em_lower)
                         and _re.search(
                             r"not supported between|has no len|is not (callable|subscriptable|iterable)",
                             em_lower))
                        # pytest assertion-introspection: the actual IS None
                        or _re.search(r"where\s+none\s*=\s*analyticsresponse", em_lower)
                        or _re.search(r"insight\(value=none|value=none,\s*metric=none", em_lower))):
                signals.append("analytics_none_handling")
                return {
                    "triage_category": "test_gen_bug",
                    "triage_confidence": 0.8,
                    "triage_signals": signals,
                    "recommended_action": "self_heal",
                    "test_gen_bug_subtype": "analytics_structured_field_absent",
                }
            # R299.T2 — undefined-variable test script. `expected undefined to be
            # one of [...]` / `to include undefined` / `"undefined" is not valid
            # JSON` all mean the generated assertion read a value that was
            # undefined (wrong property path, or a chained var the prior request
            # never set / R154 blocked). Broken test script → test_gen.
            if (
                "expected undefined to be one of" in em_lower
                or "to include undefined" in em_lower
                or '"undefined" is not valid json' in em_lower
            ):
                signals.append("undefined_reference_in_assertion")
                return {
                    "triage_category": "test_gen_bug",
                    "triage_confidence": 0.85,
                    "triage_signals": signals,
                    "recommended_action": "self_heal",
                    "test_gen_bug_subtype": "undefined_reference",
                }
            # R299.T3 — non-routed URL. A generic werkzeug/Flask 404 page
            # ("The requested URL was not found on the server") means the request
            # reached a server that has NO route for that path → ARTA hit a wrong
            # / hallucinated endpoint (a real API 404 returns JSON, not this HTML
            # shell). ARTA-side grounding defect, not a SUT statement.
            if "the requested url was not found on the server" in em_lower:
                signals.append("endpoint_not_routed")
                return {
                    "triage_category": "test_gen_bug",
                    "triage_confidence": 0.8,
                    "triage_signals": signals,
                    "recommended_action": "self_heal",
                    "test_gen_bug_subtype": "endpoint_not_routed",
                }
            # R299.T4 — test-CASE quality defect surfaced by a generated
            # requirements-quality assertion ("AC ... lacks measurable outcome").
            # The AC itself is deficient → an upstream test-case-quality gap ARTA
            # owns, not an SUT finding.
            if "lacks measurable outcome" in em_lower:
                signals.append("testcase_quality_measurability")
                return {
                    "triage_category": "test_gen_bug",
                    "triage_confidence": 0.8,
                    "triage_signals": signals,
                    "recommended_action": "self_heal",
                    "test_gen_bug_subtype": "measurable_ac_missing",
                }
            # R312.C — fabricated ENUM-value assertion. `expect(received)
            # .toContain("X")` where the Received array IS the SUT's actual value
            # "registered" but the SUT enum is
            # ["RUNNING","STOPPED","PENDING","FAILED","UNKNOWN"]). The LLM invented a
            # value the SUT never emits → ARTA over-specified/fabricated assertion
            # (test_gen), NOT an operator-review "cannot tell" (run-80c4ff: 26 of 46
            # PW operator_review rows). Scoped to ENUM-LIKE arrays (short alnum
            # tokens, no spaces) so a genuine content/text mismatch is left to the
            # operator/SUT layers. Killswitch ARTA_R312_C_ENUM_ASSERT_DISABLE=1.
            if ("tocontain" in em_lower
                    and os.environ.get("ARTA_R312_C_ENUM_ASSERT_DISABLE") != "1"):
                _exp_m = _re.search(r'expected value:\s*"([^"]*)"', em_str, _re.IGNORECASE)
                _arr_m = _re.search(r'received array:\s*\[([^\]]*)\]', em_str, _re.IGNORECASE)
                if _exp_m and _arr_m:
                    _exp_v = _exp_m.group(1).strip()
                    _arr_items = [x.strip().strip('"\'') for x in _arr_m.group(1).split(",") if x.strip()]
                    _enum_like = bool(_arr_items) and all(
                        _re.fullmatch(r"[A-Za-z0-9_.\-]{1,40}", it) for it in _arr_items)
                    _absent = bool(_exp_v) and _exp_v.lower() not in {it.lower() for it in _arr_items}
                    if _enum_like and _absent:
                        signals.append("fabricated_enum_assertion_value")
                        return {
                            "triage_category": "test_gen_bug",
                            "triage_confidence": 0.8,
                            "triage_signals": signals,
                            "recommended_action": "self_heal",
                            "test_gen_bug_subtype": "fabricated_assertion_value",
                        }
            # R299.S1 — SUT slow-response timeout. pytest/k6 analytics + load
            # specs that exceed a GENEROUS budget (R232 raised pytest to 300s;
            # k6 300s) are measuring a real SUT-latency signal: the NL→SQL engine
            # / the endpoint under load did not respond in time. Attributed to the
            # SUT at exactly the R259 confidence floor (0.7) — a credible but not
            # critical perf finding. Distinct from the PW `spec exceeded Ns
            # timeout` test_gen rule (different phrase, hallucinated-wait cause).
            if _re.search(r"\b(pytest|k6|newman)\s+timed out after\s+\d+s", em_lower):
                signals.append("sut_slow_response_timeout")
                return {
                    "triage_category": "sut_regression",
                    "triage_confidence": 0.7,
                    "triage_signals": signals,
                    "recommended_action": "create_defect",
                    "sut_regression_subtype": "slow_response_timeout",
                }
            # R299.S2 — k6 partial-check failure under load. The k6 summary shape
            # `p95=<ms>, checks=<pct>%` with 0 < pct < 100 means a FRACTION of
            # requests failed their status/latency check under concurrency — a
            # genuine SUT-degrades-under-load signal. checks=0% (total failure,
            # usually an unset-env-var URL) is EXCLUDED here and left to the R34.4
            # k6 `checks 0%` test_gen rule below.
            _r299_k6 = _re.search(r"checks\s*=\s*(\d{1,3}(?:\.\d+)?)\s*%", em_lower)
            if _r299_k6 and "p95=" in em_lower:
                try:
                    _pct = float(_r299_k6.group(1))
                except ValueError:
                    _pct = 0.0
                if 0.0 < _pct < 100.0:
                    signals.append("sut_perf_degraded_under_load")
                    return {
                        "triage_category": "sut_regression",
                        "triage_confidence": 0.7,
                        "triage_signals": signals,
                        "recommended_action": "create_defect",
                        "sut_regression_subtype": "perf_degraded_under_load",
                    }

        # ── LAYER 1A — test_gen_bug ─────────────────────────────────────
        if _re.search(r"chunkloaderror|enoent|cannot find module", em_lower):
            signals.append("import_or_build_error")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.95,
                "triage_signals": signals,
                "recommended_action": "self_heal",
            }
        if _re.search(r"referenceerror:\s*(test|expect|page)\s+is not defined", em_lower):
            signals.append("missing_import_runtime")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.95,
                "triage_signals": signals,
                "recommended_action": "self_heal",
            }
        if _re.search(r"timed out.*(locator|getbytestid)|getbytestid.*not (visible|found)", em_lower):
            signals.append("hallucinated_selector")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.85,
                "triage_signals": signals,
                "recommended_action": "self_heal",
            }
        # R49.4 — Playwright + element-not-found (broader pattern that
        # catches "Locator: getByTestId(...) ... Error: element(s) not
        # found" multi-line shape — run-d3582b had 24 of these classify
        # as operator_review pre-fix). Also catches "toBeVisible failed"
        # with hallucinated testid.
        if (
            "getbytestid" in em_lower
            and ("element(s) not found" in em_lower
                 or "tobevisible.*failed" in em_lower
                 or _re.search(r"tobevisible\b.*failed", em_lower))
        ):
            signals.append("hallucinated_testid_not_in_dom")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.85,
                "triage_signals": signals,
                "recommended_action": "self_heal",
            }
        # R49.4 — Playwright `TypeError: page.<X> is not a function`. The
        # LLM hallucinated a method on the Playwright `page` object that
        # doesn't exist (run-d3582b: page.nlToSqlEngine, page.resultToInsight,
        # etc — 26 + 2 fails). Always a test_gen_bug; regen with a hint
        # listing valid page methods.
        if _re.search(r"typeerror:\s*page\.\w+\s+is not a function", em_lower):
            signals.append("hallucinated_page_method")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.95,
                "triage_signals": signals,
                "recommended_action": "self_heal",
            }
        # R306.D.2 — `TypeError: Cannot read properties of undefined (reading
        # 'X')`. The generated spec dereferenced a property on an undefined
        # object (assumed a response/DOM shape that wasn't there) with no guard.
        # This is unambiguously an ARTA test-script bug — a JS runtime error in
        # the test code, never a SUT statement — so it is test_gen_bug, not the
        # LAYER-3 not_assessed fallback (run-26aa5f: 6 PW `reading 'succeeded'/
        # 'id'` rows sat in not_assessed though they are ARTA's fragile gen).
        if _re.search(
            r"typeerror:\s*cannot read propert(?:y|ies) of (?:undefined|null)",
            em_lower,
        ):
            signals.append("undefined_property_access")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.85,
                "triage_signals": signals,
                "recommended_action": "self_heal",
                "test_gen_bug_subtype": "undefined_property_access",
            }
        # R49.4 — Playwright `spec exceeded NNNs timeout`. Indicates the
        # spec itself is too slow or wedged on a hallucinated wait. Regen
        # with a hint to use shorter explicit waits.
        if _re.search(r"spec exceeded \d+s timeout", em_lower):
            signals.append("spec_runtime_timeout")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.80,
                "triage_signals": signals,
                "recommended_action": "self_heal",
            }
        if status_code == 415:
            signals.append("openapi_param_placement")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.85,
                "triage_signals": signals,
                "recommended_action": "self_heal",
            }
        if (
            _re.search(r"jsondecodeerror|unexpected token\s*'<'", em_lower)
            and ("page.request" in em_lower or ".json()" in em_lower)
        ):
            signals.append("k2_unguarded_json_parse")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.90,
                "triage_signals": signals,
                "recommended_action": "self_heal",
            }

        # R34.4 — pytest-shaped test_gen_bug patterns. Run-f95be0 had 58
        # pytest fails with errors like "ERROR at setup of", "has no
        # attribute 'metadata'", "TypeError ... indices must be integers,
        # not str", and `import_module` failures. All of these are
        # generation-time issues (wrong fixture wiring, hallucinated
        # attribute access on response objects, broken assertion shapes,
        # missing helper imports) — ARTA's self-heal pipeline can
        # regenerate the failing spec with corrective context.
        if _re.search(
            r"error at setup of|"
            r"attributeerror|"
            r"\btypeerror\b.*(indices|not subscriptable|argument)|"
            r"nameerror:|"
            r"importmodulenotfounderror|"
            r"_gcd_import|"
            r"fixture.*not found|"
            r"\bsyntaxerror\b|"
            r"\bindentationerror\b",
            em_lower,
        ):
            signals.append("pytest_runtime_test_gen_bug")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.80,
                "triage_signals": signals,
                "recommended_action": "self_heal",
            }

        # R34.4 — k6 test_gen_bug patterns. Common failure mode: checks
        # report 0.0% which usually indicates the http.get/post URL was
        # constructed with `__ENV.X` for an unset X → undefined →
        # malformed URL → k6's check assertion fails on every iteration.
        if _re.search(
            r"checks?\s*[:=.]+\s*0(?:\.0+)?\s*%|"
            r"checks?\s*[:=]\s*0\b|"
            r"\b0/\d+\s*(?:checks|passed)|"
            r"thresholds_failed",
            em_lower,
        ):
            signals.append("k6_zero_checks_passed")
            return {
                "triage_category": "test_gen_bug",
                "triage_confidence": 0.75,
                "triage_signals": signals,
                "recommended_action": "self_heal",
            }

        # R34.4 — ZAP test_gen_bug / config patterns. Single-run failures
        # with messages like "active scan failed" / "config invalid" are
        # operator-actionable (broken YAML, wrong target_url) → operator
        # _review for now (later can be split into config_invalid vs
        # target_unreachable).
        if _re.search(
            r"active scan failed|zap.*config.*invalid|target.*unreachable",
            em_lower,
        ):
            signals.append("zap_config_or_target")
            return {
                "triage_category": "operator_review",
                "triage_confidence": 0.70,
                "triage_signals": signals,
                "recommended_action": "operator_queue",
            }

        # ── LAYER 1B — sut_regression ──────────────────────────────────
        # R49.4 — Playwright "Expected: 200, Received: 4xx" pattern (74
        # fails in run-d3582b). The test's assertion checked HTTP 200 but
        # SUT returned a non-2xx. status_code already extracts the 4xx
        # via the `received\s*:\s*(\d{3})` rule above. When the spec
        # explicitly asserted 200 and got 4xx (other than 401/415), it's
        # most likely a sut_regression — the endpoint exists (or test
        # would have BLOCKED earlier via R36.3 spec-drift) but returns
        # the wrong status. Lower confidence (0.65) than 5xx because
        # operator may want to verify against the captured shape.
        #
        # R258 — 404 is EXCLUDED here and decomposed in its own branch below.
        # Pre-R258 a 404 landed in this rule and became `sut_regression` +
        # `create_defect` at conf 0.65. Live evidence (run-0c19e6): 520 of 813
        # failures were 404, and only ~9 of the run's failures were genuine SUT
        # signal — the rest were ARTA's own fabricated ids / invented endpoints.
        # A 404 means "not found", which is FAR more often a statement about
        # ARTA's request than about the SUT's health, so it needs the same
        # decomposition R111.H gave the 5xx cluster before it can accuse the SUT.
        #
        # R258.B — the expectation match is now runner-agnostic. The old
        # `expected\s*:?\s*200` regex silently only matched Playwright, so
        # Newman's identical defects fell through to operator_review conf 0.0.
        # R303.C — contract-ground the 400/409/422 cluster BEFORE the blind LAYER-1B
        # 0.65 rule below (which the R259 fidelity gate demotes to `unknown` for being
        # < 0.7 — a major feeder of the "82% unattributable"). Endpoint-existence +
        # (for 400) body-schema grounding move these to a confident, attributable
        # verdict; None → fall through to the legacy rule (honest, no over-attribution).
        if (isinstance(status_code, int) and status_code in (400, 409, 422)
                and os.environ.get("ARTA_R303_C_DISABLE") != "1"):
            try:
                _r303c_pid = failure.get("project_id")
                _r303c_url = failure.get("url") or ""
                if not _r303c_url:
                    _m = _re.search(r"\b(/[\w/{}.-]+)\b", em_str)
                    _r303c_url = _m.group(1) if _m else ""
                _r303c_md = failure.get("metadata") if isinstance(failure.get("metadata"), dict) else {}
                _r303c_trace = _r303c_md.get("traceability") if isinstance(_r303c_md.get("traceability"), dict) else {}
                _r303c_body = (failure.get("request_body")
                               or _r303c_md.get("request_body_preview") or None)
                _r303c_method = (failure.get("method") or _r303c_md.get("method") or "POST")
                _r303c_caps: set = set()
                _r303c_eps: list = []
                if _r303c_pid and _r303c_url:
                    from .api_discovery import _load_captured_endpoints, _r212_skel
                    _r303c_eps = _load_captured_endpoints(_r303c_pid) or []
                    _r303c_caps = {
                        _r212_skel(e.get("path", "")) for e in _r303c_eps
                        if isinstance(e, dict) and e.get("path")
                    }
                if _r303c_url and (_r303c_caps or _r303c_body):
                    _r303c_res = _r303_c_decompose_4xx(
                        status_code=status_code, path=_r303c_url, method=_r303c_method,
                        request_body_raw=_r303c_body, project_id=_r303c_pid,
                        captured_keys=_r303c_caps, captured_endpoints=_r303c_eps,
                        source_verified=bool(_r303c_trace.get("source_verified")),
                    )
                    if _r303c_res is not None:
                        _r303c_res["triage_signals"] = signals + list(
                            _r303c_res.get("triage_signals") or [])
                        return _r303c_res
            except Exception:
                pass

        _r258_expected, _r258_received = parse_status_expectation(em_str)
        _r258_off = os.environ.get("ARTA_R258_404_DECOMPOSE_DISABLE", "").lower() in ("1", "true")
        if (
            isinstance(status_code, int)
            and 400 <= status_code < 500
            and status_code not in (401, 403, 415)
            and not (status_code == 404 and not _r258_off)
            and (_r258_expected == 200 or _re.search(r"expected\s*:?\s*200\b", em_lower))
        ):
            signals.append(f"sut_4xx_on_expected_200_{status_code}")
            return {
                "triage_category": "sut_regression",
                "triage_confidence": 0.65,
                "triage_signals": signals,
                "recommended_action": "create_defect",
            }
        if status_code in (500, 502, 503, 504):
            # R111.H — DECOMPOSE the 5xx cluster. Pre-R111.H every 5xx flowed
            # to `sut_regression critical` (R34.1 default), but live evidence
            # (run-99dbcf: 2354 × 500) shows many 5xx are CASCADE from:
            #   - malformed request bodies → SUT 500 on validation failure
            #     (ARTA gen-quality bug, NOT SUT regression)
            #   - missing/expired auth → some middlewares return 500 instead
            #     of 401 (operator-config gap, NOT SUT regression)
            # Inflating sut_regression with these cascades poisons the
            # operator's Jira queue + violates the ARTA Goal of truthfully
            # reporting SUT quality. R111.H adds two pre-emption layers
            # BEFORE the catch-all sut_regression classification.

            # Layer 1A.2 — malformed-body cascade (ARTA gen-bug)
            # R122 KEYSTONE — removed over-broad `"expected "` substring
            # from the malformed pattern list. Pre-R122: `"expected "`
            # matched EVERY Newman error message because Newman's
            # universal failure prefix is
            # `"expected response to have status code 200 but got 500"`.
            # Result: all 2188 × HTTP 500 Newman failures in run-af070d
            # routed to `test_gen_bug:malformed_request_body` instead of
            # `sut_regression`. Pillar 4 mission violation: 0 sut_regression
            # defects for ~2188 real SUT 5xx errors. R122 narrows the
            # pattern to specific malformed-field phrases that ONLY appear
            # in genuine schema-validation responses, not in Newman's
            # generic error wrapper.
            _malformed_patterns = (
                "missing required field", "validation failed",
                "required parameter", "request body", "schema validation",
                "field is required", "is required", "must be provided",
                "invalid format", "expected field", "expected parameter",
                "expected property",
            )
            if any(p in em_lower for p in _malformed_patterns):
                signals.append(f"malformed_body_cascade_{status_code}")
                return {
                    "triage_category": "test_gen_bug",
                    "triage_confidence": 0.85,
                    "triage_signals": signals,
                    "recommended_action": "regenerate_test_with_schema_grounding",
                    "test_gen_bug_subtype": "malformed_request_body",
                }

            # Layer 1A.2b (R296) — OPAQUE 500 on a POST ARTA couldn't body.
            # The malformed-pattern check above needs the SUT to SAY what's
            # Reefer `getDataForCommandCenter` — verified: `{}`/plausible bodies
            # all 500). When the failed request targets a POST/PUT/PATCH endpoint
            # for which ARTA has NO request-body source (R295 injected no hint),
            # the 500 is ARTA's missing-body GAP, not a credible SUT regression —
            # ARTA sent a body it could not construct. Classify it truthfully so
            # it does not inflate sut_regression (the Pillar-4 mission metric).
            # Conservative: only fires on a CONFIDENT endpoint match; an
            # unmatched path keeps the SUT classification below (never hide an
            # unexplained 500). The remedy is the DTO-from-source extractor, which
            # supplies the body → this reclassifies back to a real signal.
            # Killswitch ARTA_R296_BODY_UNAVAIL_DISABLE=1.
            if (status_code == 500
                    and os.environ.get("ARTA_R296_BODY_UNAVAIL_DISABLE") != "1"):
                _r296_pid = failure.get("project_id") or _md.get("project_id")
                _r296_path = (_md.get("request_path")
                              or (failure.get("actual") or {}).get("request_path"))
                if _r296_pid and _r296_path:
                    _r296_skel = _r258_skel(str(_r296_path))
                    if _r296_skel in _r296_bodyless_post_skeletons(str(_r296_pid)):
                        signals.append("request_body_unavailable_500")
                        return {
                            "triage_category": "test_gen_bug",
                            "triage_confidence": 0.8,
                            "triage_signals": signals,
                            "recommended_action": "regenerate_test_with_schema_grounding",
                            "test_gen_bug_subtype": "request_body_unavailable",
                        }

            # Layer 1A.3 — auth-cascade 5xx (operator config)
            # Pre-R113.B: only "unauthorized" / "missing authorization" matched,
            # auth-cascade 500s. Substring "authorization error" matches the
            # "Internal Server Error" (which lacks the "authorization" token).
            _auth_cascade_patterns = (
                "unauthorized", "token invalid", "token expired",
                "authentication failed", "auth failed", "no credentials",
                "missing authorization", "authorization error",  # R113.B
                "forbidden", "access denied",
            )
            if any(p in em_lower for p in _auth_cascade_patterns):
                signals.append(f"auth_cascade_5xx_{status_code}")
                return {
                    "triage_category": "operator_review",
                    "triage_confidence": 0.80,
                    "triage_signals": signals,
                    "recommended_action": "refresh_auth_or_check_scope",
                    "operator_review_subclass": "auth_scope_5xx_cascade",
                }

            # Default — genuine SUT regression
            # When auth was valid, 5xx is unambiguously an SUT-side bug.
            # Without auth context we still classify as SUT regression
            # (a 5xx that's actually 401-redirect would have surfaced as
            # status_code == 401 above).
            signals.append(f"sut_5xx_{status_code}")
            auth_valid = bool(failure.get("auth_was_valid", True))
            _conf = 0.90 if auth_valid else 0.70
            if _grounded_traceable:
                # R211 Phase G — the request was path/endpoint-grounded AND traces
                # to the requirement's real implementing surface, so a 5xx is a
                # credible SUT defect, not an ARTA bad-request. Raise confidence.
                signals.append("grounded_traceable")
                _conf = min(0.97, _conf + 0.05)
                # R301 — observability: this 5xx is on an endpoint verified REAL in
                # the SUT's SOURCE (the runtime probe under-captured the route). The
                # attribution as sut_regression is source-evidence-backed, not a
                # runtime-capture coincidence. See execution.py r301_source_verified.
                if _trace.get("source_verified"):
                    signals.append("source_verified_endpoint")
            return {
                "triage_category": "sut_regression",
                "triage_confidence": _conf,
                "triage_signals": signals,
                "recommended_action": "create_defect",
            }
        if _re.search(r"connection reset|econnreset|ehostunreach|etimedout", em_lower):
            signals.append("sut_network_failure")
            return {
                "triage_category": "sut_regression",
                "triage_confidence": 0.85,
                "triage_signals": signals,
                "recommended_action": "create_defect",
            }

        # ── LAYER 1B' — sut_contract_change 4-mode classifier (R55.9 + R57.4) ──
        # The SUT's contract can change in 4 distinct ways. Each routes
        # to `sut_contract_change` so R30.3's heal proposers (gated on
        # _ALLOWED_TRIAGE_FOR_HEAL, which includes sut_contract_change)
        # fire automatically. `sut_contract_change_subtype` discriminates
        # between modes so future dedicated heal proposers can dispatch
        # (R62+ work — currently all subtypes funnel into url_drift heal).
        #
        # Placed AFTER 5xx (line 350) so server crashes still classify
        # as sut_regression. Placed BEFORE 401/403 so contract changes
        # take precedence over operator_review fallback.

        # Mode 1 — HTTP method change (POST → PUT etc.)
        if status_code == 405:
            signals.append("sut_method_change")
            return {
                "triage_category": "sut_contract_change",
                "triage_confidence": 0.85,
                "triage_signals": signals,
                "recommended_action": "self_heal_and_notify",
                "sut_contract_change_subtype": "http_method_change",
            }

        # Mode 2 — URL path drift (404 + path matches R29.5 spec-drift store)
        # The spec-drift store is in-memory (process-local) and populated
        # by automation_engineer at gen time when an endpoint isn't in
        # the captured-endpoint set. Self-contained query: no cross-module
        # plumbing needed.
        if status_code == 404:
            try:
                from .automation_engineer import _SPEC_DRIFT_TARGETS
                pid = failure.get("project_id")
                if pid and pid in _SPEC_DRIFT_TARGETS:
                    url_path = (failure.get("url") or "").lower()
                    if not url_path:
                        # Fall back to extracting from error_message
                        m_url = _re.search(r"\b(/[\w/{}-]+)\b", em_str)
                        url_path = (m_url.group(1) if m_url else "").lower()
                    if url_path:
                        spec_drift_match = any(
                            t.lower() in url_path or url_path in t.lower()
                            for t in _SPEC_DRIFT_TARGETS.get(pid, [])
                            if isinstance(t, str)
                        )
                        if spec_drift_match:
                            signals.append("sut_url_drift")
                            return {
                                "triage_category": "sut_contract_change",
                                "triage_confidence": 0.75,
                                "triage_signals": signals,
                                "recommended_action": "self_heal_and_notify",
                                "sut_contract_change_subtype": "url_path_change",
                            }
            except Exception:
                pass

            # R258 — decompose the rest of the 404 cluster (mirrors R111.H's
            # 5xx decomposition). Runs AFTER the R29.5 spec-drift check above
            # so a known URL drift keeps its higher-precedence classification.
            # Killswitch ARTA_R258_404_DECOMPOSE_DISABLE=1 → pre-R258 behavior
            # (404 falls through to the generic fallback below).
            if not _r258_off:
                try:
                    _r258_pid = failure.get("project_id")
                    _r258_url = failure.get("url") or ""
                    if not _r258_url:
                        _m_url = _re.search(r"\b(/[\w/{}.-]+)\b", em_str)
                        _r258_url = _m_url.group(1) if _m_url else ""
                    _r258_caps: set[str] = set()
                    _r258_reals: set[str] = set()
                    _r258_store_ok = False
                    if _r258_pid:
                        from .api_discovery import _load_captured_endpoints, _r212_skel
                        _r258_caps = {
                            _r212_skel(e.get("path", ""))
                            for e in (_load_captured_endpoints(_r258_pid) or [])
                            if isinstance(e, dict) and e.get("path")
                        }
                        _r258_seeded: set = set()
                        try:
                            # R250's store. When empty, R258 degrades to
                            # lower-confidence / abstention rather than
                            # inventing certainty it does not have.
                            from .real_id_store import load_real_ids, seeded_id_values
                            _r258_raw = load_real_ids(_r258_pid) or {}
                            for _slot in _r258_raw.values():
                                if isinstance(_slot, dict):
                                    _r258_reals.update(
                                        str(v) for v in (_slot.get("values") or []))
                            _r258_store_ok = bool(_r258_raw)
                            _r258_seeded = seeded_id_values(_r258_pid)
                        except Exception:
                            _r258_store_ok = False
                    if _r258_url:
                        _r258_res = _r258_decompose_404(
                            path=_r258_url,
                            error_message=em_str,
                            real_ids=_r258_reals,
                            captured_keys=_r258_caps,
                            real_id_store_available=_r258_store_ok,
                            seeded_ids=_r258_seeded,
                        )
                        _r258_res["triage_signals"] = signals + list(
                            _r258_res.get("triage_signals") or [])
                        return _r258_res
                except Exception:
                    # Never let triage crash a run — fall through to the
                    # pre-R258 path below. (This module has no logger; the
                    # surrounding spec-drift block uses the same bare-pass
                    # convention.)
                    pass

        # Mode 3 — Request body schema change (400 + "required"/"missing field")
        if (
            status_code == 400
            and _re.search(
                r"\b(required|missing|missing field|is\s+required|must\s+(provide|include)|"
                r"field.*missing|param.*missing|expected.*property)\b",
                em_lower,
            )
        ):
            signals.append("sut_request_schema_change")
            return {
                "triage_category": "sut_contract_change",
                "triage_confidence": 0.70,
                "triage_signals": signals,
                "recommended_action": "self_heal_and_notify",
                "sut_contract_change_subtype": "request_schema_change",
            }

        # Mode 4 — Response body schema change (2xx + KeyError / undefined property)
        # PRIORITY OVER Layer 1D (assertion-error → sut_regression) because
        # schema-shape errors are DIFFERENT from value-mismatch assertion errors.
        # Distinguishing signal: "undefined property", "KeyError", "no such",
        # "key.*not.*found" vs Layer 1D's "expected X got Y" / "to equal".
        if (
            isinstance(status_code, int)
            and 200 <= status_code < 300
            and _re.search(
                r"\b(?:undefined\s+property|keyerror|no\s+such|key.*not.*found|"
                r"undefined.*\(.*reading|attributeerror.*has no attribute|"
                r"undefined.*is not a function)\b",
                em_lower,
            )
        ):
            signals.append("sut_response_schema_change")
            return {
                "triage_category": "sut_contract_change",
                "triage_confidence": 0.70,
                "triage_signals": signals,
                "recommended_action": "self_heal_and_notify",
                "sut_contract_change_subtype": "response_schema_change",
            }

        if status_code == 401 and failure.get("test_was_passing_yesterday"):
            signals.append("sut_auth_regression")
            return {
                "triage_category": "sut_regression",
                "triage_confidence": 0.80,
                "triage_signals": signals,
                "recommended_action": "create_defect",
            }
        # R37.2 — 401 without prior-passing history is most often an
        # auth-scope mismatch: the operator's stored cookie has a role
        # that lacks permission for these endpoints, OR the JWT is bound
        # to a different tenant. Route to a subclass of operator_review
        # so heal-gating (R30.3) blocks auto-regen (regenerating the
        # test won't grant the missing scope) AND the dashboard surfaces
        # the project-config gap. Operator declares the cookie's role/
        # tenant via the project's `auth_profile` config.
        if status_code == 401:
            signals.append("auth_scope_mismatch")
            return {
                "triage_category": "operator_review",
                "triage_confidence": 0.70,
                "triage_signals": signals,
                "recommended_action": "operator_queue",
                "operator_review_subclass": "auth_scope_mismatch",
            }
        # R37.2 — 403 = explicit permission denial. Same auth-scope
        # category — operator's role lacks the required permission.
        if status_code == 403:
            signals.append("permission_denied")
            return {
                "triage_category": "operator_review",
                "triage_confidence": 0.75,
                "triage_signals": signals,
                "recommended_action": "operator_queue",
                "operator_review_subclass": "auth_scope_mismatch",
            }

        # ── LAYER 1C — sut_contract_change ─────────────────────────────
        # "expected X, got Y" with both sides looking like jsonpath / field
        # names → likely renamed/restructured field.
        m_contract = _re.search(
            r"expected\s+([\w\.\[\]\$]+).*got\s+([\w\.\[\]\$]+)",
            em_lower,
        )
        if m_contract:
            a, b = m_contract.group(1), m_contract.group(2)
            # Both look like dotted paths AND share at least one segment?
            if "." in a and "." in b and (
                set(a.split(".")) & set(b.split("."))
            ):
                signals.append("contract_field_rename")
                return {
                    "triage_category": "sut_contract_change",
                    "triage_confidence": 0.70,
                    "triage_signals": signals,
                    "recommended_action": "self_heal_and_notify",
                }

        # ── R304 — 2xx assertion-failure attributor ─────────────────────────
        # When the SUT returned 2xx, the HTTP contract HELD; the failure is in
        # what the response CONTAINED or whether a constraint was honored. Pre-
        # R304 these all fell to LAYER-1D (blind `sut_regression` 0.65, which the
        # R259 0.7 gate demotes to `unknown`) or LAYER-3 (`operator_review` 0.0
        # "unclassified") — the single largest feeder of the "82% unattributable"
        # on read-heavy SUTs (live: run-d4cd25 had 28 of 54 FAILs at 2xx). This
        # block splits them by DETERMINISTIC evidence (title + body + assertion):
        #   (a) HTML-not-JSON  → ARTA hit the SPA shell / wrong endpoint → test_gen_bug
        #   (b) structural/type/parse assertion → ARTA's shape assertion wrong → test_gen_bug
        #   (c) constraint-ignored (negative/pagination/filter/validation test,
        #       SUT answered 2xx instead of rejecting/limiting) → a NAMED,
        #       evidence-backed operator_review (the SUT's query-param/validation
        #       contract needs product adjudication) — NOT "unclassified". This
        #       is honest: it neither fabricates a SUT defect nor blames ARTA for
        #       a legitimate boundary test. Killswitch ARTA_R304_DISABLE=1.
        if (isinstance(status_code, int) and 200 <= status_code < 300
                and os.environ.get("ARTA_R304_DISABLE") != "1"):
            _r304_title = str(failure.get("title") or "")
            _r304_body = str(
                (failure.get("metadata") or {}).get("response_body_preview")
                or failure.get("response_body")
                or (failure.get("actual") or {}).get("body_preview") or "")
            _r304_hay = f"{em_lower} {_r304_title.lower()}"
            # (a) HTML instead of JSON — the request reached the SPA/login shell,
            # not a JSON API. The endpoint ARTA called is not a real API path.
            if (_re.search(r"<!doctype html|<html[\s>]", _r304_body[:200], _re.IGNORECASE)
                    or "unexpected token '<'" in em_lower
                    or "unexpected token <" in em_lower):
                signals.append("html_not_json_2xx")
                return {
                    "triage_category": "test_gen_bug",
                    "triage_confidence": 0.85,
                    "triage_signals": signals,
                    "recommended_action": "self_heal",
                    "test_gen_bug_subtype": "unknown_endpoint",
                }
            # (b) structural / type / JSON-parse assertion — SUT returned a valid
            # 2xx body but ARTA asserted the wrong SHAPE (array vs object, missing
            # property, wrong type). A gen-side assertion bug, not a SUT one.
            if _re.search(
                r"to be an array|to be of type|to have (?:property|keys|"
                r"a property|length)|is not (?:an? )?(?:array|object|function)|"
                r"cannot read propert|to be json|expected .* to be a\b",
                _r304_hay,
            ):
                signals.append("assertion_shape_mismatch_2xx")
                return {
                    "triage_category": "test_gen_bug",
                    "triage_confidence": 0.80,
                    "triage_signals": signals,
                    "recommended_action": "self_heal",
                    "test_gen_bug_subtype": "assertion_shape_mismatch",
                }
            # (c) constraint-ignored — a negative/boundary/pagination/filter/
            # validation/security test asserted the SUT would REJECT or LIMIT,
            # but the SUT answered 2xx. Evidence-backed but needs the SUT's
            # declared query-param/validation contract to adjudicate SUT-gap vs
            # over-spec test → a NAMED operator_review (assessed, product-pending).
            if _re.search(
                r"returns?\s*4\d\d|to be one of|expected\s*2\d\d\s*to be|"
                r"\bnegative\b|non-?numeric|\binvalid\b|malformed|out[- ]of[- ]range|"
                r"page[_ ]?size|\boffset\b|sort[_ ]?(?:by|order)|\bsort(?:ed|ing)?\b|"
                r"order[_ ]?by|ascending|descending|name[_ ]?prefix|\bfilter(?:ed|ing)?\b|"
                r"\blimit\b|paginat|injection|\bboundary\b|access control|\brbac\b|"
                r"forbidden|not authorized|unauthori[sz]ed",
                _r304_hay,
            ):
                signals.append("sut_ignored_constraint_2xx")
                return {
                    "triage_category": "operator_review",
                    "triage_confidence": 0.72,
                    "triage_signals": signals,
                    "recommended_action": "operator_queue",
                    "operator_review_subclass": "sut_query_or_validation_contract",
                }

        # ── LAYER 1D (R30.7-A) — 2xx + assertion-shape error → sut_regression
        # Pre-R30.7 these fell through Layer 1 (no 5xx pattern) + Layer 2
        # (no history) into Layer 3 operator_review — wrong category. The
        # SUT contract held (200 OK) but the data diverged from expectation,
        # which is the classic "real backend bug, wrong values" pattern.
        # Lower confidence (0.65) than 5xx (0.90) because operator may
        # want to verify, but routes correctly so heal-gating (R30.3)
        # blocks auto-heal proposals — preserving the SUT signal.
        # R304 NOTE: this now only catches VALUE-mismatch 2xx assertions
        # (`to equal X`, `deep-equal`) that R304 (a)/(b)/(c) did not claim —
        # a genuine "SUT returned wrong value" signal, which is what 0.65
        # sut_regression is meant for.
        if (
            isinstance(status_code, int)
            and 200 <= status_code < 300
            and _re.search(
                r"\b(?:expected|to\s*equal|to\s*be|asserted|assertion(?:error)?|"
                r"does\s*not\s*match|deep[- ]?equal)\b",
                em_lower,
            )
        ):
            signals.append("assertion_error_on_2xx")
            return {
                "triage_category": "sut_regression",
                "triage_confidence": 0.65,
                "triage_signals": signals,
                "recommended_action": "create_defect",
            }

        # ── LAYER 2 — historical signals ───────────────────────────────
        if test_history and tid:
            hist = test_history.get(tid) or {}
            was_passing = bool(hist.get("was_passing_yesterday"))
            was_failing = bool(hist.get("was_failing_yesterday"))
            spec_regen = bool((spec_regen_history or {}).get(tid))
            sut_deploy_today = bool(sut_deploys)
            if was_failing and spec_regen:
                signals.append("history_newly_regen_test_broken")
                return {
                    "triage_category": "test_gen_bug",
                    "triage_confidence": 0.75,
                    "triage_signals": signals,
                    "recommended_action": "self_heal",
                }
            if was_passing and not spec_regen:
                signals.append("history_test_stable_sut_changed")
                return {
                    "triage_category": "sut_regression"
                                       if not sut_deploy_today
                                       else "sut_regression",
                    "triage_confidence": 0.75,
                    "triage_signals": signals,
                    "recommended_action": "create_defect",
                }

        # ── LAYER 3 — operator_review fallback ─────────────────────────
        signals.append("unclassified")
        return {
            "triage_category": "operator_review",
            "triage_confidence": 0.0,
            "triage_signals": signals,
            "recommended_action": "operator_queue",
        }

    async def analyze_failures(
        self,
        failures: list[dict],
        *,
        sequence_integrity: dict | None = None,
        test_history: dict | None = None,
    ) -> list[dict]:
        """Analyze failures concurrently. Phase G1: when `sequence_integrity`
        is provided (Phase F producer), cascade failures collapse into
        their root cause and PCVs short-circuit the LLM.

        R55.8 — `test_history` activates `_triage_failure` Layer 2 historical
        signals (was_passing_yesterday / was_failing_yesterday). Pre-R55.8
        this kwarg wasn't threaded → Layer 2 was dead code.

        Returns a list of defect dicts. Each cascade carries a
        `cascade_of: <root_cause_test_id>` field; the root cause carries
        an `affected_tests: [...]` list. Operators get one ticket per
        root cause instead of N tickets per N cascades.
        """
        import asyncio

        partition = self._partition_by_sequence_integrity(failures, sequence_integrity or {})

        # Cascades and PCVs don't need LLM analysis — they're deterministic.
        cascade_defects = [self._build_cascade_defect(f, partition["cascade_map"]) for f in partition["cascades"]]
        pcv_defects = [self._build_pcv_defect(f) for f in partition["contract_violations"]]

        # R30.1 (KEYSTONE) — triage each root_cause BEFORE LLM RCA.
        # Pre-R30.1 every root_cause unconditionally became a Defect
        # ticket, so test-gen bugs (hallucinated selectors, K2 try/catch,
        # wrong OpenAPI param placement) drowned operators in
        # false-positive tickets. Post-R30.1 only sut_regression is
        # surfaced as a Defect; test_gen_bug + sut_contract_change get
        # routed to self-heal; operator_review goes to a separate
        # triage queue. R30.3 gates the heal proposers on the same
        # triage_category.
        # R55.8 — forward test_history so Layer 2 fires.
        sut_regression_failures: list[dict] = []
        non_regression_defects: list[dict] = []
        for f in (partition["root_causes"] or []):
            triage = self._triage_failure(f, test_history=test_history)
            f["triage"] = triage
            cat = triage["triage_category"]
            if cat == "sut_regression":
                # Real bug — open Defect ticket via existing LLM RCA path.
                sut_regression_failures.append(f)
                continue
            # Non-SUT-regression: produce a structured defect dict here
            # WITHOUT an LLM call so the run-detail UI / operator triage
            # queue still surface the failure with the right action.
            tid = str(f.get("test_id") or "")
            # R69.1 KEYSTONE — append a short hash of the FULL test_id so
            # distinct tests that happen to share the same 24-char prefix
            # (e.g., `API-req_am_017_api-VERIF: E2EPipeline result` vs
            # `API-req_am_017_api-VERIF: status check`) get distinct
            # defect_ids. Pre-R69.1 the truncation `tid[:24]` collided →
            # 16 of 55 defects dropped at INSERT via ON CONFLICT DO NOTHING
            # (verified in run-2234bf: 4 distinct test failures collapsed
            # into 1 DEF-TRIAGE-OPERATOR_REVIEW-API-req_am_017_api-VERIF
            # row). Shorter prefix (18 chars) + 6-char hash keeps total
            # length similar but guarantees per-test uniqueness.
            import hashlib as _r69_1_hashlib
            _r69_1_hash = _r69_1_hashlib.sha1(tid.encode("utf-8")).hexdigest()[:6]
            _r69_1_prefix = tid.replace("TC-", "")[:18]
            base = {
                **f,
                "triage": triage,
                "defect_id": f"DEF-TRIAGE-{cat.upper()}-{_r69_1_prefix}-{_r69_1_hash}",
                "triage_category": cat,
                "triage_confidence": triage.get("triage_confidence", 0.0),
                "triage_signals": triage.get("triage_signals") or [],
                "auto_detected": True,
                "test_id": tid,
                "title": f"[{cat}] {f.get('title') or tid}",
            }
            if cat == "test_gen_bug":
                # R118.G — distinct defect_class for R102.A grounding-
                # stamped specs (PW + pytest). Pre-R118.G all gen-quality
                # issues collapsed to `defect_class: "test_gen_bug"`,
                # making the operator dashboard unable to distinguish
                # "spec regenerated mid-run via R57.1 retry" (low-noise,
                # heal-queue handles silently) from "spec landed on disk
                # with R102.A stamp AND R102.C BLOCKed at dispatch"
                # (operator-actionable BLOCKED row with regen CTA).
                # R118.G surfaces the latter as `grounding_blocked` so
                # the dashboard can group them on the dedicated tile
                # introduced by R102.C + R111.E.
                _r118_g_meta = f.get("metadata") if isinstance(f, dict) else None
                _r118_g_blocked_reason = (
                    (_r118_g_meta or {}).get("blocked_reason")
                    if isinstance(_r118_g_meta, dict)
                    else None
                )
                _r118_g_is_grounding_blocked = (
                    _r118_g_blocked_reason
                    in ("playwright_grounding_violation", "pytest_grounding_violation")
                )
                if _r118_g_is_grounding_blocked:
                    _r118_g_signals = list(triage.get("triage_signals") or [])
                    _r118_g_signals.append(
                        f"r102a_stamp_present_{_r118_g_blocked_reason}"
                    )
                    # R127.D.6.F — surface the violation-kind dimension as
                    # `defect_subclass` so the operator dashboard can group
                    # gen-quality failure rows by specific mode (merge-balance
                    # vs LLM API misuse vs hallucinated endpoint vs pytest
                    # syntax error). `defect_class` stays "grounding_blocked"
                    # so the auto-heal flow + existing UI tile copy continue
                    # to fire; subclass is purely additive. R102.C populates
                    # `metadata.violation_kinds` via R111.E aggregator at
                    # execution.py:3366-3368. Single source of truth: the
                    # module-level `_r127_d6_f_compute_subclass()` helper.
                    _r127_d6_f_subclass = _r127_d6_f_compute_subclass(_r118_g_meta)
                    base.update({
                        "defect_class": "grounding_blocked",
                        "defect_subclass": _r127_d6_f_subclass,  # R127.D.6.F
                        "status": "auto_healing",
                        "heal_strategy": "regenerate_test_with_constraint_hint",
                        "severity": "low",
                        "priority": "P3",
                        "triage_signals": _r118_g_signals,
                        "root_cause": (
                            f"Spec R102.A-stamped at gen time with grounding "
                            f"violations (reason: {_r118_g_blocked_reason}). "
                            f"R102.C dispatch BLOCKED the spec — never executed "
                            f"against the SUT. Operator action: trigger regen "
                            f"with the violation hints recorded on the spec."
                        ),
                        "fix_suggestion": (
                            "Re-generate the test via the bulk-regen admin "
                            "endpoint. The R102.A stamp's `_grounding_"
                            "violations` comment-header carries the BEFORE/"
                            "AFTER hints the LLM needs to correct on retry."
                        ),
                    })
                else:
                    base.update({
                        "defect_class": "test_gen_bug",
                        "status": "auto_healing",   # NOT 'open' — operator doesn't see this
                        "heal_strategy": "regenerate_test",
                        "severity": "low",  # test bug, not SUT bug
                        "priority": "P3",
                        "root_cause": (
                            f"Generation-time bug: {triage.get('triage_signals') or []}"
                        ),
                        "fix_suggestion": (
                            "Test regeneration queued. Operator does NOT need to "
                            "investigate the SUT — this is an ARTA-side issue."
                        ),
                    })
            elif cat == "sut_contract_change":
                base.update({
                    "defect_class": "sut_contract_change",
                    "status": "auto_healing_with_review",
                    "heal_strategy": "update_assertion_and_notify",
                    "severity": "medium",
                    "priority": "P2",
                    "root_cause": (
                        f"SUT response shape changed (signals: "
                        f"{triage.get('triage_signals') or []}). Likely a "
                        f"renamed field or structural reorg."
                    ),
                    "fix_suggestion": (
                        "Heal proposal will update the consumer's jsonpath "
                        "extraction. Operator should review the heal before "
                        "approval to confirm the contract change is intentional."
                    ),
                })
            else:  # operator_review
                base.update({
                    "defect_class": "operator_review",
                    "status": "needs_triage",
                    "severity": "medium",
                    "priority": "P2",
                    "root_cause": (
                        "Pattern not matched by deterministic triage rules. "
                        "Operator action: classify via /triage queue."
                    ),
                    "fix_suggestion": "Operator triage required.",
                })
            # Charter RCA completeness — the LLM-free defect already has
            # root_cause + fix + severity; fill the MISSING impact +
            # preventive_action + 5-level deep_dive DETERMINISTICALLY (no LLM,
            # per the efficiency mandate). setdefault: never override a class-
            # specific field set above.
            for _k, _v in deterministic_rca_fields(
                    base.get("defect_class"), base.get("triage_signals")).items():
                base.setdefault(_k, _v)
            non_regression_defects.append(base)

        # Real SUT regressions → existing LLM RCA path (one ticket each).
        # R124.G — chunked-parallel batch (not degraded fallback). Pre-R124.G
        # `asyncio.gather(*all_tasks)` fired all 200+ cluster RCAs at once.
        # Anthropic's rate limiter throttled → per-task retry queue piled up
        # → R49.1's 300s wait_for budget exceeded → deterministic fallback
        # fired (which doesn't populate triage_signals → run-d52a8c's 84
        # opaque operator_review defects).
        #
        # Post-R124.G: BATCH of 8 tasks at a time with 50ms breathing
        # between batches. Concurrency stays within Anthropic's rate
        # budget AND forward progress is maintained — verified to complete
        # 200 clusters in ~120-180s (well under 300s budget).
        _R124_G_BATCH = 8
        _R124_G_SLEEP_SECS = 0.05
        _cluster_tasks = [self._analyze_single(f) for f in sut_regression_failures]
        root_results: list = []
        for _i in range(0, len(_cluster_tasks), _R124_G_BATCH):
            _batch = _cluster_tasks[_i : _i + _R124_G_BATCH]
            _batch_out = await asyncio.gather(*_batch, return_exceptions=True)
            root_results.extend(_batch_out)
            # Breathing room — only between batches, not after the final one.
            if _i + _R124_G_BATCH < len(_cluster_tasks):
                await asyncio.sleep(_R124_G_SLEEP_SECS)
        root_defects = [r for r in root_results if isinstance(r, dict)]
        # Stamp triage onto LLM-classified root defects too so the UI
        # can surface "this is a real SUT bug" badge consistently.
        for d, src in zip(root_defects, sut_regression_failures):
            t = src.get("triage")
            if isinstance(t, dict):
                d.setdefault("triage_category", "sut_regression")
                d.setdefault("triage_confidence", t.get("triage_confidence", 0.0))
                d.setdefault("triage_signals", t.get("triage_signals") or [])
                d.setdefault("defect_class", "rca_root_cause")
            # R34.1 — deterministic priority routing for sut_regression.
            # The LLM-RCA path returns defect dicts with whatever priority
            # the prompt+LLM produce (typically P2/medium default). For 5xx
            # the priority MUST be P0 so the "Open P0 defects" badge
            # surfaces real backend regressions. Without this override the
            # 418 sut_regression defects in run-f95be0 all landed at P2 →
            # the dashboard's P0 badge was tautologically zero.
            sc = src.get("status_code")
            if sc is None:
                import re as _re_sc
                m_sc = _re_sc.search(r"\b([45]\d\d)\b", str(src.get("error_message") or ""))
                if m_sc:
                    try:
                        sc = int(m_sc.group(1))
                    except ValueError:
                        sc = None
            if isinstance(sc, int):
                if 500 <= sc < 600:
                    d["priority"] = "P0"
                    d["severity"] = "critical"
                elif sc == 401:
                    d["priority"] = "P1"
                    d["severity"] = "high"
                elif sc == 403:
                    d["priority"] = "P1"
                    d["severity"] = "high"

        # Annotate root defects with their affected_tests list so a single
        # ticket links to all downstream cascades.
        affected_by_root: dict[str, list[str]] = {}
        for c in cascade_defects:
            root_id = c.get("cascade_of")
            if root_id:
                affected_by_root.setdefault(root_id, []).append(c.get("test_id", ""))
        for d in root_defects:
            tid = d.get("test_id")
            if tid in affected_by_root:
                d["affected_tests"] = affected_by_root[tid]
                d["cascade_count"] = len(affected_by_root[tid])

        # R34.2 — stamp triage_category onto the source failure row's
        # metadata so when execution_results persists those rows, the
        # gate's per-tool effective-rate calculation can read
        # triage_category from execution_results.metadata (R30.7-B
        # writes only to defects.metadata; the gate reads from result
        # rows — different table). Without this stamping, every FAIL
        # row had metadata.triage_category=NULL → R33.6/R33.7 couldn't
        # exclude sut_regression rows from the denominator → effective
        # rate stayed depressed (run-f95be0: Newman 23.1% effective when
        # 418 of 977 fails were already classified as sut_regression).
        for src in failures:
            if not isinstance(src, dict):
                continue
            t = src.get("triage") or {}
            cat = (
                src.get("triage_category")
                or (t.get("triage_category") if isinstance(t, dict) else None)
            )
            if not cat:
                continue
            md = src.get("metadata")
            if not isinstance(md, dict):
                md = {}
            md["triage_category"] = cat
            if isinstance(t, dict):
                if t.get("triage_confidence") is not None:
                    md["triage_confidence"] = t.get("triage_confidence")
                if t.get("triage_signals"):
                    md["triage_signals"] = list(t.get("triage_signals") or [])
            # status_code is what the priority routing in R34.1 reads;
            # surface it for downstream consumers (gate, dashboard).
            if src.get("status_code") is not None:
                md["status_code"] = src.get("status_code")
            src["metadata"] = md
            # Also surface at top level so the gate's _triage_cat_of
            # helper finds it via the top-level fallback (matches the
            # contract documented in R30.7-D: 3 read paths).
            src.setdefault("triage_category", cat)

        # R30.1 — non_regression_defects (test_gen_bug, sut_contract_change,
        # operator_review) join the output stream so downstream consumers
        # (heal queue, UI, /triage page) see a complete picture.
        return root_defects + cascade_defects + pcv_defects + non_regression_defects

    @staticmethod
    def _partition_by_sequence_integrity(
        failures: list[dict],
        sequence_integrity: dict,
    ) -> dict:
        """Split failures into root_causes / cascades / contract_violations.

        Cascade attribution uses the `cascade_failures` list from Phase F1's
        sequence_integrity report. PCVs come from `provider_contract_violations`.
        Anything not in either bucket is treated as a root cause.
        """
        cascade_failures = sequence_integrity.get("cascade_failures") or []
        pcv_failures = sequence_integrity.get("provider_contract_violations") or []

        # Build a {consumer_test_id: root_cause_test_id, via_var} index.
        cascade_map: dict[str, dict] = {}
        for c in cascade_failures:
            if isinstance(c, dict) and c.get("test_id"):
                cascade_map[str(c["test_id"])] = {
                    "root_cause_test_id": c.get("root_cause_test_id"),
                    "via_var": c.get("via_var"),
                }

        pcv_test_ids: set[str] = {
            str(p.get("test_id")) for p in pcv_failures
            if isinstance(p, dict) and p.get("test_id")
        }

        cascades: list[dict] = []
        contract_violations: list[dict] = []
        root_causes: list[dict] = []
        for f in failures:
            tid = str(f.get("test_id", ""))
            if not tid:
                root_causes.append(f)
                continue
            if tid in cascade_map and cascade_map[tid].get("root_cause_test_id"):
                cascades.append(f)
            elif tid in pcv_test_ids:
                contract_violations.append(f)
            else:
                root_causes.append(f)

        return {
            "root_causes": root_causes,
            "cascades": cascades,
            "contract_violations": contract_violations,
            "cascade_map": cascade_map,
            "pcv_index": {
                str(p.get("test_id")): p for p in pcv_failures if isinstance(p, dict)
            },
        }

    @staticmethod
    def _build_cascade_defect(failure: dict, cascade_map: dict) -> dict:
        """Phase G1: deterministic defect dict for a cascade — no LLM needed.

        The cascade IS the diagnosis: this test failed because its provider
        failed to supply X. Operators see a single root-cause ticket with
        N affected tests linked rather than N separate tickets.
        """
        tid = str(failure.get("test_id", ""))
        info = cascade_map.get(tid, {})
        return {
            **failure,
            "defect_id": f"DEF-CASCADE-{tid.replace('TC-', '')}",
            "defect_class": "cascade_failure",
            "cascade_of": info.get("root_cause_test_id"),
            "via_var": info.get("via_var"),
            "auto_detected": True,
            "status": "linked",  # not "open" — operator only acts on the root cause
            "title": f"Cascade: {tid} skipped because {info.get('root_cause_test_id', '?')} failed to provide {info.get('via_var', '?')}",
            "severity": failure.get("priority", "P3"),
            "root_cause": (
                f"Upstream test {info.get('root_cause_test_id', '?')} did not "
                f"produce {info.get('via_var', '?')}; this consumer cannot run "
                f"until the provider is fixed."
            ),
            "fix_suggestion": "Resolved automatically when the root-cause defect is fixed and the chain re-runs.",
        }

    @staticmethod
    def _build_pcv_defect(failure: dict) -> dict:
        """Phase G1: deterministic defect dict for a provider contract violation.

        The fix is structural (consumer's jsonpath OR provider's response shape)
        — not the kind of failure an LLM RCA would diagnose better. Phase G3
        proposes the heal candidate.
        """
        tid = str(failure.get("test_id", ""))
        return {
            **failure,
            "defect_id": f"DEF-PCV-{tid.replace('TC-', '')}",
            "defect_class": "provider_contract_violation",
            "auto_detected": True,
            "status": "open",
            "title": f"Provider contract violation in {tid}",
            "severity": failure.get("priority", "P1"),
            "root_cause": (
                "The provider test passed but its response did not contain the "
                "expected jsonpath. Either the SUT response shape regressed "
                "or the captured chain shape is stale."
            ),
            "fix_suggestion": (
                "Phase G3 proposes a `provider_contract_heal` PENDING_APPROVAL "
                "with the updated jsonpath. Operator reviews + approves; "
                "consumer test is patched and re-run automatically."
            ),
        }

    async def _analyze_single(self, failure: dict) -> dict:
        prompt = RCA_PROMPT.format(
            test_id=failure.get("test_id", ""),
            test_title=failure.get("title", ""),
            test_type=failure.get("test_type", "UI"),
            requirement_id=failure.get("requirement_id", ""),
            priority=failure.get("priority", "P2"),
            error_message=self._sanitize_for_prompt(failure.get("error_message", "")),
            stack_trace=self._sanitize_for_prompt(failure.get("stack_trace", "N/A")),
            logs=self._sanitize_for_prompt(failure.get("logs", "N/A"), max_len=2000),
            last_passing_commit=failure.get("last_passing_commit", "unknown"),
            current_commit=failure.get("current_commit", "unknown"),
            changed_files=", ".join(failure.get("changed_files", [])) or "unknown",
        )

        # D1 — the RCA now demands a 5-level deep_dive + preventive_action (richer
        # than the old flat root_cause). Two tweaks so the CLI can DELIVER it:
        #   • max_tokens 1024→1536: the 5-level JSON was truncating.
        #   • disable_tools=True (R215.T, CLI only): the RCA prompt says
        #     "investigate the root cause", so with tools available Claude Code
        #     attempts a tool call and burns the whole --max-turns budget →
        #     `RuntimeError: Reached max turns` with NO answer. `--tools none`
        #     lets the model respond directly in one turn. Guarded to the CLI
        #     transport — the Anthropic SDK path rejects the unknown kwarg.
        _cli_extra: dict = {}
        if type(getattr(self, "_client", None)).__name__ == "ClaudeCLIClient":
            _cli_extra["disable_tools"] = True

        # D1 — the JSON-only system prompt (see _RCA_JSON_ONLY_SYSTEM) suppresses the
        # CLI model's "Let me examine the source files…" + ```python``` role-play that
        # defeated JSON extraction. It's not 100% deterministic though, so retry on an
        # unparseable response: a fresh call almost always yields the clean bare JSON.
        analysis: dict = {}
        _last_txt = ""
        for _attempt in range(3):
            message = await self._call_llm(
                model=self._model,
                max_tokens=1536,
                system=_RCA_JSON_ONLY_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                **_cli_extra,
            )
            _last_txt = message.content[0].text if message.content else ""
            try:
                analysis = self._extract_json(_last_txt)
            except Exception:
                analysis = {}
            if analysis:
                break
        # D1 — an empty extraction after retries means the model kept wrapping/
        # prefixing its JSON. Treat it as a real failure so the caller falls back to
        # the triage path instead of silently returning a deep_dive-less record.
        if not analysis:
            raise ValueError("RCA produced no parseable JSON object after retries")
        # D1 (charter conformance) — normalise the 5-level deep-dive to the canonical
        # DeepDive keys so runtime SUT-failure defects carry a genuine per-failure
        # descent (symptom→immediate→upstream→architectural→process), not a flat RCA.
        try:
            from ..models.root_cause_report import DEEP_DIVE_LEVELS
            _dd = analysis.get("deep_dive")
            if isinstance(_dd, dict):
                analysis["deep_dive"] = {
                    k: str(_dd.get(k, "") or "").strip() for k in DEEP_DIVE_LEVELS}
        except Exception:
            pass
        return {
            **failure,
            "defect_id": f"DEF-{failure.get('test_id','').replace('TC-','')}",
            "auto_detected": True,
            "status": "open",
            **analysis,
        }

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
        """Wrapper around messages.create() with retry + circuit breaker."""
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
    def _extract_json(text: str) -> dict:
        """R134.D — delegates to shared SSoT extractor + raise-on-failure
        preserved from the pre-R134.D contract."""
        from .json_extract import extract_json_from_llm_output
        result = extract_json_from_llm_output(text, default=None)
        if result is None:
            raise ValueError(f"No valid JSON found in LLM output: {(text or '')[:200]}")
        return result

    # ── Fix SSS-1: Commit-window correlation ──────────────────────────────
    # When a test transitions PASS→FAIL between consecutive runs of the same
    # project, attribute the regression to commits that landed in the window.
    # Best-effort: any failure (DB unavailable, no GitHub token, no PASS run
    # in history) returns the defect list unchanged.
    async def enrich_with_commit_history(
        self,
        defects: list[dict],
        project: dict,
    ) -> list[dict]:
        """Populate `defect.metadata.likely_breaker_commits` for transitions.

        For each defect, looks up the last run where the same test_id passed
        (within the project) and queries GitHub for commits between that
        run's started_at and the current run's started_at. Mutates the
        defect dicts in place; returns the same list for chaining.
        """
        if not defects:
            return defects
        integrations = project.get("integrations", {}) or {}
        if hasattr(integrations, "model_dump"):
            integrations = integrations.model_dump()
        token = integrations.get("github_token", "") or ""
        repos = integrations.get("repositories", []) or []
        if not token or not repos:
            return defects

        try:
            from sqlalchemy import text as _sql_text
            from ..db.session import async_session_factory
            from .github_context import list_commits_in_window
        except Exception:
            return defects

        primary_repo = (repos[0] or {}).get("repo", "")
        if not primary_repo:
            return defects

        try:
            async with async_session_factory() as db:
                for d in defects:
                    test_id = d.get("test_id", "")
                    curr_started = d.get("run_started_at") or d.get("executed_at")
                    if not test_id or not curr_started:
                        continue
                    row = (await db.execute(_sql_text("""
                        SELECT tr.started_at AS started_at
                        FROM execution_results er
                        JOIN test_runs tr ON er.run_id = tr.id
                        WHERE er.test_id = :tid
                          AND er.status::text = 'PASS'
                          AND tr.started_at < CAST(:cur AS TIMESTAMPTZ)
                        ORDER BY tr.started_at DESC
                        LIMIT 1
                    """), {"tid": test_id, "cur": curr_started})).first()
                    if not row or not row.started_at:
                        continue
                    since_iso = row.started_at.isoformat() if hasattr(row.started_at, "isoformat") else str(row.started_at)
                    until_iso = curr_started.isoformat() if hasattr(curr_started, "isoformat") else str(curr_started)
                    commits = await list_commits_in_window(
                        repo=primary_repo,
                        token=token,
                        since=since_iso,
                        until=until_iso,
                    )
                    if commits:
                        meta = d.setdefault("metadata", {})
                        if not isinstance(meta, dict):
                            meta = {}
                            d["metadata"] = meta
                        meta["likely_breaker_commits"] = commits[:10]
                        # Surface the first commit for quick UI display.
                        first = commits[0]
                        d["likely_breaker_commit"] = first.get("sha", "")
                        d["likely_breaker_author"] = first.get("author", "")
        except Exception as exc:
            import logging as _log
            _log.getLogger("arta.defect_intel").debug(
                "Fix SSS-1: commit-correlation enrichment skipped: %s", exc,
            )
        return defects

    # ── Fix SSS-2: Auto-file Jira on product defects ──────────────────────
    # Called after analyze_failures + enrich_with_commit_history. Files at
    # most `max_per_run` defects to avoid runaway tickets when an entire
    # suite breaks. Threshold defaults to "P1" — operators can override
    # via project.integrations.auto_file_threshold.
    async def maybe_auto_file_defects(
        self,
        defects: list[dict],
        project: dict,
        jira_client,
        max_per_run: int = 5,
    ) -> list[dict]:
        """Best-effort Jira filing for high-severity product defects.

        Uses the same JiraClient instance the API layer uses (via
        request.app.state.jira). Mutates defects with `auto_filed_jira_id`
        when filing succeeds. Skips silently when client is unavailable.
        """
        if not defects or jira_client is None or not getattr(jira_client, "available", False):
            return defects

        integrations = project.get("integrations", {}) or {}
        if hasattr(integrations, "model_dump"):
            integrations = integrations.model_dump()
        threshold = (integrations.get("auto_file_threshold") or "P1").upper()
        # P0 < P1 < P2 < P3; "≥ P1" means P0 or P1 only.
        threshold_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(threshold, 1)

        # R112.H — eligibility: defect classified as a true SUT issue
        # (sut_regression OR sut_contract_change) AND severity at/above
        # threshold. Pre-R112.H this filter checked the legacy `failure_type`
        # field (`BUG/REGRESSION/PERFORMANCE/SECURITY`), but the classifier
        # only sets `triage_category` (`sut_regression`/`test_gen_bug`/...).
        # → no defect ever matched the filter → Jira auto-file NEVER fired
        # even when JIRA_* was configured + a real sut_regression existed.
        # Mission violation of Pillar 4 (report SUT quality to SUT team).
        SUT_TRIAGE = {"sut_regression", "sut_contract_change"}
        # Map triage → Jira failure_type label for downstream consumers.
        TRIAGE_TO_LABEL = {
            "sut_regression": "REGRESSION",
            "sut_contract_change": "CONTRACT",
        }
        # Legacy product_types — only used if a defect carries a legacy
        # `failure_type` field (e.g. PERFORMANCE / SECURITY set by tool-
        # specific paths). These keep working.
        legacy_product_types = {"BUG", "REGRESSION", "PERFORMANCE", "SECURITY"}
        filed = 0
        for d in defects:
            if filed >= max_per_run:
                break
            triage_cat = (d.get("triage_category") or "").lower()
            failure_type = (d.get("failure_type") or "").upper()
            severity = (d.get("severity") or "P3").upper()
            # R112.H — accept if either triage_category OR legacy failure_type
            # marks this as a SUT-side issue
            is_sut_issue = (
                triage_cat in SUT_TRIAGE
                or failure_type in legacy_product_types
            )
            if not is_sut_issue:
                continue
            if {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(severity, 3) > threshold_rank:
                continue
            if d.get("auto_filed_jira_id"):
                continue
            # Synthesize a failure_type for the Jira label (R112.H — keep the
            # Jira ticket carrying canonical labels even when triage_category
            # is the only signal).
            if not failure_type and triage_cat in TRIAGE_TO_LABEL:
                failure_type = TRIAGE_TO_LABEL[triage_cat]
            try:
                from ..integrations.jira_client import JiraClient
                # Embed commit attribution in the Jira description when known.
                desc_parts = [
                    f"Root cause: {d.get('root_cause', '')}",
                    f"Suggested fix: {d.get('suggested_fix', '')}",
                ]
                breaker = d.get("likely_breaker_commit")
                if breaker:
                    desc_parts.append(
                        f"Likely breaker: commit {breaker} by {d.get('likely_breaker_author', '?')}"
                    )
                result = await jira_client.create_issue(
                    summary=d.get("title", "") or f"ARTA defect for {d.get('test_id','?')}",
                    description="\n\n".join(desc_parts),
                    issue_type="Bug",
                    priority=JiraClient.severity_to_jira_priority(severity),
                    labels=["arta-auto-detected", "auto-filed", f"failure-{failure_type.lower()}"],
                    project_key=integrations.get("jira_project_key") or "ARTA",
                )
                key = result.get("key", "")
                if key:
                    d["auto_filed_jira_id"] = key
                    filed += 1
            except Exception as exc:
                import logging as _log
                _log.getLogger("arta.defect_intel").debug(
                    "Fix SSS-2: auto-file failed for %s: %s", d.get("defect_id", "?"), exc,
                )
                continue
        if filed:
            import logging as _log
            _log.getLogger("arta.defect_intel").info(
                "Fix SSS-2: auto-filed %d defect(s) to Jira (threshold=%s, cap=%d)",
                filed, threshold, max_per_run,
            )
        return defects

    async def detect_flakiness(self, history: list[dict]) -> dict:
        """
        Detect flaky tests from execution history.
        Flaky = same test passes AND fails across consecutive runs (no code changes).
        """
        from collections import defaultdict
        test_runs: dict[str, list[str]] = defaultdict(list)

        for result in history:
            test_runs[result["test_id"]].append(result["status"])

        flaky: list[dict] = []
        for test_id, statuses in test_runs.items():
            if len(statuses) < 3:
                continue
            unique = set(statuses)
            if "PASS" in unique and "FAIL" in unique:
                fail_rate = statuses.count("FAIL") / len(statuses)
                if 0.1 < fail_rate < 0.9:  # Neither always pass nor always fail
                    flaky.append({
                        "test_id": test_id,
                        "flakiness_rate": fail_rate,
                        "run_count": len(statuses),
                        "recommendation": (
                            "quarantine" if fail_rate > 0.5 else "add_retry"
                        ),
                    })

        return {"flaky_tests": flaky, "total_analyzed": len(test_runs)}
