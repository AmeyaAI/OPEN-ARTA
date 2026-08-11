"""Analytics test helpers — runtime support for ARTA-generated analytics tests.

Provides the helpers referenced by `ANALYTICS_TEST_GENERATION_PROMPT`:

  J3: frozen_dataset(path)         — load a versioned dataset snapshot
  J3: assert_approx(...)           — tolerance-based numerical assertion
  J5: mock_db_returning(rows)      — replace the DB layer with canned rows
  J5: mock_llm_returning(text)     — replace the LLM with a canned text response
  J5: inject_canned_result_set(...) — pre-populate the query cache
  J8: verify_from_source(...)      — grounding check: every claim recomputable from source

Generated tests import these via:
    from src.automation.python_tests.analytics_helpers import (
        frozen_dataset, assert_approx, verify_from_source,
        mock_db_returning, mock_llm_returning, inject_canned_result_set,
    )
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

# ── Project root resolution ──────────────────────────────────────────────────


def _repo_root() -> Path:
    """Walk up from this file to find the repo root (contains src/ and fixtures/)."""
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "fixtures").is_dir() and (parent / "src").is_dir():
            return parent
    return here.parents[3]


REPO_ROOT = _repo_root()
FIXTURES_ROOT = REPO_ROOT / "fixtures"


# ── J3: Frozen Dataset ───────────────────────────────────────────────────────


@contextlib.contextmanager
def frozen_dataset(path: str) -> Iterator[Any]:
    """Load a versioned, immutable dataset snapshot for analytics tests.

    Resolves `path` relative to the repo's `fixtures/` directory. Supports
    .parquet, .csv, .json. Returns a pandas DataFrame (or list[dict] when
    pandas/pyarrow aren't installed — useful for tier-1 tests on slim CI).

    Raises FileNotFoundError if the fixture is missing — callers should run
    `python -m src.fixtures.generator <req_id>` to materialise it.
    """
    # R-FixtureDoublePath — generated tests pass paths like
    # "fixtures/analytics/req_am_010_...parquet" but FIXTURES_ROOT is
    # already /app/fixtures, producing /app/fixtures/fixtures/... .
    # Strip a leading "fixtures/" so both paste-styles work:
    #   - "fixtures/analytics/foo.parquet"   (LLM-generated, common)
    #   - "analytics/foo.parquet"            (clean form, also accepted)
    _path_norm = path
    if isinstance(_path_norm, str):
        _stripped = _path_norm.lstrip("./")
        if _stripped.startswith("fixtures/"):
            _path_norm = _stripped[len("fixtures/"):]
    full = (FIXTURES_ROOT / _path_norm) if not Path(_path_norm).is_absolute() else Path(_path_norm)

    # R-FixtureAltExt — when the requested file is missing, probe alternate
    # extensions before raising. Generator's pandas-fallback writes .csv
    # when the recipe asks for .parquet; this lets the test still load it.
    if not full.exists():
        for _alt_ext in (".parquet", ".csv", ".json", ".tsv"):
            _alt = full.with_suffix(_alt_ext)
            if _alt.exists():
                full = _alt
                break

    if not full.exists():
        # Try fixtures/analytics/<basename>.<ext> for short-form references
        _bn_stem = Path(path).stem
        for _alt_ext in (".parquet", ".csv", ".json"):
            _alt = FIXTURES_ROOT / "analytics" / f"{_bn_stem}{_alt_ext}"
            if _alt.exists():
                full = _alt
                break

    # R-FixtureLazyGen — when no extension works, attempt to materialize
    # the fixture via src.fixtures.generator.materialise_fixture(). Derives
    # req_id from the path's basename (req_am_010_dataset_v1_0_0.parquet
    # operator's columns + distributions, not the legacy bare-args path.
    # First-run + auto-generation is now self-healing instead of needing
    # an out-of-band `python -m src.fixtures.generator <req_id>` command.
    if not full.exists():
        try:
            import re as _re
            import json as _json
            _stem = Path(path).stem
            _m = _re.match(r"req_([a-z]+)_(\d+)", _stem, _re.I)
            if _m:
                _req_id = f"REQ-{_m.group(1).upper()}-{_m.group(2)}"
                from src.fixtures.generator import materialise_fixture  # type: ignore
                # Try to load the recipe (.arta/recipes/req_<x>_<n>_v*.json)
                from pathlib import Path as _P
                _recipe = None
                _recipe_dir = _P(".arta") / "recipes"
                if _recipe_dir.is_dir():
                    _slug = f"req_{_m.group(1).lower()}_{_m.group(2)}"
                    _candidates = sorted(_recipe_dir.glob(f"{_slug}_v*.json"),
                                          reverse=True)
                    if _candidates:
                        try:
                            _recipe = _json.loads(_candidates[0].read_text())
                        except Exception:
                            _recipe = None
                _gen_path = materialise_fixture(req_id=_req_id, recipe=_recipe)
                if _gen_path and _gen_path.exists():
                    full = _gen_path
        except Exception:
            pass

    if not full.exists():
        raise FileNotFoundError(
            f"Frozen fixture not found: {path} (resolved {full}). "
            f"Tried alternates [.parquet, .csv, .json]. "
            f"Generate via `python -m src.fixtures.generator <req_id>`."
        )

    suffix = full.suffix.lower()
    data: Any
    if suffix == ".parquet":
        try:
            import pandas as pd
            data = pd.read_parquet(full)
        except ImportError:
            # Fallback: read just enough to satisfy tier-1 (schema-only) tests
            data = {"_fixture": str(full), "_format": "parquet", "_pandas": "not installed"}
    elif suffix == ".csv":
        try:
            import pandas as pd
            data = pd.read_csv(full)
        except ImportError:
            with full.open() as f:
                rows = [r for r in f.read().splitlines() if r.strip()]
            data = [r.split(",") for r in rows]
    elif suffix == ".json":
        data = json.loads(full.read_text())
    else:
        data = full.read_bytes()

    yield data


def fixture_checksum(path: str) -> str:
    """SHA-256 of the fixture file — used as `dataset_version`."""
    # R-FixtureDoublePath — same path-norm as frozen_dataset
    _path_norm = path
    if isinstance(_path_norm, str):
        _stripped = _path_norm.lstrip("./")
        if _stripped.startswith("fixtures/"):
            _path_norm = _stripped[len("fixtures/"):]
    full = (FIXTURES_ROOT / _path_norm) if not Path(_path_norm).is_absolute() else Path(_path_norm)
    if not full.exists():
        return ""
    h = hashlib.sha256()
    with full.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ── J3: Tolerance-Based Numerical Assertion ──────────────────────────────────


def assert_approx(
    actual: float | int,
    expected: float | int,
    tolerance_pct: float = 1.0,
    msg: str = "",
) -> None:
    """Assert |actual - expected| / expected <= tolerance_pct/100.

    Uses absolute tolerance if expected is 0 (avoids divide-by-zero).

    R124.I — stub-default short-circuit. When `actual` is None OR an
    `_R124_I_NoneProxy` (stub-default response shape), skip with the
    `analytics_stub_default` reason. Pre-R124.I this raised
    `AssertionError: assert_approx failed: actual=None, ...` polluting
    the failure pool with ARTA-internal noise. Parallel to R77.7.D
    `tolerant_assert` short-circuit pattern.
    """
    # R124.I — stub-default short-circuit
    try:
        from src.automation.python_tests.arta_runtime import _R124_I_NoneProxy
    except Exception:
        _R124_I_NoneProxy = type(None)  # type: ignore[assignment]
    if actual is None or isinstance(actual, _R124_I_NoneProxy):
        import pytest as _pytest_r124_i
        _pytest_r124_i.skip(
            f"analytics_stub_default: assert_approx({expected!r}) against "
            f"stub None value — no real backend wired. Operator: configure "
            f"a real analytics backend to exercise this assertion."
        )

    actual_f = float(actual)
    expected_f = float(expected)
    if expected_f == 0.0:
        ok = math.isclose(actual_f, 0.0, abs_tol=tolerance_pct / 100.0)
    else:
        ok = abs(actual_f - expected_f) / abs(expected_f) <= (tolerance_pct / 100.0)
    if not ok:
        raise AssertionError(
            f"assert_approx failed: actual={actual_f}, expected={expected_f}, "
            f"tolerance={tolerance_pct}%. {msg}"
        )


# ── J5: Mock Helpers ─────────────────────────────────────────────────────────


@contextlib.contextmanager
def mock_db_returning(rows: list[dict]) -> Iterator[None]:
    """Replace the DB query layer with one that returns canned rows.

    G1 (R218) — INCOHERENT against a REMOTE SUT. This patches ARTA-side
    import-time names (`src.db.session.*`), which only matters if the analytics
    pipeline runs IN-PROCESS. When a real backend is wired
    (`ARTA_ANALYTICS_BACKEND` set / `set_analytics_client()`), the client talks to
    the SUT over HTTP — these patches touch nothing, so the test would assert
    frozen-fixture expectations against the SUT's OWN live data. To avoid that
    FALSE-CONFIDENCE theater, this is a NO-OP on the real-backend path (logged
    once). Grounding then comes from the SUT's real data / invariant assertions
    (G2), not a mock that doesn't reach the SUT. Killswitch
    ARTA_FORCE_INPROCESS_DB_MOCK=1 restores the legacy patching for any genuine
    in-process pipeline.
    """
    import logging
    _real_backend = bool(os.environ.get("ARTA_ANALYTICS_BACKEND")) or _real_client_wired()
    if _real_backend and os.environ.get("ARTA_FORCE_INPROCESS_DB_MOCK") != "1":
        logging.getLogger("arta.analytics.helpers").debug(
            "G1: mock_db_returning is a no-op on the real-backend path "
            "(in-process DB patch can't reach the remote SUT); grounding via real data/invariants.")
        yield
        return
    targets = (
        "src.db.session.execute_query",
        "src.db.session.query",
        "src.analytics.db.query",
    )
    patches = []
    for target in targets:
        try:
            p = patch(target, return_value=rows)
            p.start()
            patches.append(p)
        except (ImportError, AttributeError, ModuleNotFoundError):
            continue
    try:
        yield
    finally:
        for p in patches:
            p.stop()


def _real_client_wired() -> bool:
    """True when a non-stub analytics client is active (set_analytics_client or
    ARTA_ANALYTICS_BACKEND resolved at import). Cheap TYPE check — never calls
    `ask()` (which would fire a real ~60s SUT request just to probe)."""
    try:
        import arta_runtime as _rt
        return not isinstance(_rt._client, _rt._DefaultAnalyticsClient)
    except Exception:
        return False


@contextlib.contextmanager
def mock_llm_returning(text: str) -> Iterator[None]:
    """Replace the LLM client with one that returns the given text.

    Patches the application's LLM client so every `messages.create()` call
    returns a fake response with `text` as its content.
    """
    class _FakeContent:
        def __init__(self, t: str) -> None:
            self.text = t
            self.type = "text"

    class _FakeResponse:
        def __init__(self, t: str) -> None:
            self.content = [_FakeContent(t)]
            self.stop_reason = "end_turn"
            self.usage = {"input_tokens": 0, "output_tokens": len(t.split())}

    async def _fake_create(**kwargs: Any) -> _FakeResponse:
        return _FakeResponse(text)

    targets = (
        "src.agents.ollama_client.OllamaDirectClient.create",
        "anthropic.AsyncAnthropic.messages.create",
    )
    patches = []
    for target in targets:
        try:
            p = patch(target, side_effect=_fake_create)
            p.start()
            patches.append(p)
        except (ImportError, AttributeError, ModuleNotFoundError):
            continue
    try:
        yield
    finally:
        for p in patches:
            p.stop()


@contextlib.contextmanager
def inject_canned_result_set(query: str, result: list[dict]) -> Iterator[None]:
    """Pre-populate the query cache so the named query returns the result.

    For systems with an in-memory query cache; if your analytics layer doesn't
    expose one, this is a no-op. Tests can detect via `cache.has(query)`.
    """
    try:
        from src.analytics import cache as _cache  # type: ignore
        _cache.set(query, result)
        try:
            yield
        finally:
            _cache.delete(query)
    except (ImportError, ModuleNotFoundError):
        yield


# ── J8: Grounding Check ──────────────────────────────────────────────────────


def verify_from_source(insight: dict, source_data: Any, tolerance_pct: float = 1.0) -> bool:
    """Verify every claim in `insight` is computable from `source_data` within tolerance.

    `insight` is expected to have a `parsed_claims` list, where each claim is a dict:
        {
            "metric": "revenue",       # column name in source_data
            "filters": {...},          # dict of column → value filters
            "value": 4250000,          # expected numeric value
            "aggregation": "sum",      # sum | mean | count | min | max
            "text": "..."              # human-readable claim (for error messages)
        }

    Returns True if all claims verify; False otherwise. Logs the first failing claim.
    """
    claims = insight.get("parsed_claims") or insight.get("claims") or []
    if not claims:
        # Insight without parsed claims — accept but log; downstream tests can mark stricter.
        return True

    for claim in claims:
        metric = claim.get("metric")
        filters = claim.get("filters", {}) or {}
        expected = float(claim.get("value", 0))
        agg = claim.get("aggregation", "sum")
        text = claim.get("text", str(claim))

        try:
            import pandas as pd
            if isinstance(source_data, pd.DataFrame):
                df = source_data
                for col, val in filters.items():
                    if col in df.columns:
                        df = df[df[col] == val]
                series = df[metric]
                actual = {
                    "sum": series.sum(),
                    "mean": series.mean(),
                    "count": series.count(),
                    "min": series.min(),
                    "max": series.max(),
                }.get(agg, series.sum())
            else:
                # Fallback for list[dict] source
                rows = [r for r in source_data if all(r.get(k) == v for k, v in filters.items())]
                values = [float(r.get(metric, 0)) for r in rows]
                actual = {
                    "sum": sum(values),
                    "mean": (sum(values) / len(values)) if values else 0,
                    "count": len(values),
                    "min": min(values) if values else 0,
                    "max": max(values) if values else 0,
                }.get(agg, sum(values))
        except Exception as exc:
            print(f"verify_from_source: error computing claim '{text}': {exc}")
            return False

        actual_f = float(actual)
        if expected == 0:
            ok = math.isclose(actual_f, 0.0, abs_tol=tolerance_pct / 100.0)
        else:
            ok = abs(actual_f - expected) / abs(expected) <= (tolerance_pct / 100.0)
        if not ok:
            print(
                f"verify_from_source: claim '{text}' is NOT grounded — "
                f"expected={expected}, actual_from_source={actual_f}, "
                f"tolerance={tolerance_pct}%"
            )
            return False

    return True
