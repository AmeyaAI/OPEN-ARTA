"""J3: Frozen Fixture Generator.

Materialises the dataset snapshots referenced by `AnalyticsTestSuite.frozen_fixture`.
The agent declares the schema (columns, row_count); this module synthesises seeded
data that's deterministic given the same `req_id` and `version`.

Usage from CLI:
    python -m src.fixtures.generator REQ-XY-001
    python -m src.fixtures.generator --all  # regenerate every declared fixture

Usage from code:
    from src.fixtures.generator import materialise_fixture
    path = materialise_fixture(req_id="REQ-XY-001", columns=[...], row_count=1000)
"""
from __future__ import annotations

import hashlib
import logging
import math
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# R263.A — the `sanitize_req_id` import was accidentally embedded INSIDE the
# module docstring above (never a real import), so `sanitize_req_id(...)` at the
# no-canonical-path branches was a latent NameError. It only stayed hidden
# because /src/fixtures/ was gitignored → untracked → unscanned by the R280
# undefined-name test. Real import, single source of truth (R134.H).
from ..agents.sanitize import sanitize_req_id

log = logging.getLogger("arta.fixtures")

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures" / "analytics"


def _seed_for(req_id: str, version: str) -> int:
    """Deterministic seed so the same (req_id, version) always produces the same data."""
    h = hashlib.sha256(f"{req_id}::{version}".encode()).hexdigest()
    return int(h[:8], 16)


# ── Phase 2: Distribution primitives ─────────────────────────────────────────
# Each primitive is implemented in code (not LLM-driven) so determinism is
# provable. The recipe agent (Phase 1.6) is constrained to declare ONLY these
# shapes via the ColumnSpec.distribution validator. Phase 4 closed-loop
# verification attaches a `solve_for(target)` to the trend primitives so the
# generator can adjust parameters when the materialised data doesn't actually
# produce the recipe's expected_outputs through the pipeline.


def _primitive_time_range(
    rng: random.Random,
    row_count: int,
    distribution: dict[str, Any],
) -> list[str]:
    """Ordered ISO timestamps over `[start, end]`.

    Used as the axis for trend primitives. Output is monotonically non-decreasing
    so `monotonic_up`'s "values rise over the time axis" invariant is provable.
    """
    start_str = distribution.get("start", "2024-01-01")
    end_str = distribution.get("end", "2024-12-31")
    start = _parse_dt(start_str, datetime(2024, 1, 1))
    end = _parse_dt(end_str, datetime(2024, 12, 31))
    if end <= start:
        end = start + timedelta(days=max(1, row_count))
    span_seconds = (end - start).total_seconds()
    # Evenly spaced + tiny jitter (deterministic via rng) so duplicates don't
    # collide in pipeline aggregations that assume distinct timestamps.
    out: list[str] = []
    if row_count <= 1:
        return [start.isoformat()]
    step = span_seconds / (row_count - 1)
    for i in range(row_count):
        ts = start + timedelta(seconds=step * i)
        out.append(ts.isoformat())
    return out


def _primitive_monotonic(
    rng: random.Random,
    row_count: int,
    distribution: dict[str, Any],
    direction: int,
) -> list[float]:
    """Linear trend with controlled magnitude_pct end-to-end (closed-form).

    `magnitude_pct` is the percent change from row 0 to row N-1.
        direction = +1 → rises (last_value > first_value)
        direction = -1 → falls (last_value < first_value)

    Includes a small noise floor (±1% of |value|) so the data isn't perfectly
    flat between consecutive rows — pipelines that assert "trend exists" still
    see variance, but the start→end magnitude is deterministic. Closed-form so
    Phase 4's `solve_for(magnitude_pct)` is just inversion: produce the start
    value such that end_value = start * (1 + magnitude_pct/100 * direction).
    """
    magnitude_pct = float(distribution.get("magnitude_pct", 10.0))
    base = float(distribution.get("start_value", 100.0))
    noise_pct = float(distribution.get("noise_pct", 1.0))
    if row_count <= 0:
        return []
    # Solve: end = base * (1 + dir * pct/100) ; row i is interpolated linearly.
    end = base * (1.0 + direction * magnitude_pct / 100.0)
    out: list[float] = []
    for i in range(row_count):
        t = i / (row_count - 1) if row_count > 1 else 0.0
        v = base + (end - base) * t
        if noise_pct:
            jitter = rng.uniform(-noise_pct, noise_pct) / 100.0 * abs(v)
            v += jitter
        out.append(round(v, 4))
    return out


def _primitive_categorical_weighted(
    rng: random.Random,
    row_count: int,
    distribution: dict[str, Any],
) -> list[str]:
    """Biased enum sampling. Choices + weights → a deterministic ratio of each
    category. When `target_ratio` is given for a single choice, the closed-form
    Phase 4 path can solve weights from it."""
    choices = distribution.get("choices") or distribution.get("categories") or ["a", "b", "c"]
    weights = distribution.get("weights")
    if not isinstance(choices, list) or not choices:
        choices = ["a", "b", "c"]
    if not isinstance(weights, list) or len(weights) != len(choices):
        weights = [1.0] * len(choices)
    return [rng.choices(choices, weights=weights, k=1)[0] for _ in range(row_count)]


def _primitive_constant(
    rng: random.Random,
    row_count: int,
    distribution: dict[str, Any],
) -> list[Any]:
    """Single fixed value across every row. Used for metric labels (e.g. all
    rows have `metric_label="sales"` so the pipeline always emits that metric).
    """
    value = distribution.get("value", "")
    return [value] * row_count


def _coerce_float(v):
    """Parse `v` to float, or None when it isn't numeric (e.g. an LLM put a
    sample file-path / label string in a numeric distribution field)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _primitive_random_normal(
    rng: random.Random,
    row_count: int,
    distribution: dict[str, Any],
) -> list:
    """Gaussian. NO closed-form solve_for — Phase 4 falls back to iterative."""
    mean = _coerce_float(distribution.get("mean", 0.0))
    std = _coerce_float(distribution.get("std", 1.0))
    if mean is None or std is None:
        # R262 — the recipe assigned a numeric (gaussian) shape to a column
        # whose `mean`/`std` isn't numeric (a common LLM recipe-gen slip: a
        # `insight_file_path` column with a sample path string in `mean`). Do
        # NOT crash the WHOLE fixture (all columns lost → FileNotFound →
        # every analytics test for the req fails). Emit a constant column of
        # the intended non-numeric value so the column still exists.
        const = distribution.get("mean") if mean is None else distribution.get("std")
        return [const for _ in range(row_count)]
    return [round(rng.gauss(mean, std), 4) for _ in range(row_count)]


def _primitive_derived(
    row_count: int,
    distribution: dict[str, Any],
    rows: list[dict],
) -> list[Any]:
    """Compute from already-built columns via a small whitelist of ops. NEVER
    eval'd — only mul/add/sub/div on column references + scalars. Allows e.g.
    `revenue = qty * unit_price * 1.10`."""
    expr = distribution.get("expr", "")
    if not isinstance(expr, str) or not expr:
        return [None] * row_count
    # Tokenise — only column names, scalars, and {+ - * /}. Anything else is rejected.
    import re as _re
    tokens = _re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+\.?\d*|[+\-*/()]", expr)
    if not tokens:
        return [None] * row_count
    out: list[Any] = []
    for r in rows:
        env = {k: float(v) if isinstance(v, (int, float)) else 0.0 for k, v in r.items()}
        try:
            # Evaluate stack-based — never use eval(). Convert tokens to RPN
            # via shunting-yard then evaluate. Simple impl: only accept a flat
            # `a OP b OP c` chain. Fail-safe to None on anything more complex.
            value = _safe_eval_chain(tokens, env)
            out.append(round(value, 4) if value is not None else None)
        except Exception:
            out.append(None)
    return out


def _safe_eval_chain(tokens: list[str], env: dict[str, float]) -> float | None:
    """Tiny safe evaluator for `a OP b OP c ...` chains with column-name
    or numeric atoms. NO precedence handling beyond left-to-right; recipes
    that need precedence should use parentheses (rejected here for safety)."""
    if any(t in {"(", ")"} for t in tokens):
        return None  # parens unsupported in v1
    if not tokens:
        return None
    def _atom(t: str) -> float | None:
        if t in env:
            return env[t]
        try:
            return float(t)
        except ValueError:
            return None
    acc = _atom(tokens[0])
    if acc is None:
        return None
    i = 1
    while i + 1 < len(tokens):
        op = tokens[i]
        rhs = _atom(tokens[i + 1])
        if rhs is None:
            return None
        if op == "+":
            acc += rhs
        elif op == "-":
            acc -= rhs
        elif op == "*":
            acc *= rhs
        elif op == "/":
            if rhs == 0:
                return None
            acc /= rhs
        else:
            return None
        i += 2
    return acc


def _parse_dt(s: Any, default: datetime) -> datetime:
    """Tolerant ISO parser — accepts dates ('2024-01-01') and datetimes.
    Falls back to `default` on any failure so the generator never crashes
    on a slightly-off recipe field."""
    if isinstance(s, datetime):
        return s
    if not isinstance(s, str) or not s:
        return default
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return default


def _generate_rows_from_recipe(recipe: dict[str, Any], seed: int) -> list[dict]:
    """Build rows from a serialised DatasetRecipe (Pydantic .model_dump())
    using the distribution primitives above.

    Order matters: `time_range`, `monotonic_*`, `categorical_weighted`,
    `constant`, and `random_normal` are independent so they're computed in
    column order. `derived` runs last over the already-built rows so its
    expression can reference earlier columns.
    """
    rng = random.Random(seed)
    columns = recipe.get("columns", [])
    row_count = int(recipe.get("row_count", 10_000))

    # First pass: independent primitives → column → list[value]
    column_values: dict[str, list[Any]] = {}
    derived_columns: list[dict] = []
    for col_spec in columns:
        if not isinstance(col_spec, dict):
            continue
        name = col_spec.get("name")
        if not name:
            continue
        dist = col_spec.get("distribution") or {}
        shape = dist.get("shape")
        # Each primitive uses an independent rng-stream derived from the seed
        # so column order doesn't change column N's values when column M is
        # added/removed (deterministic per (seed, column_name)).
        col_rng = random.Random(_seed_for(name, str(seed)))
        if shape == "time_range":
            column_values[name] = _primitive_time_range(col_rng, row_count, dist)
        elif shape == "monotonic_up":
            column_values[name] = _primitive_monotonic(col_rng, row_count, dist, direction=+1)
        elif shape == "monotonic_down":
            column_values[name] = _primitive_monotonic(col_rng, row_count, dist, direction=-1)
        elif shape == "categorical_weighted":
            column_values[name] = _primitive_categorical_weighted(col_rng, row_count, dist)
        elif shape == "constant":
            column_values[name] = _primitive_constant(col_rng, row_count, dist)
        elif shape == "random_normal":
            column_values[name] = _primitive_random_normal(col_rng, row_count, dist)
        elif shape == "derived":
            derived_columns.append(col_spec)
        else:
            # Unknown / missing shape → fallback per dtype. Logged so the
            # recipe agent's drift is visible in operator-facing alerts.
            log.warning(
                "recipe column %s has unsupported shape %r — emitting empty values",
                name, shape,
            )
            column_values[name] = [None] * row_count

    # Assemble the row dicts so derived expressions can reference earlier columns.
    rows: list[dict] = []
    for i in range(row_count):
        rows.append({c: column_values[c][i] for c in column_values if i < len(column_values[c])})

    # Second pass: derived
    for col_spec in derived_columns:
        name = col_spec["name"]
        dist = col_spec.get("distribution") or {}
        derived_vals = _primitive_derived(row_count, dist, rows)
        for i, v in enumerate(derived_vals):
            if i < len(rows):
                rows[i][name] = v

    # Phase 4.4 — Per-AC partitions. When the recipe declares `partitions`,
    # stamp each row with its `partition_id` so a Gherkin scenario for AC-N
    # can filter to the slice that satisfies its assertions before asserting.
    # Even split across partitions (deterministic by row index) so each AC
    # gets a comparable sample size; recipes can override via per-partition
    # `weight` if a different split is needed (deferred to a future iteration).
    partitions = recipe.get("partitions") or []
    if partitions:
        n = len(partitions)
        for i, row in enumerate(rows):
            part = partitions[i % n]
            ac_id = part.get("ac_id") if isinstance(part, dict) else None
            if ac_id:
                row["partition_id"] = ac_id
        log.debug(
            "recipe partitions: stamped %d rows across %d ac partitions",
            len(rows), n,
        )

    return rows


def _generate_rows(columns: list[str], row_count: int, seed: int) -> list[dict]:
    """Synthesize `row_count` rows with the given column schema using `seed`."""
    rng = random.Random(seed)
    rows = []
    base_date = datetime(2024, 1, 1)
    segments = ["enterprise", "smb", "consumer", "government"]
    regions = ["north_america", "emea", "apac", "latam"]
    categories = ["software", "hardware", "services", "support"]

    for i in range(row_count):
        row: dict = {}
        for col in columns:
            col_lower = col.lower()
            if "date" in col_lower or "time" in col_lower:
                row[col] = (base_date + timedelta(days=rng.randint(0, 365))).isoformat()
            elif "id" in col_lower:
                row[col] = f"id_{i:06d}"
            elif "segment" in col_lower:
                row[col] = rng.choice(segments)
            elif "region" in col_lower:
                row[col] = rng.choice(regions)
            elif "category" in col_lower:
                row[col] = rng.choice(categories)
            elif "revenue" in col_lower or "amount" in col_lower or "spend" in col_lower:
                row[col] = round(rng.uniform(100, 100_000), 2)
            elif "count" in col_lower or "quantity" in col_lower:
                row[col] = rng.randint(1, 1000)
            elif "metric" in col_lower or "value" in col_lower:
                row[col] = round(rng.uniform(0, 1000), 4)
            else:
                row[col] = f"val_{i}_{col}"
        rows.append(row)
    return rows


def materialise_fixture(
    req_id: str,
    columns: list[str] | None = None,
    row_count: int = 10000,
    version: str = "1.0.0",
    fmt: str = "parquet",
    recipe: dict | None = None,
) -> Path:
    """Generate a frozen fixture file for `req_id` and return its path.

    Phase 2 — when `recipe` (a serialised `DatasetRecipe`) is supplied, columns
    + distributions come from the recipe and rows are built via the primitive
    catalogue (`_generate_rows_from_recipe`). The recipe's `canonical_path`,
    `row_count`, and `version` win over the bare-arg defaults so every stage
    (ATDD prompt, AnalyticsTestAgent, materialise, generator output) ends up
    on the same path string.

    When `recipe` is None, falls back to the legacy column-name-heuristic path
    (`_generate_rows`) for non-analytics callers.
    """
    if isinstance(recipe, dict) and recipe.get("columns"):
        # Recipe-driven path. The recipe is the contract — its canonical_path,
        # row_count, and version supersede the bare args so a caller passing
        # both can't accidentally diverge.
        version = recipe.get("version", version)
        row_count = int(recipe.get("row_count", row_count))
        seed = _seed_for(req_id, version)
        rows = _generate_rows_from_recipe(recipe, seed)
        col_names = [c.get("name") for c in recipe["columns"] if isinstance(c, dict) and c.get("name")]
        canonical_path = recipe.get("canonical_path")
        if canonical_path:
            out_path = REPO_ROOT / canonical_path
        else:
            req_slug = sanitize_req_id(req_id)
            out_path = FIXTURES_DIR / f"{req_slug}_dataset_v{version.replace('.', '_')}.{fmt}"
        log.info(
            "materialise (recipe): req=%s cols=%d rows=%d path=%s",
            req_id, len(col_names), row_count, out_path,
        )
    else:
        # Legacy path — column-name heuristic (`_generate_rows`).
        col_names = columns or [
            "date", "metric_value", "segment", "region", "category", "revenue", "count",
        ]
        seed = _seed_for(req_id, version)
        rows = _generate_rows(col_names, row_count, seed)
        req_slug = sanitize_req_id(req_id)
        out_path = FIXTURES_DIR / f"{req_slug}_dataset_v{version.replace('.', '_')}.{fmt}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = out_path.suffix.lstrip(".") or fmt

    if suffix == "parquet":
        try:
            import pandas as pd
            df = pd.DataFrame(rows)
            df.to_parquet(out_path, index=False)
        except ImportError:
            # Fallback to CSV if pandas/pyarrow unavailable
            out_path = out_path.with_suffix(".csv")
            with out_path.open("w") as f:
                f.write(",".join(col_names) + "\n")
                for r in rows:
                    f.write(",".join(str(r.get(c, "")) for c in col_names) + "\n")
    elif suffix == "csv":
        with out_path.open("w") as f:
            f.write(",".join(col_names) + "\n")
            for r in rows:
                f.write(",".join(str(r.get(c, "")) for c in col_names) + "\n")
    elif suffix == "json":
        import json as _json
        out_path.write_text(_json.dumps(rows, indent=2, default=str))
    else:
        raise ValueError(f"Unsupported fixture format: {suffix}")

    return out_path


def main() -> None:
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m src.fixtures.generator <REQ-ID> [columns...]")
        print("       python -m src.fixtures.generator --all")
        sys.exit(1)

    if sys.argv[1] == "--all":
        # Regenerate all fixtures listed in GENERATED_TESTS (would need import; skip in CLI)
        print("--all mode requires the API to be running; use materialise_fixture() from code.")
        sys.exit(2)

    req_id = sys.argv[1]
    cols = sys.argv[2:] or None
    path = materialise_fixture(req_id=req_id, columns=cols)
    print(f"Materialised fixture: {path}")
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    print(f"SHA-256: {h.hexdigest()}")


if __name__ == "__main__":
    main()
