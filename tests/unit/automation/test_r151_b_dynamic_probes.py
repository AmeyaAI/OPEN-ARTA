"""R151.B KEYSTONE — dynamic API_PROBES expansion source-level guards.

Verifies the discovery_probe.spec.ts shipped:
  (a) `loadCapturedEndpointPaths` helper (reads
      `.arta/discovered_endpoints/<pid>.json`, filters analytics paths,
      caps at 30)
  (b) Merged-probes call-site (concat hardcoded API_PROBES + dynamic
      dedup against hardcoded prefix set, console.log marker)
  (c) Killswitch env var honored
  (d) R151.D path canonical (probe reads `discovered_endpoints/` not
      `captured_endpoints/`)

Source-level inspection because the probe runs as a Playwright subprocess;
behavioral test would require spawning chromium (out of unit-test scope).
The R151.D + R151.A integration test runs end-to-end via
`docker compose exec` after restart (documented in plan-file verification
section).
"""
from __future__ import annotations

import re
from pathlib import Path

PROBE_PATH = (
    Path(__file__).resolve().parents[3]
    / "src" / "automation" / "playwright" / "discovery_probe.spec.ts"
)


def _probe_source() -> str:
    assert PROBE_PATH.is_file(), f"discovery_probe.spec.ts missing at {PROBE_PATH}"
    return PROBE_PATH.read_text(encoding="utf-8")


# ── R151.D: path canonical name ────────────────────────────────────────────


def test_r151_d_path_canonical_discovered_endpoints():
    """All probe references to captured-endpoints reads use the canonical
    `discovered_endpoints/` path (matches Python writer at
    api_discovery.py:442). Regression guard against the original
    `captured_endpoints/` mismatch that silently failed via ENOENT catch.
    """
    src = _probe_source()
    # NO `captured_endpoints/` path references should remain in path.resolve calls
    bad_pattern = re.compile(
        r"path\.resolve\([^)]*captured_endpoints", re.DOTALL,
    )
    assert not bad_pattern.search(src), (
        "R151.D: stale `captured_endpoints/` path still present in path.resolve "
        "call — should be `discovered_endpoints/`"
    )
    # Confirm canonical path appears at least 3 times (R86.0, R150.I, R151.B)
    canonical_count = src.count(".arta/discovered_endpoints/")
    assert canonical_count >= 3, (
        f"R151.D: expected ≥3 references to canonical `discovered_endpoints/` "
        f"path (R86.0 + R150.I + R151.B); found {canonical_count}"
    )


# ── R151.B: helper definition ──────────────────────────────────────────────


def test_r151_b_helper_function_defined():
    """`loadCapturedEndpointPaths` helper function declared in probe."""
    src = _probe_source()
    assert "function loadCapturedEndpointPaths(" in src, (
        "R151.B: `loadCapturedEndpointPaths` helper missing"
    )
    # Signature: takes optional projectId, returns tuple-array
    assert re.search(
        r"function\s+loadCapturedEndpointPaths\s*\(\s*projectId\s*:",
        src,
    ), "R151.B: helper signature must take `projectId: string | undefined`"


def test_r151_b_helper_uses_canonical_path():
    """R151.B helper reads from `discovered_endpoints/<pid>.json`
    (R151.D's canonical path)."""
    src = _probe_source()
    # The R151.B helper block must include a `.arta/discovered_endpoints/` reference
    # AND it must be inside the loadCapturedEndpointPaths function
    helper_start = src.find("function loadCapturedEndpointPaths(")
    assert helper_start > 0
    # Look ahead ~2000 chars (helper body length)
    helper_block = src[helper_start:helper_start + 2500]
    assert ".arta/discovered_endpoints/" in helper_block, (
        "R151.B: helper must read from canonical `discovered_endpoints/` path"
    )


def test_r151_b_analytics_filter_regex_present():
    """Helper filters records by analytics-domain path keywords."""
    src = _probe_source()
    # The R151.B analytics regex
    assert re.search(
        r"insight\|pipeline\|dashboard\|dataset\|query\|metric",
        src,
    ), "R151.B: analytics-domain filter regex missing"


def test_r151_b_cap_at_30_routes():
    """R151.B helper caps dynamic probes at 30 entries (perf bound)."""
    src = _probe_source()
    # Look for the cap inside the R151.B block
    helper_start = src.find("function loadCapturedEndpointPaths(")
    helper_end = src.find("function ", helper_start + 1)
    helper_block = src[helper_start:helper_end] if helper_end > 0 else src[helper_start:helper_start + 2500]
    assert re.search(r"out\.length\s*>=?\s*30", helper_block), (
        "R151.B: 30-entry cap missing from helper body"
    )


def test_r151_b_killswitch_recognized():
    """`ARTA_R151_B_DYNAMIC_PROBES_DISABLE` env var honored — when set,
    helper returns empty array → merged probes == hardcoded only."""
    src = _probe_source()
    assert "ARTA_R151_B_DYNAMIC_PROBES_DISABLE" in src, (
        "R151.B: killswitch env var name missing"
    )


# ── R151.B: call-site wiring ───────────────────────────────────────────────


def test_r151_b_call_site_merges_with_hardcoded():
    """Call site MUST merge dynamic with hardcoded API_PROBES + dedup."""
    src = _probe_source()
    # Look for the R151.B merge log line
    assert re.search(
        r"R151\.B:\s+API_PROBES\s+hardcoded=",
        src,
    ), "R151.B: merge log line missing at call-site"
    # Verify it uses Set for dedup against hardcoded prefix
    assert "_r151b_hardcodedSet" in src or "_r151b_merged" in src, (
        "R151.B: merge-set variable naming missing"
    )


def test_r151_b_loop_uses_merged_not_hardcoded():
    """The API_PROBES iteration loop uses the MERGED array, not the
    raw hardcoded API_PROBES."""
    src = _probe_source()
    # The loop should iterate _r151b_merged
    assert re.search(
        r"for\s*\(\s*const\s+\[p,\s*idVarName\]\s+of\s+_r151b_merged\s*\)",
        src,
    ), "R151.B: loop must iterate `_r151b_merged`, not API_PROBES directly"


def test_r151_b_uses_target_project_id_env():
    """Call site reads project_id from env (TARGET_PROJECT_ID propagated
    by discovery_executor via spawn_kwargs at line 149)."""
    src = _probe_source()
    # The call site must read project ID from either env var
    assert (
        "TARGET_PROJECT_ID" in src
        and "ARTA_PROJECT_ID" in src
    ), "R151.B: call site must read project_id from TARGET_PROJECT_ID env"
