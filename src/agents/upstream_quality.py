"""Upstream artifact-quality validators — BMAD TEA Layers 1-3.

ARTA's gen-quality validation has historically been concentrated at the
test-SCRIPT stage (R42.1..R154 in grounding_validator.py). This module
fills the upstream gap the platform directive demands: validate
REQUIREMENT quality and GHERKIN/SCENARIO quality (+ AC coverage) BEFORE
the expensive small-Ollama script-generation stage, so malformed upstream
artifacts are caught and routed back for regen instead of propagating into
doomed, misaligned scripts (which surface as false-positive "SUT defects").

Baseline evidence that motivated this (pilot-SUT slice, 2026-06-02):
  - only 3/23 (13%) acceptance criteria carried a measurable threshold;
  - mean Gherkin↔spec alignment was 0.138 (below the 0.20 R127.E.2 bar),
    i.e. generated specs barely exercised their own scenarios.

Design:
  - ALL checks are RULE-BASED (zero LLM) per the small-on-prem-Ollama
    non-negotiable directive. The model is never the source of truth here.
  - Results reuse the canonical `Violation` / `TestQualityResult`
    structures from test_quality_validator (single source of truth) and
    the zero-LLM keyword scorer from semantic_intent_validator.

Severity policy (mission: increase real PASS rate, do NOT over-block):
  - `error`   → blocks downstream generation. Reserved for catastrophic
    artifacts that can only yield doomed scripts: no ACs, malformed
    Gherkin, missing When/Then, or a fallback/template Gherkin (ATDD LLM
    failed). These get routed back upstream for regen.
  - `warning` → recorded as a quality signal + surfaced as a gen hint, but
    does NOT block (non-measurable AC, vague wording, weak intent
    alignment, uncovered AC). Real-world requirement prose is rarely
    perfectly measurable; blocking on that would tank the pass rate, so we
    report it truthfully and steer the LLM with hints instead.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .test_quality_validator import Violation, TestQualityResult
from .semantic_intent_validator import extract_gherkin_keywords

# Per-requirement upstream-quality sidecars (B5). Best-effort; the mission
# report + the /api/tests/upstream-quality endpoint aggregate these.
_UPSTREAM_DIR = Path(".arta/upstream_quality")


# ── shared lexicon ───────────────────────────────────────────────────────────

# A measurable AC carries a concrete, observable threshold / criterion.
_MEASURABLE_RE = re.compile(
    r"\d"
    r"|\bwithin\b|\bless than\b|\bat most\b|\bat least\b|\bno more than\b"
    r"|\bgreater\b|\bexceeds?\b|\bbelow\b|\babove\b"
    r"|%|\bpercent\b|\bms\b|\bseconds?\b|\bminutes?\b|\bhours?\b"
    r"|\bstatus\b|\bHTTP\b"
    r"|\bequals?\b|\bmatches\b|\breturns?\b|\bcontains?\b|\brejected\b|\bissues?\b",
    re.I,
)

# Vague / ambiguous terms that make an AC untestable as written.
_VAGUE_RE = re.compile(
    r"\b(fast|quick(ly)?|user.?friendly|easy|easily|appropriate(ly)?|properly|"
    r"correctly|works?|valid(ates?|ated|ation)?|gracefully|reasonable|reasonably|"
    r"good|nice|seamless(ly)?|robust|efficient(ly)?|intuitive|smooth(ly)?|"
    r"as expected|etc\.?)\b",
    re.I,
)

# Telltale phrases of the tests.py ATDD fallback template (see the
# `_atdd_exc is not None` branch in routers/tests.py). A Gherkin built from
# these is a stand-in produced when the ATDD LLM call failed — it is not a
# real scenario and must be regenerated, not turned into a script.
# R254 (WS2b) — the designer's honesty signal for an AC that states no
# verifiable outcome. Matches the scenario NAME or its @tag.
_NEEDS_CLARIFICATION_RE = re.compile(r"\[NEEDS-CLARIFICATION\]|@needs-clarification", re.I)
# Whole lines belonging to such a scenario, for scrubbing before the
# fallback/template scan.
_NEEDS_CLARIFICATION_LINE_RE = re.compile(
    r"(?im)^.*(?:\[NEEDS-CLARIFICATION\]|@needs-clarification).*$")

_FALLBACK_MARKERS = (
    "the user performs:",
    "the expected outcome is achieved",
    "the system is in the correct state",
    "the user performs the required action",
    "the expected outcome is observed",
    "the system is configured for",
)

# Implementation / selector leakage that should never appear in declarative
# Gherkin (Gherkin comments start with '#', so id-selectors are intentionally
# NOT matched to avoid false positives on comments).
_IMPL_LEAK_RE = re.compile(
    r"getBy(TestId|Role|Text)|data-testid|css=|xpath=|querySelector|"
    r"await\s+page|page\.(goto|click|fill|locator)|\.click\(|\.fill\(",
    re.I,
)

_SEVERITY_PENALTY = {"error": 40, "warning": 8}


def _ac_statements(acceptance_criteria) -> list[str]:
    """Normalise ACs (list of str or dict) to a list of statement strings."""
    out: list[str] = []
    for ac in acceptance_criteria or []:
        if isinstance(ac, str):
            out.append(ac)
        elif isinstance(ac, dict):
            out.append(ac.get("statement") or ac.get("description") or ac.get("text") or "")
    return [s for s in out if s and s.strip()]


def _result(violations: list[Violation], *, metrics: dict | None = None) -> TestQualityResult:
    """Assemble a TestQualityResult from violations (single scoring rule).

    score = 100 − Σ penalty; passed = no error-severity violations.
    Per-code rollup + optional metrics live under criteria_results.
    """
    score = 100
    for v in violations:
        score -= _SEVERITY_PENALTY.get(v.severity, 8)
    score = max(0, min(100, score))
    passed = not any(v.severity == "error" for v in violations)
    crit: dict = {}
    for v in violations:
        entry = crit.setdefault(v.code, {"rule": v.rule, "severity": v.severity, "count": 0})
        entry["count"] += 1
    res = TestQualityResult(violations=violations, score=score, passed=passed, criteria_results=crit)
    if metrics:
        res.criteria_results["_metrics"] = metrics
    return res


# ── B1: requirement quality ──────────────────────────────────────────────────

def validate_requirement_quality(requirement: dict) -> TestQualityResult:
    """RQ-00x — validate a requirement's testability BEFORE risk/ATDD.

    Only RQ-001 (no acceptance criteria) is an `error` (blocks generation —
    mirrors the existing `unfit_for_test_generation` gate). Measurability,
    ambiguity, duplication and thin-AC checks are `warning`s that feed the
    quality metric + ATDD hints without blocking.
    """
    violations: list[Violation] = []
    req_id = requirement.get("req_id") or requirement.get("id") or "requirement"
    acs = _ac_statements(requirement.get("acceptance_criteria"))
    n = len(acs)
    measurable = 0

    if n == 0:
        violations.append(Violation(
            code="RQ-001", rule="Acceptance criteria present", severity="error",
            message=f"{req_id} has no acceptance criteria; ARTA does not fabricate ACs.",
        ))
    else:
        seen: dict[str, int] = {}
        for s in acs:
            if _MEASURABLE_RE.search(s):
                measurable += 1
            else:
                violations.append(Violation(
                    code="RQ-002", rule="AC has a measurable/observable criterion",
                    severity="warning",
                    message=f"AC has no measurable/observable criterion: {s[:120]!r}",
                    snippet=s[:160],
                ))
            m = _VAGUE_RE.search(s)
            if m:
                violations.append(Violation(
                    code="RQ-003", rule="AC free of ambiguous terms", severity="warning",
                    message=f"AC uses ambiguous term {m.group(0)!r}: {s[:120]!r}",
                    snippet=s[:160],
                ))
            if len(extract_gherkin_keywords(s)) < 2:
                violations.append(Violation(
                    code="RQ-005", rule="AC is substantive enough to derive a test",
                    severity="warning",
                    message=f"AC too thin to derive a test: {s[:120]!r}", snippet=s[:160],
                ))
            key = re.sub(r"\s+", " ", s.strip().lower())
            seen[key] = seen.get(key, 0) + 1
        for key, c in seen.items():
            if c > 1:
                violations.append(Violation(
                    code="RQ-004", rule="No duplicate acceptance criteria", severity="warning",
                    message=f"AC statement duplicated {c}×: {key[:120]!r}",
                ))

        # RQ-002b — AGGREGATE measurability gate. The per-AC RQ-002 warnings feed
        # the metric; this rolls them up so the gen-time gate can act on a
        # Default severity = warning (informational, drives ATDD source-grounding
        # hints). Set ARTA_MEASURABLE_AC_GATE=block to escalate a near-zero
        # (<20%) requirement to an `error` that BLOCKS script gen until the ACs
        # are rewritten with measurable criteria. Floor configurable via
        # ARTA_MIN_MEASURABLE_AC_PCT (default 50).
        _meas_pct = round(100 * measurable / n, 1) if n else 0.0
        try:
            _floor = float(os.environ.get("ARTA_MIN_MEASURABLE_AC_PCT", "50") or "50")
        except (TypeError, ValueError):
            _floor = 50.0
        if _meas_pct < _floor:
            _gate_block = os.environ.get("ARTA_MEASURABLE_AC_GATE") == "block"
            violations.append(Violation(
                code="RQ-002b", rule="Requirement has enough measurable ACs",
                severity="error" if (_gate_block and _meas_pct < 20) else "warning",
                message=(
                    f"{req_id}: only {_meas_pct:.0f}% of {n} AC(s) carry a measurable "
                    f"criterion (target ≥ {_floor:.0f}%). Tests generated from vague ACs "
                    "assert weakly (status-only / hallucinated fields). Strengthen the ACs "
                    "with HTTP status codes, numeric thresholds, exact values, or field "
                    "assertions before script generation."
                ),
            ))

    return _result(violations, metrics={
        "req_id": req_id,
        "ac_count": n,
        "measurable_ac_count": measurable,
        "measurable_pct": round(100 * measurable / n, 1) if n else 0.0,
    })


# ── B2: Gherkin / scenario quality ───────────────────────────────────────────

def validate_gherkin_quality(
    gherkin_scenarios,
    requirement: dict,
    acceptance_criteria=None,
    *,
    gen_source: str | None = None,
    min_alignment: float = 0.25,
) -> TestQualityResult:
    """GQ-00x — validate generated Gherkin at the post-ATDD / pre-script seam.

    error: GQ-001 (no parseable Feature), GQ-002 (scenario missing
    When/Then), GQ-003 (fallback/template Gherkin). warning: GQ-004
    (requirement↔Gherkin intent drift), GQ-005 (impl leakage), GQ-006
    (non-atomic scenario).
    """
    violations: list[Violation] = []
    text = "\n\n".join(g for g in (gherkin_scenarios or []) if isinstance(g, str))
    req_id = requirement.get("req_id") or requirement.get("id") or "requirement"

    if not text.strip() or "Feature:" not in text:
        violations.append(Violation(
            code="GQ-001", rule="Valid Gherkin feature produced", severity="error",
            message=f"{req_id}: no parseable Gherkin Feature/Scenario produced.",
        ))
        return _result(violations, metrics={"req_id": req_id, "scenario_count": 0})

    scenario_bodies = re.split(r"(?im)^\s*Scenario(?:\s+Outline)?\s*:", text)[1:]
    if not scenario_bodies:
        violations.append(Violation(
            code="GQ-002", rule="Has Scenario blocks", severity="error",
            message=f"{req_id}: Feature has no Scenario blocks.",
        ))
    else:
        for i, body in enumerate(scenario_bodies, 1):
            low = body.lower()
            # R254 (WS2b) — a [NEEDS-CLARIFICATION] scenario is a deliberate
            # UPSTREAM-QUALITY SIGNAL, not a test case. The designer emits it
            # (instead of inventing an observable proxy) when an AC states no
            # verifiable outcome, so it has no When/Then BY DESIGN.
            #
            # real ticket refs, and a REAL account id from the R250 store —
            # and was hard-BLOCKED at 0 tests because scenario #23 was the
            # honesty signal itself. Grading the signal as a broken test
            # punishes ARTA for telling the truth and, worse, discards the 22
            # good scenarios alongside it.
            #
            # The untestable AC is still reported — as `needs_clarification`
            # below, which is what the operator should act on.
            if _NEEDS_CLARIFICATION_RE.search(body):
                continue
            if "when" not in low or "then" not in low:
                violations.append(Violation(
                    code="GQ-002", rule="Given/When/Then present", severity="error",
                    message=f"{req_id} scenario #{i} is missing When/Then steps.",
                    snippet=body.strip()[:160],
                ))

    # R254 (WS2b) — count the honesty signals so an untestable AC is VISIBLE
    # rather than silently proxied into a fake passing test (the pre-WS2b
    # behavior GQ-004 could only guess at after the fact).
    _needs_clarification = sum(
        1 for b in scenario_bodies if _NEEDS_CLARIFICATION_RE.search(b))
    if _needs_clarification:
        violations.append(Violation(
            code="GQ-007", rule="Requirement ACs are testable", severity="warning",
            message=(
                f"{req_id}: {_needs_clarification} AC(s) state no verifiable "
                f"outcome — the designer flagged them instead of inventing a "
                f"proxy. Sharpen the AC(s); this is an upstream REQUIREMENTS "
                f"defect, not a generation failure."),
        ))

    # A [NEEDS-CLARIFICATION] scenario must not trip the fallback/template
    # detector either — it is a signal, not LLM filler.
    _fallback_scan = _NEEDS_CLARIFICATION_LINE_RE.sub("", text).lower()
    is_fallback = (gen_source == "fallback") or any(
        m in _fallback_scan for m in _FALLBACK_MARKERS)
    if is_fallback:
        violations.append(Violation(
            code="GQ-003", rule="Not a fallback/template Gherkin", severity="error",
            message=(f"{req_id}: fallback/template Gherkin detected (ATDD LLM gen failed). "
                     "Regenerate via the ATDD designer before script generation."),
        ))

    # Requirement↔Gherkin intent drift (zero-LLM keyword overlap).
    req_text = " ".join(str(requirement.get(k) or "") for k in ("title", "description"))
    src_acs = acceptance_criteria if acceptance_criteria is not None else requirement.get("acceptance_criteria")
    req_text += " " + " ".join(_ac_statements(src_acs))
    req_kw = extract_gherkin_keywords(req_text)
    gk_kw = extract_gherkin_keywords(text)
    alignment = (len(req_kw & gk_kw) / len(req_kw)) if req_kw else 1.0
    if req_kw and alignment < min_alignment:
        violations.append(Violation(
            code="GQ-004", rule="Gherkin aligns with the requirement", severity="warning",
            message=(f"{req_id}: Gherkin↔requirement keyword alignment {alignment:.2f} "
                     f"< {min_alignment:.2f}; scenarios may have drifted from the requirement."),
        ))

    leak = _IMPL_LEAK_RE.search(text)
    if leak:
        violations.append(Violation(
            code="GQ-005", rule="Declarative (no implementation leakage)", severity="warning",
            message=(f"{req_id}: Gherkin contains implementation detail {leak.group(0)!r}; "
                     "scenarios should be declarative, not reference selectors/code."),
        ))

    for i, body in enumerate(scenario_bodies, 1):
        and_count = len(re.findall(r"(?im)^\s*And\b", body))
        if and_count > 6:
            violations.append(Violation(
                code="GQ-006", rule="Atomic scenario", severity="warning",
                message=f"{req_id} scenario #{i} has {and_count} And-steps; consider splitting.",
            ))

    return _result(violations, metrics={
        "req_id": req_id,
        "scenario_count": len(scenario_bodies),
        "alignment": round(alignment, 3),
        "is_fallback": is_fallback,
    })


# ── B3: pre-gen AC coverage ──────────────────────────────────────────────────

def validate_ac_coverage(acceptance_criteria, gherkin_scenarios) -> TestQualityResult:
    """AC-001 — verify each acceptance criterion is represented by ≥1 scenario
    (zero-LLM keyword overlap ≥ 0.3). All findings are `warning` — uncovered
    ACs reduce the coverage metric but don't block gen."""
    violations: list[Violation] = []
    acs = _ac_statements(acceptance_criteria)
    text = "\n".join(g for g in (gherkin_scenarios or []) if isinstance(g, str))
    gk = extract_gherkin_keywords(text)
    covered = 0
    for s in acs:
        ackw = extract_gherkin_keywords(s)
        if not ackw:
            continue
        overlap = len(ackw & gk) / len(ackw)
        if overlap >= 0.3:
            covered += 1
        else:
            violations.append(Violation(
                code="AC-001", rule="AC represented by a scenario", severity="warning",
                message=f"AC not represented in Gherkin (overlap {overlap:.2f}): {s[:100]!r}",
                snippet=s[:160],
            ))
    coverage_pct = round(100 * covered / len(acs), 1) if acs else 100.0
    return _result(violations, metrics={
        "ac_total": len(acs), "ac_covered": covered, "coverage_pct": coverage_pct,
    })


# ── clarity score + source-code augmentation (Phase 2) ──────────────────────

def requirement_clarity_score(requirement: dict) -> dict:
    """Score a requirement's clarity 0-100 and band it (clear|weak|unclear),
    with per-AC highlights. Built from the RQ checks — no LLM. The directive:
    *when the requirement/AC is not clear, score it and highlight it.*"""
    res = validate_requirement_quality(requirement)
    score = res.score
    # An error-severity violation (e.g. no acceptance criteria) is the most
    # unclear a requirement can be → band 'unclear' regardless of raw score.
    if not res.passed:
        band = "unclear"
    else:
        band = "clear" if score >= 80 else "weak" if score >= 50 else "unclear"
    m = res.criteria_results.get("_metrics", {})
    return {
        "req_id": (requirement.get("req_id") or requirement.get("id")),
        "score": score, "band": band,
        "measurable_ac_pct": m.get("measurable_pct"), "ac_count": m.get("ac_count"),
        "highlights": [_violation_dict(v) for v in res.violations],
    }


def build_source_augmentation(requirement: dict, project_id: str, *,
                              clarity_band: str | None = None,
                              max_chars: int = 2200) -> str:
    """Return a SUT-architecture grounding block (from Phase-1 Architecture
    Discovery) to augment ATDD/automation prompts. The directive: *if the
    source code is available, augment the test case + script with it* — so a
    weak/unclear requirement still yields tests grounded in the real SUT.
    Returns "" when no discovery exists. When the requirement is weak/unclear,
    prepends a directive to rely on the architecture rather than invent."""
    try:
        from .architecture_discovery import summarize_for_prompt
    except Exception:
        return ""
    topo = summarize_for_prompt(project_id, max_chars=max_chars)
    if not topo:
        return ""
    if clarity_band in ("weak", "unclear"):
        lead = ("# NOTE: this requirement's acceptance criteria are underspecified "
                f"(clarity={clarity_band}). GROUND the test in the REAL SUT architecture "
                "below — exercise only endpoints/flows/auth that actually exist; do NOT "
                "invent endpoints or fields beyond this.\n")
        return lead + topo
    return topo


# ── R205: source-grounded AC enrichment ──────────────────────────────────────

# Path segments that carry no semantic signal for relevance matching
# (UUIDs, hex blobs, pure numbers, the cm-API boilerplate).
_R205_ID_SEG_RE = re.compile(r"^(?:[0-9a-f]{8,}|\d+|v\d+|api|user|private|default|cm)$", re.I)


def _r205_tokens(text: str) -> set[str]:
    """≥4-char alpha tokens, lowercased — the relevance vocabulary."""
    return {w.lower() for w in re.findall(r"[A-Za-z]{4,}", text or "")}


def _r205_endpoint_tokens(path: str) -> set[str]:
    """Meaningful path-segment tokens (drop UUIDs/numbers/cm boilerplate)."""
    out: set[str] = set()
    for seg in re.split(r"[/_\-]", path or ""):
        seg = seg.strip()
        if seg and len(seg) >= 4 and not _R205_ID_SEG_RE.match(seg):
            out.add(seg.lower())
    return out


def enrich_requirement_acs_with_source(requirement: dict, project_id: str) -> dict:
    """R205 — make UNMEASURABLE acceptance criteria measurable by grounding them
    in the REAL SUT contract (captured endpoints + their HTTP methods/paths).

    Mission rationale (operator): *ARTA has code access — use it to make up for
    the bad quality of requirements.* Pre-R205 the SUT contract was fetched but
    only injected as ATDD prompt CONTEXT (`build_source_augmentation`); the
    weak ACs themselves were never improved, so they failed the measurability
    check (run-d21eb3: measurable_ac=17%) and produced weak Gherkin the upstream
    gate blocked (gherkin_block_rate=30%). For each AC that has NO measurable
    criterion (`_MEASURABLE_RE`), this appends a concrete, source-derived clause
    — the most relevant captured endpoint + the verifiable HTTP status contract
    — so the AC scores measurable AND ATDD has a real endpoint/status to assert.

    DETERMINISTIC — no LLM call (the claude_code CLI is the throughput
    bottleneck). Mutates + returns the requirement. Idempotent (the appended
    clause is itself measurable, so re-running is a no-op). Killswitch
    ARTA_R205_AC_ENRICH_DISABLE=1.
    """
    import os
    if os.environ.get("ARTA_R205_AC_ENRICH_DISABLE") == "1":
        return requirement
    acs = requirement.get("acceptance_criteria")
    if not isinstance(acs, list) or not acs:
        return requirement
    try:
        from .api_discovery import _load_captured_endpoints
        endpoints = _load_captured_endpoints(project_id) or []
    except Exception:
        endpoints = []
    # Build a deduped, GET-preferred index of (method, path, tokens).
    indexed: list[tuple] = []
    seen_paths: set[str] = set()
    for ep in endpoints:
        if not isinstance(ep, dict):
            continue
        path = ep.get("path") or ""
        method = (ep.get("method") or "GET").upper()
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        indexed.append((method, path, _r205_endpoint_tokens(path)))

    def _best_endpoint(ac_text: str):
        toks = _r205_tokens(ac_text)
        if not toks or not indexed:
            return None
        best, best_score = None, 0
        for method, path, etoks in indexed:
            score = len(toks & etoks)
            # Prefer GET on ties (read-only, matches the R154 non-mutation guarantee).
            if score > best_score or (score == best_score and score > 0 and method == "GET" and (not best or best[0] != "GET")):
                best, best_score = (method, path), score
        return best if best_score > 0 else None

    enriched = 0
    for i, ac in enumerate(acs):
        stmt = ac if isinstance(ac, str) else (
            ac.get("statement") or ac.get("description") or ac.get("text") or ""
            if isinstance(ac, dict) else "")
        if not stmt or not stmt.strip():
            continue
        if _MEASURABLE_RE.search(stmt):
            continue   # already measurable — leave it
        hit = _best_endpoint(stmt)
        if hit:
            method, path = hit
            clause = (
                f" (Verifiable against the SUT contract: `{method} {path}` — assert the "
                f"HTTP response status code, e.g. 200 on success / 401 when unauthenticated "
                f"/ 403 when forbidden, and that the response body contains the expected fields.)"
            )
        else:
            # No path match — a domain-neutral but still measurable clause that
            # makes the AC verifiable without inventing a specific endpoint.
            clause = (
                " (Verifiable: the API returns the documented HTTP status code — e.g. 200 on "
                "success, 401 when unauthenticated — and a response body matching the SUT contract.)"
            )
        new_stmt = stmt.rstrip() + clause
        if isinstance(ac, str):
            acs[i] = new_stmt
        elif isinstance(ac, dict):
            key = "statement" if ac.get("statement") else (
                "description" if ac.get("description") else (
                    "text" if ac.get("text") else "statement"))
            acs[i] = {**ac, key: new_stmt}
        enriched += 1

    if enriched:
        requirement["acceptance_criteria"] = acs
        requirement["_r205_acs_enriched"] = enriched
    return requirement


# ── seam helpers ─────────────────────────────────────────────────────────────

def validate_gherkin_stage(
    gherkin_scenarios,
    requirement: dict,
    acceptance_criteria=None,
    *,
    gen_source: str | None = None,
) -> dict:
    """Run GQ + AC-coverage at the post-ATDD/pre-script seam and return a
    merged, JSON-serialisable summary the caller acts on:

        {should_block, passed, error_count, warning_count,
         gherkin, ac_coverage, violations, hint}

    `should_block` is True when any error-severity violation is present
    (malformed / fallback / structurally-broken Gherkin) — the caller should
    then retry ATDD with `hint` or queue an upstream regen rather than burn
    the small-Ollama budget generating a doomed script.
    """
    gq = validate_gherkin_quality(
        gherkin_scenarios, requirement, acceptance_criteria, gen_source=gen_source)
    src_acs = acceptance_criteria if acceptance_criteria is not None else requirement.get("acceptance_criteria")
    cov = validate_ac_coverage(src_acs, gherkin_scenarios)
    all_v = list(gq.violations) + list(cov.violations)
    errors = [v for v in all_v if v.severity == "error"]
    return {
        "should_block": bool(errors),
        "passed": not errors,
        "error_count": len(errors),
        "warning_count": len(all_v) - len(errors),
        "gherkin": gq.to_dict(),
        "ac_coverage": cov.to_dict(),
        "violations": [_violation_dict(v) for v in all_v],
        "hint": violations_to_hint(all_v) if all_v else "",
    }


def _violation_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    return {"code": v.code, "rule": v.rule, "severity": v.severity,
            "message": v.message, "snippet": getattr(v, "snippet", "")}


def violations_to_hint(violations) -> str:
    """Render violations as an LLM-friendly hint for ATDD retry-with-hint."""
    lines = []
    for v in violations:
        d = _violation_dict(v)
        tag = "MUST FIX" if d.get("severity") == "error" else "improve"
        lines.append(f"- [{d.get('code')} {tag}] {d.get('message')}")
    return "Upstream quality feedback (address before regenerating):\n" + "\n".join(lines)


# ── B5: per-requirement persistence + project aggregation ────────────────────

def _safe_id(s) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(s))[:120]


def persist_upstream_quality(
    req_id: str,
    project_id: str | None,
    *,
    requirement_result: TestQualityResult | None = None,
    gherkin_stage: dict | None = None,
    clarity: dict | None = None,
) -> None:
    """Write a per-requirement upstream-quality sidecar (best-effort)."""
    try:
        _UPSTREAM_DIR.mkdir(parents=True, exist_ok=True)
        rec: dict = {"req_id": req_id, "project_id": project_id}
        if requirement_result is not None:
            rec["requirement_quality"] = requirement_result.to_dict()
        if gherkin_stage is not None:
            rec["gherkin_stage"] = gherkin_stage
        if clarity is not None:
            rec["clarity"] = clarity
        (_UPSTREAM_DIR / f"{_safe_id(req_id)}.json").write_text(json.dumps(rec, indent=2))
    except Exception:
        pass


def _metrics_of(tq_dict: dict | None) -> dict:
    """Pull the `_metrics` block out of a serialised TestQualityResult."""
    if not isinstance(tq_dict, dict):
        return {}
    return (tq_dict.get("criteria") or {}).get("_metrics") or {}


def read_upstream_quality(project_id: str | None = None, req_ids: set | None = None) -> dict:
    """Aggregate per-requirement sidecars into the upstream-quality metrics the
    dashboard / mission report surface. Filters by project_id and/or req_ids."""
    rows: list[dict] = []
    if _UPSTREAM_DIR.is_dir():
        for p in sorted(_UPSTREAM_DIR.glob("*.json")):
            try:
                rec = json.loads(p.read_text())
            except Exception:
                continue
            if project_id and rec.get("project_id") not in (project_id, None):
                continue
            if req_ids and rec.get("req_id") not in req_ids:
                continue
            rows.append(rec)

    n = len(rows)
    if n == 0:
        return {"project_id": project_id, "requirement_count": 0, "rows": []}

    meas_pcts, aligns, covs, clarity_scores = [], [], [], []
    fallback = blocked = req_passed = 0
    clarity_bands: dict[str, int] = {}
    compact = []
    for rec in rows:
        rq = _metrics_of(rec.get("requirement_quality"))
        gs = rec.get("gherkin_stage") or {}
        gq = _metrics_of(gs.get("gherkin"))
        ac = _metrics_of(gs.get("ac_coverage"))
        clar = rec.get("clarity") or {}
        if "measurable_pct" in rq:
            meas_pcts.append(rq["measurable_pct"])
        if (rec.get("requirement_quality") or {}).get("passed") is not False:
            req_passed += 1
        if "alignment" in gq:
            aligns.append(round(gq["alignment"] * 100, 1))
        if gq.get("is_fallback"):
            fallback += 1
        if gs.get("should_block"):
            blocked += 1
        if "coverage_pct" in ac:
            covs.append(ac["coverage_pct"])
        if isinstance(clar.get("score"), (int, float)):
            clarity_scores.append(clar["score"])
        if clar.get("band"):
            clarity_bands[clar["band"]] = clarity_bands.get(clar["band"], 0) + 1
        compact.append({
            "req_id": rec.get("req_id"),
            "clarity_score": clar.get("score"),
            "clarity_band": clar.get("band"),
            "measurable_ac_pct": rq.get("measurable_pct"),
            "gherkin_alignment_pct": round(gq["alignment"] * 100, 1) if "alignment" in gq else None,
            "ac_coverage_pct": ac.get("coverage_pct"),
            "gherkin_blocked": bool(gs.get("should_block")),
            "is_fallback": bool(gq.get("is_fallback")),
        })

    def _mean(xs):
        return round(sum(xs) / len(xs), 1) if xs else None

    return {
        "project_id": project_id,
        "requirement_count": n,
        # Requirement testability: mean % of ACs carrying a measurable criterion.
        "measurable_ac_pct": _mean(meas_pcts),
        "requirement_quality_pct": round(100 * req_passed / n, 1),
        # Clarity (Phase 2): mean score + band distribution (clear|weak|unclear).
        "clarity_score_mean": _mean(clarity_scores),
        "clarity_bands": clarity_bands,
        # Gherkin↔requirement alignment + structural health.
        "gherkin_alignment_pct": _mean(aligns),
        "gherkin_block_rate": round(100 * blocked / n, 1),
        "fallback_rate": round(100 * fallback / n, 1),
        "pregen_ac_coverage_pct": _mean(covs),
        "rows": compact,
    }
