"""Phase 4 — DatasetRecipe closed-loop verification.

The architectural promise from Phase 1: recipe.expected_outputs is what a Then
step asserts on. Phase 2 made the generator produce data shaped by recipe
primitives. But "shaped by" isn't "produces" — a recipe asking for
`magnitude_pct=12.5` got data that aggregated to 11.20% because the categorical
metric_label split the trend across three subsets. Without verification, that
gap silently fails every test.

This module closes the loop:

  1. Materialise the data per recipe.
  2. Run a deterministic mock pipeline that mirrors the aggregations the
     production analytics pipeline performs (trend magnitude/direction,
     categorical mode, constant lookup).
  3. Diff actual_outputs vs recipe.expected_outputs.
  4. If gaps exist, apply a TWO-TIER correction:
       Tier 1 (closed-form): each primitive ships a `solve_for` that inverts
                              the math so we know exactly what parameter to set.
       Tier 2 (iterative):   bisect/scale until within tolerance, bounded to
                              N attempts.
  5. Re-materialise + re-verify. Persist `verification_attempts`,
     `verification_strategy`, and `actual_outputs` on the recipe so auditors
     see the loop ran.
"""
from __future__ import annotations

import collections
import logging
import os
from typing import Any

log = logging.getLogger("arta.recipe_verifier")


def _sync_string_expected(recipe: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """T3 — replace STRING/categorical `expected_outputs` values with the value COMPUTED
    from the generated rows (the deterministic oracle in `actual`). Pre-T3 these keys
    (insight.metric label, most_common category, etc.) shipped as LLM GUESSES: the
    closed-form/iterative correctors only touch NUMERIC magnitudes, so a string expected
    was asserted verbatim against the SUT — violating "ground-truth COMPUTED from the
    generated rows, never LLM-invented." Numeric keys are left to the existing machinery.
    Returns the list of keys made oracle-backed. Killswitch ARTA_T3_SYNC_DISABLE=1."""
    if os.environ.get("ARTA_T3_SYNC_DISABLE") == "1":
        return []
    expected = recipe.get("expected_outputs") or {}
    synced: list[str] = []
    for k, v in list(expected.items()):
        # only DESCRIPTIVE string labels — never numbers/bools (trend targets the
        # closed-form loop tunes the data to hit).
        if isinstance(v, bool) or not isinstance(v, str):
            continue
        a = actual.get(k)
        # oracle must be a concrete label (str or int); floats stay on the numeric path.
        if a is None or isinstance(a, (bool, float)) or not isinstance(a, (str, int)):
            continue
        expected[k] = a
        synced.append(k)
    if synced:
        recipe["expected_outputs"] = expected
    return synced


_DEFAULT_TOLERANCE = 0.01  # 1%
_DEFAULT_MAX_ITER = 10


# ── Mock pipeline ─────────────────────────────────────────────────────────────
# Mirrors the aggregations downstream analytics tests assert on. Pure-Python so
# determinism is provable; no LLM, no network. v1 covers:
#   - per-trend magnitude_pct + direction (from first→last value)
#   - constant column → insight.metric (heuristic; LLM-recipe convention)
#   - categorical_weighted column → most_common
# Future extensions (avg, sum, percentile) plug in here as new aggregators.


def _trend_aggregations(rows: list[dict], trend_column: str) -> dict[str, Any]:
    """Compute magnitude_pct + direction for one column treated as a time series.

    Uses ALL non-null values in row order — relies on the generator emitting
    rows in `time_range` order. Returns 0.0 magnitude when there are <2 values
    or the start value is 0 (avoids divide-by-zero).
    """
    vals: list[float] = []
    for r in rows:
        v = r.get(trend_column)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(vals) < 2:
        return {"magnitude_pct": 0.0, "direction": "flat"}
    first, last = vals[0], vals[-1]
    if first == 0:
        return {"magnitude_pct": 0.0, "direction": "flat"}
    raw_pct = (last - first) / abs(first) * 100.0
    direction = "up" if raw_pct > 0 else "down" if raw_pct < 0 else "flat"
    return {
        "magnitude_pct": round(abs(raw_pct), 4),
        "direction": direction,
        "first_value": round(first, 4),
        "last_value": round(last, 4),
    }


def run_mock_pipeline(rows: list[dict], recipe: dict[str, Any]) -> dict[str, Any]:
    """Aggregate `rows` per the recipe's declared trends + columns.

    Output keys mirror what `recipe.expected_outputs` is conventionally
    populated with. The diff step matches by key, so any expected_output the
    LLM stamped that the pipeline doesn't compute will surface as a "missing"
    gap rather than silently passing.
    """
    out: dict[str, Any] = {}

    # Trend-based aggregations (one per declared trend)
    for trend in recipe.get("trends") or []:
        if not isinstance(trend, dict):
            continue
        col = trend.get("column")
        if not col:
            continue
        agg = _trend_aggregations(rows, col)
        # Stamp under both general (insight.magnitude_pct) and column-scoped
        # keys so recipes using either convention work.
        out["insight.magnitude_pct"] = agg["magnitude_pct"]
        out["insight.direction"] = agg["direction"]
        out[f"insight.{col}.magnitude_pct"] = agg["magnitude_pct"]
        out[f"insight.{col}.direction"] = agg["direction"]

    # Constant + categorical_weighted column lookups
    for col in recipe.get("columns") or []:
        if not isinstance(col, dict):
            continue
        name = col.get("name")
        dist = col.get("distribution") or {}
        shape = dist.get("shape")
        if shape == "constant":
            # Convention: a constant column named `metric_label` (or that holds
            # a single value) typically maps to `insight.metric` in analytics
            # pipelines. Stamp both so recipes using either key match.
            value = dist.get("value")
            out["insight.metric"] = value
            out[f"insight.{name}"] = value
        elif shape == "categorical_weighted":
            counts = collections.Counter(r.get(name) for r in rows if r.get(name) is not None)
            most = counts.most_common(1)
            if most:
                out[f"insight.{name}.most_common"] = most[0][0]

    return out


def compute_aggregate(rows, agg, column=None, *, filter_col=None, filter_val=None,
                      percentile=50.0):
    """AN1 (R218) — INDEPENDENT ground-truth aggregator over the generated rows
    (pure-Python, deterministic — no LLM, no SUT). Covers the insight aggregations
    a requirement may ask for beyond `run_mock_pipeline`'s trend/constant/
    categorical (the file's marked plug-in point): `count`, `sum`, `avg`/`mean`,
    `min`, `max`, `percentile`, `top_category`. Optional single-column equality
    filter. This is the oracle AN4 verifies the SUT's answer against on the
    CONTROLLED data (the manual-tester computed answer)."""
    sel = rows
    if filter_col is not None:
        sel = [r for r in rows if r.get(filter_col) == filter_val]
    if agg == "count":
        return len(sel)
    if agg in ("top_category", "mode"):
        c = collections.Counter(r.get(column) for r in sel if r.get(column) is not None)
        m = c.most_common(1)
        return m[0][0] if m else None
    vals: list[float] = []
    for r in sel:
        v = r.get(column)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None
    if agg == "sum":
        return round(sum(vals), 4)
    if agg in ("avg", "mean"):
        return round(sum(vals) / len(vals), 4)
    if agg == "min":
        return round(min(vals), 4)
    if agg == "max":
        return round(max(vals), 4)
    if agg == "percentile":
        vals.sort()
        k = (len(vals) - 1) * (float(percentile) / 100.0)
        lo = int(k)
        hi = min(lo + 1, len(vals) - 1)
        return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 4)
    return None


# ── Diff ──────────────────────────────────────────────────────────────────────


def diff_outputs(
    actual: dict[str, Any],
    expected: dict[str, Any],
    tolerance: float = _DEFAULT_TOLERANCE,
) -> list[dict[str, Any]]:
    """Per-key difference list. Numeric keys use relative tolerance; string keys
    use case-insensitive trim equality. Missing keys are reported as `gap=missing`.
    """
    gaps: list[dict[str, Any]] = []
    for key, exp in expected.items():
        act = actual.get(key)
        if act is None:
            gaps.append({"key": key, "expected": exp, "actual": None, "kind": "missing"})
            continue
        # Numeric path
        try:
            exp_f = float(exp)
            act_f = float(act)
            denom = max(abs(exp_f), 1.0)  # 1-floor so 0-target doesn't blow up
            rel = abs(exp_f - act_f) / denom
            if rel > tolerance:
                gaps.append({
                    "key": key, "expected": exp_f, "actual": act_f,
                    "kind": "numeric_mismatch", "relative_gap": round(rel, 4),
                })
            continue
        except (TypeError, ValueError):
            pass
        # String path
        if str(exp).strip().lower() != str(act).strip().lower():
            gaps.append({
                "key": key, "expected": exp, "actual": act,
                "kind": "string_mismatch",
            })
    return gaps


# ── Closed-form correction (Tier 1) ──────────────────────────────────────────
# Each primitive that has a known inverse exposes a `solve_for(target,
# observed, current_param)` function. The verifier uses these to compute the
# corrected parameter in a single math op (no iteration).


def _solve_monotonic_pct(target_pct: float, observed_pct: float, current_pct: float) -> float | None:
    """Inverse for monotonic_up / monotonic_down magnitude_pct.

    The generator's monotonic primitive scales linearly with `magnitude_pct`,
    so observed magnitude is approximately `current_pct * actual_signal_factor`
    where the factor accounts for any other column (e.g. categorical_weighted
    metric_label) that splits the data. To hit `target_pct`, scale the
    parameter by the inverse ratio.
    """
    if observed_pct <= 0 or current_pct <= 0:
        return None
    scale = target_pct / observed_pct
    new_pct = current_pct * scale
    # Clamp to a sensible range — refuse magnitudes > 1000% or < 0.001%
    if not 0.001 <= new_pct <= 1000.0:
        return None
    return round(new_pct, 4)


def closed_form_correct(
    recipe: dict[str, Any],
    gaps: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Apply Tier 1 closed-form corrections in-place on a copy of `recipe`.

    Returns the corrected recipe dict, or None when no correction was applicable
    (caller should fall back to iterative). Only handles numeric gaps on
    magnitude_pct keys for now — string mismatches (insight.metric, direction)
    require a different fix (changing constant.value or trend direction).
    """
    import copy
    corrected = copy.deepcopy(recipe)
    fixed_any = False

    # Index columns by name so we can mutate distributions in place
    cols_by_name = {c["name"]: c for c in corrected.get("columns") or [] if isinstance(c, dict) and c.get("name")}
    trends = corrected.get("trends") or []

    for gap in gaps:
        if gap["kind"] != "numeric_mismatch":
            continue
        key = gap["key"]
        if "magnitude_pct" not in key:
            continue
        # Scope: try to find the trend whose column matches the key
        # (e.g. `insight.value.magnitude_pct` → trend on `value`).
        target_col: str | None = None
        if key != "insight.magnitude_pct":
            # column-scoped
            parts = key.split(".")
            if len(parts) >= 3:
                target_col = parts[1]
        # Walk trends; for the matching one (or the first one when
        # the key is the unscoped general `insight.magnitude_pct`)
        for t in trends:
            if not isinstance(t, dict):
                continue
            col = t.get("column")
            if target_col and col != target_col:
                continue
            cur_pct = t.get("magnitude_pct")
            if not isinstance(cur_pct, (int, float)):
                continue
            new_pct = _solve_monotonic_pct(
                target_pct=float(gap["expected"]),
                observed_pct=float(gap["actual"]),
                current_pct=float(cur_pct),
            )
            if new_pct is None:
                continue
            t["magnitude_pct"] = new_pct
            # Propagate to the matching column's distribution if present
            col_spec = cols_by_name.get(col)
            if col_spec:
                dist = col_spec.setdefault("distribution", {})
                dist["magnitude_pct"] = new_pct
            log.debug(
                "closed-form: trend %s magnitude_pct %.4f → %.4f (target %s, observed %s)",
                col, cur_pct, new_pct, gap["expected"], gap["actual"],
            )
            fixed_any = True
            if not target_col:
                break  # general key — only fix the first trend

    return corrected if fixed_any else None


# ── Iterative correction (Tier 2) ────────────────────────────────────────────


def _columns_in_scope(recipe: dict[str, Any]) -> set[str]:
    """Phase A6 (per plan DR-7): leaf columns referenced by expected_outputs.

    Iterative damping must only touch columns whose values feed into a failing
    expected_output — not every numeric column in the recipe. Walking every
    column over-corrects unrelated dimensions and can flip a passing
    `correlation` aggregate while trying to fix `magnitude_pct`.

    A column is in-scope when:
      - it is the `column` of any declared trend (its magnitude/direction
        is a mock-pipeline output), OR
      - it is named in any expected_output key as `insight.<col>.<suffix>`, OR
      - it is a `constant`-shape column AND `insight.metric` is expected
        (constant values map to insight.metric per the mock pipeline's
        convention).
    """
    expected = recipe.get("expected_outputs") or {}
    cols_by_name: dict[str, dict] = {
        c["name"]: c
        for c in (recipe.get("columns") or [])
        if isinstance(c, dict) and c.get("name")
    }
    scope: set[str] = set()
    # 1. Trend columns
    for t in recipe.get("trends") or []:
        if isinstance(t, dict) and t.get("column"):
            scope.add(t["column"])
    # 2. Column-scoped expected_output keys: `insight.<col>.<suffix>`
    for key in expected.keys():
        parts = key.split(".")
        if len(parts) >= 3 and parts[1] in cols_by_name:
            scope.add(parts[1])
    # 3. Constant columns linked to insight.metric
    if "insight.metric" in expected:
        for name, col in cols_by_name.items():
            if (col.get("distribution") or {}).get("shape") == "constant":
                scope.add(name)
    return scope


def iterative_correct(
    recipe: dict[str, Any],
    gaps: list[dict[str, Any]],
    attempt: int,
) -> dict[str, Any] | None:
    """Tier 2 — apply a damped scaling step toward the target.

    Used when closed-form returns None (e.g. a `derived` column whose math we
    can't invert symbolically). Damping factor decreases with attempt so the
    loop converges rather than oscillating.

    Phase A6: only adjusts columns in `_columns_in_scope(recipe)` — the prior
    implementation walked every numeric column and could over-correct
    dimensions unrelated to the failing aggregation.
    """
    import copy
    corrected = copy.deepcopy(recipe)
    damping = max(0.3, 1.0 - attempt * 0.1)  # 1.0, 0.9, 0.8, ...
    fixed_any = False

    in_scope = _columns_in_scope(corrected)
    cols_by_name = {
        c["name"]: c
        for c in corrected.get("columns") or []
        if isinstance(c, dict) and c.get("name") in in_scope
    }
    if not cols_by_name:
        log.debug(
            "iterative_correct: no in-scope columns for %s; refusing to over-correct",
            recipe.get("requirement_id", "?"),
        )
        return None

    for gap in gaps:
        if gap["kind"] != "numeric_mismatch":
            continue
        try:
            target = float(gap["expected"])
            observed = float(gap["actual"])
        except (TypeError, ValueError):
            continue
        if observed == 0:
            continue
        # Walk every numeric distribution param and dampen-scale toward target
        for col in cols_by_name.values():
            dist = col.get("distribution") or {}
            for param_key in ("magnitude_pct", "mean", "std", "start_value"):
                if param_key in dist:
                    cur = dist[param_key]
                    if not isinstance(cur, (int, float)):
                        continue
                    ratio = target / observed
                    # Apply damped move: new = cur * (1 + damping * (ratio - 1))
                    new = cur * (1.0 + damping * (ratio - 1.0))
                    dist[param_key] = round(new, 4)
                    fixed_any = True

    return corrected if fixed_any else None


# ── Public API ───────────────────────────────────────────────────────────────


def _verify_per_ac(recipe: dict[str, Any], tolerance: float) -> dict[str, Any]:
    """Phase R4.3 — per-AC advisory verification.

    When `expected_outputs_by_ac` is populated (R4.1 schema, R4.2 prompt),
    diff each AC's expected outputs against the requirement-level
    `actual_outputs`. Surface mismatches in `verification_failed_by_ac`
    for operator review.

    v1 LIMITATION: re-uses the requirement-level `actual_outputs` (from
    one pipeline run). Full per-AC data slicing — filter parquet by each
    AC's `measurable_inputs` scope, run pipeline per slice, diff per slice
    — is v2 deferred work because it requires the analytics pipeline to
    support per-slice invocation, which it doesn't yet.

    Mutates and returns `recipe`.
    """
    by_ac = recipe.get("expected_outputs_by_ac") or {}
    if not by_ac:
        # No per-AC contract — leave verification_failed_by_ac empty (its default)
        recipe.setdefault("verification_failed_by_ac", {})
        return recipe

    actual = recipe.get("actual_outputs") or {}
    per_ac_failures: dict[str, dict[str, Any]] = {}
    for ac_id, ac_expected in by_ac.items():
        if not isinstance(ac_expected, dict) or not ac_expected:
            continue   # schema validator should have caught this; defensive only
        gaps = diff_outputs(actual, ac_expected, tolerance=tolerance)
        if gaps:
            per_ac_failures[ac_id] = {
                # Keep gap shape consistent with verification_residual_gaps
                "gaps": gaps,
            }

    recipe["verification_failed_by_ac"] = per_ac_failures
    if per_ac_failures:
        # Existing strategy is preserved; this is a SUPPLEMENTARY signal.
        # Operators see both the requirement-level strategy AND the per-AC
        # failures. Don't overwrite a meaningful existing strategy.
        if not recipe.get("verification_strategy"):
            recipe["verification_strategy"] = "per_ac_advisory"
        log.info(
            "R4.3 per-AC verification: %d/%d ACs have contract drift for %s",
            len(per_ac_failures), len(by_ac),
            recipe.get("requirement_id", "?"),
        )
    return recipe


def verify_and_correct(
    recipe: dict[str, Any],
    seed: int,
    *,
    max_iter: int = _DEFAULT_MAX_ITER,
    tolerance: float = _DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Run the closed loop. Returns the (possibly corrected) recipe with
    `verification_attempts`, `verification_strategy`, `actual_outputs`, and
    `verification_failed` stamped on.

    Phase R4.3 — also runs per-AC advisory verification when the recipe
    has `expected_outputs_by_ac` populated (R4.1+R4.2). Per-AC mismatches
    surface in `verification_failed_by_ac` for operator review without
    blocking recipe usability.

    Importantly: this function NEVER raises for verification failure — a
    verification_failed=True recipe is still usable downstream (with reduced
    fidelity); the gate check decides what to do about it.
    """
    # Lazy import to break a circular dependency: generator imports nothing
    # from this module today, but we don't want that to change accidentally.
    from ..fixtures.generator import _generate_rows_from_recipe  # noqa: E501

    expected = recipe.get("expected_outputs") or {}
    if not expected:
        recipe["verification_attempts"] = 0
        recipe["verification_strategy"] = "skipped_no_expected_outputs"
        recipe["actual_outputs"] = {}
        recipe["verification_failed"] = False
        return _verify_per_ac(recipe, tolerance)

    strategy = "closed_form"
    last_actual: dict[str, Any] = {}
    # R130.H — stable-gaps detection. Pre-R130.H the loop ran all `max_iter`
    # iterations even when corrections produced the SAME gap set repeatedly
    # for 10 straight iterations → ~8-12s wasted/req on iterations that
    # couldn't reduce the gap. Post-R130.H: when 2 consecutive iterations
    # produce IDENTICAL gap signatures, exit early with truthful
    # `verification_failed=True` + reason="stable_gaps_non_converging".
    _prior_gap_signature: frozenset | None = None
    for attempt in range(1, max_iter + 1):
        rows = _generate_rows_from_recipe(recipe, seed)
        actual = run_mock_pipeline(rows, recipe)
        last_actual = actual
        # T3 — on the first pass, make STRING/categorical expected_outputs oracle-backed
        # (computed from the rows) instead of LLM-guessed. Categorical mode is stable under
        # the numeric closed-form corrections, so this holds across later iterations.
        if attempt == 1:
            _t3 = _sync_string_expected(recipe, actual)
            if _t3:
                expected = recipe.get("expected_outputs") or {}
                recipe["t3_computed_expected_keys"] = _t3
                log.info("T3: %d string expected_outputs computed from rows for %s: %s",
                         len(_t3), recipe.get("requirement_id", "?"), _t3)
        gaps = diff_outputs(actual, expected, tolerance=tolerance)
        if not gaps:
            log.info(
                "recipe verified for %s in %d attempt(s) (strategy=%s)",
                recipe.get("requirement_id", "?"), attempt, strategy,
            )
            recipe["verification_attempts"] = attempt
            recipe["verification_strategy"] = strategy
            recipe["actual_outputs"] = actual
            recipe["verification_failed"] = False
            # R130.H telemetry — iterations saved vs the max budget
            recipe["verification_iterations_saved"] = max_iter - attempt
            return _verify_per_ac(recipe, tolerance)

        # R130.H — detect non-convergence: if this iteration's gap set is
        # IDENTICAL to the prior iteration's (same keys + same diff
        # signature), corrections aren't making progress. Exit early.
        gap_signature = frozenset(
            (g.get("output_id"), g.get("kind"))
            for g in (gaps or [])
            if isinstance(g, dict)
        )
        if _prior_gap_signature is not None and gap_signature == _prior_gap_signature:
            log.info(
                "R130.H: recipe verifier early-exit for %s at iter=%d/%d "
                "(gap signature stable across consecutive iterations — "
                "corrections not converging)",
                recipe.get("requirement_id", "?"), attempt, max_iter,
            )
            recipe["verification_attempts"] = attempt
            recipe["verification_strategy"] = strategy
            recipe["actual_outputs"] = actual
            recipe["verification_failed"] = True
            recipe["verification_residual_gaps"] = gaps
            recipe["verification_failure_reason"] = "stable_gaps_non_converging"
            recipe["verification_iterations_saved"] = max_iter - attempt
            return _verify_per_ac(recipe, tolerance)
        _prior_gap_signature = gap_signature

        # Try closed-form first; if no primitive could correct it, switch
        # strategy to iterative for this and future attempts.
        corrected = closed_form_correct(recipe, gaps)
        if corrected is None:
            strategy = "iterative"
            corrected = iterative_correct(recipe, gaps, attempt)
        if corrected is None:
            # Nothing in the catalogue could correct these gaps — bail out
            # rather than spinning the rest of the iteration budget.
            log.warning(
                "recipe verifier exhausted corrections for %s after %d attempt(s); "
                "%d gap(s) remain",
                recipe.get("requirement_id", "?"), attempt, len(gaps),
            )
            recipe["verification_attempts"] = attempt
            recipe["verification_strategy"] = strategy
            recipe["actual_outputs"] = actual
            recipe["verification_failed"] = True
            recipe["verification_residual_gaps"] = gaps
            recipe["verification_failure_reason"] = "no_correction_applicable"
            recipe["verification_iterations_saved"] = max_iter - attempt
            return _verify_per_ac(recipe, tolerance)
        recipe = corrected

    # Iteration budget exhausted — last gaps stand
    final_gaps = diff_outputs(last_actual, expected, tolerance=tolerance)
    log.warning(
        "recipe verifier hit max_iter=%d for %s; %d residual gap(s)",
        max_iter, recipe.get("requirement_id", "?"), len(final_gaps),
    )
    recipe["verification_attempts"] = max_iter
    recipe["verification_strategy"] = strategy
    recipe["actual_outputs"] = last_actual
    recipe["verification_failed"] = bool(final_gaps)
    recipe["verification_residual_gaps"] = final_gaps
    if final_gaps:
        # Phase A7: explicit reason so the gate / admin recipe-review queue
        # can distinguish "did not converge" (recipe is mathematically
        # un-correctable for these expected_outputs) from
        # "exhausted catalogue" (no primitive could correct the gap).
        recipe["verification_failure_reason"] = "iterative_did_not_converge"
    return _verify_per_ac(recipe, tolerance)
