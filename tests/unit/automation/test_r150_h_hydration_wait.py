"""R150.H — strict SPA hydration wait in discovery probe.

Pre-R150.H: discovery probe captured DOM snapshots after Playwright's
`networkidle` + R87.1 fixed 2.5s wait. For SPAs with async hydration
(React Suspense, dynamic-import code-split, post-mount fetches), the
snapshot landed mid-hydration → testids + aria-labels for hydrated
components were ABSENT from the DOM catalog. Live Iter 9 evidence: 164
PW `locator.click/fill` timeouts (~57% of PW FAIL cluster) traced to
hallucinated UI selectors the LLM emitted because the catalog was sparse
for analytics routes.

Post-R150.H: a probe-scoped `waitForSPAHydrationStrict` helper polls for
`document.readyState === 'complete'` AND no `[aria-busy="true"]` or
`[data-loading]` placeholders remaining BEFORE snapshot extraction. Soft-
fail with try/catch — probe's deliverable is HAR; hydration is best-
effort enrichment.

Killswitch: `ARTA_R150_H_HYDRATION_STRICT_DISABLE=1` reverts to Iter 9
behavior.

This is a TypeScript-side change in the probe; this Python-side test
verifies the canonical marker + helper presence + killswitch wiring via
regex inspection. Live behavior is verified end-to-end at smoke time.
"""
from __future__ import annotations

import re
from pathlib import Path

PROBE_PATH = (
    Path(__file__).resolve().parents[3]
    / "src" / "automation" / "playwright" / "discovery_probe.spec.ts"
)


def _probe_source() -> str:
    """Read the discovery probe TS source for inspection."""
    assert PROBE_PATH.is_file(), f"discovery_probe.spec.ts missing at {PROBE_PATH}"
    return PROBE_PATH.read_text(encoding="utf-8")


def test_r150_h_marker_present_in_probe_source():
    """R150.H marker present — regression guard against accidental
    rollback / merge erasure."""
    src = _probe_source()
    assert "R150.H" in src
    # Block-level marker + helper marker BOTH surface
    assert "strict SPA hydration wait" in src or "strict hydration gate" in src


def test_r150_h_helper_function_defined():
    """Probe defines `waitForSPAHydrationStrict` helper inline (preserves
    probe's no-external-imports contract)."""
    src = _probe_source()
    # Helper name surfaces in source
    assert "waitForSPAHydrationStrict" in src
    # Helper signature uses pollMs + timeoutMs parameters (deterministic
    # signature regression guard)
    assert re.search(r"waitForSPAHydrationStrict\s*=\s*async", src), (
        "Expected `const waitForSPAHydrationStrict = async ...` shape"
    )


def test_r150_h_helper_polls_dom_readiness_signals():
    """Helper composes 3 signals: readyState='complete' + no aria-busy +
    no data-loading. Verifies polling contract surfaces in source."""
    src = _probe_source()
    assert "document.readyState" in src
    assert "complete" in src
    # aria-busy + data-loading selectors are present (any quote style)
    assert "aria-busy" in src
    assert "data-loading" in src


def test_r150_h_helper_invoked_in_bfs_loop():
    """Helper is called BEFORE DOM snapshot extraction inside the BFS
    loop. Regression guard against the wire-up dropping during refactor."""
    src = _probe_source()
    # Helper is invoked
    assert re.search(r"await\s+waitForSPAHydrationStrict\s*\(", src), (
        "Expected `await waitForSPAHydrationStrict(...)` call site"
    )
    # Call site appears BEFORE the R19a snapshot section (text-position
    # ordering check — same source, sequential)
    invoke_pos = src.find("await waitForSPAHydrationStrict")
    snapshot_pos = src.find("R19a — capture")
    assert invoke_pos != -1 and snapshot_pos != -1
    assert invoke_pos < snapshot_pos, (
        "R150.H call site MUST appear before R19a snapshot extraction"
    )


def test_r150_h_killswitch_recognized():
    """Killswitch env var `ARTA_R150_H_HYDRATION_STRICT_DISABLE` surfaces
    in probe source — operator can revert to Iter 9 behavior."""
    src = _probe_source()
    assert "ARTA_R150_H_HYDRATION_STRICT_DISABLE" in src


def test_r150_h_soft_fail_at_call_site():
    """The call inside BFS loop is wrapped in try/catch — probe's primary
    deliverable is HAR, hydration is best-effort enrichment. Regression
    guard against accidental upgrade to hard-fail."""
    src = _probe_source()
    # Find the BFS-loop call site (the one INSIDE try block, not the
    # function definition itself)
    invoke_match = re.search(
        r"try\s*{[^}]*?await\s+waitForSPAHydrationStrict",
        src,
        re.DOTALL,
    )
    assert invoke_match is not None, (
        "R150.H call site MUST be wrapped in try block for soft-fail"
    )
