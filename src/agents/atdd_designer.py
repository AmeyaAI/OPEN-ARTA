"""
ARTA ATDD Designer Agent — TEA Layer 3: Acceptance Test Definition

Generates Gherkin BDD scenarios following true ATDD:
  1. Tests are generated BEFORE implementation (red phase)
  2. Scenarios are linked to acceptance criteria
  3. Edge cases, security, and performance assertions included

Project-type-aware generation:
  - web_app (default): standard 8-scenario-type strategy (incl. a11y interaction for P0/P1 UI)
  - analytics: TEA-extended 8-principle strategy (frozen fixtures, LLM-as-judge,
    provenance, 3-tier execution tags, adversarial scenarios)
  - mobile_app: Appium step templates + accessibility scenarios
  - api_microservice: contract + schema validation step templates
"""
from __future__ import annotations

import logging
import os
import re
import uuid

import anthropic
from anthropic import AsyncAnthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .retry_policy import LLM_RETRYABLE_EXC   # R134.C — single-source-of-truth retry tuple

# R280 — module-level logger. Four `log.warning(...)` call sites existed with NO
# `log` defined (F821), ALL inside `except` handlers — so when they fired, the
# ERROR HANDLER ITSELF raised NameError and masked the original failure. Matches
# the "arta.atdd" name the two local `_logging.getLogger` call sites already use.
log = logging.getLogger("arta.atdd")


GHERKIN_GEN_PROMPT = """\
You are an expert ATDD (Acceptance Test Driven Development) designer \
using the BMAD TEA methodology.

Generate comprehensive Gherkin scenarios for this acceptance criterion.

REQUIREMENT: {req_id} — {req_title}
PRIORITY: {priority} (Risk Score: {risk_score})
ACCEPTANCE CRITERION: {ac_statement}
DOMAIN ENTITIES: {entities}
CONSTRAINTS: {constraints}
DATA PATTERNS: {data_patterns}
{req_description}{ac_gwt}{jira_context}{endpoint_contract}{real_data_contract}

REQUIRED SCENARIO TYPES (generate ALL applicable):
1. Happy path — primary success flow with concrete data values
2. Boundary conditions — min/max/empty/null/whitespace
3. Negative cases — invalid inputs, wrong state, missing preconditions
4. Security — SQL injection, XSS, CSRF, auth bypass, data exposure
5. Performance assertion — include threshold in Then step if timing constraint
6. Idempotency — if operation mutates state, verify no double-execution
7. Concurrency — if high-traffic (P0/P1), include concurrent user scenario
8. Accessibility interaction — ONLY if priority is P0 or P1 AND the AC involves a UI
   component (form, button, modal, table, navigation, widget, or any interactive element):
   a. Keyboard-only navigation: Tab/Shift+Tab traversal, Enter/Space activation,
      Escape dismissal. Every interactive element must be reachable without a mouse.
   b. Screen-reader announcement: Focus change triggers an appropriate ARIA live-region
      or role announcement. Assert the announced text is meaningful (not empty or
      generic "button").
   Tag these scenarios @a11y @wcag-2.1.

RULES:
- Use CONCRETE data values (not <placeholder> or generic "valid input")
- Include Examples: tables for Scenario Outlines
- Add # comments linking to requirement and AC ids
- Steps must be atomic and independently verifiable
- Performance thresholds must come from acceptance criteria (not guessed)
- MEASURABLE THEN (mandatory): every scenario's `Then`/`And` assertion MUST be
  objectively verifiable — an exact HTTP status ("Then the response status is 200"),
  a number+comparator+unit ("Then the response time is under 2000ms", "Then at least
  1 result is returned"), an exact value/message ("Then the `status` field equals
  'active'"), or a concrete field assertion ("Then the response body contains a
  non-empty `token`"). NEVER write vague outcomes ("the user is logged in", "it works",
  "loads gracefully", "errors are handled properly"). Avoid: fast,
  slow, good, properly, gracefully, seamless, robust, correct, as expected.
- VAGUE AC (mandatory): if the AC does not state an objectively verifiable outcome,
  do NOT invent one. Emit exactly one scenario for it:
      @needs-clarification
      Scenario: [NEEDS-CLARIFICATION] <ac_id>
        # The AC does not define a verifiable outcome. State what is missing.
  An AC that cannot be tested is an upstream REQUIREMENTS defect and must be
  reported as one — a guessed proxy silently converts it into a fake passing
  test and hides the defect from the operator.

[K2 — AUTH PROPAGATION FOR API CALLS]
When the generated Playwright test calls `page.request.post()`, `page.request.get()`,
etc., it MUST be wrapped in a try/catch around `response.json()`:

  try {{
    const response = await page.request.post('/api/...', {{ data: {{...}} }});
    if (!response.ok()) test.skip(true, `API returned ${{response.status()}}`);
    const body = await response.json();   // wrap in try/catch — see below
    expect(body.field).toEqual('value');
  }} catch (err) {{
    if (String(err).includes('SyntaxError')) {{
      test.skip(true, 'API returned HTML — likely auth redirect');
    }} else {{
      throw err;
    }}
  }}

Auth cookies propagate via `extraHTTPHeaders` configured in playwright.config.ts.
The TARGET_AUTH_COOKIE_VALUE env var is set by ARTA's runtime; tests do NOT need
to set Cookie headers explicitly.

OUTPUT: A complete, valid .feature file. No prose before or after. Return ONLY the raw Gherkin — do NOT wrap in markdown code fences (no ```).
"""

ANALYTICS_GHERKIN_PROMPT = """\
You are a TEA Acceptance Test Designer specializing in ANALYTICS AI systems.
Apply the TEA-extended 8-principle methodology to generate Gherkin scenarios.

REQUIREMENT: {req_id} — {req_title}
PRIORITY: {priority} (Risk Score: {risk_score})
ACCEPTANCE CRITERION: {ac_statement}
DOMAIN ENTITIES: {entities}
DATA FIXTURE: {fixture_path}

[CONTRACT — MUST FOLLOW] DATA RECIPE EXPECTED OUTPUTS:
The fixture has been designed to produce these EXACT pipeline outputs.
Do NOT invent magnitudes, directions, or metric names — they will not match the data.

{expected_outputs_intro}

{expected_outputs_block}

REQUIRED SCENARIO TYPES (generate ALL):
1. @tier1 Frozen data happy path — assert on structured insight properties, NOT narrative text
2. @tier2 Layer isolation — separate Given/When/Then for each pipeline layer
   (NL→Query, Query→Result, Result→Insight, Insight→Narrative)
3. @tier2 Tolerance-based numerical assertion — use "within X%" not exact match
4. @tier2 Grounding check — verify every claim is traceable to source data
5. @tier3 LLM-as-judge semantic eval — include rubric comment block
6. @tier3 Adversarial: ambiguous metric — ask "Show me performance" without specifying metric
7. @tier3 Adversarial: conflicting time filter — "Q3 vs last year Q3"
8. @tier3 Adversarial: missing data segment — ask about a category with no data
9. @tier3 Adversarial: leading question — ask "Why did sales drop?" when they rose

RULES:
- Tag every scenario with @tier1, @tier2, or @tier3
- Add provenance comment block: data_snapshot, model_version, prompt_version
- Use frozen_dataset("{fixture_path}") in Background — exactly this path string
- Assert on insight.metric, insight.direction, insight.magnitude_pct — NOT on narrative text
- Tolerance assertions: "within 1.0% of <expected_value>" where <expected_value>
  is from the recipe's expected_outputs above
- Each layer test must mock all other layers
- For adversarial scenarios (@tier3): assertions on `expected_outputs` may be
  inverted (e.g. "magnitude should NOT be 12.5") but still cite the recipe's
  numbers verbatim — never invent fresh values

[K3 — ASSERTION SOURCE: response, NOT rendered DOM text]
The recipe's `expected_outputs` are PIPELINE outputs (the values the
analytics backend computes), NOT the literal text the UI renders. The
SUT may format them, round them, attach units, or render them inside
icons/charts. Run-89e80da6 had 150 of 213 Playwright failures because
generated tests asserted `await expect(getByTestId('insight-magnitude-pct')).toHaveText('12.5')`
when the SUT rendered "+12.5%" or "12.5 percent" or "12.5±0.2%".

For analytics value assertions in Playwright, PREFER (in this order):
  1. `await page.waitForResponse(/\\/api\\/.*\\/insight/)` then assert
     `(await response.json()).<jsonpath>` matches expected_outputs.
     This pulls the raw pipeline value, not the rendered text.
  2. `await expect(getByTestId('...')).toContainText('12.5')` —
     loose match handles formatted variants ("+12.5%", "12.5%").
  3. `await expect(getByTestId('...')).toBeVisible()` — presence
     check when the value is dynamic / time-sensitive.
  4. ONLY use exact `toHaveText('<value>')` for stable static labels
     (status text "Ready", error messages "Recipe missing").

For analytics tier3 LLM-judge tests: emit Python pytest, NOT Playwright.
Playwright is for UI-rendered behavior. Numerical/aggregation correctness
goes through the pytest analytics path (which uses `tolerant_assert`
under K13).

OUTPUT: A complete, valid .feature file with tier tags. No prose before or after. Return ONLY the raw Gherkin — do NOT wrap in markdown code fences (no ```).
"""


def _ac_as_dict(ac):
    """Normalize an AC to a plain dict.

    Tolerates: dataclass instances (`AcceptanceCriterion`), ORM/Pydantic models,
    dicts, falsy/garbage. Returns {} for anything we can't make sense of so the
    caller's `.get(...)` calls degrade to defaults instead of crashing.

    Defensive belt-and-braces: the canonical fix is `command_dispatcher.py`'s
    `_to_plain_dict` which deep-converts at the contract boundary, but agents
    should not assume their inputs are already in the right shape.
    """
    if isinstance(ac, dict):
        return ac
    if hasattr(ac, '__dataclass_fields__'):
        return {k: getattr(ac, k) for k in ac.__dataclass_fields__}
    if hasattr(ac, '__dict__'):  # ORM / Pydantic
        return {k: v for k, v in vars(ac).items() if not k.startswith('_')}
    return {}


# Phase A8 (per plan DR-9): web_app/mobile_app requirements often carry
# quantitative ACs ("response < 500ms", "p95 ≤ 200ms", "85% pass rate",
# "$50 minimum spend"). These need recipe-bound Gherkin generation just
# like analytics — without it, the LLM invents threshold values that
# don't match the actual SUT behavior. The pattern below is a regex-only
# heuristic (no schema migration); operators can override per-project.
_QUANTITATIVE_AC_PATTERN = re.compile(
    r"(?:"
    r"\b\d+\s*%"                              # 85%
    r"|\$\s*\d+(?:[,.]\d+)?"                  # $50 or $50.00
    r"|\b\d+\s*(?:ms|sec(?:onds?)?|min(?:utes?)?|h(?:ours?)?|day|week|month|year)s?\b"  # 500ms / 2 hours
    r"|\bp\d{2,3}\b"                          # p95, p99
    r"|\b\d{4}-\d{2}-\d{2}\b"                 # ISO dates
    r"|\b\d+(?:\.\d+)?\s*(?:gb|mb|kb|tb)\b"   # 100 MB
    r"|(?:>=|<=|≥|≤|>|<)\s*\d"                # comparators with numbers
    r")",
    re.IGNORECASE,
)


# Phase R1 — domain-classification tokens. Auth/identity requirements
# often have latency thresholds (p95<800ms) that match _QUANTITATIVE_AC_PATTERN
# but they need TEST-USER / JWT / OAUTH-MOCK fixtures, NOT analytics datasets.
# down the analytics-recipe path, recipe agent rejected it (no analytics
# shape), and ATDD emitted RECIPE_INVALID stubs for all 15 of its tests.
_AUTH_DOMAIN_TOKENS = (
    " auth", "oauth", "login", "logout", " jwt", "token", "session",
    "credential", "password", "permission", "role-based", "rbac",
    "mfa", "2fa", "sso", "saml", "openid", "sign in", "sign-in", "sign up",
)
_ANALYTICS_DOMAIN_TOKENS = (
    "metric", "insight", "aggregate", "trend", "magnitude", "correlation",
    "dataset", "histogram", "median", "percentile", "average", "sum of",
)
# R218 C3 — PERFORMANCE/latency thresholds (p95<800ms, response time) are k6's
# domain, NOT an analytics dataset. A requirement whose ONLY quantitative AC is a
# perf threshold (with no analytics-domain signal) must NOT be routed to the
# DatasetRecipe path — that path rejects it (no analytics shape) and emits a
# RECIPE_INVALID "# No scenarios" stub. These tokens identify perf-only ACs.
_PERF_AC_TOKENS = (
    "ms", "millisecond", "latency", "p95", "p99", "p50", "response time",
    "responsetime", "throughput", "requests per", "req/s", "rps", "load time",
    "render time", "ttfb", "round trip", "round-trip",
)


def _requires_data_fixtures(req: dict) -> bool:
    """Heuristic: does this requirement assert quantitative thresholds?

    True when ANY AC's text (statement + given + when + then + measurable_threshold)
    contains a percentage, currency, time-unit, percentile bucket (p95/p99), date,
    storage size, or numeric comparator. These are the cases where a recipe-bound
    prompt is the difference between "asserts the actual SUT value" and "asserts
    a value the LLM imagined."

    Pure-flow ACs ("user clicks login → sees dashboard") return False — those
    don't need a recipe.

    Phase R1 — auth/identity domain bypass. Even when latency thresholds
    (p95<800ms) match the quantitative pattern, requirements dominated by
    auth/identity tokens are NOT analytics. They need a different fixture
    type (test users, JWT tokens, mock OAuth) which we don't synthesize
    via DatasetRecipeAgent. Returning False here lets ATDD take the
    standard prompt path instead of producing a RECIPE_INVALID stub.
    """
    # Phase R1 domain classification — count signal tokens
    auth_signal = 0
    analytics_signal = 0
    for ac in req.get("acceptance_criteria") or []:
        ac_dict = _ac_as_dict(ac)
        text_for_class = " ".join(
            str(ac_dict.get(f, "") or "")
            for f in ("statement", "given", "when", "then", "measurable_threshold")
        ).lower()
        for tok in _AUTH_DOMAIN_TOKENS:
            if tok in text_for_class:
                auth_signal += 1
        for tok in _ANALYTICS_DOMAIN_TOKENS:
            if tok in text_for_class:
                analytics_signal += 1
    # Auth-dominant requirements skip the data-fixture path.
    # Threshold: ≥2 auth tokens AND zero analytics tokens. Two auth tokens
    # (not one) avoid false-positives for analytics requirements that
    # happen to mention "session" or "user" peripherally.
    if auth_signal >= 2 and analytics_signal == 0:
        return False

    for ac in req.get("acceptance_criteria") or []:
        ac_dict = _ac_as_dict(ac)
        text = " ".join(
            str(ac_dict.get(f, "") or "")
            for f in ("statement", "given", "when", "then", "measurable_threshold")
        )
        if _QUANTITATIVE_AC_PATTERN.search(text):
            # R218 C3 — a PERF/latency-only quantitative AC (no analytics-domain
            # signal anywhere in the requirement) is k6's domain, not a dataset.
            # Don't route it to the recipe path (→ RECIPE_INVALID stub). Genuine
            # analytics reqs have analytics_signal>0 and are unaffected.
            _tl = text.lower()
            if (analytics_signal == 0
                    and any(p in _tl for p in _PERF_AC_TOKENS)
                    and os.environ.get("ARTA_C3_PERF_NOT_RECIPE_DISABLE") != "1"):
                continue
            return True
    return False


class ATDDDesignerAgent:
    """
    Generates failing Gherkin scenarios for each acceptance criterion.
    Enforces red-green ATDD cycle by creating tests before implementation.
    """

    def __init__(self, client: AsyncAnthropic, model: str = "claude-haiku-4-5-20251001"):
        self._client = client
        self._model = model

    @staticmethod
    def _build_available_selectors_block(project_id: str) -> str:
        """R19c — thin wrapper around `api_discovery.format_dom_catalog_for_prompt`.

        Single source of truth lives in api_discovery.py (next to the
        DOM catalog persistence helpers). Both ATDD/Gherkin generation
        and Playwright spec generation call the same formatter so the
        constraint block is identical across all LLM call sites.
        """
        try:
            from .api_discovery import format_dom_catalog_for_prompt
        except Exception:
            return ""
        return format_dom_catalog_for_prompt(project_id)

    # R254 (WS2b) — per-field caps. The 52.9%->38.6% precedent was bulk
    # endpoint injection; every field here is hard-capped and the total is
    # asserted <=4000 chars by unit test. Mirrors upstream_quality's existing
    # max_chars=2200 discipline.
    _R254_CAPS = {
        "req_description": 1200,
        "ac_gwt": 800,
        "jira_context": 1000,
        "endpoint_contract": 600,
        "real_data_contract": 400,
    }

    @staticmethod
    def _r254_design_context(
        req: dict, risk: dict, *, ac_gwt: str = "",
    ) -> dict:
        """R254 (WS2b) — build the designer's grounding blocks.

        Returns exactly the keys GHERKIN_GEN_PROMPT interpolates, ALWAYS (an
        absent key is a runtime KeyError in `.format()`), each capped per
        _R254_CAPS. Every block is "" when its source is unavailable, so the
        prompt degrades to its pre-WS2b shape rather than carrying empty
        headers.

        NOTE: this grounds the GENERIC branch only. The analytics branch
        (ANALYTICS_GHERKIN_PROMPT — the analytics recipe path) has its own
        expected_outputs contract and is deliberately untouched; that split is
        why the analytics path is structurally insulated from this change.
        """
        import os as _os
        out = {k: "" for k in ATDDDesignerAgent._R254_CAPS}
        if _os.environ.get("ARTA_R254_DESIGN_GROUNDING_DISABLE", "").lower() in ("1", "true"):
            return out
        caps = ATDDDesignerAgent._R254_CAPS
        pid = str(req.get("project_id") or risk.get("project_id") or "")

        # 1. The requirement's OWN text — never passed to the designer before.
        _desc = (req.get("description") or "").strip()
        if _desc:
            out["req_description"] = (
                "\nREQUIREMENT DESCRIPTION (the source of truth for what to test):\n"
                + _desc[:caps["req_description"]] + "\n")

        # 2. The AC's structured given/when/then (built by the caller).
        if ac_gwt:
            out["ac_gwt"] = ac_gwt[:caps["ac_gwt"]]

        # 3. JIRA's untruncated, unparaphrased issue text (R257/WS2e).
        try:
            _jira = ((req.get("metadata") or {}).get("jira") or {})
            _txt = (_jira.get("enriched_text") or "").strip()
            if _txt:
                _url = req.get("jira_url") or req.get("source_url") or ""
                out["jira_context"] = (
                    f"\nJIRA SOURCE ({req.get('jira_key') or ''} {_url}) — the "
                    f"ORIGINAL issue text. The REQUIREMENT DESCRIPTION above is an "
                    f"LLM paraphrase of it; where they differ, THIS wins:\n"
                    + _txt[:caps["jira_context"]] + "\n")
        except Exception as _jx:
            log.warning("R254: jira context unavailable for %s: %s", req.get("id"), _jx)

        # 4. The real endpoint contract — the SAME list R213 grades against, so
        #    the designer is judged on what it was actually shown.
        try:
            from .api_discovery import _load_captured_endpoints
            _eps = _load_captured_endpoints(pid) or [] if pid else []
            if _eps:
                _lines = []
                for _e in _eps:
                    if not isinstance(_e, dict):
                        continue
                    _p = _e.get("path") or _e.get("url") or ""
                    if _p:
                        _lines.append(f"  {(_e.get('method') or 'GET').upper()} {_p}")
                _blk = "\n".join(_lines)[:caps["endpoint_contract"]]
                if _blk:
                    out["endpoint_contract"] = (
                        "\nSUT API SURFACE (real, discovery-verified — scenarios "
                        "must target THESE, not invented paths):\n" + _blk + "\n")
        except Exception as _ex:
            log.warning("R254: endpoint contract unavailable for %s: %s", req.get("id"), _ex)

        # 5. Real ids (R250/WS1a) — so the designer's "concrete data values" are
        #    the SUT's, not the LLM's.
        try:
            from .real_id_store import entities_known, real_ids_for_entity
            if pid:
                _parts = []
                for _e in entities_known(pid)[:4]:
                    _vals = real_ids_for_entity(pid, _e)[:2]
                    if _vals:
                        _parts.append(f"  {_e}: {', '.join(_vals)}")
                if _parts:
                    out["real_data_contract"] = (
                        "\nREAL TEST DATA (ids the SUT actually served — use these "
                        "verbatim; never invent an id):\n"
                        + "\n".join(_parts)[:caps["real_data_contract"]] + "\n")
        except Exception as _rx:
            log.warning("R254: real-data contract unavailable for %s: %s", req.get("id"), _rx)
        return out

    async def generate(
        self,
        requirements: list[dict],
        risk_profiles: list[dict],
        project_type: str = "web_app",
        project_meta: dict | None = None,
    ) -> dict:
        """
        Generate acceptance criteria and Gherkin for all requirements.

        Args:
            requirements:  List of structured requirement dicts.
            risk_profiles: Risk profiles from StrategyArchitectAgent.
            project_type:  One of web_app | mobile_app | api_microservice |
                           analytics | enterprise_app. Selects scenario strategy.
            project_meta:  R145.B — optional project config dict. When
                           `auto_detect_auth_bypass(project_meta)` is True,
                           ACs whose statement/given/when/then matches PURE
                           login flow are filtered out BEFORE the LLM batch
                           (saves Ollama input tokens; prevents structurally
                           unpassable login tests).

        Returns:
          {
            "acceptance_criteria": [...],
            "gherkin_scenarios": ["feature file content", ...],
            "test_data_fixtures": [{"fixture_id": ..., "test_id": ..., "rows": ...}, ...]
          }
        """
        # R145.B — auth-bypass AC filter (upstream-most filter site).
        # Mutates `requirements` IN-PLACE by replacing each req's
        # `acceptance_criteria` list with a filtered version when the
        # project's auth-bypass auto-detect fires (or when operator
        # explicitly enabled). Reports bypass count via _r145_b_bypassed.
        _r145_b_bypassed_count = 0
        try:
            from ..shared.auth_route_patterns import (
                auto_detect_auth_bypass, is_login_flow_text,
            )
            if auto_detect_auth_bypass(project_meta):
                for req in requirements or []:
                    if not isinstance(req, dict):
                        continue
                    acs = req.get("acceptance_criteria") or []
                    kept: list = []
                    for ac in acs:
                        if not isinstance(ac, dict):
                            kept.append(ac)
                            continue
                        ac_text = " ".join(str(ac.get(k, "") or "")
                                            for k in ("statement", "given", "when", "then"))
                        if is_login_flow_text(ac_text):
                            _r145_b_bypassed_count += 1
                            continue
                        kept.append(ac)
                    req["acceptance_criteria"] = kept
        except Exception as _r145_b_exc:
            import logging as _logging_r145_b
            _logging_r145_b.getLogger("arta.atdd").debug(
                "R145.B: AC filter skipped (%s)", _r145_b_exc,
            )
        self._r145_b_bypassed_count = _r145_b_bypassed_count
        import asyncio

        risk_by_req = {r["requirement_id"]: r for r in risk_profiles}

        tasks = [
            self._generate_for_requirement(req, risk_by_req.get(req["id"], {}), project_type)
            for req in requirements
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_ac = []
        all_gherkin = []
        test_ids = []

        import logging as _logging
        _log = _logging.getLogger("arta.atdd")

        for req, result in zip(requirements, results):
            if isinstance(result, Exception):
                # Robustly extract req_id whether req is dict or dataclass —
                # we may be raising BEFORE the canonical _to_plain_dict has
                # been applied (e.g. if a future caller forgets).
                if isinstance(req, dict):
                    req_id = req.get("id", "?")
                else:
                    req_id = getattr(req, "id", "?")
                _log.error(
                    "ATDD generation failed for %s (%s: %s)",
                    req_id, type(result).__name__, result,
                )
                # NO FALLBACK — per user policy, silent degradation is forbidden.
                # The @retry decorator on `_call_llm` already gave the LLM 3
                # attempts; if all 3 failed the requirement is genuinely
                # broken (malformed AC, prompt-injection content, exceeded
                # token budget, etc.) and a human must fix the input. Raise
                # so the caller sees the failure explicitly.
                raise RuntimeError(
                    f"ATDD generation failed for {req_id} after 3 attempts: "
                    f"{type(result).__name__}: {result}"
                ) from result
            all_ac.extend(result["acceptance_criteria"])
            all_gherkin.append(result["feature_file"])
            test_ids.extend(result.get("test_ids", []))

        fixtures = await self.generate_test_data_fixtures(all_gherkin, test_ids)

        return {
            "acceptance_criteria": all_ac,
            "gherkin_scenarios": all_gherkin,
            "test_data_fixtures": fixtures,
        }

    async def generate_test_data_fixtures(
        self,
        gherkin_scenarios: list[str],
        test_ids: list[str],
    ) -> list[dict]:
        """
        Parse Examples: tables from Gherkin feature files and produce companion
        JSON fixture objects for each Scenario Outline.

        Returns a list of fixture dicts:
          {
            "fixture_id": "TD-001",
            "test_id": "TC-124",
            "columns": ["card_type", "amount", "expected_status"],
            "rows": [["VISA", "99.99", "confirmed"], ...],
            "seeded_at": null
          }
        """
        fixtures: list[dict] = []
        fixture_counter = 1

        for i, feature_text in enumerate(gherkin_scenarios):
            test_id = test_ids[i] if i < len(test_ids) else f"TC-{1000 + i}"
            fixtures.extend(
                self._extract_examples_fixtures(feature_text, test_id, fixture_counter)
            )
            fixture_counter += len(fixtures)

        return fixtures

    def _extract_examples_fixtures(
        self, feature_text: str, test_id: str, start_counter: int
    ) -> list[dict]:
        """Extract all Examples: tables from a feature file."""
        fixtures: list[dict] = []
        counter = start_counter

        # Match Examples: blocks (table header + rows)
        examples_pattern = re.compile(
            r"Examples:\s*\n\s*(\|[^\n]+\|)\n((?:\s*\|[^\n]+\|\n?)+)",
            re.MULTILINE,
        )

        for match in examples_pattern.finditer(feature_text):
            header_line = match.group(1)
            rows_text = match.group(2)

            columns = [c.strip() for c in header_line.split("|") if c.strip()]
            rows: list[list[str]] = []

            for row_line in rows_text.splitlines():
                row_line = row_line.strip()
                if row_line.startswith("|"):
                    cells = [c.strip() for c in row_line.split("|") if c.strip()]
                    if cells and cells != columns:
                        rows.append(cells)

            if columns and rows:
                fixtures.append({
                    "fixture_id": f"TD-{counter:03d}",
                    "test_id": test_id,
                    "columns": columns,
                    "rows": rows,
                    "seeded_at": None,
                })
                counter += 1

        return fixtures

    async def _generate_for_requirement(
        self,
        req: dict,
        risk: dict,
        project_type: str = "web_app",
    ) -> dict:
        feature_parts: list[str] = []
        ac_records: list[dict] = []
        test_ids: list[str] = []

        # Batch ALL acceptance criteria into a single LLM call (saves ~80% tokens).
        # Coerce ACs to plain dicts at the boundary so all `.get()` calls below
        # work regardless of whether the caller passed dataclass instances.
        acs = [_ac_as_dict(ac) for ac in (req.get("acceptance_criteria", []) or [])]

        # R284 — label each AC by its REAL id, not a positional `AC-{i+1}`.
        #
        # This was the root of the chronic "zero `// AC:` comments" rejection.
        # ARTA handed the LLM `AC-1: <statement>` as the PRIMARY label while the
        # real id (`AC-OR-022-01`) appeared only parenthetically in the R254
        # block below — so the Gherkin carried BOTH forms and the model used the
        # prominent one, emitting `test('AC-1 | ...')`. The model was never
        # disobeying: it traced to the label we gave it. Downstream, the F1
        # validator then rejected specs for lacking traceability to an id it had
        # never been shown, and TWO prompt-level fixes (R274, R283) could not
        # help — the id simply was not there to use.
        #
        # this is a no-op wherever ids are absent.
        def _r284_label(i: int, ac: dict) -> str:
            return str(ac.get("id") or ac.get("ac_id") or f"AC-{i+1}")

        # Gen-time measurability weighting (never filtering): non-measurable
        # ACs are ANNOTATED in place — reordering measurable-first would corrupt
        # R284's positional labels for id-less ACs. Source order is traceability.
        # Killswitch ARTA_AC_MEASURABILITY_WEIGHT_DISABLE=1.
        _unmeasured_labels: list[str] = []
        if os.environ.get("ARTA_AC_MEASURABILITY_WEIGHT_DISABLE") != "1":
            try:
                from .upstream_quality import ac_measurability_flags
                # Import-time verdict FIRST (metadata.quality.ac_flags): R205
                # enrichment appends measurable clauses before we run, so the
                # heuristic alone would see enriched text and mark nothing —
                # the source-level truth is the stamped pre-enrichment verdict.
                _stamped = set(((req.get("metadata") or {}).get("quality") or {})
                               .get("ac_flags") or [])
                _unmeasured_labels = [
                    _r284_label(_i, _a) for _i, _a in enumerate(acs)
                    if isinstance(_a, dict)
                    and (_r284_label(_i, _a) in _stamped
                         or ac_measurability_flags([{**_a, "id": _r284_label(_i, _a)}]))
                ]
            except Exception:
                _unmeasured_labels = []

        def _meas_tag(i: int, ac: dict) -> str:
            return " [UNMEASURED]" if _r284_label(i, ac) in _unmeasured_labels else ""

        all_ac_statements = "\n".join(
            f"  {_r284_label(i, ac)}: {ac.get('statement', '')}{_meas_tag(i, ac)}"
            for i, ac in enumerate(acs)
        ) or "  AC-1: Core functionality works correctly"

        # R254 (WS2b) — the AC's STRUCTURED given/when/then.
        #
        # `all_ac_statements` above reads only `statement`, but
        # AcceptanceCriterion has carried given/when_/then_ columns all along.
        # The designer was handed a one-line summary of an AC whose structured
        # form was sitting right there — then told to produce Given/When/Then
        # from it. Reconstructing what was already known is exactly where
        # invention starts.
        _ac_gwt_lines: list[str] = []
        for _i, _a in enumerate(acs):
            _g, _w, _t = _a.get("given"), _a.get("when_") or _a.get("when"), _a.get("then_") or _a.get("then")
            if not (_g or _w or _t):
                continue
            # R284 — one canonical label. Was `AC-{i+1} (<real id>)`, which gave
            # the LLM two names for one criterion and it picked the positional
            # one. The id IS the label.
            _ac_gwt_lines.append(
                f"  {_r284_label(_i, _a)}:"
                + (f"\n    Given: {_g}" if _g else "")
                + (f"\n    When: {_w}" if _w else "")
                + (f"\n    Then: {_t}" if _t else "")
            )
        _ac_gwt = ""
        if _ac_gwt_lines:
            _ac_gwt = ("\nAC STRUCTURED FORM (authoritative — use these verbatim; "
                       "do NOT re-derive them):\n"
                       + "\n".join(_ac_gwt_lines)[:800] + "\n")

        # Phase A8 (per DR-9): recipe binding fires for analytics projects AND
        # for any web/mobile requirement with quantitative ACs. Pure-flow
        # requirements still take the standard prompt below — they don't need
        # data fixtures.
        # R212 — the project-type gate alone fires for EVERY req in an analytics
        # recipe-bound path and BLOCKS with RECIPE_INVALID when no recipe exists
        # (Stage 1.5 now skips the recipe for non-analytics reqs). Require the
        # REQUIREMENT to be analytics too — same is_analytics_requirement signal
        # the Stage 1.5 recipe gate uses (single source of truth, lazy import to
        # avoid the router↔agent cycle; conservative test_types/category
        # fallback). Quantitative-AC web/mobile reqs still get a recipe via
        # _requires_data_fixtures. Killswitch ARTA_R212_RECIPE_GATE_DISABLE=1.
        import os as _os_r212r
        _req_is_analytics = True
        if _os_r212r.environ.get("ARTA_R212_RECIPE_GATE_DISABLE", "").lower() not in ("1", "true"):
            try:
                from ..api.routers.tests import is_analytics_requirement as _is_analytics_req
                _req_is_analytics = bool(_is_analytics_req(req))
            except Exception:
                _req_is_analytics = (
                    any(str(t).strip().lower() == "analytics" for t in (req.get("test_types") or []))
                    or req.get("category") == "analytics_ai")
        # R219.X — the _requires_data_fixtures clause routed ANY quantitative-AC
        # req to the recipe-bound path, but a recipe is only ever PRODUCED for
        # analytics projects (project_type=="analytics" → Stage 1.5
        # DatasetRecipeAgent). On a NON-analytics SUT the recipe is always None
        # are FUNCTIONALLY testable ("assets count increases by N" = an API
        # assertion the LLM grounds from captured endpoints), so gate the WHOLE
        # recipe route on project_type=="analytics". For analytics projects this
        # is IDENTICAL to the prior logic — ((analytics AND _req_is_analytics)
        # OR _requires_data_fixtures) collapses to (_req_is_analytics OR
        # _requires_data_fixtures) when project_type=="analytics"; only
        # non-analytics projects change (dead recipe route → standard Gherkin).
        # Killswitch ARTA_R219X_RECIPE_PROJECT_GATE_DISABLE=1.
        if _os_r212r.environ.get("ARTA_R219X_RECIPE_PROJECT_GATE_DISABLE", "").lower() in ("1", "true"):
            needs_recipe = (
                (project_type == "analytics" and _req_is_analytics)
                or _requires_data_fixtures(req)
            )
        else:
            needs_recipe = (
                project_type == "analytics"
                and (_req_is_analytics or _requires_data_fixtures(req))
            )

        if needs_recipe:
            # Phase 1.7 — Recipe-bound prompt. The DatasetRecipeAgent (Phase 1.6)
            # runs upstream and stamps `req["dataset_recipe"]` as a serialised
            # DatasetRecipe dict. Two values flow into the prompt:
            #   1. canonical_path — single path used by every stage, eliminates
            #      the historical "NOT YET GENERATED" mismatch where ATDD wrote
            #      `req_am_020_fixture.parquet` but the generator wrote
            #      `fixtures/analytics/req_am_020_dataset_v1_0_0.parquet`.
            #   2. expected_outputs — the LLM is forbidden from inventing
            #      assertion values; it cites these verbatim. Phase 4
            #      closed-loop verification then proves the recipe's data
            #      actually produces these outputs through the pipeline.
            recipe = req.get("dataset_recipe") if isinstance(req.get("dataset_recipe"), dict) else None
            expected = (recipe or {}).get("expected_outputs") if recipe else None
            # Phase 5 follow-up #2b — the recipe Pydantic schema (Phase 5 #2a)
            # now requires non-empty `expected_outputs` and `columns`. Any
            # historical recipe / cross-process dict that slipped past that
            # validator with an empty contract is treated as STRUCTURALLY
            # INVALID here: emit a stub Gherkin marked `# RECIPE_INVALID`
            # rather than calling the LLM with a hollow contract. Without
            # this guard, ATDD would invent assertion values and re-create
            # the Gherkin↔data mismatch Phase 1 was built to fix.
            if recipe is None or not recipe.get("canonical_path") or not expected or not recipe.get("columns"):
                import logging as _log
                _log.getLogger("arta.atdd").warning(
                    "analytics Gherkin BLOCKED for %s — recipe missing or "
                    "structurally invalid (recipe=%s, has_path=%s, has_expected=%s, "
                    "has_columns=%s). Emitting RECIPE_INVALID stub instead of "
                    "letting the LLM hallucinate assertions.",
                    req.get("id", "?"),
                    bool(recipe),
                    bool(recipe and recipe.get("canonical_path")),
                    bool(expected),
                    bool(recipe and recipe.get("columns")),
                )
                _stub_tc_id = f"TC-{req['id'].upper().replace('-', '_')}-RECIPE-INVALID"
                stub_feature = (
                    f"# ARTA Auto-Generated · {req['id']}\n"
                    f"# RECIPE_INVALID — DatasetRecipe missing or empty contract\n"
                    f"# This stub blocks Gherkin generation until a valid recipe is\n"
                    f"# produced upstream. Re-run /api/tests/generate after\n"
                    f"# DatasetRecipeAgent succeeds for this requirement.\n"
                    f"\n"
                    f"Feature: {req.get('title', req['id'])}\n"
                    f"  # No scenarios — recipe contract was missing or empty.\n"
                )
                return {
                    "acceptance_criteria": [{
                        "id": "AC-RECIPE-INVALID",
                        "statement": "Recipe contract missing — generation blocked",
                        "requirement_id": req["id"],
                        "test_id": _stub_tc_id,
                        "scenario_count": 0,
                        "covered": False,
                    }],
                    "feature_file": stub_feature,
                    "test_ids": [_stub_tc_id],
                }
            fixture_path = recipe["canonical_path"]

            # Phase R5 — when recipe.expected_outputs_by_ac is populated
            # (R4.1 schema), build a per-AC expected_outputs block so the
            # LLM knows AC-1 should assert different values than AC-2.
            # When absent (legacy recipes, default empty dict), fall back
            # to the single requirement-level block — same behavior as
            # pre-R5. Per-AC fallback rule: an AC missing from the override
            # map uses the requirement-level expected_outputs.
            expected_by_ac = (recipe or {}).get("expected_outputs_by_ac") or {}

            if expected_by_ac:
                blocks: list[str] = []
                for ac in acs:
                    ac_id = ac.get("id", "")
                    ac_outputs = expected_by_ac.get(ac_id) or expected
                    block = (
                        f"For {ac_id}:\n"
                        + "\n".join(
                            f"  - {k} = {v!r}" for k, v in ac_outputs.items()
                        )
                    )
                    blocks.append(block)
                expected_outputs_block = "\n\n".join(blocks)
                expected_outputs_intro = (
                    "Each AC has its OWN expected_outputs (mapped below). "
                    "Every Then/And step in `Scenario: AC-N` MUST cite "
                    "values from AC-N's block, NOT from a different AC's "
                    "block."
                )
            else:
                # Pre-R4 recipes: single block, all scenarios cite same values
                expected_outputs_block = "\n".join(
                    f"  - {k} = {v!r}" for k, v in expected.items()
                )
                expected_outputs_intro = (
                    "Every Then/And step asserting on `insight.*` MUST cite "
                    "a value verbatim from this list."
                )

            prompt = ANALYTICS_GHERKIN_PROMPT.format(
                req_id=req["id"],
                req_title=req.get("title", ""),
                priority=risk.get("priority", "P2"),
                risk_score=risk.get("risk_score", 5.0),
                ac_statement=all_ac_statements,
                entities=", ".join(req.get("entities", [])),
                fixture_path=fixture_path,
                expected_outputs_intro=expected_outputs_intro,
                expected_outputs_block=expected_outputs_block,
            )
        else:
            # R254 (WS2b) — GROUND the designer.
            #
            # Pre-WS2b this call passed 8 fields and `req["description"]` was
            # not among them: the designer never saw the requirement's own
            # text, only a one-line AC summary. It was asked to invent
            # verifiable scenarios for a requirement it could not read — so it
            # invented. Everything added here is HARD-CAPPED (<=4000 chars
            # total): the documented 52.9%->38.6% regression came from bulk
            # prompt injection, so grounding that grows without bound trades one
            # failure mode for another.
            # Killswitch ARTA_R254_DESIGN_GROUNDING_DISABLE=1 -> pre-WS2b prompt.
            _ctx = self._r254_design_context(req, risk, ac_gwt=_ac_gwt)
            prompt = GHERKIN_GEN_PROMPT.format(
                req_id=req["id"],
                req_title=req.get("title", ""),
                priority=risk.get("priority", "P2"),
                risk_score=risk.get("risk_score", 5.0),
                ac_statement=all_ac_statements,
                entities=", ".join(req.get("entities", [])),
                constraints=", ".join(req.get("constraints", [])),
                data_patterns=req.get("data_patterns", "standard web app"),
                **_ctx,
            )

        # R19c — append the project's DOM-selector catalog so the LLM
        # can ONLY emit testids that actually exist in the SUT. No-op
        # when discovery never ran or this project has no catalog.
        _selector_block = self._build_available_selectors_block(
            risk.get("project_id") or ""
        )
        if _selector_block:
            prompt = prompt + "\n\n" + _selector_block

        # Phase 2 — augment the test CASE with the SUT architecture (Phase 1
        # discovery graphs). The directive: when the requirement/ACs are weak
        # or unclear, ARTA scores+highlights it AND — if source is available —
        # grounds the generated case in the real SUT so it stays correct.
        # No-op when Architecture Discovery hasn't run for this project.
        # R254 (WS2b) — this except used to be a bare `pass`, which made
        # design-time SUT grounding fail INVISIBLY: when it threw, the LLM
        # designed blind and R213 then reported `grounded=0.0%` as though the
        # scenarios were bad, when the truth was that grounding never ran. A
        # silent degradation that gets scored as a quality result is worse than
        # no score — it sends the operator to fix the wrong thing.
        _grounding_available = bool(_selector_block)
        try:
            from .upstream_quality import requirement_clarity_score, build_source_augmentation
            _clarity = requirement_clarity_score(req)
            _aug = build_source_augmentation(
                req, risk.get("project_id") or "", clarity_band=_clarity.get("band"))
            if _aug:
                prompt = prompt + "\n\n" + _aug
                _grounding_available = True
        except Exception as _aug_exc:
            log.warning(
                "R254: design-time SUT grounding unavailable for %s: %s — the "
                "LLM is designing WITHOUT SUT context; a low R213 grounded_pct "
                "for this requirement reflects THAT, not the scenarios",
                req.get("id"), _aug_exc,
            )

        # Measurability constraint for the [UNMEASURED]-tagged ACs above:
        # cover them, but never pad them with invented thresholds.
        if _unmeasured_labels:
            prompt += (
                "\n\n# AC MEASURABILITY (weighting — not filtering)\n"
                f"ACs marked [UNMEASURED] ({', '.join(_unmeasured_labels[:12])}) state no "
                "measurable criterion in the source. Still cover each with a scenario, but "
                "assert ONLY what the AC states: never invent numeric thresholds, status "
                "codes, or exact values the source does not state — use presence/visibility/"
                "read-side observable checks instead.\n"
            )

        # R213.D — scale max_tokens by AC count. The flat 8192 budget truncates
        # merged feature ends up with zero line-anchored Scenario blocks → the
        # upstream Gherkin gate (GQ-002) blocks generation entirely (0 tests).
        # ~1500 tokens/AC headroom over a 5000 base, capped at 16384 (the
        # model ceiling). Low-AC reqs are unchanged (≤5 ACs still ≈ 8192-ish).
        # Killswitch ARTA_R213_D_ATDD_TOKEN_SCALE_DISABLE=1 restores flat 8192.
        import os as _os_r213d
        if _os_r213d.environ.get("ARTA_R213_D_ATDD_TOKEN_SCALE_DISABLE") == "1":
            _max_tokens = 8192
        else:
            _max_tokens = min(16384, max(8192, 5000 + 1500 * len(acs or [])))
        message = await self._call_llm(
            model=self._model,
            max_tokens=_max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        feature_text = message.content[0].text.strip()
        # Strip LLM reasoning blocks. Qwen / DeepSeek-class models emit
        # `<think>…</think>` reasoning before the answer; the closing tag
        # sometimes appears as an orphan when the opener was suppressed.
        # Without removing both forms, tags get written verbatim into .feature
        # files (verified live in run-7c5ff8 where `</think>` ended up in a
        # k6 file at line 239).
        # Phase A9: tolerate truncated reasoning. When max_tokens is hit
        # mid-thinking, the closing `</think>` is absent — without the
        # `(?:...|$)` alternation the orphan opener leaks raw reasoning into
        # the feature file. Strip from `<think>` to either its closer OR EOF.
        feature_text = re.sub(
            r"<think>[\s\S]*?(?:</think>|$)", "", feature_text, flags=re.IGNORECASE,
        )
        feature_text = re.sub(r"</?think>", "", feature_text, flags=re.IGNORECASE)
        feature_text = feature_text.strip()
        # Strip markdown fences if LLM wrapped the output
        if feature_text.startswith("```"):
            lines = feature_text.split("\n")
            lines = lines[1:]  # skip ```gherkin or ```
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            feature_text = "\n".join(lines).strip()
        feature_parts.append(feature_text)

        # Count scenarios generated from the batched response
        scenario_count = len(re.findall(
            r"^\s*Scenario(?:\s+Outline)?:", feature_text, re.MULTILINE
        ))

        # Per-AC truncation recovery: if the batched LLM response produced
        # fewer scenarios than ACs, the response was almost certainly truncated
        # before reaching the trailing ACs. Recover by calling the LLM per-AC
        # for the missing tail. Previously this was silent — the AC records
        # below would claim coverage (scenario_count // len(acs) ≥ 1) but the
        # feature file contained zero scenarios for those ACs.
        if project_type != "analytics" and acs and scenario_count < len(acs):
            missing_count = len(acs) - scenario_count
            missing_acs = acs[-missing_count:]
            import logging as _log_recovery
            _log_recovery.getLogger("arta.atdd").warning(
                "ATDD for %s: batched response has %d scenarios for %d ACs — "
                "recovering %d trailing ACs per-AC",
                req["id"], scenario_count, len(acs), missing_count,
            )
            for ac in missing_acs:
                try:
                    ac_prompt = GHERKIN_GEN_PROMPT.format(
                        req_id=req["id"],
                        req_title=req.get("title", ""),
                        priority=risk.get("priority", "P2"),
                        risk_score=risk.get("risk_score", 5.0),
                        ac_statement=f"  AC: {ac.get('statement', '')}",
                        entities=", ".join(req.get("entities", [])),
                        constraints=", ".join(req.get("constraints", [])),
                        data_patterns=req.get("data_patterns", "standard web app"),
                    )
                    # R19c — same selector grounding on per-AC recovery path.
                    _ac_sel_block = self._build_available_selectors_block(
                        risk.get("project_id") or ""
                    )
                    if _ac_sel_block:
                        ac_prompt = ac_prompt + "\n\n" + _ac_sel_block
                    ac_msg = await self._call_llm(
                        model=self._model,
                        max_tokens=4096,
                        messages=[{"role": "user", "content": ac_prompt}],
                    )
                    ac_feature = ac_msg.content[0].text.strip()
                    # Strip thinking tags before fence handling (see line 327)
                    ac_feature = re.sub(r"<think>[\s\S]*?</think>", "", ac_feature, flags=re.IGNORECASE)
                    ac_feature = re.sub(r"</?think>", "", ac_feature, flags=re.IGNORECASE)
                    ac_feature = ac_feature.strip()
                    if ac_feature.startswith("```"):
                        _lines = ac_feature.split("\n")[1:]
                        if _lines and _lines[-1].strip() == "```":
                            _lines = _lines[:-1]
                        ac_feature = "\n".join(_lines).strip()
                    if "Scenario" in ac_feature:
                        feature_parts.append(ac_feature)
                except Exception as _ac_exc:
                    _log_recovery.getLogger("arta.atdd").warning(
                        "ATDD per-AC recovery for %s/%s failed (%s): fallback to AC-stub Gherkin",
                        req["id"], ac.get("id", "?"), _ac_exc,
                    )
            # Re-count scenarios after recovery
            combined_for_count = "\n".join(feature_parts)
            scenario_count = len(re.findall(
                r"^\s*Scenario(?:\s+Outline)?:", combined_for_count, re.MULTILINE
            ))

        # Phase 1.9 — monotonic test_id scoped per requirement.
        # The previous hash-mod-900 scheme had ~50% collision probability at
        # 25 tests (birthday bound). Per-requirement counter is collision-free
        # within a requirement and stable across regenerations (same AC ids
        # produce the same TC ids), so traceability links don't churn.
        # Format: TC-{REQ_SLUG}-{NN}  e.g. "TC-REQ_AM_020-01"
        req_slug = re.sub(r"[^A-Z0-9_]", "", req["id"].upper().replace("-", "_"))
        for idx, ac in enumerate(acs, start=1):
            tc_id = f"TC-{req_slug}-{idx:02d}"
            test_ids.append(tc_id)

            ac_records.append({
                **ac,
                "requirement_id": req["id"],
                "test_id": tc_id,
                "scenario_count": max(1, scenario_count // max(len(acs), 1)),
                "covered": False,   # Will become True after first passing run
                "generated_by": "arta-atdd-agent",
                "project_type": project_type,
            })

        # Combine all ACs into one feature file
        combined_feature = self._merge_feature_files(req, feature_parts)

        # F8-5: Deduplicate Scenario blocks. Two paths produce duplicates:
        # (a) the @retry on _call_llm sometimes succeeds on attempt 2 with text
        #     that was partially returned on attempt 1 (network truncation); and
        # (b) the LLM occasionally emits two near-identical Scenario blocks for
        #     a single AC. Both are silent until execution sees N copies of the
        #     same TC firing. Hash-based dedup drops byte-identical scenarios.
        combined_feature = self._dedup_scenarios(combined_feature, req["id"])

        # Hard correctness check — NO fallback. If after merge + per-AC recovery
        # there's still no Scenario, the LLM genuinely failed. Raise so the
        # outer @retry sees the failure (and the caller propagates a hard error
        # to the user) instead of writing a stub feature file that no one will
        # look at twice. Per user policy: silent degradation is forbidden.
        # R213.D — use the SAME line-anchored regex the downstream Gherkin gate
        # (upstream_quality GQ-002, atdd line ~714) uses to COUNT scenarios.
        # The prior substring check (`"Scenario:" in text`) passed when the
        # feature only contained "Scenario:" in prose / a comment / leaked
        # reasoning, so ATDD returned a feature with ZERO real Scenario blocks
        # instead lets the @retry re-attempt generation rather than shipping a
        # doomed feature past this check.
        _real_scenarios = re.findall(
            r"^\s*Scenario(?:\s+Outline)?\s*:", combined_feature, re.MULTILINE
        )
        if not _real_scenarios:
            raise RuntimeError(
                f"ATDD for {req['id']}: merged feature contains zero Scenario steps "
                f"after batched + per-AC recovery passes. The LLM did not produce "
                f"valid Gherkin. Inspect the AC text for prompt-injection content, "
                f"missing fields, or content that exceeds the token budget."
            )

        return {
            "acceptance_criteria": ac_records,
            "feature_file": combined_feature,
            "test_ids": test_ids,
        }

    @retry(
        # R134.C — single-source-of-truth retry tuple covers anthropic.* SDK
        # errors, RuntimeError (ClaudeCLI/Ollama subprocess failures), AND
        # httpx transient network errors (ConnectError, ReadTimeout, etc.).
        retry=retry_if_exception_type(LLM_RETRYABLE_EXC),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _call_llm(self, **kwargs):
        """Wrapper around messages.create() with exponential-backoff retry + circuit breaker."""
        # I8: Route through circuit breaker so a degraded provider fails fast.
        from .circuit_breaker import get_breaker
        provider_name = getattr(self._client, "provider", None) or type(self._client).__name__
        breaker = get_breaker(str(provider_name))
        # R215.T (extended) — ATDD Gherkin gen is a SINGLE-SHOT structured-output
        # call; ARTA already injects all needed context (Tier-1), so the CLI's tools
        # are never needed. Leaving them EXPOSED (the pre-fix default) let the model
        # attempt a tool call on a "generate the scenarios" prompt → it burned the
        # whole `--max-turns` budget → returned EMPTY/truncated ("ATDD attempt 1/3
        # failed: empty" retries, 300s CLI timeouts). Same root cause + fix already
        # applied to strategy_architect / requirement_intel / defect_intel; the two
        # highest-volume generators (ATDD here + PW gen) were missed. Guarded to the
        # CLI transport (SDK paths reject the unknown kwarg). Caller override wins.
        if type(getattr(self, "_client", None)).__name__ == "ClaudeCLIClient":
            kwargs.setdefault("disable_tools", True)
        return await breaker.call(self._client.messages.create, **kwargs)

    def _dedup_scenarios(self, feature_text: str, req_id: str) -> str:
        """F8-5: Drop Scenario/Scenario Outline blocks whose normalised body is
        byte-identical to one already seen in this feature.

        Normalisation strips leading whitespace on each step line and lowercases
        tag names so minor formatting jitter doesn't defeat the check.
        """
        import hashlib
        import logging as _logging
        _log = _logging.getLogger("arta.atdd")

        lines = feature_text.splitlines(keepends=False)
        # Walk the file, collect (prefix_block, scenario_blocks[]) using the same
        # scanner as _merge_feature_files so behaviour stays consistent.
        header_lines: list[str] = []
        scenario_blocks: list[list[str]] = []
        current: list[str] = []
        in_scenario = False
        for line in lines:
            if re.match(r"^\s*(Scenario|Scenario Outline)\s*:", line):
                if in_scenario and current:
                    scenario_blocks.append(current)
                current = [line]
                in_scenario = True
            elif in_scenario:
                current.append(line)
            else:
                header_lines.append(line)
        if in_scenario and current:
            scenario_blocks.append(current)

        if len(scenario_blocks) <= 1:
            return feature_text  # nothing to dedup

        seen: set[str] = set()
        unique_blocks: list[list[str]] = []
        dropped = 0
        for block in scenario_blocks:
            normalised = "\n".join(ln.strip() for ln in block if ln.strip())
            digest = hashlib.sha1(normalised.encode("utf-8")).hexdigest()
            if digest in seen:
                dropped += 1
                continue
            seen.add(digest)
            unique_blocks.append(block)

        if dropped == 0:
            return feature_text

        _log.info("ATDD dedup (%s): dropped %d duplicate Scenario block(s)", req_id, dropped)
        rebuilt = "\n".join(header_lines).rstrip() + "\n\n"
        rebuilt += "\n\n".join("\n".join(b) for b in unique_blocks) + "\n"
        return rebuilt

    def _merge_feature_files(self, req: dict, feature_parts: list[str]) -> str:
        """Merge multiple AC feature files into one cohesive feature file."""
        header = (
            f"# ARTA Auto-Generated · {req['id']}\n"
            f"# Risk: {req.get('risk_score','?')} · Priority: {req.get('priority','?')}\n"
            f"# DO NOT EDIT — managed by ARTA ATDD Agent\n\n"
            f"Feature: {req.get('title', 'Feature')}\n"
            f"  # {req.get('description', '')}\n\n"
        )

        # Extract scenarios from each part (skip duplicate Feature: headers)
        all_scenarios: list[str] = []
        for part in feature_parts:
            lines = part.splitlines()
            in_scenario = False
            scenario_lines: list[str] = []

            for line in lines:
                if re.match(r"^\s*(Scenario|Background|Rule)", line):
                    if scenario_lines:
                        all_scenarios.append("\n".join(scenario_lines))
                    scenario_lines = [line]
                    in_scenario = True
                elif in_scenario:
                    scenario_lines.append(line)

            if scenario_lines:
                all_scenarios.append("\n".join(scenario_lines))

        body = "\n\n".join(f"  {s}" for s in all_scenarios)
        return header + body
