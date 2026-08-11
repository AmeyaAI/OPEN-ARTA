"""Phase J1 — Post-run chain-aware processing.

`ARTAOrchestrator` was supposed to drive Phase B/F/G end-to-end, but it
is never instantiated in production. Production runs flow through:

    execution.trigger_run → _run_playwright/_run_newman/_run_k6 →
    _persist_run_to_db → _trigger_post_execution_healing

This module is the runtime equivalent of `_run_traceability +
defect classification + chain heal proposals` lifted out of the
orchestrator class so it can be invoked from the existing flow.

The five things this MUST do (mapped to the 5 ARTA pillars):

  1. [Traceability]      Ingest captured chains into Neo4j
  2. [Test execution]    Persist `{run_id}-steps.jsonl` evidence artifact
  3. [Traceability]      Compute sequence_integrity (DR-3 single-Cypher)
  4. [Defect intel]      Cascade-aware analyze_failures with seq_integrity
  5. [Self-healing]      Auto-queue chain_reverify / provider_contract_heal

Best-effort throughout: every failure is logged + swallowed so a
mis-configured Neo4j or missing chain sidecar never blocks the main run.
"""
from __future__ import annotations

import hashlib   # R124.D — per-row unique result_id derivation
import logging
import os        # R141.B — env-var read for diagnostic context in warn logs
from pathlib import Path
from typing import Any

log = logging.getLogger("arta.post_run_chain_pipeline")


async def run_chain_aware_post_processing(
    run_id: str,
    project_id: str,
    execution_results: list[dict],
    *,
    results_dir: Path | None = None,
) -> dict:
    """Phase J1 entrypoint. Returns the sequence_integrity dict so callers
    can persist it onto the run record.

    Args:
        run_id: workflow / run identifier; used to fetch step records.
        project_id: project the run belongs to.
        execution_results: list of `ExecutionResult`-like dicts with
            `test_id` + `status`. Same shape as `_REAL_RESULTS[run_id]`.
        results_dir: where to write `{run_id}-steps.jsonl`. Defaults to
            `.arta/results`.

    Returns:
        sequence_integrity dict (cascade_failures, provider_contract_violations,
        unresolved_chain_starts, param_provenance, degraded, degraded_reason).
        Empty/degraded shape on failure — never raises.
    """
    out: dict[str, Any] = {
        "cascade_failures": [],
        "provider_contract_violations": [],
        "unresolved_chain_starts": [],
        "param_provenance": {},
        "degraded": True,
        "degraded_reason": "not_run",
    }
    if not run_id or not project_id:
        out["degraded_reason"] = "missing_run_id_or_project_id"
        return out

    # ── 1. Build TraceabilityAgent + read chains ────────────────────────────
    try:
        from ...agents import api_discovery
        from ...agents.traceability_agent import TraceabilityAgent
    except ImportError as exc:
        log.warning("J1: agent imports failed: %s", exc)
        out["degraded_reason"] = f"import_failed:{exc}"
        return out

    chains = []
    try:
        chains = api_discovery.load_chains(project_id) or []
    except Exception as exc:
        log.debug("J1: load_chains failed: %s", exc)

    neo4j_driver = _resolve_neo4j_driver()
    agent = TraceabilityAgent(neo4j_driver=neo4j_driver)

    # ── 2. Ingest chains → Neo4j (writes Endpoint/EnvVar/CallChain nodes) ──
    if chains and neo4j_driver is not None:
        try:
            await agent.ingest_chains(chains, project_id=project_id)
            log.info("J1: ingested %d chains into Neo4j for project %s", len(chains), project_id)
        except Exception as exc:
            log.warning("J1: ingest_chains failed: %s", exc)

    # ── 3. Read steps + emit JSONL artifact (Phase E2) ──────────────────────
    steps: list[dict] = []
    try:
        from ..routers.execution import get_steps, emit_steps_jsonl
        steps = get_steps(run_id) or []
        rdir = results_dir or Path(".arta/results")
        rdir.mkdir(parents=True, exist_ok=True)
        emitted = emit_steps_jsonl(run_id, rdir)
        if emitted:
            log.info("J1: wrote steps.jsonl for run=%s steps=%d path=%s", run_id, len(steps), emitted)
    except Exception as exc:
        log.debug("J1: step retrieval/emission failed: %s", exc)

    # ── 4. Compute sequence_integrity ──────────────────────────────────────
    try:
        seq_integrity = await agent.compute_sequence_integrity(
            execution_results,
            project_id=project_id,
            chains=chains,
            steps=steps,
        )
        if isinstance(seq_integrity, dict):
            out = seq_integrity
    except Exception as exc:
        log.warning("J1: compute_sequence_integrity failed: %s", exc)
        out["degraded_reason"] = f"seq_integrity_compute_failed:{type(exc).__name__}"

    # ── 5. Cascade-aware defect classification + heal proposals ────────────
    failures = [
        r for r in (execution_results or [])
        if isinstance(r, dict)
        and r.get("status") == "FAIL"
        and r.get("automation_tool") != "preflight"
        and r.get("failure_class") != "environment"
    ]
    defects: list[dict] = []
    if failures:
        # R49.1 — wallclock budget (mirror of the sync-path fix). LLM
        # RCA can stall for 30-60 min on a run with thousands of
        # failures; cap at 5 min and fall through to deterministic
        # clustering so Jira/R48.3/regen aren't gated indefinitely.
        import asyncio as _aio_49_1_full
        _RCA_BUDGET_SEC_FULL = 300
        # R55.8 — fetch test_history for Layer 2 classification BEFORE
        # the LLM RCA. Cheap (~100ms for 1-10k test_ids), unlocks
        # ~5-15% accurate classification for tests with prior history.
        _r55_8_test_ids = [
            f.get("test_id") for f in failures if isinstance(f, dict) and f.get("test_id")
        ]
        try:
            _r55_8_history = await _build_test_history(run_id, _r55_8_test_ids)
        except Exception as _r55_8_exc:
            log.debug("R55.8: history fetch failed (full path): %s", _r55_8_exc)
            _r55_8_history = {}
        try:
            defects = await _aio_49_1_full.wait_for(
                _classify_defects_with_seq_integrity(failures, out, test_history=_r55_8_history),
                timeout=_RCA_BUDGET_SEC_FULL,
            )
        except _aio_49_1_full.TimeoutError:
            log.warning(
                "R49.1: J1 (full path) defect classification exceeded %ds "
                "budget — falling through to deterministic fallback",
                _RCA_BUDGET_SEC_FULL,
            )
        except Exception as exc:
            log.warning("J1: defect classification failed: %s", exc)
        # R-DefectFallback — when LLM-driven RCA returns 0 defects (Ollama
        # isn't wired into app.state.anthropic, or remote LLM is down),
        # produce a deterministic defect per failure cluster so Pillar 4
        # surfaces SOMETHING.
        #
        # R141.B — replace the silent "log.info only when defects > 0"
        # behavior with truthful WARN log on EVERY fallback fire, and add
        # a blanket-defect safety net when BOTH the LLM cascade AND the
        # deterministic builder return empty. Pre-R141.B: R135 evidence
        # showed 522+224 failures producing 0 defects across 2 iters with
        # ZERO log signal — Pillar 4 mission violation. Post-R141.B:
        # operator sees the per-iter classifier-health truthfully via the
        # dashboard tile R141.C consumes downstream.
        if not defects and failures:
            deterministic = _build_deterministic_defects(failures)
            log.warning(
                "R141.B: LLM RCA produced 0 defects from %d failures; "
                "deterministic fallback emitted %d defects. "
                "Operator: investigate Claude CLI state "
                "(ARTA_CLAUDE_CLI_MAX_TURNS=%s; R141.A.0 diagnostic visible "
                "in INFO logs above)",
                len(failures), len(deterministic),
                os.environ.get("ARTA_CLAUDE_CLI_MAX_TURNS", "2"),
            )
            defects = deterministic

        # R141.B blanket-defect safety net — when BOTH the LLM cascade and
        # the deterministic builder return empty (e.g., R135 Iter 1 evidence
        # showed every cluster's failure shape was upstream-blocked with no
        # parseable error_message), emit ONE blanket defect per failures
        # batch so the dashboard NEVER reads "0 defects" silently while
        # hundreds of FAILs sit in execution_results. Severity=medium,
        # triage_category=unclassified, triage_signals=[r141_b_blanket].
        # Operator sees the alert via R141.C health tile.
        if not defects and failures:
            log.error(
                "R141.B: BOTH LLM cascade AND deterministic fallback returned "
                "0 defects from %d failures. Emitting 1 blanket defect to "
                "keep dashboard truthful (R141.C tile will alert). "
                "Investigate _build_deterministic_defects against the R135 "
                "failure shape.",
                len(failures),
            )
            try:
                defects = [_r141_b_build_blanket_defect(failures, run_id)]
            except Exception as _r141_b_exc:
                log.warning("R141.B: blanket defect construction skipped: %s", _r141_b_exc)

    # ── R29.1: Persist ExecutionResult + Defect nodes/edges to Neo4j ───────
    # Pre-R29.1 the production pipeline only persisted Endpoint/EnvVar/CallChain
    # via `ingest_chains`. R28.6's per-run traceability graph endpoint then
    # returned empty Result lists because no production code wrote Result or
    # Defect nodes. Frontend rendered TC nodes in PENDING state forever.
    #
    # This wires Result + Defect persistence using the existing Cypher
    # templates (UPSERT_RESULT/LINK_TC_RESULT/LINK_RESULT_DEFECT in
    # traceability_agent.py:60-93) so the chain Req → AC → TC → Result →
    # Defect actually exists in Neo4j after every run.
    try:
        link_stats = await _link_results_and_defects_to_neo4j(
            agent, run_id, project_id, execution_results, defects,
        )
        log.info(
            "R29.1: wrote %d ExecutionResult nodes + linked %d defects for run %s",
            link_stats.get("results_written", 0),
            link_stats.get("defects_linked", 0),
            run_id,
        )
    except Exception as exc:
        log.warning("R29.1: result-graph write failed for run %s: %s", run_id, exc)

    # ── R30.6: Chain-health sanity check ──────────────────────────────────
    # After R29.1 writes Result + Defect edges, traverse the canonical
    # chain and count per-requirement edge depth. Stamps
    # `traceability_chain_health` onto _REAL_RUNS so the gate
    # (_check_traceability_health) can WARN when any requirement has 0
    # results despite having tests dispatched (broken trace chain).
    try:
        chain_health = await _compute_traceability_chain_health(
            agent, run_id, project_id,
        )
        # R55.13 — separate Cypher query for endpoint coverage. Merged
        # into the same chain_health dict so it lands on _REAL_RUNS with
        # one stamp + flows through the gate via _check_endpoint_coverage.
        # After R55.7 wired Result→Endpoint for Newman (96.6% of dispatch
        # volume), this metric becomes meaningful for the first time.
        try:
            ep_coverage = await _compute_endpoint_coverage(
                agent, run_id, project_id,
            )
            chain_health["endpoint_coverage"] = ep_coverage
            log.info(
                "R55.13: endpoint coverage for run %s — %d/%d (%.1f%%)",
                run_id,
                ep_coverage.get("covered_endpoints", 0),
                ep_coverage.get("total_endpoints", 0),
                ep_coverage.get("coverage_pct", 0.0),
            )
        except Exception as _r55_13_exc:
            log.debug(
                "R55.13: endpoint coverage skipped for run %s: %s",
                run_id, _r55_13_exc,
            )
        try:
            from ..routers.execution import _REAL_RUNS
            if run_id in _REAL_RUNS:
                _REAL_RUNS[run_id]["traceability_chain_health"] = chain_health
        except Exception:
            pass
        if chain_health.get("requirements_without_results"):
            log.warning(
                "R30.6: %d requirement(s) had tests dispatched but 0 "
                "ExecutionResult edges in Neo4j for run %s — traceability "
                "chain partially broken.",
                len(chain_health["requirements_without_results"]), run_id,
            )
    except Exception as exc:
        log.debug("R30.6: chain-health check failed for run %s: %s", run_id, exc)

    if defects:
        try:
            await _queue_chain_heal_proposals(defects)
        except Exception as exc:
            log.warning("J1: heal proposal queueing failed: %s", exc)
        # R37.4 — auto-file Jira tickets for sut_regression defects
        # BEFORE persisting so the jira_key lands in the persisted
        # metadata (gives the dashboard a stable link from defect → ticket).
        # Best-effort: silently no-ops when Jira creds aren't configured.
        try:
            jira_count = await _create_jira_for_sut_regressions(
                defects, run_id=run_id, project_id=project_id,
            )
            if jira_count:
                log.info(
                    "R37.4: filed %d Jira ticket(s) for sut_regression defects (run %s)",
                    jira_count, run_id,
                )
        except Exception as exc:
            log.warning("R37.4: Jira filing failed for run %s: %s", run_id, exc)
        # R-DefectPersist — write the classified defects to the `defects`
        # table so /api/defects + dashboard surface them. Pre-fix the
        # defects were stamped onto _REAL_RUNS only (in-memory), so the
        # operator's "Open P0 defects: 0" badge was structurally
        # incapable of going non-zero from real runs (synthetic seeds
        # were the only DB rows).
        try:
            persisted = await _persist_defects_to_db(
                defects, run_id=run_id, project_id=project_id,
            )
            log.info(
                "J1: classified %d defects (cascade=%d, root=%d, pcv=%d) "
                "for run %s, persisted %d to DB",
                len(defects),
                sum(1 for d in defects if d.get("defect_class") == "cascade_failure"),
                sum(1 for d in defects if d.get("defect_class") == "rca_root_cause"),
                sum(1 for d in defects if d.get("defect_class") == "provider_contract_violation"),
                run_id, persisted,
            )
        except Exception as exc:
            log.warning("J1: defect persistence failed: %s", exc)

    # Stamp defects onto the run state so the gate router and frontend
    # can read both the cascade-aware classification and the proposal IDs.
    try:
        from ..routers.execution import _REAL_RUNS
        if run_id in _REAL_RUNS:
            _REAL_RUNS[run_id]["sequence_integrity"] = out
            if defects:
                _REAL_RUNS[run_id]["chain_aware_defects"] = defects
    except Exception as exc:
        log.debug("J1: persisting sequence_integrity onto _REAL_RUNS failed: %s", exc)

    # R48.3 — close the ARTA improvement loop. For every tool below
    # the 95% RAW target, route FAIL rows by triage_category:
    # test_gen_bug → regen marker; sut_regression → ticket counter;
    # cascade/skip → replay counter. The next R42.6 consumer cycle
    # (every 5 min) drains the regen markers; next dispatched run
    # picks up the corrections. Loop runs once per run automatically.
    try:
        from .improvement_loop import post_run_coordinate
        loop_result = await post_run_coordinate(run_id, project_id)
        log.info(
            "R48.3: improvement-loop result for %s: %s",
            run_id, loop_result.get("actions_queued"),
        )
    except Exception as exc:
        log.warning("R48.3: improvement-loop coordinator failed for %s: %s",
                    run_id, exc)

    return out


def _build_deterministic_defects(failures: list[dict]) -> list[dict]:
    """R-DefectFallback — group failures by error-signature into one
    defect per cluster. Cheap, no LLM, always works.

    Cluster key:
      - HTTP status code from error message ("status code 200 but got 500" → status_500)
      - Otherwise first 80 chars of error message

    Severity:
      - 5xx → critical (server crashed)
      - 4xx 401/403 → high (auth/permission misconfiguration)
      - 4xx 4** other → medium (test/SUT validation mismatch)
      - non-HTTP → medium

    Returns one defect per cluster with `affected_tests` listing all
    test_ids that share the signature. Operator gets one ticket per
    distinct failure mode rather than 500 tickets for 500 5xx failures.
    """
    import re as _re
    from collections import defaultdict

    clusters: dict[str, dict] = defaultdict(lambda: {
        "test_ids": [], "errors": [], "tools": set(),
        "status_code": None, "first_error": "",
        # R67.E — carry the source failure's triage signals forward so the
        # defect dict can be persisted with non-NULL triage_category +
        # subtype. Pre-R67.E _build_deterministic_defects dropped these
        # fields entirely; defects.triage_category landed as NULL for ALL
        # rows; R34.1 P0 routing + R37.4 Jira filter + R48.3 routing all
        # silently no-op'd because they read `triage_category` to decide.
        "triage_category": None,
        "triage_confidence": None,
        "triage_signals": [],
        "operator_review_subclass": None,
        "sut_contract_change_subtype": None,
    })

    # R124.F.1 — perf threshold normalizer. Pre-R124.F.1, every distinct
    # k6/perf latency value generated its own defect (`expected 10033 to
    # be below 5000`, `expected 10108 to be below 5000`, etc. → cardinality
    # EXPLOSION). Normalize to (budget, overshoot bucket).
    _PERF_RE = _re.compile(
        r"expected\s+(\d+)\s+to\s+be\s+below\s+(\d+)",
        _re.IGNORECASE,
    )

    for _row_idx, f in enumerate(failures):
        if not isinstance(f, dict):
            continue
        em = (f.get("error_message") or f.get("error") or "").strip()
        # Normalize whitespace + ANSI escapes for clustering
        em_clean = _re.sub(r"\x1b\[[0-9;]*m", "", em)
        em_clean = _re.sub(r"\s+", " ", em_clean)[:300]

        # Extract HTTP status from common patterns
        status_match = _re.search(
            r"(?:status code\s+\d+\s+but\s+got|status\s+code|got|received)\s*:?\s*(\d{3})",
            em_clean, _re.IGNORECASE,
        ) or _re.search(r"\b([45]\d\d)\b", em_clean)
        status = int(status_match.group(1)) if status_match else None

        # R124.F.1 — perf threshold normalization (BEFORE other key derivation)
        perf_match = _PERF_RE.search(em_clean)
        is_perf = bool(perf_match)
        if is_perf:
            actual_ms = int(perf_match.group(1))
            budget_ms = int(perf_match.group(2))
            overshoot_pct = (
                ((actual_ms - budget_ms) / max(budget_ms, 1)) * 100
                if budget_ms > 0 else 0
            )
            bucket = (
                "just_over" if overshoot_pct < 20
                else "moderate" if overshoot_pct < 100
                else "severe"
            )
            key = f"perf_threshold_breach|budget_{budget_ms}|{bucket}"
            # Override em_clean for downstream defect title use (collapse
            # individual ms-value into the bucket name)
            em_clean = (
                f"perf threshold breach (budget={budget_ms}ms, "
                f"actual~{actual_ms}ms, {bucket})"
            )
        elif status:
            # R124.F.2 — add 40-char discriminator so status-bound clusters
            # still differentiate distinct error texts. Pre-R124.F.2, all 8x
            # 500 "Internal authorization error" collapsed with 8x 500
            # "NullPointerException" — different SUT bugs, one defect.
            key = f"http_{status}|{em_clean[:40]}"
        else:
            # R124.F.2 — incorporate first-token + first-sentence so
            # "Cannot read property 'X'" + "Cannot read property 'Y'"
            # produce TWO clusters (was one due to 80-char msg prefix
            # collapse). The first sentence contains the quoted property
            # name; that's the discriminator. Pre-R124.F all 17 mixed
            # X/Y messages collapsed into ONE cluster; post-R124.F the
            # quoted-name carries the distinction.
            #
            # Earlier draft also included a test_id family prefix, but
            # that over-split clusters when tests within the SAME
            # requirement (TC-AM-001-1, TC-AM-001-2, ...) had the same
            # error — 3 tests becoming 3 clusters defeats the purpose.
            first_tok_match = _re.match(r"\W*(\w[\w-]{2,})", em_clean)
            tok = first_tok_match.group(1)[:24] if first_tok_match else "na"
            key = f"msg:{tok}|{em_clean.split('.')[0][:60]}"

        c = clusters[key]
        c["test_ids"].append(f.get("test_id") or "?")
        c["errors"].append(em_clean[:200])
        c["tools"].add(f.get("automation_tool") or "?")
        c["status_code"] = status
        if not c["first_error"]:
            c["first_error"] = em_clean[:300]

        # R67.E — capture triage from the FIRST failure in each cluster.
        # The classifier (defect_intel._triage_failure) stamps these on
        # execution_results.metadata for every FAIL row (via R35.1).
        # Reading them here ensures the deterministic-fallback defect
        # inherits the same classification as the failures that birthed it.
        if c["triage_category"] is None:
            # Failures may carry triage at top-level OR nested under .metadata
            # (depending on R35.1 / R34.2 stamping path). Check both.
            f_md = f.get("metadata") if isinstance(f.get("metadata"), dict) else {}
            c["triage_category"] = (
                f.get("triage_category")
                or f_md.get("triage_category")
                or ((f.get("triage") or {}) if isinstance(f.get("triage"), dict) else {}).get("triage_category")
            )
            c["triage_confidence"] = (
                f.get("triage_confidence")
                or f_md.get("triage_confidence")
                or ((f.get("triage") or {}) if isinstance(f.get("triage"), dict) else {}).get("triage_confidence")
            )
            _signals = (
                f.get("triage_signals")
                or f_md.get("triage_signals")
                or ((f.get("triage") or {}) if isinstance(f.get("triage"), dict) else {}).get("triage_signals")
            )
            c["triage_signals"] = _signals if isinstance(_signals, list) else []
            c["operator_review_subclass"] = (
                f.get("operator_review_subclass")
                or f_md.get("operator_review_subclass")
            )
            c["sut_contract_change_subtype"] = (
                f.get("sut_contract_change_subtype")
                or f_md.get("sut_contract_change_subtype")
            )

    # R301.E — real HTTP 5xx codes only. Pre-R301.E `500 <= status < 600`
    # accepted ANY 3-digit number in that range, so a bare number misread from a
    # pytest analytics failure (594/529/557/584 — not real HTTP statuses) was
    # titled "SUT 5xx error pattern (594)" and mis-presented as an HTTP server
    # error (run-446c26). Restrict to the actual HTTP 5xx set so a pytest
    # assertion failure is never dressed up as an HTTP 5xx.
    from ...agents.defect_intel import _VALID_HTTP_STATUS as _R301E_VALID
    _R301E_REAL_5XX = frozenset(s for s in _R301E_VALID if 500 <= s < 600)

    # Build one defect per cluster
    defects: list[dict] = []
    for key, c in clusters.items():
        n = len(c["test_ids"])
        status = c["status_code"]
        if status and status in _R301E_REAL_5XX:
            severity, priority = "critical", "P0"
            title = f"SUT 5xx error pattern ({status}) on {n} test{'s' if n>1 else ''}"
            category = "sut_server_error"
            fix = (f"Investigate the SUT backend logs around the test runs. "
                   f"5xx is the server's responsibility; ARTA's job is signal.")
        elif status == 401:
            severity, priority = "high", "P1"
            title = f"Auth (401) on {n} endpoint{'s' if n>1 else ''}"
            category = "auth_scope_mismatch"
            fix = ("The session token may not have the scope required for these "
                   "endpoints, or the SUT changed its auth contract. Check the "
                   "operator's role in the project's environments.")
        elif status == 403:
            severity, priority = "high", "P1"
            title = f"Forbidden (403) on {n} endpoint{'s' if n>1 else ''}"
            category = "permission_denied"
            fix = "Operator's role lacks the permission these endpoints require."
        elif status == 415:
            severity, priority = "medium", "P2"
            title = f"Unsupported Media Type (415) on {n} request{'s' if n>1 else ''}"
            category = "test_payload_content_type"
            fix = ("Test-gen produced wrong Content-Type header. Likely the LLM "
                   "wrote 'application/json' where the SUT expects "
                   "'multipart/form-data' or vice versa.")
        elif status == 400:
            severity, priority = "medium", "P2"
            title = f"Bad Request (400) on {n} request{'s' if n>1 else ''}"
            category = "test_payload_validation"
            fix = ("Test-gen produced a payload the SUT rejected. Likely missing "
                   "required field or wrong field type. Refine the prompt with "
                   "the SUT's OpenAPI schema for these endpoints.")
        elif status == 404:
            severity, priority = "low", "P2"
            title = f"Not Found (404) on {n} request{'s' if n>1 else ''}"
            category = "spec_drift_or_unset_param"
            fix = ("Either the endpoint moved (spec drift) or the test referenced "
                   "an unfilled env-var that became a sentinel string. Run discovery + "
                   "verify the project's environment variables are filled.")
        else:
            severity, priority = "medium", "P2"
            title = f"{n} test{'s' if n>1 else ''} failed with: {c['first_error'][:60]}"
            category = "unclassified"
            fix = "Inspect the run-detail page for full error context."

        # R124.F.2 — slug widened [:20] → [:40] so the discriminator added
        # to the cluster key (perf bucket, status+message head, family
        # suffix) survives truncation. Pre-R124.F: `DEF-MSG-EXPECTED-` was
        # all that survived for the dominant perf cluster.
        defect_id = f"DEF-{key.upper().replace(':','-').replace('|','-').replace(' ','-')[:40]}"

        # R67.E — derive `triage_category` if the cluster didn't inherit
        # one from source-failure metadata (e.g., legacy paths or fail rows
        # written before R35.1's inline triage stamping). Use the cluster's
        # category (sut_server_error / auth_scope_mismatch / etc.) to map to
        # the canonical triage taxonomy.
        derived_triage = c["triage_category"]
        if not derived_triage:
            if category == "sut_server_error":
                derived_triage = "sut_regression"
            elif category in ("auth_scope_mismatch", "permission_denied"):
                derived_triage = "operator_review"
            elif category in (
                "test_payload_content_type", "test_payload_validation",
            ):
                derived_triage = "test_gen_bug"
            elif category == "spec_drift_or_unset_param":
                derived_triage = "sut_contract_change"
            else:
                derived_triage = "operator_review"

        defect = {
            "defect_id": defect_id,
            "title": title,
            "description": (
                f"Error signature: {c['first_error'][:200]}\n\n"
                f"Tools: {', '.join(sorted(c['tools']))}\n"
                f"Affected tests ({n}): {', '.join(c['test_ids'][:8])}"
                f"{', ...' if n > 8 else ''}"
            ),
            "severity": severity,
            "priority": priority,
            "status": "open",
            "test_id": c["test_ids"][0] if c["test_ids"] else None,
            "affected_tests": c["test_ids"],
            "cascade_count": n,
            "root_cause": c["first_error"][:500],
            "root_cause_category": category,
            "suggested_fix": fix,
            "defect_class": "rca_root_cause",
            "auto_detected": True,
            "detection_method": "deterministic_fallback",
            # R67.E KEYSTONE — carry triage so defects.triage_category column
            # + metadata.triage_category JSONB get populated. Unlocks:
            #   - R34.1 5xx → P0 priority routing
            #   - R37.4 Jira auto-file (filters on triage_category=sut_regression)
            #   - R48.3 improvement-loop routing (correct bucket counts)
            #   - Dashboard "Open P0 defects" badge (correct filter)
            "triage_category": derived_triage,
            "triage_confidence": c["triage_confidence"] or 0.85,
            "triage_signals": c["triage_signals"] or [],
        }
        if c["operator_review_subclass"]:
            defect["operator_review_subclass"] = c["operator_review_subclass"]
        if c["sut_contract_change_subtype"]:
            defect["sut_contract_change_subtype"] = c["sut_contract_change_subtype"]
        defects.append(defect)
    return defects


def _r141_b_build_blanket_defect(failures: list[dict], run_id: str) -> dict:
    """R141.B — last-resort blanket defect when BOTH the LLM cascade AND
    the deterministic builder return empty. Emits ONE defect with
    `triage_category="unclassified"` and `triage_signals=["r141_b_blanket"]`
    so the dashboard NEVER reads "0 defects" while hundreds of FAILs sit
    in execution_results. Tools + sample test_ids carry forward so the
    operator can drill into specific failures.

    Severity = medium (NOT critical) because we don't know if these are
    real SUT regressions or ARTA gen bugs — the unclassified signal
    surfaces the classifier outage to the operator via R141.C tile.
    """
    tools = sorted({
        f.get("automation_tool") for f in (failures or [])
        if isinstance(f, dict) and f.get("automation_tool")
    })
    sample_test_ids = [
        f.get("test_id") for f in (failures or [])[:10]
        if isinstance(f, dict) and f.get("test_id")
    ]
    return {
        "defect_id": f"DEF-R141-B-BLANKET-{run_id[:12]}",
        "title": (
            f"R141.B: defect classifier outage — {len(failures)} failures "
            f"unclassified ({len(tools)} tool(s): {', '.join(tools) or 'unknown'})"
        ),
        "description": (
            "Both the LLM RCA cascade (R49.1) and the deterministic "
            "fallback builder (`_build_deterministic_defects`) returned 0 "
            "defects from the run's failure set. This blanket defect "
            "surfaces the classifier outage to the operator dashboard "
            "(R141.C health tile). Investigate Claude CLI state via the "
            "R141.A.0 diagnostic INFO logs."
        ),
        "severity": "medium",
        "triage_category": "unclassified",
        "triage_confidence": 0.0,
        "triage_signals": ["r141_b_blanket"],
        "affected_tests": sample_test_ids,
        "cluster_size": len(failures or []),
        "tools": tools,
    }


async def _create_jira_for_sut_regressions(
    defects: list[dict],
    *,
    run_id: str,
    project_id: str,
) -> int:
    """R37.4 — auto-file Jira tickets for high-priority sut_regression defects.

    Closes the SUT-quality feedback loop: when ARTA detects a real
    backend regression, the operator's existing Jira workflow gets a
    ticket they can triage / assign / fix. Without this, sut_regression
    defects sit in ARTA's DB with no path to the SUT team's queue.

    Idempotent via the defect_id stable key — if a ticket already
    exists in the defect's metadata.jira_key, skip creation. Only
    files for triage_category=sut_regression AND priority in {P0,P1}
    so we don't flood Jira with low-severity noise.

    Returns the number of new Jira tickets created. Best-effort: a
    Jira outage doesn't fail the run.
    """
    try:
        from ...integrations.jira_client import JiraClient
    except Exception as exc:
        log.debug("R37.4: JiraClient import failed: %s", exc)
        return 0

    client = JiraClient()
    if not client.url or not client.email or not client.api_token:
        # Jira not configured — skip silently. Operator hasn't opted in.
        return 0
    if not await client.connect():
        return 0

    created = 0
    for d in defects:
        if not isinstance(d, dict):
            continue
        triage_cat = (
            d.get("triage_category")
            or (d.get("triage") or {}).get("triage_category")
        )
        if triage_cat != "sut_regression":
            continue
        priority = (d.get("priority") or "P2").upper()
        if priority not in ("P0", "P1"):
            continue
        md = d.get("metadata") or {}
        if isinstance(md, dict) and md.get("jira_key"):
            continue   # already filed

        title = d.get("title") or "ARTA-detected SUT regression"
        affected = d.get("affected_tests") or []
        sample_error = (
            d.get("error_message")
            or d.get("description")
            or md.get("error_message")
            or ""
        )[:1500]
        suggested_fix = d.get("suggested_fix") or ""
        body_lines = [
            f"*ARTA detected a SUT regression in run `{run_id}`.*",
            "",
            f"*Triage category:* sut_regression (confidence={d.get('triage_confidence', 0)})",
            f"*Severity:* {d.get('severity', 'medium')} | *Priority:* {priority}",
            f"*Affected tests:* {len(affected)}",
            "",
            "*Sample error:*",
            "{code}",
            sample_error or "(no error message captured)",
            "{code}",
        ]
        if suggested_fix:
            body_lines += ["", "*Suggested fix:*", suggested_fix]
        body_lines += [
            "",
            f"*ARTA defect ID:* `{d.get('defect_id')}`",
            f"*Project ID:* `{project_id}`",
        ]
        body = "\n".join(body_lines)

        sev = (d.get("severity") or "medium").lower()
        jira_priority = JiraClient.severity_to_jira_priority(sev) \
            if hasattr(JiraClient, "severity_to_jira_priority") else "High"
        try:
            result = await client.create_issue(
                summary=f"[ARTA] {title}"[:200],
                description=body,
                issue_type="Bug",
                priority=jira_priority,
                labels=["arta", "sut_regression", priority.lower()],
            )
            jira_key = result.get("key") if isinstance(result, dict) else None
            if jira_key:
                # Stamp onto the in-memory defect at TWO paths:
                #   1. metadata.jira_key (legacy callers reading JSONB)
                #   2. d["jira_key"] — the Defect model's dedicated
                #      column. _persist_defects_to_db doesn't currently
                #      project this column from the dict; R37.4 extends
                #      the persist path to do so.
                if not isinstance(md, dict):
                    md = {}
                md["jira_key"] = jira_key
                md["jira_created_at"] = __import__("datetime").datetime.utcnow().isoformat() + "Z"
                d["metadata"] = md
                d["jira_key"] = jira_key
                if client.url:
                    d["jira_url"] = f"{client.url.rstrip('/')}/browse/{jira_key}"
                created += 1
                log.info(
                    "R37.4: filed Jira %s for defect=%s (priority=%s) run=%s",
                    jira_key, d.get("defect_id"), priority, run_id,
                )
        except Exception as exc:
            log.warning(
                "R37.4: Jira create failed for defect=%s: %s",
                d.get("defect_id"), exc,
            )
    return created


async def _persist_defects_to_db(
    defects: list[dict],
    *,
    run_id: str,
    project_id: str,
) -> int:
    """R-DefectPersist — insert classified defects into the `defects` table.

    Args:
        defects: list of defect dicts from `analyze_failures` — each has
            `test_id`, `title`, `severity`, `defect_class`,
            `root_cause`, `root_cause_category`, `suggested_fix`, etc.
        run_id: external run identifier (run-a9249e). Resolved to the
            test_runs.id UUID for the FK link.
        project_id: project UUID string.

    Returns: number of rows actually inserted (skips rows that violate
    constraints, e.g., duplicate defect_id from a re-run).
    """
    if not defects:
        return 0
    import uuid as _uuid_mod
    from ..db_adapter import try_db
    inserted = 0
    try:
        async with try_db() as db:
            if db is None:
                return 0
            from ...db.repository import DefectRepo
            from ...db.models import (
                DefectSeverity, DefectStatusEnum, RiskPriority, TestRun,
            )
            from sqlalchemy import select as _sel

            # Resolve external run_id ("run-a9249e") → test_runs.id UUID.
            tr_id = None
            try:
                row = (await db.execute(
                    _sel(TestRun.id).where(TestRun.run_id == run_id)
                )).first()
                if row:
                    tr_id = row[0]
            except Exception:
                pass

            try:
                pid_uuid = _uuid_mod.UUID(project_id) if project_id else None
            except (ValueError, AttributeError):
                pid_uuid = None

            # R-DefectIdempotent — defect_ids like "DEF-HTTP_500" are
            # SHARED across runs (cluster signature is stable). UNIQUE
            # constraint on defects.defect_id means re-inserting the
            # same DEF-HTTP_500 collides → IntegrityError → SQLAlchemy
            # session is poisoned → ALL subsequent inserts in the same
            # session fail with "Session's transaction has been rolled
            # back". Pre-fix only the FIRST 3 of 42 defects landed.
            #
            # Use Postgres INSERT ... ON CONFLICT DO NOTHING so the
            # existing defect row stays (the gate already counts it),
            # and the new run's affected_tests are simply not added.
            # This matches the model where one logical defect persists
            # across runs that re-discover it; gate already fails on
            # the existing row's P0 status.
            from sqlalchemy.dialects.postgresql import insert as _pg_insert
            from ...db.models import Defect as _DefectModel

            for d in defects:
                # Map analyze_failures dict → Defect model fields.
                sev_raw = (d.get("severity") or "medium").lower()
                try:
                    severity = DefectSeverity(sev_raw)
                except ValueError:
                    severity = DefectSeverity.medium

                pri_raw = (d.get("priority") or "P2").upper()
                try:
                    priority = RiskPriority(pri_raw)
                except ValueError:
                    priority = RiskPriority.P2

                cls = d.get("defect_class") or "rca_root_cause"
                status = DefectStatusEnum.open

                title = (d.get("title") or "Untitled defect")[:500]
                defect_id = (
                    d.get("defect_id")
                    or f"DEF-{run_id.split('-')[-1].upper()[:6]}-{_uuid_mod.uuid4().hex[:4].upper()}"
                )

                # R30.8 — also populate the dedicated triage columns
                # alongside the metadata JSONB. Both paths stay populated
                # for backward compat (older callers reading from metadata
                # still work). The columns enable the indexed queries the
                # triage page uses (idx_def_triage_cat, idx_def_triage_proj_status).
                triage_cat_val = d.get("triage_category")
                triage_conf_val = d.get("triage_confidence")
                triage_sigs_val = list(d.get("triage_signals") or [])

                payload = {
                    "id": _uuid_mod.uuid4(),
                    "defect_id": defect_id,
                    "title": title,
                    "description": d.get("description") or d.get("error_message") or "",
                    "severity": severity,
                    "priority": priority,
                    "status": status,
                    "run_id": tr_id,
                    "project_id": pid_uuid,
                    "root_cause": d.get("root_cause") or "",
                    "root_cause_category": d.get("root_cause_category") or cls,
                    "suggested_fix": d.get("suggested_fix") or "",
                    "impacted_files": list(d.get("impacted_files") or []),
                    "reporter": "ARTA",
                    "tags": [cls] + list(d.get("tags") or []),
                    "triage_category": triage_cat_val,
                    "triage_confidence": triage_conf_val,
                    "triage_signals": triage_sigs_val,
                    # R37.4 — propagate Jira fields filed by
                    # _create_jira_for_sut_regressions onto the Defect
                    # row so the dashboard can link defect → ticket.
                    "jira_key": d.get("jira_key"),
                    "jira_url": d.get("jira_url"),
                    "metadata": {
                        "defect_class": cls,
                        "test_id": d.get("test_id"),
                        "cascade_of": d.get("cascade_of"),
                        "affected_tests": d.get("affected_tests") or [],
                        "cascade_count": d.get("cascade_count", 0),
                        "last_seen_run_id": run_id,
                        # R30.7-B — preserve triage fields so the /api/triage
                        # queue and downstream heal-gating can read them after
                        # the defect round-trips through the DB. R30.1 stamps
                        # these on the in-memory dict; pre-R30.7-B they were
                        # dropped on persistence and the triage queue had no
                        # signal data to surface to operators.
                        "triage_category": d.get("triage_category"),
                        "triage_confidence": d.get("triage_confidence"),
                        "triage_signals": d.get("triage_signals") or [],
                        # R38.5 — preserve the auth_scope subclass stamped
                        # by R37.2 in defect_intel._triage_failure for 401/
                        # 403 failures. Without this, operator_review rows
                        # for "cookie role lacks scope" are indistinguishable
                        # from generic unclassified failures in the triage
                        # queue → operator wastes time investigating wrong
                        # remediation path.
                        "operator_review_subclass": d.get("operator_review_subclass"),
                    },
                }
                # R28.3 — composite key (defect_id, run_id). Pre-R28.3
                # the constraint was on defect_id alone, which silently
                # dropped second-run defects with the same cluster
                # signature. Now the same DEF-HTTP_500 cluster can land
                # in BOTH run-A and run-B as separate rows; ON CONFLICT
                # only fires within the same (defect_id, run_id) pair —
                # i.e., on within-run retries (R20a fast-path retry).
                stmt = (
                    _pg_insert(_DefectModel.__table__)
                    .values(payload)
                    .on_conflict_do_nothing(index_elements=["defect_id", "run_id"])
                )
                try:
                    result = await db.execute(stmt)
                    if result.rowcount and result.rowcount > 0:
                        inserted += 1
                    else:
                        # R67.F — ON CONFLICT silent skip. Pre-R67.F
                        # these were invisible (rowcount=0 not logged).
                        # On run-168362: 83 classified → 67 persisted →
                        # 16 silently lost. Most likely cause: duplicate
                        # `(defect_id, run_id)` within the same run-defect
                        # set (clustering produced same defect_id twice).
                        _r67_f_conflict_count = locals().get(
                            "_r67_f_conflict_count", 0
                        ) + 1
                        locals().update(_r67_f_conflict_count=_r67_f_conflict_count)
                        log.warning(
                            "R67.F: defect insert dropped by ON CONFLICT "
                            "for defect_id=%s run=%s — duplicate cluster signature?",
                            defect_id, run_id,
                        )
                except Exception as exc:
                    # R67.F — promote from debug to warning + include
                    # exception class for triage. ValidationError →
                    # schema mismatch; ProgrammingError → SQL bug;
                    # IntegrityError → constraint violation.
                    log.warning(
                        "R67.F: defect persist failed for defect_id=%s run=%s "
                        "(%s): %s",
                        defect_id, run_id, type(exc).__name__, str(exc)[:200],
                    )
            await db.commit()
            # R67.F — summary log line after the batch. Lets operators
            # diagnose persist gaps without grepping for individual rows.
            dropped = len(defects) - inserted
            if dropped > 0:
                log.warning(
                    "R67.F: persisted %d/%d defects for run %s (%d rows lost — "
                    "see per-row R67.F log lines above for reasons)",
                    inserted, len(defects), run_id, dropped,
                )
    except Exception as exc:
        log.warning("R-DefectPersist: DB session failed: %s", exc)
    return inserted


def _resolve_neo4j_driver() -> Any:
    """Pull the Neo4j driver off `app.state.neo4j` when available.

    Falls back to None (stub mode) if main.py hasn't initialized one.
    """
    try:
        from ..main import app as _app
        return getattr(_app.state, "neo4j", None)
    except ImportError:
        return None


async def _build_test_history(run_id: str, test_ids: list[str]) -> dict[str, dict]:
    """R55.8 — fetch prior-run pass/fail status for every test_id in the
    current failure set so `_triage_failure` Layer 2 (defect_intel.py:443-466)
    can fire.

    Pre-R55.8 the classifier's Layer 2 was dead code — neither call-site
    of `_classify_defects_with_seq_integrity` passed test_history. With
    ~5-15% of failures expected to be sut_regression that are currently
    mis-classified as operator_review (no historical signal), this
    plumbing unlocks accurate classification.

    Implementation per R57.6 (scale-safe):
    - 14-day window (older runs don't reflect current SUT/test state)
    - LIMIT 100000 safety cap
    - Bucket per test_id keeping only most-recent 5 entries

    Returns shape: {tid: {was_passing_yesterday, was_failing_yesterday, recent_count}}
    Empty dict on DB unavailable or empty test_ids (degrades gracefully).
    """
    if not test_ids:
        return {}
    try:
        from ..db_adapter import try_db
        from sqlalchemy import text
    except ImportError:
        return {}
    deduped = sorted({t for t in test_ids if isinstance(t, str) and t})
    if not deduped:
        return {}
    history: dict[str, dict] = {}
    async with try_db() as db:
        if not db:
            return {}
        try:
            result = await db.execute(
                text("""
                    SELECT er.test_id, er.status, er.executed_at
                      FROM execution_results er
                      JOIN test_runs tr ON tr.id = er.run_id
                     WHERE er.test_id = ANY(:tids)
                       AND tr.run_id != :current
                       AND tr.started_at > NOW() - INTERVAL '14 days'
                     ORDER BY er.test_id, er.executed_at DESC
                     LIMIT 100000
                """),
                {"tids": deduped, "current": run_id},
            )
            buckets: dict[str, list[str]] = {}
            for row in result:
                tid = row[0]
                status = str(row[1])
                bucket = buckets.setdefault(tid, [])
                if len(bucket) < 5:
                    bucket.append(status)
            for tid, statuses in buckets.items():
                if not statuses:
                    continue
                history[tid] = {
                    "was_passing_yesterday": any(s.upper() == "PASS" for s in statuses[:5]),
                    "was_failing_yesterday": all(s.upper() != "PASS" for s in statuses[:5]),
                    "recent_count": len(statuses),
                }
        except Exception as exc:
            log.debug("R55.8: _build_test_history query failed: %s", exc)
    # R90.4 — distinguish empty-result (no prior runs) from skipped
    # (DB unavailable). Pre-R90.4 a uniform INFO log made it impossible
    # to tell whether Layer 2 historical-signal classification fired.
    # Cold-start projects with no prior runs SHOULD see "0 entries";
    # projects with prior runs should see non-zero; if 0 with prior
    # runs present, the query semantics are broken.
    if not history:
        log.info(
            "R90.4: _build_test_history returned 0 entries for %d test_ids "
            "(run_id=%s). Layer 2 historical-signal classifier will fall "
            "through to Layer 1+3 deterministic patterns. This is expected "
            "for first-paste projects; if prior completed runs exist for "
            "this project, the query at this function may need debugging.",
            len(deduped), run_id,
        )
    else:
        log.info(
            "R90.4: _build_test_history returned %d entries for %d test_ids "
            "(run_id=%s). Layer 2 historical-signal classifier active.",
            len(history), len(deduped), run_id,
        )
    return history


async def _classify_defects_with_seq_integrity(
    failures: list[dict],
    seq_integrity: dict,
    test_history: dict | None = None,
) -> list[dict]:
    """Phase G1 production wiring — call DefectIntelAgent with sequence_integrity.

    R55.8 — extended to accept `test_history` so `_triage_failure` Layer 2
    can fire (was previously dead code).

    Builds a fresh agent each call (cheap; no LLM during cascade/PCV
    classification). When no LLM client is available, the root-cause
    LLM RCA path silently no-ops — cascade defects still land deterministically.
    """
    try:
        from ...agents.defect_intel import DefectIntelAgent
        from ..main import app as _app
    except ImportError as exc:
        log.debug("defect_intel import failed: %s", exc)
        return []

    client = getattr(_app.state, "anthropic", None)
    if client is None:
        # No LLM — DefectIntelAgent's deterministic cascade/PCV paths still
        # work. We pass a benign client stub via duck typing.
        class _NoOpClient:
            class messages:
                @staticmethod
                async def create(**kwargs):
                    raise RuntimeError("LLM unavailable; cascade/PCV paths only")
        client = _NoOpClient()

    agent = DefectIntelAgent(client)

    # R49.2 — pre-cluster failures by signature BEFORE calling LLM RCA.
    # Pre-fix `analyze_failures` did one LLM call per failure → 3400
    # same-signature 5xx failures = 3400 LLM calls = 30+ min stall.
    # Post-fix: cluster by (status_code, error_message-prefix), call
    # LLM once per cluster, then fan the resulting defect's
    # `affected_tests` to all members. Bypasses LLM thrashing without
    # touching the agent code. Skipped when failure count is small
    # (< 200) so small runs keep their per-failure RCA precision.
    PRE_CLUSTER_THRESHOLD = 200
    if len(failures) >= PRE_CLUSTER_THRESHOLD:
        clusters: dict[str, list[dict]] = {}
        import re as _re_r124_g
        for f in failures:
            if not isinstance(f, dict):
                continue
            em = str(f.get("error_message") or f.get("error") or "").strip()
            # R124.G — body-aware key. When error_message is sparse (≤20
            # chars or just an HTTP status line), fall back to the
            # persisted response body so the cluster key has meaningful
            # signal. Pre-R124.G + pre-R124.K: 2451 failures with empty
            # bodies all hashed to "500|" → ONE cluster → not actually
            # reducing LLM workload. Post-R124.K body persists; R124.G
            # uses it.
            body = str(
                (f.get("metadata") or {}).get("response_body_preview") or ""
            )[:200]
            combined = em if len(em) > 20 else (em + " | body: " + body)
            sc = f.get("status_code")
            # First non-whitespace token gives a more semantic discriminator
            # than raw prefix (e.g., "missing" vs "unauthorized" vs
            # "Internal" become distinct clusters even when status is same).
            first_tok_match = _re_r124_g.match(r"\W*(\w[\w-]{2,})", combined)
            first_tok = (
                first_tok_match.group(1)[:24] if first_tok_match else "empty"
            )
            # Triage signal (R124.G: if R35.1 stamped a signal upstream,
            # use it as a clustering dimension — keeps auth-cascade rows
            # separate from real-SUT rows even when status text matches).
            triage_sig = "none"
            if isinstance(f.get("triage_signals"), list) and f["triage_signals"]:
                triage_sig = str(f["triage_signals"][0])[:32]
            key = f"{sc or 'na'}|{triage_sig}|{first_tok}|{combined[:60]}"
            clusters.setdefault(key, []).append(f)
        if len(clusters) > 1:
            log.info(
                "R49.2: pre-clustered %d failures into %d signature buckets "
                "before LLM RCA (was %d × LLM calls; now %d × LLM calls)",
                len(failures), len(clusters), len(failures), len(clusters),
            )
            # Build representative list: one failure per cluster, but
            # the representative carries an `_arta_cluster_members` list
            # so the resulting defect's affected_tests fans correctly.
            representatives: list[dict] = []
            for key, members in clusters.items():
                rep = dict(members[0])
                rep["_arta_cluster_members"] = [
                    m.get("test_id") for m in members if isinstance(m, dict)
                ]
                rep["_arta_cluster_size"] = len(members)
                representatives.append(rep)
            try:
                # R55.8 — forward test_history so Layer 2 historical
                # signals can fire inside _triage_failure for each rep.
                cluster_defects = await agent.analyze_failures(
                    representatives,
                    sequence_integrity=seq_integrity,
                    test_history=test_history,
                )
            except TypeError:
                # Older signature without test_history kwarg
                try:
                    cluster_defects = await agent.analyze_failures(
                        representatives, sequence_integrity=seq_integrity,
                    )
                except TypeError:
                    cluster_defects = await agent.analyze_failures(representatives)
            # Fan affected_tests for each cluster-derived defect.
            out_defects: list[dict] = []
            for d in cluster_defects or []:
                if not isinstance(d, dict):
                    continue
                # Find the representative whose test_id matches the
                # defect's test_id, then attach its cluster members.
                tid = d.get("test_id")
                rep = next(
                    (r for r in representatives if r.get("test_id") == tid),
                    None,
                )
                if rep and rep.get("_arta_cluster_members"):
                    existing = d.get("affected_tests") or []
                    extra = [
                        t for t in rep["_arta_cluster_members"]
                        if t and t not in existing
                    ]
                    d["affected_tests"] = list(existing) + extra
                    d["cluster_size"] = rep.get("_arta_cluster_size")
                out_defects.append(d)
            return out_defects

    # Fast/small path — analyze every failure (pre-R49.2 behaviour).
    try:
        # R55.8 — pass test_history so Layer 2 fires
        return await agent.analyze_failures(
            failures,
            sequence_integrity=seq_integrity,
            test_history=test_history,
        )
    except TypeError:
        try:
            return await agent.analyze_failures(failures, sequence_integrity=seq_integrity)
        except TypeError:
            return await agent.analyze_failures(failures)


async def _queue_chain_heal_proposals(defects: list[dict]) -> None:
    """Phase G2 + G3 production wiring — emit chain_reverify and
    provider_contract_heal PENDING_APPROVALs.

    Mirrors `ARTAOrchestrator._queue_chain_heals_from_defects` but is
    callable from any production path. Best-effort throughout.
    """
    try:
        from ...agents.self_healing import SelfHealingAgent
        from ..main import app as _app
    except ImportError as exc:
        log.debug("self_healing import failed: %s", exc)
        return

    client = getattr(_app.state, "anthropic", None)
    if client is None:
        # Build a no-op client; the propose_* methods don't make LLM calls.
        class _NoOpClient:
            class messages:
                @staticmethod
                async def create(**kwargs):
                    raise RuntimeError("LLM unavailable")
        client = _NoOpClient()

    healer = SelfHealingAgent(client)

    cascade_by_root: dict[str, list[str]] = {}
    cascade_via_var: dict[str, str] = {}
    pcvs: list[dict] = []
    # R37.1 — group test_gen_bug defects by (status_code, signal-tuple)
    # so we issue ONE regen proposal per failure pattern instead of one
    # per affected test. Cluster key collapses 399 415-failures into a
    # single "regen all 415-affected items" proposal.
    test_gen_clusters: dict[tuple, dict] = {}
    # R30.3 — gate proposals on triage_category. Allowed:
    # test_gen_bug (regen test) and sut_contract_change (heal jsonpath).
    # Blocked: sut_regression (real bug — patching the test would HIDE
    # the SUT issue), operator_review (waiting for human triage).
    _ALLOWED_TRIAGE_FOR_HEAL = {"test_gen_bug", "sut_contract_change", None, ""}
    blocked_by_triage = 0
    blocked_by_operator = 0
    for d in defects:
        if not isinstance(d, dict):
            continue
        # R30.7-D — read triage_category from THREE possible paths:
        #   1. Top-level field (fresh from analyze_failures)
        #   2. Nested triage object (older shape)
        #   3. metadata.operator_triage.category (operator's verdict via
        #      /api/triage POST persisted to DB)
        # The operator's path-3 verdict OVERRIDES path-1/2 because it's
        # the most recent human-curated signal.
        md = d.get("metadata") or {}
        if not isinstance(md, dict):
            md = {}
        operator_triage = md.get("operator_triage") or {}
        if not isinstance(operator_triage, dict):
            operator_triage = {}
        operator_cat = operator_triage.get("category") if operator_triage else None
        triage_cat = (
            operator_cat
            or d.get("triage_category")
            or (d.get("triage") or {}).get("triage_category")
        )
        # R30.7-D — operator's "skip_healing" flag (stamped by /api/triage
        # POST when verdict=sut_regression) blocks heal proposals
        # regardless of triage_category. Sticky across runs because it
        # lives in DB metadata.
        skip_healing = bool(md.get("skip_healing") or d.get("skip_healing"))
        if skip_healing:
            blocked_by_operator += 1
            continue
        if triage_cat and triage_cat not in _ALLOWED_TRIAGE_FOR_HEAL:
            blocked_by_triage += 1
            continue
        cls = d.get("defect_class")
        if cls == "cascade_failure":
            root = d.get("cascade_of")
            if root:
                cascade_by_root.setdefault(root, []).append(d.get("test_id", ""))
                cascade_via_var[root] = d.get("via_var") or cascade_via_var.get(root, "")
        elif cls == "provider_contract_violation":
            pcvs.append(d)
        # R37.1 — every test_gen_bug defect (regardless of defect_class)
        # joins a regen cluster keyed by (status_code, signal-set). One
        # proposal per cluster covers all affected tests.
        if triage_cat == "test_gen_bug":
            sc = d.get("status_code") or md.get("status_code")
            try:
                sc = int(sc) if sc is not None else None
            except (TypeError, ValueError):
                sc = None
            sigs = d.get("triage_signals") or md.get("triage_signals") or []
            if not isinstance(sigs, list):
                sigs = []
            cluster_key = (sc, tuple(sorted(str(s) for s in sigs))[:3])
            cluster = test_gen_clusters.setdefault(cluster_key, {
                "status_code": sc,
                "signals": list(sigs),
                "affected_tests": [],
                "sample_error": "",
            })
            tid = d.get("test_id") or ""
            if tid and tid not in cluster["affected_tests"]:
                cluster["affected_tests"].append(tid)
            for t in d.get("affected_tests") or []:
                if t and t not in cluster["affected_tests"]:
                    cluster["affected_tests"].append(t)
            if not cluster["sample_error"]:
                cluster["sample_error"] = (
                    d.get("error_message")
                    or d.get("description")
                    or md.get("error_message")
                    or ""
                )
    if blocked_by_triage:
        log.info(
            "R30.3: %d defect(s) blocked from heal-proposal queueing "
            "(triage_category=sut_regression or operator_review). Operators "
            "investigate via Defect Intelligence; ARTA does NOT auto-heal SUT bugs.",
            blocked_by_triage,
        )
    if blocked_by_operator:
        log.info(
            "R30.7-D: %d defect(s) blocked from heal-proposal queueing "
            "(operator marked sut_regression via /api/triage; skip_healing=true). "
            "Sticky across runs until operator unblocks.",
            blocked_by_operator,
        )

    # K12 — auto-apply when confidence ≥ 0.85 (default for chain_reverify
    # is exactly 0.85, so cascades default to auto-heal). Operators can
    # disable by stamping `auto_heal_enabled=False` on the project.
    auto_heal_threshold = 0.85
    auto_applied_count = 0

    for root_id, affected in cascade_by_root.items():
        try:
            pid = await healer.propose_chain_reverify(
                root_cause_test_id=root_id,
                affected_tests=affected,
                via_var=cascade_via_var.get(root_id),
            )
            log.info("J1: queued chain_reverify=%s root=%s affected=%d",
                     pid, root_id, len(affected))
            # K12 — auto-apply if confidence ≥ threshold
            try:
                from ...api.routers.healing import PENDING_APPROVALS
                proposal = PENDING_APPROVALS.get(pid) or {}
                conf = float(proposal.get("confidence", 0) or 0)
                if conf >= auto_heal_threshold:
                    if await healer.auto_apply_heal(pid):
                        auto_applied_count += 1
                        log.info("K12: auto-applied chain_reverify=%s (confidence=%.2f, %d cascades recovered)",
                                 pid, conf, len(affected))
            except Exception as exc:
                log.debug("K12: auto-heal apply failed for %s: %s", pid, exc)
        except Exception as exc:
            log.warning("J1: chain_reverify proposal failed for %s: %s", root_id, exc)

    for pcv in pcvs:
        try:
            pid = await healer.propose_provider_contract_heal(
                test_id=pcv.get("test_id", ""),
                var_name=pcv.get("var_name", ""),
                old_jsonpath=pcv.get("expected_jsonpath", "$.id"),
                new_jsonpath=None,
                provider_response_shape=pcv.get("provider_response_shape") or {},
            )
            log.info("J1: queued provider_contract_heal=%s test=%s",
                     pid, pcv.get("test_id"))
            # K12 — auto-apply if confidence ≥ threshold (provider_contract_heal
            # without an explicit new_jsonpath has confidence=0.5, so won't auto;
            # contract heals WITH explicit new path land at 0.75 — also won't
            # auto. Only chain_reverify auto-fires by default. This is the
            # intentional cap — contract heals require operator review of the
            # new jsonpath.)
            try:
                from ...api.routers.healing import PENDING_APPROVALS
                proposal = PENDING_APPROVALS.get(pid) or {}
                conf = float(proposal.get("confidence", 0) or 0)
                if conf >= auto_heal_threshold:
                    if await healer.auto_apply_heal(pid):
                        auto_applied_count += 1
                        log.info("K12: auto-applied provider_contract_heal=%s (confidence=%.2f)",
                                 pid, conf)
            except Exception as exc:
                log.debug("K12: auto-heal apply failed for %s: %s", pid, exc)
        except Exception as exc:
            log.warning("J1: provider_contract_heal proposal failed: %s", exc)

    # R37.1 — emit one regen proposal per test_gen_bug cluster. Each
    # cluster covers all affected tests sharing the same (status_code,
    # signal-set) so 399 415-failures collapse into ONE proposal asking
    # to regenerate the affected specs with the runtime failure as a
    # Tier-2 hint. Confidence 0.85 (matches K12 threshold) so it
    # auto-applies. Caps cluster size at 50 tests/proposal so a single
    # toxic pattern doesn't poison the heal queue with one giant blob.
    _MAX_TESTS_PER_REGEN = 50
    for cluster_key, cluster in test_gen_clusters.items():
        affected = cluster.get("affected_tests") or []
        if not affected:
            continue
        sc = cluster.get("status_code")
        signals = cluster.get("signals") or []
        # Chunk large clusters so each proposal is operator-reviewable.
        for chunk_start in range(0, len(affected), _MAX_TESTS_PER_REGEN):
            chunk = affected[chunk_start:chunk_start + _MAX_TESTS_PER_REGEN]
            try:
                pid = await healer.propose_test_gen_regen(
                    test_id=chunk[0],
                    affected_tests=chunk,
                    triage_category="test_gen_bug",
                    status_code=sc,
                    signals=signals,
                    sample_error=cluster.get("sample_error", ""),
                )
                log.info(
                    "R37.1: queued test_gen_regen=%s status=%s signals=%s affected=%d",
                    pid, sc, signals[:3], len(chunk),
                )
                try:
                    from ...api.routers.healing import PENDING_APPROVALS
                    proposal = PENDING_APPROVALS.get(pid) or {}
                    conf = float(proposal.get("confidence", 0) or 0)
                    if conf >= auto_heal_threshold:
                        if await healer.auto_apply_heal(pid):
                            auto_applied_count += 1
                            log.info(
                                "K12: auto-applied test_gen_regen=%s (confidence=%.2f, %d tests queued for regen)",
                                pid, conf, len(chunk),
                            )
                except Exception as exc:
                    log.debug("K12: test_gen_regen auto-apply failed for %s: %s", pid, exc)
            except Exception as exc:
                log.warning("R37.1: test_gen_regen proposal failed (status=%s): %s", sc, exc)

    if auto_applied_count:
        log.info(
            "K12: auto-applied %d high-confidence heal proposals (no operator approval needed)",
            auto_applied_count,
        )


async def run_chain_aware_post_processing_sync(
    run_id: str,
    project_id: str,
    execution_results: list[dict],
    *,
    results_dir: Path | None = None,
) -> dict:
    """R20a — synchronous fast-path of the post-run pipeline.

    Does ONLY the cheap, restart-resilient work:
      1. Emit `{run_id}-steps.jsonl` evidence artifact (deterministic file write)
      2. Compute sequence_integrity in stub mode (no Neo4j roundtrip)
      3. Cascade-aware defect classification (LLM RCA path or deterministic fallback)
      4. Persist defects to the `defects` table

    The slow path — Neo4j chain ingest + heal-proposal queueing — stays
    in `run_chain_aware_post_processing` (the supervised fire-and-forget
    entry point) because Neo4j roundtrips are slow + retryable + idempotent.

    Why split: pre-fix the entire pipeline ran as a fire-and-forget task.
    If the container restarted between run completion and task execution,
    defects never persisted → "Open P0 defects" gate badge incorrectly
    showed 0 → operator missed real bugs. Splitting puts the load-bearing
    DB work on the synchronous path so `_persist_run_to_db` can guarantee
    defects exist by the time it returns. Verified live: run-8287b8
    completed in DB at 17:41 but had 0 defect rows linked because the
    background task was lost across a restart.

    Returns: sequence_integrity dict (degraded shape — Neo4j path skipped).
    """
    out: dict[str, Any] = {
        "cascade_failures": [],
        "provider_contract_violations": [],
        "unresolved_chain_starts": [],
        "param_provenance": {},
        "degraded": True,
        "degraded_reason": "fast_path_skips_neo4j",
    }
    if not run_id or not project_id:
        out["degraded_reason"] = "missing_run_id_or_project_id"
        return out

    # 1. Steps.jsonl emit (Phase E2) — deterministic file write
    try:
        from ..routers.execution import get_steps, emit_steps_jsonl
        steps = get_steps(run_id) or []
        rdir = results_dir or Path(".arta/results")
        rdir.mkdir(parents=True, exist_ok=True)
        emitted = emit_steps_jsonl(run_id, rdir)
        if emitted:
            log.info("J1-fast: wrote steps.jsonl for run=%s steps=%d",
                     run_id, len(steps))
    except Exception as exc:
        log.debug("J1-fast: step emit failed: %s", exc)

    # 2. Filter failures (same shape as the existing combined entry)
    failures = [
        r for r in (execution_results or [])
        if isinstance(r, dict)
        and r.get("status") == "FAIL"
        and r.get("automation_tool") != "preflight"
        and r.get("failure_class") != "environment"
    ]

    # 3. Classify defects (LLM path may return [] when Anthropic absent;
    #    R-DefectFallback produces deterministic clusters in that case)
    # R35.2 — instrument every phase so the operator can see exactly
    # where the path dies. Pre-R35.2 only "wrote steps.jsonl" surfaced;
    # if classification produced 0 defects (run-349a4d), no log line
    # explained why.
    log.info(
        "J1-fast: starting defect classification for %d failures (run %s)",
        len(failures), run_id,
    )
    defects: list[dict] = []
    if failures:
        # R49.1 KEYSTONE — wallclock budget. Pre-R49.1 the LLM RCA
        # could run for 30-60 min on a run with 3000+ sut_regression
        # failures (concurrency=4, ~10s/call, 1 call per cluster). That
        # stalls Jira filing (R37.4), R48.3 coordinator, R42.6 regen
        # queueing — the entire 5-pillar loop is gated on this. Cap at
        # 5 min; if LLM doesn't converge, fall through to the
        # deterministic fallback (which is fast + correct, just less
        # narrative-rich on root_cause).
        import asyncio as _aio_49_1
        _RCA_BUDGET_SEC = 300  # 5 minutes
        # R55.8 — fetch test_history for Layer 2 classification
        _r55_8_test_ids_sync = [
            f.get("test_id") for f in failures
            if isinstance(f, dict) and f.get("test_id")
        ]
        try:
            _r55_8_history_sync = await _build_test_history(run_id, _r55_8_test_ids_sync)
        except Exception as _r55_8_sync_exc:
            log.debug("R55.8: history fetch failed (sync path): %s", _r55_8_sync_exc)
            _r55_8_history_sync = {}
        try:
            defects = await _aio_49_1.wait_for(
                _classify_defects_with_seq_integrity(
                    failures, out, test_history=_r55_8_history_sync,
                ),
                timeout=_RCA_BUDGET_SEC,
            )
            log.info(
                "J1-fast: LLM/cascade/PCV classification produced %d defects "
                "(run %s) within %ds budget",
                len(defects), run_id, _RCA_BUDGET_SEC,
            )
        except _aio_49_1.TimeoutError:
            log.warning(
                "R49.1: defect classification exceeded %ds budget for run %s "
                "(%d failures) — falling through to deterministic fallback. "
                "Defect quality may be less narrative-rich but the rest of "
                "the loop (Jira + R48.3 + regen) unblocks.",
                _RCA_BUDGET_SEC, run_id, len(failures),
            )
        except Exception as exc:
            log.warning(
                "J1-fast: defect classification raised: %s (run %s)", exc, run_id,
            )
        if not defects and failures:
            defects = _build_deterministic_defects(failures)
            log.info(
                "J1-fast: deterministic fallback produced %d defects from %d "
                "failures (run %s)",
                len(defects), len(failures), run_id,
            )

    # 4. Persist defects to DB (R-DefectPersist) — synchronously
    if defects:
        try:
            persisted = await _persist_defects_to_db(
                defects, run_id=run_id, project_id=project_id,
            )
            log.info(
                "J1-fast: classified %d defects (cascade=%d, root=%d, pcv=%d) "
                "for run %s, persisted %d to DB",
                len(defects),
                sum(1 for d in defects if d.get("defect_class") == "cascade_failure"),
                sum(1 for d in defects if d.get("defect_class") == "rca_root_cause"),
                sum(1 for d in defects if d.get("defect_class") == "provider_contract_violation"),
                run_id, persisted,
            )
        except Exception as exc:
            log.warning("J1-fast: defect persistence failed: %s", exc)
    else:
        # R35.2 — explicit ZERO-defects log so operators don't have to
        # infer why "Open P0 defects = 0". When this fires for a run
        # with FAIL rows, something in classification went wrong.
        if failures:
            log.warning(
                "J1-fast: ZERO defects to persist for run %s despite %d "
                "FAIL rows — investigate classification flow",
                run_id, len(failures),
            )

    # Stamp onto _REAL_RUNS so frontend reads the latest state immediately
    try:
        from ..routers.execution import _REAL_RUNS
        if run_id in _REAL_RUNS:
            _REAL_RUNS[run_id]["sequence_integrity"] = out
            if defects:
                _REAL_RUNS[run_id]["chain_aware_defects"] = defects
    except Exception:
        pass

    # R67.D — wire chain-health + endpoint-coverage into J1-fast (sync)
    # path. Pre-R67.D these helpers lived ONLY in the FULL async path
    # (`run_chain_aware_post_processing` at line ~207) but production
    # runs (run-168362 confirmed) hit the SYNC path and skipped both.
    # Result: R55.13 endpoint-coverage gate row never fired in real runs;
    # `_REAL_RUNS[run_id].traceability_chain_health` was always empty;
    # operator couldn't see endpoint coverage on the quality-score page.
    try:
        from ...agents.traceability_agent import TraceabilityAgent
        from ..main import app as _r67_d_app
        _r67_d_neo4j = getattr(_r67_d_app.state, "neo4j", None) if _r67_d_app else None
        if _r67_d_neo4j is not None:
            _r67_d_agent = TraceabilityAgent(_r67_d_neo4j)
            chain_health = await _compute_traceability_chain_health(
                _r67_d_agent, run_id, project_id,
            )
            try:
                ep_coverage = await _compute_endpoint_coverage(
                    _r67_d_agent, run_id, project_id,
                )
                chain_health["endpoint_coverage"] = ep_coverage
                log.info(
                    "R55.13 (J1-fast): endpoint coverage for run %s — %d/%d (%.1f%%)",
                    run_id,
                    ep_coverage.get("covered_endpoints", 0),
                    ep_coverage.get("total_endpoints", 0),
                    ep_coverage.get("coverage_pct", 0.0),
                )
            except Exception as _r55_13_exc:
                log.debug(
                    "R55.13 (J1-fast): endpoint coverage skipped for %s: %s",
                    run_id, _r55_13_exc,
                )
            try:
                from ..routers.execution import _REAL_RUNS as _rr_67_d
                if run_id in _rr_67_d:
                    _rr_67_d[run_id]["traceability_chain_health"] = chain_health
            except Exception:
                pass
            if chain_health.get("requirements_without_results"):
                log.warning(
                    "R30.6 (J1-fast): %d requirement(s) had tests dispatched "
                    "but 0 ExecutionResult edges in Neo4j for run %s",
                    len(chain_health["requirements_without_results"]), run_id,
                )
        else:
            log.debug("R67.D: Neo4j unavailable; chain-health skipped for %s", run_id)
    except Exception as _r67_d_exc:
        log.debug("R67.D: chain-health wiring failed for run %s: %s", run_id, _r67_d_exc)

    # R48.3 — close the ARTA improvement loop (FAST path). Pre-fix this
    # was only wired into the FULL `run_chain_aware_post_processing`
    # but run-6d9d92 hit the FAST `_sync` path → coordinator never
    # fired → `.arta/improvement_loop/` empty → no regen markers
    # queued. Now both paths invoke the coordinator.
    try:
        from .improvement_loop import post_run_coordinate
        loop_result = await post_run_coordinate(run_id, project_id)
        log.info(
            "R48.3 (sync): improvement-loop result for %s: %s",
            run_id, loop_result.get("actions_queued"),
        )
    except Exception as _r48_3_exc:
        log.warning(
            "R48.3 (sync): improvement-loop coordinator failed for %s: %s",
            run_id, _r48_3_exc,
        )

    return out


async def _link_results_and_defects_to_neo4j(
    agent: Any,
    run_id: str,
    project_id: str,
    execution_results: list[dict],
    defects: list[dict],
) -> dict:
    """R29.1 — persist ExecutionResult nodes + LINK_TC_RESULT +
    LINK_RESULT_DEFECT edges so /api/traceability/graph?run_id=... can
    show the full chain Req → AC → TC → Result → Defect.

    Pre-R29.1 the production pipeline only persisted Endpoint/EnvVar/CallChain
    (via ingest_chains). The R28.6 graph endpoint then returned empty
    Result lists because the Cypher MATCH found no nodes. Frontend rendered
    TC nodes in PENDING state forever.

    Best-effort:
      - No agent driver (Neo4j down → stub mode) → returns zeros
      - Single result fails → log + continue (other results still persist)
      - All exceptions caught — never raises
    """
    if not agent or not getattr(agent, "_driver", None):
        return {"results_written": 0, "defects_linked": 0,
                "skipped_reason": "no_neo4j_driver"}

    try:
        from ...agents.traceability_agent import (
            UPSERT_RESULT, LINK_TC_RESULT, LINK_RESULT_DEFECT,
            UPSERT_TEST_CASE,
        )
    except ImportError as exc:
        log.warning("R29.1: traceability_agent import failed: %s", exc)
        return {"results_written": 0, "defects_linked": 0,
                "skipped_reason": "import_failed"}

    written = 0
    linked = 0
    write_failures = 0
    try:
        async with agent._driver.session() as session:
            for _row_idx, r in enumerate(execution_results or []):
                if not isinstance(r, dict):
                    continue
                tc_id = r.get("test_id") or r.get("id") or ""
                if not tc_id:
                    continue
                # R124.D ROOT FIX — per-row unique result_id.
                #
                # Pre-R124.D: `result_id = f"{run_id}:{tc_id}"` collided when
                # N items shared the same TC (e.g. 15 Newman items per spec
                # all named after the same `test_id` family). MERGE on a
                # colliding id matched the existing node + last-write
                # overwrote earlier rows → run-d52a8c surfaced as
                # "wrote 0 ExecutionResult nodes" for 3137 results.
                #
                # Post-R124.D: incorporate (test_id, status, status_code,
                # endpoint_path, row_idx) into a stable per-row hash. Same
                # row re-classified → same hash (idempotent within a run).
                # Distinct rows → distinct hash → distinct Neo4j nodes.
                _md = r.get("metadata") or {}
                _r124_d_key = "|".join([
                    str(tc_id),
                    str(r.get("status") or ""),
                    str(_md.get("status_code") or ""),
                    str(_md.get("request_path") or ""),
                    str(r.get("automation_tool") or r.get("tool") or ""),
                    str(_row_idx),
                ])
                result_id = (
                    f"{run_id}:"
                    + hashlib.sha1(_r124_d_key.encode()).hexdigest()[:16]
                )
                # Ensure the TestCase node exists (create-on-first-mention).
                # Production runs may surface tests not in the requirement
                # graph (smoke specs, regression suites). Without this,
                # LINK_TC_RESULT would silently no-op for unknown TCs.
                try:
                    await session.run(UPSERT_TEST_CASE, {
                        "tc_id": tc_id,
                        "title": r.get("title", ""),
                        "automation_type": r.get("automation_tool") or r.get("tool", "playwright"),
                    })
                except Exception as exc:
                    log.debug("R29.1: UPSERT_TEST_CASE failed for %s: %s", tc_id, exc)
                try:
                    await session.run(UPSERT_RESULT, {
                        "result_id": result_id,
                        "run_id": run_id,
                        "status": str(r.get("status", "UNKNOWN")).upper(),
                        "duration_ms": int(r.get("duration_ms") or 0),
                        "build_id": r.get("build_id", ""),
                        "tool": r.get("automation_tool") or r.get("tool", ""),
                    })
                    await session.run(LINK_TC_RESULT, {
                        "tc_id": tc_id,
                        "result_id": result_id,
                    })
                    written += 1
                    # R57.5 — Result→Endpoint edge. Read endpoint_keys
                    # from r.metadata (stamped at Newman result-emit time
                    # by R55.7's `_r55_7_extract_endpoint_key`). MATCH —
                    # not MERGE — on the Endpoint node so a missing
                    # endpoint doesn't silently create a phantom; chain
                    # ingestion at run start is the only authoritative
                    # creator. Pillar 3 gap closed: operators can now
                    # query "show defects per SUT endpoint" via:
                    #   MATCH (e:Endpoint)<-[:EXECUTED_AGAINST]-(r:Result)
                    #         -[:HAS_DEFECT]->(d:Defect)
                    #   RETURN e.endpoint_key, count(d)
                    ep_keys = (r.get("metadata") or {}).get("endpoint_keys") or []
                    if isinstance(ep_keys, list):
                        for ep_key in ep_keys:
                            if not ep_key or not isinstance(ep_key, str):
                                continue
                            try:
                                await session.run(
                                    """
                                    MATCH (res:Result {result_id: $result_id}),
                                          (ep:Endpoint {endpoint_key: $ep_key})
                                    MERGE (res)-[:EXECUTED_AGAINST]->(ep)
                                    """,
                                    {"result_id": result_id, "ep_key": ep_key},
                                )
                            except Exception as r57_5_exc:
                                log.debug(
                                    "R57.5: Result→Endpoint edge failed result=%s ep=%s: %s",
                                    result_id, ep_key, r57_5_exc,
                                )
                except Exception as exc:
                    # R124.D — promoted from log.debug → log.warning so
                    # per-row write failures are operator-visible. NOT a
                    # fail-loud fallback patch — the root cause was result_id
                    # collisions (fixed above); this log surfaces residual
                    # Cypher-layer errors (constraint violations, driver
                    # network blips) that otherwise stayed hidden.
                    write_failures += 1
                    log.warning(
                        "R124.D: Neo4j result write failed tc=%s run=%s err=%s: %s",
                        tc_id, run_id, type(exc).__name__, str(exc)[:200],
                    )

            for d in defects or []:
                if not isinstance(d, dict):
                    continue
                tc_id = d.get("test_id") or ""
                defect_id = d.get("defect_id") or d.get("id") or ""
                if not (tc_id and defect_id):
                    continue
                result_id = f"{run_id}:{tc_id}"
                try:
                    await session.run(LINK_RESULT_DEFECT, {
                        "result_id": result_id,
                        "defect_id": defect_id,
                    })
                    linked += 1
                except Exception as exc:
                    log.debug(
                        "R29.1: defect link failed for defect=%s tc=%s: %s",
                        defect_id, tc_id, exc,
                    )
    except Exception as exc:
        log.warning("R29.1: Neo4j session failed for run %s: %s", run_id, exc)

    return {
        "results_written": written,
        "defects_linked": linked,
        "write_failures": write_failures,  # R124.D — operator visibility
    }


async def _compute_traceability_chain_health(
    agent: Any,
    run_id: str,
    project_id: str,
) -> dict:
    """R30.6 — single-Cypher chain-health query.

    For every requirement scoped to this project + run, count the chain
    depth: ACs, TCs, Results, Defects. Identify requirements that have
    test cases but ZERO results in Neo4j — those are broken-chain
    indicators (e.g., R29.1 didn't write edges for them, OR the test
    wasn't dispatched, OR the TC node never registered).

    Returns:
        {
          "total_requirements": int,
          "requirements_without_results": [req_id, ...],
          "max_chain_depth": int,
          "avg_chain_depth": float,
          "degraded": bool,        # Neo4j unavailable
        }
    """
    out: dict[str, Any] = {
        "total_requirements": 0,
        "requirements_without_results": [],
        "max_chain_depth": 0,
        "avg_chain_depth": 0.0,
        "degraded": False,
    }
    if not agent or not getattr(agent, "_driver", None):
        out["degraded"] = True
        return out
    try:
        async with agent._driver.session() as session:
            # NOTE: `req.project_id` may not be set on Requirement nodes
            # produced by `ingest_requirements_and_acs` in stub or partial
            # paths; we filter best-effort then count edges per req.
            result = await session.run(
                """
                MATCH (req:Requirement)
                WHERE ($project_id IS NULL OR req.project_id = $project_id
                       OR req.project_id IS NULL)
                OPTIONAL MATCH (req)-[:HAS_AC]->(ac:AcceptanceCriteria)
                OPTIONAL MATCH (ac)<-[:COVERS]-(tc:TestCase)
                OPTIONAL MATCH (tc)-[:PRODUCED]->(res:ExecutionResult)
                  WHERE res.run_id = $run_id
                OPTIONAL MATCH (res)-[:TRIGGERED]->(d:Defect)
                RETURN req.id AS req_id,
                       count(DISTINCT ac) AS ac_count,
                       count(DISTINCT tc) AS tc_count,
                       count(DISTINCT res) AS res_count,
                       count(DISTINCT d) AS defect_count
                """,
                project_id=project_id, run_id=run_id,
            )
            depths: list[int] = []
            broken: list[str] = []
            async for row in result:
                req_id = row.get("req_id")
                tc = int(row.get("tc_count") or 0)
                res = int(row.get("res_count") or 0)
                if req_id and tc > 0 and res == 0:
                    broken.append(req_id)
                # Chain depth = how many of {AC, TC, Result, Defect} are populated.
                depth = sum(
                    1 for k in ("ac_count", "tc_count", "res_count", "defect_count")
                    if int(row.get(k) or 0) > 0
                )
                depths.append(depth)
            out["total_requirements"] = len(depths)
            out["requirements_without_results"] = broken
            if depths:
                out["max_chain_depth"] = max(depths)
                out["avg_chain_depth"] = round(sum(depths) / len(depths), 2)
    except Exception as exc:
        log.debug("R30.6: chain-health Cypher failed for run %s: %s", run_id, exc)
        out["degraded"] = True
    return out


async def _compute_endpoint_coverage(
    agent: Any,
    run_id: str,
    project_id: str,
) -> dict:
    """R55.13 — count SUT endpoint coverage by Result→Endpoint edges.

    After R55.7 wired `(Result)-[:EXECUTED_AGAINST]->(Endpoint)`
    (currently Newman only — 96.6% of dispatch volume), this metric
    becomes meaningful. Operators see "47 of 89 SUT endpoints have at
    least one test that ran in run X" instead of just "trace chain has
    results".

    Returns:
        {
          "total_endpoints": int,           # Endpoint nodes in scope
          "covered_endpoints": int,         # Endpoints with ≥1 Result edge
          "coverage_pct": float,            # 0-100
          "sample_uncovered": [str, ...],   # up to 5 endpoint_keys for UI hints
          "degraded": bool,                 # Neo4j unavailable
        }

    Endpoint scope: filters by project_id when set on the Endpoint node;
    otherwise unscoped (matches R30.6's best-effort handling — Endpoint
    nodes produced by chain ingestion don't always carry project_id).
    """
    out: dict[str, Any] = {
        "total_endpoints": 0,
        "covered_endpoints": 0,
        "coverage_pct": 0.0,
        "sample_uncovered": [],
        "degraded": False,
    }
    if not agent or not getattr(agent, "_driver", None):
        out["degraded"] = True
        return out
    try:
        async with agent._driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Endpoint)
                WHERE ($project_id IS NULL OR e.project_id = $project_id
                       OR e.project_id IS NULL)
                OPTIONAL MATCH (e)<-[:EXECUTED_AGAINST]-(r:Result)
                  WHERE r.run_id = $run_id
                WITH e, count(DISTINCT r) AS hits
                RETURN
                  count(DISTINCT e) AS total_endpoints,
                  count(DISTINCT CASE WHEN hits > 0 THEN e END) AS covered_endpoints,
                  collect(DISTINCT CASE WHEN hits = 0 THEN e.endpoint_key END)[..5]
                    AS sample_uncovered
                """,
                project_id=project_id, run_id=run_id,
            )
            row = await result.single()
            if row:
                total = int(row.get("total_endpoints") or 0)
                covered = int(row.get("covered_endpoints") or 0)
                out["total_endpoints"] = total
                out["covered_endpoints"] = covered
                out["coverage_pct"] = (
                    round(covered / total * 100.0, 1) if total > 0 else 0.0
                )
                out["sample_uncovered"] = [
                    k for k in (row.get("sample_uncovered") or [])
                    if isinstance(k, str)
                ][:5]
    except Exception as exc:
        log.debug(
            "R55.13: endpoint coverage Cypher failed for run %s: %s",
            run_id, exc,
        )
        out["degraded"] = True
    return out


__all__ = [
    "run_chain_aware_post_processing",
    "run_chain_aware_post_processing_sync",
]
