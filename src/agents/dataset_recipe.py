"""Phase 1.6 — DatasetRecipeAgent: the upstream truth-bearer for analytics tests.

The pipeline historically had three independent stages making locally-valid but
globally-inconsistent decisions: ATDD designer wrote Gherkin asserting on
`insight.metric=="sales"`, AnalyticsTestAgent declared a hardcoded 7-column
schema, and the generator emitted random rows. Nothing reconciled them, so the
generated tests asserted on values the data couldn't produce.

`DatasetRecipeAgent.design(req, risk_profile) -> DatasetRecipe` produces a
single artifact that both the Gherkin generator (Phase 1.7) and the data
generator (Phase 2) consume. Invariant: every value in `recipe.expected_outputs`
is what the analytics pipeline would compute when given data shaped by
`recipe.columns + recipe.trends`. Phase 4 closes the loop by actually running
the pipeline + verifying.

The recipe is:
  - LLM-drafted (Haiku) from the requirement + structured measurable triples
    extracted in Phase 1.2.
  - Validated against a fixed catalogue of distribution primitives so the LLM
    can't invent unsupported shapes.
  - Persisted twice: as `.arta/recipes/{req_id}_v{ver}.json` (audit/replay
    sidecar, mirrors strategy_architect's persist pattern) and on
    `TestCase.metadata_["dataset_recipe"]` (replay).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field, ValidationError, field_validator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .retry_policy import LLM_RETRYABLE_EXC   # R134.C — single-source-of-truth retry tuple

from .sanitize import sanitize_req_id  # R134.H — sanitize_req_id SSoT

log = logging.getLogger("arta.recipe")


# ── Distribution-primitive catalogue ─────────────────────────────────────────
# The LLM is constrained to these names. Phase 2 implements each in
# fixtures/generator.py; Phase 4 attaches a `solve_for(target)` to the ones
# that admit closed-form correction.

DistributionShape = Literal[
    "monotonic_up",        # value rises over `over` axis with magnitude_pct
    "monotonic_down",      # mirror
    "categorical_weighted",  # biased enum
    "time_range",          # ordered timestamps over [start, end] at freq
    "constant",            # single fixed value (e.g. metric_label="sales")
    "random_normal",       # gaussian with mean/std (no closed-form solve)
    "derived",             # computed from other columns (e.g. revenue=qty*price)
]


class ColumnSpec(BaseModel):
    """One column's schema + how to populate it. Distribution is required so
    the generator never has to fall back to its column-name heuristic."""
    name: str = Field(min_length=1, max_length=64)
    # R38.6 — added "boolean" so analytics requirements that include
    # binary signal columns (active flags, opt-in indicators, churn
    # markers) can land in the recipe. Without boolean, the LLM
    # generates such columns as kind="categorical" with values [True,
    # False] which the Pydantic enum rejected → recipe validation
    # failed → no fixture → uncovered analytics ACs. Boolean assertions
    # use strict equality (no tolerance) — see arta_runtime.tolerant_assert.
    dtype: Literal["date", "number", "string", "categorical", "boolean"]
    distribution: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, v: str) -> str:
        # Lowercase snake_case ASCII — produces clean parquet/CSV column headers.
        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError(f"column name {v!r} must be lowercase snake_case")
        return v

    @field_validator("distribution")
    @classmethod
    def _shape_in_catalogue(cls, v: dict[str, Any]) -> dict[str, Any]:
        shape = v.get("shape")
        if shape is None:
            # Empty distribution is allowed for trivially-typed columns; the
            # generator falls back to dtype defaults. Still safer to require
            # a shape so we never produce uninstrumented columns silently.
            return v
        valid = {
            "monotonic_up", "monotonic_down", "categorical_weighted",
            "time_range", "constant", "random_normal", "derived",
        }
        if shape not in valid:
            raise ValueError(
                f"unknown distribution shape {shape!r} (valid: {sorted(valid)})"
            )
        return v


class TrendSpec(BaseModel):
    """A target trend the data must demonstrate. Used by Phase 4 closed-loop
    verification: after generating data, the pipeline runs and we check that
    grouping `over` and aggregating `column` produces a `magnitude_pct` change
    matching the recipe."""
    column: str
    over: str  # the axis column (usually time)
    shape: Literal["monotonic_up", "monotonic_down"]
    magnitude_pct: float


class ACPartition(BaseModel):
    """Phase 4 — one partition of the fixture for a specific AC. When a
    requirement has ACs with conflicting data needs (AC-1 wants up-trend,
    AC-2 wants down-trend), each gets its own partition; the fixture has a
    `partition_id` column and the Gherkin scenario filters before asserting.
    v1 (Phase 1.6) emits a single partition; per-AC partitions land in Phase 4.
    """
    ac_id: str
    filter: str | None = None
    expected_outputs: dict[str, Any] = Field(default_factory=dict)


class DatasetRecipe(BaseModel):
    version: str = "1.0.0"
    requirement_id: str
    canonical_path: str  # ONE path used by every stage (kills the path-mismatch bug)
    row_count: int = Field(default=10_000, ge=10, le=10_000_000)
    # Phase 5 follow-up #2a — both columns and expected_outputs are required
    # non-empty for analytics recipes. An empty `columns` list would force
    # the generator into the legacy heuristic path; an empty `expected_outputs`
    # would let the ATDD prompt fall through to free-form LLM imagination,
    # re-introducing the Gherkin↔data mismatch Phase 1 was built to fix.
    columns: list[ColumnSpec] = Field(min_length=1)
    trends: list[TrendSpec] = Field(default_factory=list)
    expected_outputs: dict[str, Any]  # the values Gherkin Then/And steps cite verbatim

    # Phase R4.1 — per-AC overrides (optional). When populated, ATDD
    # generates distinct Then-blocks per scenario (R5). When empty
    # (default), all scenarios share `expected_outputs` — the original
    # pre-R behavior. Backward-compat: existing recipes have empty
    # default → flow through unchanged.
    expected_outputs_by_ac: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Per-AC override of expected_outputs. Keys are AC ids "
            "(e.g. 'AC-AM-002-01'); values mirror expected_outputs shape. "
            "Use when ACs target distinct entities/states (e.g. AC-1 "
            "creates an Org with credits, AC-2 creates a Workspace with "
            "role, AC-3 creates a Project with permissions). Leave "
            "empty when ACs are smoke variants of the same behavior."
        ),
    )

    partitions: list[ACPartition] = Field(default_factory=list)
    rationale: str  # 1-2 sentence LLM justification
    measurable_inputs: list[dict[str, Any]] = Field(default_factory=list)
    discovered_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Phase 4 — closed-loop verification metadata. Populated by recipe_verifier
    # before the recipe is persisted; auditors can see exactly how the loop ran.
    verification_attempts: int = 0
    verification_strategy: str = ""  # "closed_form" | "iterative" | "skipped_no_expected_outputs"
    actual_outputs: dict[str, Any] = Field(default_factory=dict)
    verification_failed: bool = False
    verification_residual_gaps: list[dict[str, Any]] = Field(default_factory=list)
    # T3 — expected_outputs keys whose value was made ORACLE-BACKED (computed from the
    # generated rows by the verifier) rather than kept as the LLM's guess. Empty when the
    # recipe had no string/categorical expected keys or T3 sync was disabled.
    t3_computed_expected_keys: list[str] = Field(default_factory=list)

    # Phase R4.3 — per-AC verification residual. Populated by recipe_verifier
    # when expected_outputs_by_ac is non-empty. Keys = AC ids whose contract
    # has drift between expected_outputs_by_ac[ac_id] and actual_outputs.
    # Empty dict when (a) all ACs match, (b) no per-AC contract exists, or
    # (c) verifier hasn't run yet. Advisory only — does NOT block recipe
    # use; surfaced for operator review via the dashboard.
    verification_failed_by_ac: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Per-AC contract drift map. Each entry is "
            "{ac_id: {key: {expected, actual}}} when expected_outputs_by_ac "
            "is populated and the actual pipeline outputs don't match. "
            "Empty when no drift OR no per-AC contract."
        ),
    )

    # R55.12 — open-loop grounding warnings. Each entry:
    # {kind: "recipe_column_not_in_sut_shape", symbol: str, location: str}
    # WARN-only — operator inspects via the dashboard; never blocks recipe
    # persistence. Empty when no warnings or when captured_endpoints
    # signal isn't available yet (cold-start). Populated by ATDD's
    # design() method after recipe_verifier (closed-loop) completes.
    grounding_warnings: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "R55.12 — recipe columns/expected_outputs flagged because "
            "the symbol is absent from all captured SUT response shapes "
            "AND doesn't match pipeline-internal column prefixes "
            "(nl_to_*, result_to_*, etc.). Cold-start (no captured) → []."
        ),
    )

    @field_validator("canonical_path")
    @classmethod
    def _under_fixtures(cls, v: str) -> str:
        # Defence in depth — this path is consumed by the generator and the
        # download endpoint, so any escape (..) would expand the blast radius.
        if not v.startswith("fixtures/") or ".." in v:
            raise ValueError(f"canonical_path must start with 'fixtures/' and have no '..': {v!r}")
        return v

    @field_validator("expected_outputs")
    @classmethod
    def _expected_outputs_present(cls, v: dict[str, Any]) -> dict[str, Any]:
        # Phase 5 follow-up #2a — empty expected_outputs is structurally
        # invalid for analytics recipes. The ATDD prompt cites these values
        # verbatim; an empty dict means the LLM has nothing to ground its
        # Then/And assertions in, and falls back to imagination. Reject at
        # validation time rather than letting downstream silently degrade.
        if not v:
            raise ValueError(
                "expected_outputs must be non-empty for analytics recipes — "
                "the ATDD prompt cites these values verbatim. An empty dict "
                "would let the LLM invent assertion values and re-introduce "
                "the Gherkin↔data mismatch Phase 1 was built to fix."
            )
        return v

    @field_validator("expected_outputs_by_ac")
    @classmethod
    def _by_ac_keys_or_empty(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        # Phase R4.1 — empty dict (default) is OK; non-empty entries must
        # all be non-empty dicts. An empty inner dict would be the per-AC
        # equivalent of the structurally-invalid case _expected_outputs_present
        # rejects — the AC has no contract to assert on.
        for ac_id, outputs in v.items():
            if not isinstance(outputs, dict) or not outputs:
                raise ValueError(
                    f"expected_outputs_by_ac[{ac_id}] must be a non-empty dict "
                    f"(got {outputs!r}). An empty per-AC contract has nothing "
                    f"for the ATDD prompt to cite — leave the AC out of the "
                    f"map (it falls back to requirement-level expected_outputs) "
                    f"OR populate it with the AC-specific assertion values."
                )
        return v


# ── Agent ────────────────────────────────────────────────────────────────────


_RECIPE_PROMPT = """\
You are a Test Data Architect for an analytics ATDD platform. Given a single \
requirement and its acceptance criteria (with structured measurable thresholds \
already extracted), design a DatasetRecipe that lets the data generator \
produce rows which, when fed through the analytics pipeline, satisfy the \
asserted numeric/qualitative values.

REQUIREMENT:
  id: {req_id}
  title: {title}
  description: {description}

ACCEPTANCE CRITERIA (with structured measurable triples — cite these in expected_outputs):
{ac_block}

RISK PROFILE:
  priority: {priority}
  test_types: {test_types}

CONSTRAINTS YOU MUST RESPECT:

  1. canonical_path MUST equal: "fixtures/analytics/{req_slug}_dataset_v1_0_0.parquet"
     This is the single path every downstream stage uses.

  2. expected_outputs is the CONTRACT between Gherkin and data. Every value here
     will be cited verbatim in a Then/And step (`Then insight.metric == "sales"`).
     Pull values from the measurable triples — DO NOT INVENT new magnitudes.

  2b. expected_outputs_by_ac (NEW, optional per-AC overrides):
     When ACs target DISTINCT behaviors (e.g., AC-1 creates an Org with credits;
     AC-2 creates a Workspace with role; AC-3 creates a Project with permissions),
     emit a per-AC dict so each Scenario asserts on its specific outputs.
     When ACs are smoke variants of the same behavior (edge cases differing only
     by input data), leave the field EMPTY ({{}}) and rely on expected_outputs.

     HEURISTIC for when to populate vs leave empty:
       - if ≥2 ACs would assert on the SAME `insight.metric` value → empty {{}}
       - if each AC tests a different entity (Org / Workspace / Project), state
         (active / expired / denied), or scope (single-user / team / org) → populate

     EXAMPLE for diverse ACs (Org-Workspace-Project hierarchy):
       "expected_outputs_by_ac": {{
         "AC-AM-002-01": {{"insight.metric": "credit_balance",  "insight.org_id": "org_1"}},
         "AC-AM-002-02": {{"insight.metric": "workspace_role",  "insight.workspace_id": "ws_1"}},
         "AC-AM-002-03": {{"insight.metric": "project_visibility", "insight.project_id": "proj_1"}}
       }}

     EXAMPLE for smoke-variant ACs (leave empty, rely on expected_outputs):
       "expected_outputs_by_ac": {{}}

  3. distribution.shape ∈ {{monotonic_up, monotonic_down, categorical_weighted,
     time_range, constant, random_normal, derived}}. No other names.

  4. Each TrendSpec in `trends` corresponds to ONE assertion in expected_outputs
     (e.g. `insight.magnitude_pct == 12.5` ↔ trend on the value column with
     magnitude_pct=12.5).

  5. column names: lowercase snake_case ASCII only.

  6. A column producing a constant scalar (e.g. metric_label="sales") uses
     distribution = {{"shape":"constant", "value":"sales"}}.

  7. A time-series trend uses distribution = {{"shape":"monotonic_up",
     "magnitude_pct": <num from measurable>, "over": "<date_column_name>"}}.

  8. row_count: pick a reasonable value (1k–100k) given the trend shape. Smaller
     for unit-style assertions; larger when statistical aggregations are tested.

[CRITICAL] Output ONLY the JSON object below. No markdown fences, no prose, no
preamble. Start with {{ and end with }}.

{{
  "version": "1.0.0",
  "requirement_id": "{req_id}",
  "canonical_path": "fixtures/analytics/{req_slug}_dataset_v1_0_0.parquet",
  "row_count": <int>,
  "columns": [
    {{"name": "...", "dtype": "date|number|string|categorical",
      "distribution": {{"shape": "...", ...}}}}
  ],
  "trends": [
    {{"column": "...", "over": "...", "shape": "monotonic_up|monotonic_down",
      "magnitude_pct": <num>}}
  ],
  "expected_outputs": {{"<dotted.key>": <value cited from measurable>, ...}},
  "expected_outputs_by_ac": {{}},
  "rationale": "1-2 sentence why this recipe satisfies the ACs"
}}
"""


class RecipeGroundingException(ValueError):
    """R150.C — raised when recipe gen produces >threshold R55.12 warnings
    AND post-verifier `verification_failed=True`. Subclass of ValueError so
    the existing caller contract at design() (fail-fast on bad recipe) is
    preserved verbatim. Carries `warning_count`, `samples` (top-5 warning
    summaries), and `operator_remediation` for downstream BLOCKED-row
    construction.

    Pre-R150.C: R55.12 warnings were advisory-only — recipes with 48
    `recipe_column_not_in_sut_shape` warnings persisted silently to disk
    and the LLM downstream gen consumed invented column names. The recipe
    verifier early-exit (R130.H) prevented runaway iteration, but the
    output STILL polluted ATDD + PW gen with hallucinated field paths.

    Post-R150.C: when warnings exceed threshold AND verification_failed,
    recipe gen fails loud. Operator sees actionable CTA naming the
    discovery-refresh path (R150.A populated shapes) OR the auth_bypass
    activation path (R150.D agent_token read fix).

    Killswitch: `ARTA_R150_C_RECIPE_BLOCK_DISABLE=1` reverts to advisory
    only (matches Iter 9 behavior for emergency rollback).

    Threshold tunable via `ARTA_R150_C_WARNING_THRESHOLD=N` (default 5).
    """

    def __init__(
        self, *, req_id: str, warning_count: int, samples: list[dict],
        verification_failed: bool, threshold: int,
    ):
        self.req_id = req_id
        self.warning_count = warning_count
        self.samples = samples
        self.verification_failed = verification_failed
        self.threshold = threshold
        self.operator_remediation = (
            f"Recipe {req_id!r} contains {warning_count} ungrounded columns "
            f"(threshold={threshold}) AND post-verifier verification_failed=True. "
            "ARTA refuses to persist this recipe because the downstream gen "
            "would emit Gherkin + PW assertions citing fields the SUT does "
            "not return. Remediation: "
            "(1) operator pastes a fresh session token → R45.3 discovery refresh "
            "    re-captures HAR with `mode: 'full'` (R150.A) → response_body_shape "
            "    populates → R130.H + R150.B hint blocks gain real signal; "
            "(2) verify `project.environments[env].variables.agent_token` "
            "    length ≥ 50 (R96.1 token-exchange minted) so R145.B "
            "    auto-detect activates the login-flow AC filter (R150.D fix); "
            "(3) for emergency rollback: set "
            "    `ARTA_R150_C_RECIPE_BLOCK_DISABLE=1`."
        )
        super().__init__(self.operator_remediation)


class RecipeCoherenceError(ValueError):
    """T1 — raised when a drafted recipe is internally INCOHERENT: the {columns,
    trends, expected_outputs} triple contradicts itself independent of any SUT
    grounding. These are deterministic authoring bugs (duplicate columns, a trend
    over a column that doesn't exist) that would ship a fixture the pipeline can't
    reconcile — one live recipe shipped 3 identical `insight_metric` columns. Fail-fast at
    design() so an incoherent recipe never reaches ATDD/generator; caller regenerates.
    Killswitch ARTA_T1_COHERENCE_DISABLE=1 reverts to advisory (log-only)."""

    def __init__(self, req_id: str, violations: list[dict]):
        self.req_id = req_id
        self.violations = violations
        summary = "; ".join(f"{v['kind']}:{v['detail']}" for v in violations[:5])
        super().__init__(f"recipe {req_id!r} incoherent ({len(violations)}): {summary}")


def validate_recipe_coherence(recipe: "DatasetRecipe") -> list[dict]:
    """T1 — deterministic internal-coherence checks on a drafted recipe. Returns a
    list of violation dicts (empty == coherent). ONLY unambiguous authoring bugs are
    flagged (never SUT-grounding — that's R55.12) so this can BLOCK without false
    positives:
      (a) column-name UNIQUENESS — duplicate names collapse in the DataFrame, so the
          asserted column is ambiguous (the live 3× insight_metric case).
      (b) every TrendSpec.column AND .over resolves to a real column — a trend over a
          non-existent column can't be generated OR verified."""
    violations: list[dict] = []
    names = [c.name for c in recipe.columns]
    seen, dups = set(), set()
    for n in names:
        (dups if n in seen else seen).add(n)
    for d in sorted(dups):
        violations.append({"kind": "duplicate_column", "symbol": d,
                           "detail": f"column {d!r} declared {names.count(d)}× — names must be unique"})
    colset = set(names)
    for t in recipe.trends:
        if t.column not in colset:
            violations.append({"kind": "trend_column_missing", "symbol": t.column,
                               "detail": f"trend.column {t.column!r} not in columns {sorted(colset)}"})
        if t.over not in colset:
            violations.append({"kind": "trend_axis_missing", "symbol": t.over,
                               "detail": f"trend.over {t.over!r} not in columns {sorted(colset)}"})
    return violations


@dataclass
class DatasetRecipeAgent:
    """LLM-backed recipe designer. Stateless — instantiate per request."""
    client: AsyncAnthropic
    model: str = "claude-haiku-4-5-20251001"

    async def design(
        self,
        requirement: dict[str, Any],
        risk_profile: dict[str, Any] | None = None,
    ) -> DatasetRecipe:
        """Design + validate a recipe for one requirement.

        Raises `ValueError` when the LLM output can't be parsed/validated after
        the @retry budget. Caller (Phase 1.7 ATDD path, Phase 1.8 router) should
        fail-fast in that case rather than fall back to defaults — a bad recipe
        would silently re-introduce the Gherkin↔data mismatch we just fixed.
        """
        req_id = str(requirement.get("id") or requirement.get("req_id") or "REQ-?")
        req_slug = sanitize_req_id(req_id)
        canonical_path = f"fixtures/analytics/{req_slug}_dataset_v1_0_0.parquet"

        # Build the ACs block — every measurable triple goes in verbatim so the
        # LLM can't drift from the source-authored values.
        acs = list(requirement.get("acceptance_criteria") or [])
        ac_lines: list[str] = []
        measurable_inputs: list[dict[str, Any]] = []
        for i, ac in enumerate(acs, start=1):
            if not isinstance(ac, dict):
                continue
            ac_id = ac.get("id") or f"AC-{i:03d}"
            then = ac.get("then", "") or ac.get("statement", "")
            measurable = ac.get("measurable")
            line = f"  - {ac_id}: then={then!r}"
            if isinstance(measurable, dict):
                line += f" measurable={json.dumps(measurable)}"
                measurable_inputs.append({"ac_id": ac_id, **measurable})
            ac_lines.append(line)
        ac_block = "\n".join(ac_lines) or "  (none — caller should not have invoked the recipe agent)"

        prompt = _RECIPE_PROMPT.format(
            req_id=req_id,
            req_slug=req_slug,
            title=requirement.get("title", ""),
            description=str(requirement.get("description", ""))[:2000],
            ac_block=ac_block,
            priority=(risk_profile or {}).get("priority", "P2"),
            test_types=", ".join((risk_profile or {}).get("test_types", []) or []) or "Analytics",
        )

        # R130.H KEYSTONE — inject SUT response-shape grounding hint so the
        # recipe LLM uses REAL captured-endpoint keys instead of inventing
        # generic analytics column names (`date, sales, region, product,
        # 10-iter verifier because the LLM had no signal about the SUT's
        # actual response shape. Post-R130.H the LLM grounds against real
        # SUT keys + verifier converges first try.
        project_id = (
            requirement.get("project_id")
            or (risk_profile or {}).get("project_id")
        )
        sut_shape_hint = await self._r130_h_compose_sut_shape_hint(project_id)
        if sut_shape_hint:
            prompt = prompt + "\n" + sut_shape_hint
            log.info(
                "R130.H: injected SUT response-shape hint (%d chars) for recipe gen %s",
                len(sut_shape_hint), req_id,
            )

        message = await self._call_llm(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = message.content[0].text

        try:
            data = self._extract_json(raw_text)
        except ValueError as exc:
            log.warning("recipe LLM returned unparseable JSON for %s: %s", req_id, exc)
            raise

        # Trust nothing the LLM said — overwrite the canonical path so it
        # always matches what the generator will write to.
        data["canonical_path"] = canonical_path
        data.setdefault("requirement_id", req_id)
        data["measurable_inputs"] = measurable_inputs

        # Lenient pre-validation: Haiku occasionally drops obvious fields
        # (`dtype` on a column whose distribution makes it self-evident).
        # Infer the default ONLY when distribution.shape uniquely implies
        # a dtype; never invent values for missing distribution.shape itself.
        # Phase A5: returns a NEW dict — earlier in-place mutation could leave
        # `data` half-corrected if a later step raised, with no rollback.
        data = self._infer_missing_dtypes(data)

        try:
            recipe = DatasetRecipe.model_validate(data)
        except ValidationError as exc:
            log.warning("recipe schema validation failed for %s: %s", req_id, exc)
            raise ValueError(f"recipe schema invalid: {exc}") from exc

        # T1 — internal-coherence gate (deterministic; unambiguous authoring bugs only).
        # Runs BEFORE the closed-loop verifier so a self-contradictory recipe (dup
        # columns, a trend over a non-existent column) never reaches ATDD/generator.
        _t1_violations = validate_recipe_coherence(recipe)
        if _t1_violations:
            if os.environ.get("ARTA_T1_COHERENCE_DISABLE") == "1":
                log.warning("T1: recipe %s incoherent but BLOCK disabled (advisory): %s",
                            req_id, _t1_violations)
            else:
                log.error("T1: recipe %s BLOCKED — incoherent: %s", req_id, _t1_violations)
                raise RecipeCoherenceError(req_id, _t1_violations)

        # Phase R4.2 — soft warning when LLM over-populates expected_outputs_by_ac.
        # If every AC's per-AC outputs have IDENTICAL key-sets, the LLM may have
        # populated the field for smoke-variant ACs (where empty {} is correct).
        # We don't reject — sometimes legitimate when ACs differ only by data
        # values not key shape — but surface for operator review.
        by_ac = recipe.expected_outputs_by_ac or {}
        if by_ac and len(by_ac) >= 2:
            key_sets = [frozenset((v or {}).keys()) for v in by_ac.values()]
            if len(set(key_sets)) == 1:
                log.warning(
                    "R4.2: recipe %s populated expected_outputs_by_ac with "
                    "IDENTICAL key-sets across all %d ACs (%s). LLM may have "
                    "over-populated for smoke-variant ACs. Review whether "
                    "leaving the field empty would be more appropriate "
                    "(empty {{}} = all scenarios share requirement-level "
                    "expected_outputs, post-R5 fallback path).",
                    req_id, len(by_ac), sorted(next(iter(key_sets))),
                )

        log.info(
            "recipe drafted for %s: %d columns, %d trends, %d expected_outputs, "
            "%d per-AC entries (path=%s)",
            req_id, len(recipe.columns), len(recipe.trends),
            len(recipe.expected_outputs), len(recipe.expected_outputs_by_ac),
            recipe.canonical_path,
        )

        # Phase 4 — closed-loop verification. Runs the recipe through the mock
        # pipeline; if the data doesn't actually produce expected_outputs, apply
        # closed-form / iterative correction until it does (or budget exhausts).
        # The corrected recipe is what gets persisted + flowed downstream, so
        # ATDD's prompt cites verifiable values, not LLM-imagined ones.
        # Op-out via env so a degraded LLM response can still ship a recipe.
        if os.environ.get("ARTA_RECIPE_CLOSED_LOOP", "1") != "0":
            try:
                from .recipe_verifier import verify_and_correct
                from ..fixtures.generator import _seed_for
                seed = _seed_for(recipe.requirement_id, recipe.version)
                verified_dict = verify_and_correct(recipe.model_dump(), seed)
                recipe = DatasetRecipe.model_validate(verified_dict)
                log.info(
                    "recipe verified for %s: attempts=%d strategy=%s failed=%s",
                    req_id, recipe.verification_attempts,
                    recipe.verification_strategy, recipe.verification_failed,
                )
            except Exception as exc:
                # A verifier crash should never block recipe ship — log and stamp
                # a synthetic "verifier_error" status so the gate can choose.
                log.warning("recipe verifier raised for %s: %s", req_id, exc)
                recipe.verification_strategy = "verifier_error"
                recipe.verification_failed = True

        # R125.G — defensive post-verifier re-validation. The verifier may
        # mutate the recipe mid-run (closed-loop correction adjusts trends +
        # expected_outputs in place), and the except block at line 471-472
        # mutates `verification_strategy` / `verification_failed` directly.
        # If either mutation produced an invalid Pydantic state (e.g. trend
        # value out of range), downstream consumers (ATDD prompt builder,
        # materialise_fixture, gate) would see a recipe that fails schema
        # checks at a less-traceable point. R125.G re-validates HERE so any
        # corruption is caught + stamped with a distinct strategy value the
        # operator can drill into.
        try:
            recipe = DatasetRecipe.model_validate(recipe.model_dump())
        except ValidationError as _r125_g_exc:
            log.error(
                "R125.G: post-verifier recipe schema validation FAILED for %s: %s. "
                "Marking verification_strategy=verifier_corrupted; downstream "
                "consumers will see a valid recipe with verification_failed=True "
                "signal so they can degrade safely.",
                req_id, _r125_g_exc,
            )
            # The original `recipe` object has the mutations that caused the
            # validation failure. We can't safely re-validate; instead stamp
            # the corruption marker so ATDD + materialise see the signal.
            recipe.verification_failed = True
            recipe.verification_strategy = "verifier_corrupted"

        # R55.12 — open-loop recipe grounding (WARN-only). Cross-references
        # recipe.columns + expected_outputs against captured SUT response
        # shapes. Complements the closed-loop recipe_verifier (above), which
        # measures CORRECTNESS by running the pipeline. R55.12 measures
        # ANCHORING: does this recipe cite columns the SUT actually returns?
        # Warnings land in recipe metadata + flow through to the dashboard.
        # Pipeline-internal columns (nl_to_*, result_to_*, etc.) are
        # allowlisted; analytics layer tests can introduce them freely.
        try:
            from .grounding_validator import validate_recipe_grounded
            from .api_discovery import _load_captured_endpoints
            pid = (
                requirement.get("project_id")
                or (risk_profile or {}).get("project_id")
            )
            captured = _load_captured_endpoints(pid) if pid else None
            warns = validate_recipe_grounded(
                recipe.model_dump(), captured_endpoints=captured,
            )
            if warns:
                # Stamp warning summaries on the recipe via the
                # grounding_warnings field (added to schema below). Capped
                # at 20 to keep the sidecar JSON readable; operators see
                # the full list in the dashboard.
                recipe.grounding_warnings = [
                    {"kind": w.kind, "symbol": w.symbol, "location": w.location}
                    for w in warns[:20]
                ]
                log.warning(
                    "R55.12: recipe %s has %d grounding warning(s): %s",
                    req_id, len(warns),
                    [(w.kind, w.symbol) for w in warns[:5]],
                )
                # R150.C KEYSTONE — promote R55.12 from advisory to BLOCKING
                # when warnings exceed threshold AND post-verifier flagged
                # warnings citing invented `insight_metric`, `insight_org_id`,
                # etc. while passing through silently → ATDD + PW gen
                # inherited the hallucinations. Killswitch
                # ARTA_R150_C_RECIPE_BLOCK_DISABLE=1 reverts to advisory.
                import os as _os_r150c
                _r150c_disabled = _os_r150c.environ.get(
                    "ARTA_R150_C_RECIPE_BLOCK_DISABLE",
                ) == "1"
                if not _r150c_disabled:
                    try:
                        _r150c_threshold = int(_os_r150c.environ.get(
                            "ARTA_R150_C_WARNING_THRESHOLD", "5",
                        ))
                    except ValueError:
                        _r150c_threshold = 5
                    if (
                        len(warns) > _r150c_threshold
                        and getattr(recipe, "verification_failed", False)
                    ):
                        _samples = [
                            {"kind": w.kind, "symbol": w.symbol,
                             "location": w.location}
                            for w in warns[:5]
                        ]
                        log.error(
                            "R150.C: recipe %s BLOCKED — %d warnings > "
                            "threshold=%d AND verification_failed=True",
                            req_id, len(warns), _r150c_threshold,
                        )
                        raise RecipeGroundingException(
                            req_id=req_id,
                            warning_count=len(warns),
                            samples=_samples,
                            verification_failed=True,
                            threshold=_r150c_threshold,
                        )
        except RecipeGroundingException:
            # R150.C — propagate the BLOCK to caller. NEVER swallow.
            raise
        except Exception as _r55_12_exc:
            log.debug("R55.12: recipe grounding skipped for %s: %s", req_id, _r55_12_exc)

        self._persist_sidecar(recipe)

        # R125.D — materialize the fixture parquet at recipe-stage so ATDD's
        # `Given frozen_dataset("...")` references resolve at pytest dispatch.
        # Pre-R125.D: materialise_fixture() was called only at the test-gen
        # endpoint (tests.py:~3506). If anything between recipe-stage and
        # gen-stage failed (LLM auth lapse, R49 budget timeout, network blip),
        # pytest hit FileNotFoundError at dispatch.
        # R125.D moves materialisation UPSTREAM: the recipe IS the fixture
        # contract, so the parquet should land alongside the recipe sidecar.
        # The downstream call at tests.py becomes idempotent (already-exists
        # short-circuit handled by materialise_fixture's overwrite semantics).
        # NON-BLOCKING: any failure is logged + the downstream gen path
        # remains as the safety net.
        try:
            from ..fixtures.generator import materialise_fixture
            fixture_path = materialise_fixture(
                req_id=recipe.requirement_id,
                recipe=recipe.model_dump(),
            )
            log.info(
                "R125.D: recipe fixture materialised at %s (req=%s, rows=%d, cols=%d)",
                fixture_path, recipe.requirement_id, recipe.row_count,
                len(recipe.columns),
            )
        except Exception as _r125_d_exc:
            log.warning(
                "R125.D: recipe fixture materialise failed for %s (will retry at "
                "tests.py): %s: %s",
                recipe.requirement_id, type(_r125_d_exc).__name__, _r125_d_exc,
            )

        return recipe

    @staticmethod
    def _r150_b_render_shape(shape: object, indent: int = 0, max_depth: int = 4) -> str:
        """R150.B — render a `_infer_shape`-produced dict into a compact
        JSON-like sample string for LLM display. Examples:

          {"type":"object","properties":{
              "metric":{"type":"string"},
              "value":{"type":"number"}}}
          →
          {
            "metric": "<string>",
            "value": <number>
          }

        Truncates at `max_depth`. Returns empty string on unrecognized shape.
        Cost-bounded: O(depth × keys-per-level).
        """
        if not isinstance(shape, dict) or indent > max_depth:
            return ""
        t = shape.get("type")
        pad = "  " * indent
        if t == "object":
            props = shape.get("properties") or {}
            if not isinstance(props, dict) or not props:
                return "{}"
            lines = ["{"]
            keys = list(props.keys())[:8]   # cap keys per level
            for i, k in enumerate(keys):
                rendered = DatasetRecipeAgent._r150_b_render_shape(
                    props[k], indent + 1, max_depth,
                )
                sep = "," if i < len(keys) - 1 else ""
                lines.append(f'{pad}  "{k}": {rendered}{sep}')
            if len(props) > 8:
                lines.append(f"{pad}  // +{len(props) - 8} more keys omitted")
            lines.append(f"{pad}}}")
            return "\n".join(lines)
        if t == "array":
            items_shape = shape.get("items") or {"type": "any"}
            inner = DatasetRecipeAgent._r150_b_render_shape(
                items_shape, indent + 1, max_depth,
            )
            return f"[{inner}]" if inner else "[<any>]"
        # Scalars
        if t == "string":
            return '"<string>"'
        if t == "number":
            return "<number>"
        if t == "integer":
            return "<integer>"
        if t == "boolean":
            return "<boolean>"
        if t == "null":
            return "null"
        return "<any>"

    def _r150_b_compose_sample_payloads(
        self, captured: list[dict], *, max_endpoints: int = 4, max_chars: int = 2400,
    ) -> str:
        """R150.B — build structured sample-payload block. Mission: give
        the recipe LLM CONCRETE response shape examples (not just key
        names) so it grounds recipe.columns against actual SUT keys.

        Pre-R150.B: R130.H surfaced "top-20 keys" as flat text. The LLM
        still invented prefixes (`insight_*`) because it couldn't see
        WHICH endpoint returns WHICH structure.

        Post-R150.B: per-endpoint sample shows the actual response shape.
        Hint format mirrors a JSON sample with placeholder values:
          {
            "data": {
              "records": [{
                "id": "<string>",
                "metric_name": "<string>",
                "value": <number>
              }],
              ...
            }
          }

        Endpoint selection: prefers endpoints with deeper (more
        informative) shapes via key-count heuristic. Char budget: ≤2400
        — total R130.H + R150.B stays under 6K combined.

        Killswitch: `ARTA_R150_B_STRUCTURED_HINT_DISABLE=1` returns "" so
        the recipe gen falls back to R130.H top-20-keys only.
        """
        import os as _os_r150b
        if _os_r150b.environ.get("ARTA_R150_B_STRUCTURED_HINT_DISABLE") == "1":
            return ""
        if not captured:
            return ""

        # Score endpoints by number of distinct keys in response shape
        # (deeper shapes are more informative). Skip endpoints with empty
        # or trivial response shapes.
        def _count_keys(shape: object, depth: int = 0) -> int:
            if depth > 3 or not isinstance(shape, dict):
                return 0
            t = shape.get("type")
            if t == "object":
                props = shape.get("properties") or {}
                if not isinstance(props, dict):
                    return 0
                return len(props) + sum(
                    _count_keys(v, depth + 1) for v in props.values()
                )
            if t == "array":
                return _count_keys(shape.get("items") or {}, depth + 1)
            return 0

        scored: list[tuple[int, dict]] = []
        for ep in captured:
            if not isinstance(ep, dict):
                continue
            shape = ep.get("response_body_shape")
            if not shape:
                continue
            n = _count_keys(shape)
            if n < 2:   # skip trivial shapes (e.g., just {"type":"object"})
                continue
            scored.append((n, ep))

        if not scored:
            return ""

        scored.sort(key=lambda kv: -kv[0])
        top = scored[:max_endpoints]

        lines = [
            "",
            "[R150.B — SUT RESPONSE SHAPE SAMPLES]",
            "Top-ranked endpoints (by shape depth) — actual structures the",
            "SUT returns. Recipe column names MUST come from keys visible",
            "in these samples (or be flattened from dotted JSON paths via",
            "R131.B), NOT invented prefixes:",
            "",
        ]
        chars_used = sum(len(s) for s in lines)
        for _n, ep in top:
            method = (ep.get("method") or "GET").upper()
            path = ep.get("path") or ""
            rendered = self._r150_b_render_shape(
                ep.get("response_body_shape") or {}, indent=0, max_depth=4,
            )
            block = f"Endpoint {method} {path} returns:\n{rendered}\n"
            if chars_used + len(block) > max_chars:
                lines.append(f"// +{len(top) - top.index((_n, ep))} more endpoints omitted (char budget)")
                break
            lines.append(block)
            chars_used += len(block)

        lines.append(
            "HARD CONSTRAINT: recipe.columns[*].name MUST exist as a key in"
        )
        lines.append(
            "at least ONE sample above (after R131.B snake_case flatten)."
        )
        return "\n".join(lines)

    def _r213k2_openapi_response_keys(
        self, project_id: str | None, limit: int = 20,
    ) -> list[str]:
        """R213.K.2 — fallback SUT response-shape grounding from the cached
        OpenAPI spec (`.arta/openapi/<pid>.json`) when no HAR
        `response_body_shape` was captured. Walks 2xx JSON response schemas,
        resolving `$ref` into `components.schemas` (OpenAPI 3) / `definitions`
        (Swagger 2), and returns the most-frequent property key names — REAL
        SUT field names the recipe LLM can ground `expected_outputs` on instead
        of inventing. Returns [] when no spec / no keys (caller then returns "")."""
        if not project_id:
            return []
        try:
            from collections import Counter
            from .api_discovery import _OPENAPI_DIR
            spec_path = _OPENAPI_DIR / f"{project_id}.json"
            if not spec_path.is_file():
                return []
            spec = json.loads(spec_path.read_text())
            schemas = ((spec.get("components") or {}).get("schemas")) or spec.get("definitions") or {}
            counts: Counter[str] = Counter()
            seen_refs: set[str] = set()

            def _resolve(node: object, depth: int = 0) -> None:
                if depth > 4 or not isinstance(node, dict):
                    return
                ref = node.get("$ref")
                if isinstance(ref, str):
                    name = ref.rsplit("/", 1)[-1]
                    if name in seen_refs:
                        return
                    seen_refs.add(name)
                    _resolve(schemas.get(name), depth + 1)
                    return
                props = node.get("properties")
                if isinstance(props, dict):
                    for k, v in props.items():
                        if isinstance(k, str):
                            counts[k] += 1
                        _resolve(v, depth + 1)
                if isinstance(node.get("items"), dict):
                    _resolve(node["items"], depth + 1)
                # allOf/anyOf/oneOf composition
                for comb in ("allOf", "anyOf", "oneOf"):
                    for sub in (node.get(comb) or []):
                        _resolve(sub, depth + 1)

            for _path, methods in (spec.get("paths") or {}).items():
                if not isinstance(methods, dict):
                    continue
                for _method, details in methods.items():
                    if not isinstance(details, dict):
                        continue
                    resps = details.get("responses") or {}
                    for code in ("200", "201", "2XX", "default"):
                        r = resps.get(code)
                        if not isinstance(r, dict):
                            continue
                        sch = ((r.get("content") or {}).get("application/json") or {}).get("schema")
                        if sch is None:  # Swagger 2 puts schema directly on the response
                            sch = r.get("schema")
                        if isinstance(sch, dict):
                            _resolve(sch)
            return [k for k, _ in counts.most_common(limit)]
        except Exception as exc:
            log.debug("R213.K.2: OpenAPI shape fallback failed for %s: %s", project_id, exc)
            return []

    async def _r130_h_compose_sut_shape_hint(
        self, project_id: str | None,
    ) -> str:
        """R130.H KEYSTONE — build a compact 'SUT response shape' hint for
        the recipe-gen prompt. Walks captured_endpoints[*].response_body_shape
        and surfaces the top-20 most-frequent keys + pipeline-prefix conventions.

        Pre-R130.H: recipe LLM invented generic column names (`date`, `sales`,
        `region`) because it had ZERO signal about the SUT's actual response
        shape — every iteration of recipe_verifier produced the same R55.12
        warnings, never converged.

        Post-R130.H: LLM sees real SUT keys as the LAST thing before
        generating → grounds columns correctly first try → verifier
        converges on iteration 1 (vs 10-iter exhaust).

        R150.B extension: appends structured sample-payload block per
        endpoint (replaces flat top-20-keys-only with concrete shape
        renders). Composes additively — R131.B + R132.B constraints
        still apply. Killswitch `ARTA_R150_B_STRUCTURED_HINT_DISABLE=1`
        reverts to R130.H base behavior.

        Returns empty string when captured store is unavailable (cold-start;
        matches R55.12 skip-with-WARN-log convention). Char budget: ≤5600
        post-R150.B (R130.H base ≤3200 + R150.B append ≤2400).
        """
        if not project_id:
            return ""
        try:
            from .api_discovery import _load_captured_endpoints
            captured = _load_captured_endpoints(project_id) or []
            from collections import Counter
            key_counts: Counter[str] = Counter()

            def _walk(shape: object, depth: int = 0) -> None:
                if depth > 3:
                    return
                if isinstance(shape, dict):
                    for k, v in shape.items():
                        if isinstance(k, str):
                            key_counts[k] += 1
                        _walk(v, depth + 1)
                elif isinstance(shape, list) and shape:
                    _walk(shape[0], depth + 1)

            for ep in captured:
                if isinstance(ep, dict):
                    _walk(ep.get("response_body_shape"))
            top_keys = [k for k, _ in key_counts.most_common(20)]
            grounding_src = "Captured endpoints"
            # R213.K.2 — OpenAPI-contract FALLBACK when no HAR response_body_shape
            # EMPTY R130.H hint → recipe LLM invented/emptied expected_outputs →
            # the `_expected_outputs_present` validator rejected → upstream_blocked,
            # never recovering). The SUT's cached OpenAPI 2xx response schemas
            # provide REAL field names to ground on, closing that cold-start gap
            # the same way captured shapes do for non-cold reqs. Killswitch
            # ARTA_RECIPE_OPENAPI_GROUNDING_DISABLE=1.
            if not top_keys and os.environ.get("ARTA_RECIPE_OPENAPI_GROUNDING_DISABLE") != "1":
                top_keys = self._r213k2_openapi_response_keys(project_id, limit=20)
                if top_keys:
                    grounding_src = "SUT OpenAPI contract (no HAR shape captured)"
                    log.info(
                        "R213.K.2: grounded recipe shape hint from OpenAPI (%d keys) for %s",
                        len(top_keys), project_id,
                    )
            if not top_keys:
                return ""
            top = ", ".join(top_keys)
            return (
                "\n[R130.H — SUT RESPONSE-SHAPE GROUNDING]\n"
                f"{grounding_src} expose these top-20 response keys "
                "(REAL DATA, not generic placeholders):\n"
                f"  {top}\n"
                "When defining recipe.columns, prefer key names that match\n"
                "or closely resemble entries above. Generic analytics names\n"
                "like `date/sales/region/product/quantity` will fail R55.12\n"
                "grounding and trigger verifier exhaust.\n"
                "Pipeline-derived columns may use these prefixes (they are\n"
                "allowlisted): nl_to_*, query_to_*, result_to_*,\n"
                "insight_to_*, _arta_*, partition_*, synth_*.\n"
                "\n"
                "[R131.B HARD CONSTRAINT — recipe.columns[*].name FORMAT]\n"
                "Each `name` MUST be lowercase snake_case with NO DOTS,\n"
                "NO CAPITAL LETTERS, and NO SPACES. The DatasetRecipe\n"
                "schema rejects anything else with a Pydantic validation\n"
                "error (verified live: job d10f8710 emitted\n"
                "12 dotted column names and the recipe stage hard-failed).\n"
                "If a captured SUT key is dotted (a nested JSON path), you\n"
                "MUST flatten it by replacing `.` with `_`.\n"
                "\n"
                "BEFORE (BROKEN — actual live output):\n"
                "  {'name': 'insight.metric', 'dtype': 'string', ...}\n"
                "  {'name': 'insight.org_id', 'dtype': 'string', ...}\n"
                "  -> recipe schema invalid: 12 validation errors\n"
                "\n"
                "AFTER (CORRECT — dotted paths flattened to snake_case):\n"
                "  {'name': 'insight_metric', 'dtype': 'string', ...}\n"
                "  {'name': 'insight_org_id', 'dtype': 'string', ...}\n"
                "\n"
                "[R132.B HARD CONSTRAINT — LITERAL ENUM FIELDS]\n"
                "Three recipe fields are `Literal[...]` enums; ANY other\n"
                "value fails Pydantic validation. The constraints are\n"
                "ASYMMETRIC across fields — note this carefully:\n"
                "\n"
                "  columns[*].dtype must be one of:\n"
                "    'date', 'number', 'string', 'categorical', 'boolean'\n"
                "\n"
                "  columns[*].distribution.shape must be one of:\n"
                "    'monotonic_up', 'monotonic_down', 'categorical_weighted',\n"
                "    'time_range', 'constant', 'random_normal', 'derived'\n"
                "\n"
                "  trends[*].shape must be one of (NARROWER set than\n"
                "  distribution.shape — only monotonic, no other choices):\n"
                "    'monotonic_up', 'monotonic_down'\n"
                "\n"
                "BEFORE (BROKEN — Iter 1 live evidence):\n"
                "  trends: [{'column': 'revenue', 'shape': 'derived', ...}]\n"
                "  -> Input should be 'monotonic_up' or 'monotonic_down'\n"
                "  (1 validation error for DatasetRecipe / trends.2.shape)\n"
                "\n"
                "AFTER (CORRECT — trends.shape picks from {up, down}):\n"
                "  trends: [{'column': 'revenue', 'shape': 'monotonic_up', ...}]\n"
                "If the underlying metric is COMPUTED rather than trending\n"
                "(e.g., revenue = qty * price), it does NOT belong in trends\n"
                "at all — model it as columns[*].distribution.shape='derived'\n"
                "and OMIT the trends entry. Trends describe direction over\n"
                "time/axis; derived columns describe formula relationships.\n"
                # R150.B — append structured sample-payload block. Killswitch
                # ARTA_R150_B_STRUCTURED_HINT_DISABLE=1 turns this off and
                # the LLM falls back to the top-20-keys-only R130.H base.
                + self._r150_b_compose_sample_payloads(captured)
            )
        except Exception as exc:
            log.debug("R130.H: SUT-shape hint skipped (%s)", exc)
            return ""

    @retry(
        # R134.C — single-source-of-truth retry tuple covers anthropic.* SDK
        # errors, RuntimeError (ClaudeCLI/Ollama subprocess failures), AND
        # httpx transient network errors.
        retry=retry_if_exception_type(LLM_RETRYABLE_EXC),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _call_llm(self, **kwargs):
        from .circuit_breaker import get_breaker
        provider_name = getattr(self.client, "provider", None) or type(self.client).__name__
        breaker = get_breaker(str(provider_name))
        return await breaker.call(self.client.messages.create, **kwargs)

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """R134.D — delegates to shared SSoT extractor + raise-on-failure
        preserved from the pre-R134.D contract."""
        from .json_extract import extract_json_from_llm_output
        result = extract_json_from_llm_output(text, default=None)
        if result is None:
            raise ValueError(f"no valid JSON in LLM output: {(text or '')[:200]!r}")
        return result

    @staticmethod
    def _infer_missing_dtypes(data: dict[str, Any]) -> dict[str, Any]:
        """Return a NEW dict with `columns` dtypes normalised — never mutate `data`.

        Closes common LLM-drift gaps:
        1. Normalise dtype synonyms (`timestamp`→`date`, `float`→`number`, ...)
           so the strict `Literal[...]` check on `ColumnSpec.dtype` doesn't
           reject otherwise-valid recipes.
        2. Fill dtype when missing but the distribution.shape uniquely implies
           one (e.g. `time_range` → `date`).
        Never invents distribution.shape — that stays the LLM's responsibility.

        Phase A5: in-place mutation made partial-corrections impossible to roll
        back. Now returns a fresh shallow-copy + deep-copied columns; if a later
        step raises, the caller's original `data` is untouched.
        """
        dtype_synonyms = {
            "timestamp": "date", "datetime": "date", "time": "date",
            "int": "number", "integer": "number", "float": "number",
            "decimal": "number", "numeric": "number", "double": "number",
            "long": "number",
            "text": "string", "varchar": "string", "str": "string",
            "enum": "categorical", "category": "categorical", "cat": "categorical",
            # R38.6 — boolean synonyms; "bool" is the most common LLM
            # output, "flag" / "binary" appear in analytics-domain prompts.
            "bool": "boolean", "flag": "boolean", "binary": "boolean",
        }
        shape_to_dtype = {
            "time_range": "date",
            "monotonic_up": "number",
            "monotonic_down": "number",
            "random_normal": "number",
            "categorical_weighted": "categorical",
            # `constant` and `derived` are dtype-ambiguous — disambiguate below
        }
        new_data = dict(data)
        new_columns: list[Any] = []
        for col in (data.get("columns") or []):
            if not isinstance(col, dict):
                new_columns.append(col)
                continue
            new_col = dict(col)
            # 1. Normalise existing dtype synonyms
            current = (new_col.get("dtype") or "").lower()
            if current and current not in {"date", "number", "string", "categorical", "boolean"}:
                normalised = dtype_synonyms.get(current)
                if normalised:
                    log.debug("recipe: normalised dtype %s→%s for column %s",
                              current, normalised, new_col.get("name", "?"))
                    new_col["dtype"] = normalised
                    new_columns.append(new_col)
                    continue
            # 2. Infer when missing
            if not new_col.get("dtype"):
                shape = (new_col.get("distribution") or {}).get("shape")
                inferred = shape_to_dtype.get(shape)
                if inferred:
                    new_col["dtype"] = inferred
                elif shape == "constant":
                    v = (new_col.get("distribution") or {}).get("value")
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        new_col["dtype"] = "number"
                    elif isinstance(v, str):
                        new_col["dtype"] = "string"
            new_columns.append(new_col)
        new_data["columns"] = new_columns
        return new_data

    @staticmethod
    def _persist_sidecar(recipe: DatasetRecipe) -> Path | None:
        """Write the recipe JSON to `.arta/recipes/{req_id}_v{ver}.json` for
        audit/replay. Best-effort — disk failures are logged, not raised."""
        out_dir = Path(os.environ.get("ARTA_RECIPES_DIR", ".arta/recipes"))
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            req_slug = sanitize_req_id(recipe.requirement_id)
            ver_slug = recipe.version.replace(".", "_")
            path = out_dir / f"{req_slug}_v{ver_slug}.json"
            path.write_text(json.dumps(recipe.model_dump(), indent=2, default=str))
            log.info("recipe persisted: %s", path)
            return path
        except Exception as exc:
            log.warning("recipe sidecar persist failed: %s", exc)
            return None
