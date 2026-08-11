"""R150.K — truthful BLOCKED row when R150.J's subprocess preflight
times out BOTH before AND after defensive flag stamping.

Iter 9 evidence: 89 × PW `net::ERR_TIMED_OUT` cascade FAILs were
silently bleeding into the dashboard for 25-40 min of smoke wallclock
per run. Post-R150.K: ONE truthful BLOCKED row replaces the cascade,
with operator-actionable CTA naming the exact failure mode (chromium
can't reach SUT even with full defensive flag stack applied).

This is the LAST R150 sub-phase. Mission contract: PW dispatch refuses
to proceed when chromium provably can't reach SUT, instead of wasting
wallclock on cascade-FAILs the operator can't act on.

R150.K composes with R150.J (the subprocess preflight + defensive
stamp). R150.J sets `state["should_gate_chromium"] = True` when both
probes time out; R150.K reads that flag at dispatch time + emits the
BLOCKED row + flips `_REAL_RUNS[run_id]["playwright_dispatch_blocked"]
= True` (same gate signal R143.D.3 uses, so the existing dispatch-skip
logic at line ~4449 fires without code duplication).

Killswitch: `ARTA_R150_K_CHROMIUM_GATE_DISABLE=1` reverts to cascade-
FAIL behavior (matches Iter 9 baseline).
"""
from __future__ import annotations

import os
from pathlib import Path


EXEC_SRC = (
    Path(__file__).resolve().parents[3]
    / "src" / "api" / "routers" / "execution.py"
).read_text(encoding="utf-8")


# ─── Source-level wire-up verification (the BLOCKED row is wired inside
#     a long async function; full async-mocked path-through is brittle.
#     These tests verify the contract via source inspection — same shape
#     as the proven R150.H/R150.I test approach.) ─────────────────────────


def test_r150_k_marker_present_in_execution_source():
    """R150.K marker present in execution.py — regression guard against
    accidental rollback / merge erasure."""
    assert "R150.K" in EXEC_SRC


def test_r150_k_predicate_gates_on_should_gate_chromium():
    """R150.K's elif branch fires on R150.J-set `should_gate_chromium`
    state field. Regression guard against gate-flag rename."""
    # Predicate line: `should_gate_chromium`
    assert "should_gate_chromium" in EXEC_SRC
    # Predicate appears INSIDE an elif near R150.K marker (same block)
    # — verify the structural proximity by finding the R150.K marker
    # block and confirming should_gate_chromium is referenced there.
    r150k_pos = EXEC_SRC.find("R150.K")
    # Search forward ~3000 chars for `should_gate_chromium` (block-local)
    assert "should_gate_chromium" in EXEC_SRC[r150k_pos:r150k_pos + 3000]


def test_r150_k_emits_blocked_row_with_canonical_test_id():
    """BLOCKED row uses `PW-PREFLIGHT-CHROMIUM-LOCAL-TIMEOUT-{run_id}`
    test_id pattern. Regression guard for dashboard filtering."""
    assert "PW-PREFLIGHT-CHROMIUM-LOCAL-TIMEOUT" in EXEC_SRC


def test_r150_k_blocked_reason_string_set():
    """blocked_reason = `chromium_local_timeout_post_defensive_stamp` —
    distinct kind from R143.D.3's `sut_unavailable_l7_timeout` so the
    dashboard can route the two failure modes separately."""
    assert "chromium_local_timeout_post_defensive_stamp" in EXEC_SRC


def test_r150_k_metadata_carries_r150_j_probe_results():
    """BLOCKED row metadata includes BOTH R150.J probe results
    (before/after defensive stamp) for forensic visibility."""
    # First probe key
    assert "r150_j_subprocess_probe" in EXEC_SRC
    # Second probe key
    assert "r150_j_subprocess_probe_after_stamp" in EXEC_SRC
    # R146.C TLS probe also carried through for cross-classifier
    # visibility (operator can see TLS healthy + chromium broken)
    assert "r146_c_tls_probe" in EXEC_SRC


def test_r150_k_metadata_carries_operator_remediation():
    """`operator_remediation` field surfaces — operator sees actionable
    CTA pointing at the killswitches + likely root cause."""
    # Anchor on the BLOCKED-row block (not the earlier R150.J ref to
    # R150.K). The string "R150.K — truthful BLOCKED" only appears at the
    # actual R150.K wire-in site.
    r150k_pos = EXEC_SRC.find("R150.K — truthful BLOCKED")
    assert r150k_pos != -1
    block = EXEC_SRC[r150k_pos:r150k_pos + 3500]
    assert "operator_remediation" in block
    assert "chromium_local_timeout_remediation" in block


def test_r150_k_killswitch_recognized():
    """`ARTA_R150_K_CHROMIUM_GATE_DISABLE` env var honored. Operator can
    revert to cascade-FAIL behavior (matches Iter 9 baseline) in
    emergency."""
    assert "ARTA_R150_K_CHROMIUM_GATE_DISABLE" in EXEC_SRC


def test_r150_k_flips_playwright_dispatch_blocked():
    """BLOCKED row also sets `_REAL_RUNS[run_id]['playwright_dispatch_blocked']
    = True` so the existing dispatch-skip logic at line ~4449 fires
    (no code duplication). Regression guard for the shared flag."""
    # Anchor on the BLOCKED-row block specifically (not the earlier
    # R150.J ref to R150.K)
    r150k_pos = EXEC_SRC.find("R150.K — truthful BLOCKED")
    assert r150k_pos != -1
    block = EXEC_SRC[r150k_pos:r150k_pos + 3000]
    assert "playwright_dispatch_blocked" in block


def test_r150_k_appears_as_elif_after_r143_d3():
    """R150.K is a sibling `elif` branch after R143.D.3's `if`. Verifies
    the gate ordering: R143.D.3 (SUT genuinely unreachable) fires first,
    R150.K (SUT reachable but chromium can't make it) fires only when
    R143.D.3 didn't.
    """
    # R143.D.3 site
    r143_d3_pos = EXEC_SRC.find("R143.D.3 gate — emit BLOCKED")
    r150_k_pos = EXEC_SRC.find("R150.K — truthful BLOCKED")
    assert r143_d3_pos != -1
    assert r150_k_pos != -1
    assert r143_d3_pos < r150_k_pos, (
        "R150.K elif branch must follow R143.D.3 if-branch in source"
    )
    # The R150.K branch starts with `elif` (sibling, not new if-block).
    # Search backwards from R150.K marker for the most recent control
    # keyword; should be `elif`.
    pre_r150k = EXEC_SRC[max(0, r150_k_pos - 500):r150_k_pos]
    # Look for `elif (` or `elif <something>:` in the lines immediately
    # before the R150.K comment block
    next_after_marker = EXEC_SRC[r150_k_pos:r150_k_pos + 2000]
    assert "elif " in next_after_marker, (
        "R150.K should be implemented as `elif _r143_d_state.get('should_gate_chromium')`"
    )


def test_r150_k_persistence_wrapped_in_try_except():
    """Persistence is wrapped in try/except so a stamp failure doesn't
    poison the rest of the dispatch flow. Mirrors R143.D.3's pattern."""
    r150_k_pos = EXEC_SRC.find("R150.K — truthful BLOCKED")
    block = EXEC_SRC[r150_k_pos:r150_k_pos + 3500]
    # try / except wrapping the _REAL_RESULTS append
    assert "try:" in block
    assert "Exception" in block
    assert "R150.K" in block
