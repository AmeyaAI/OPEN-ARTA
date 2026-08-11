"""R48.3 — post-run improvement loop coordinator.

ARTA's 5 pillars exist as discrete pieces:
- R42.1 grounding (gen-time)
- R42.6 regen consumer (5-min cycle, drains marker files)
- R37.4 Jira workflow (per-defect, fires inside post-pipeline)
- R29.1 traceability (per-run, Neo4j edge writes)
- R30.1 triage (per-failure classifier)

This module closes the loop by orchestrating those pieces. After
every run completes, `post_run_coordinate`:

  1. Reads gate-decision per-tool RAW pass rates (R42.5).
  2. For each tool below the 95% target, partitions the FAIL rows
     by triage_category (R30.1's classification, stamped at INSERT
     via R35.1).
  3. Routes each FAIL to the appropriate corrective action:
       - `test_gen_bug` → write a regen marker (R42.6 picks up)
       - `sut_regression` → already filed via R37.4 in-pipeline;
         counted toward ticket-actions for the ledger
       - `cascade` / `skip` → tagged for future R42.3 replay
       - else → operator_review counter
  4. Persists a ledger row so the dashboard can show
     "ARTA queued N corrections from this run".

The coordinator runs ONCE per run, called at the very end of
`run_chain_aware_post_processing_sync`. R42.6's existing 5-min loop
drains the marker files; the NEXT dispatched run reflects the
corrections.

Per the user's goal: *"Keep improving ARTA in a loop until it meets
the desired goal."*
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("arta.improvement_loop")

LEDGER_DIR = Path(".arta/improvement_loop")
REGEN_QUEUE_DIR = Path(".arta/regen_queue")
TARGET_RAW_PCT = 95.0
# R259.F — max tolerated ARTA-attributed : SUT-signal ratio among a tool's
# residual failures. 0.2 = "at most 1 ARTA defect per 5 real SUT findings".
# Raw pass-rate alone is blind to this: a tool can hit TARGET_RAW_PCT while
# every remaining failure is ARTA's own fabricated id, which satisfies the loop
# while leaving the mission (report SUT quality) unmet.
TARGET_NOISE_RATIO = 0.2


async def post_run_coordinate(run_id: str, project_id: str | None) -> dict:
    """Close the ARTA improvement loop for one run.

    Args:
        run_id: just-completed run identifier
        project_id: project UUID string (optional; used for ledger context)

    Returns:
        {
            "run_id": str,
            "actions_queued": {regen, ticket, replay, operator_review, ok},
            "tools_below_target": [{tool, raw_pct, fail_count}, ...],
            "ledger_path": str | None,
        }
    """
    try:
        from ..routers.execution import _REAL_RUNS, _REAL_RESULTS
    except Exception as exc:
        log.debug("R48.3: cannot import run state: %s", exc)
        return {
            "run_id": run_id,
            "actions_queued": {"regen": 0, "ticket": 0, "replay": 0,
                               "operator_review": 0, "ok": 0},
            "tools_below_target": [],
            "ledger_path": None,
        }

    run = _REAL_RUNS.get(run_id) or {}
    results = run.get("results") or _REAL_RESULTS.get(run_id) or []
    if not isinstance(results, list) or not results:
        log.info("R48.3: run %s has no results yet — loop deferred", run_id)
        return {
            "run_id": run_id,
            "actions_queued": {"regen": 0, "ticket": 0, "replay": 0,
                               "operator_review": 0, "ok": 0},
            "tools_below_target": [],
            "ledger_path": None,
        }

    # 1. Compute per-tool RAW.
    per_tool = _per_tool_raw(results)
    tools_below: list[dict] = [
        {"tool": t, "raw_pct": d["raw_pct"], "fail_count": d["failed"]}
        for t, d in per_tool.items()
        if d["raw_pct"] < TARGET_RAW_PCT and d["total"] > 0
    ]

    # 2. Build a test_id → triage_category map from the just-classified
    # defects in `chain_aware_defects`. R50.1 — pre-fix R48.3 read
    # `triage_category` from `execution_results.metadata` but R49.2's
    # CLUSTERED defect classifier writes results to the `defects` table
    # / `chain_aware_defects` runtime state, NOT back onto each result
    # row's metadata. That caused run-8ecf17 to misroute 8383 fails
    # into operator_review (regen=0, ticket=0). Fix: read triage from
    # the canonical defect source.
    defect_index: dict[str, dict] = {}
    chain_defects = run.get("chain_aware_defects") or []
    if isinstance(chain_defects, list):
        for d in chain_defects:
            if not isinstance(d, dict):
                continue
            cat = (
                d.get("triage_category")
                or (d.get("triage") or {}).get("triage_category")
            )
            if not cat:
                continue
            primary_tid = d.get("test_id")
            affected = d.get("affected_tests") or []
            test_ids = [primary_tid] if primary_tid else []
            for at in affected:
                if at and at not in test_ids:
                    test_ids.append(at)
            for tid in test_ids:
                if tid and tid not in defect_index:
                    defect_index[tid] = {
                        "triage_category": cat,
                        "priority": d.get("priority"),
                        "status_code": d.get("status_code"),
                        "signals": d.get("triage_signals") or [],
                        "sample_error": (
                            d.get("error_message") or d.get("description")
                            or d.get("root_cause") or ""
                        ),
                    }
    log.info(
        "R48.3/R50.1: built defect index with %d test_id entries from %d "
        "chain_aware_defects",
        len(defect_index), len(chain_defects) if isinstance(chain_defects, list) else 0,
    )

    # 2b. R259.F — the loop's exit criterion is NOISE-AWARE, not just raw pct.
    #
    # `TARGET_RAW_PCT` alone would let the loop stop at 95% raw while every
    # remaining failure is ARTA's own fabricated-id 404 — mission unmet, loop
    # satisfied. A tool is only "done" when it both passes enough AND its
    # residual failures are actually about the SUT.
    # Killswitch ARTA_R259_F_NOISE_EXIT_DISABLE=1 → pre-R259.F (raw pct only).
    per_tool_fidelity: dict[str, dict] = {}
    if os.environ.get("ARTA_R259_F_NOISE_EXIT_DISABLE") != "1":
        try:
            per_tool_fidelity = _per_tool_fidelity(results, defect_index)
            _below = {t["tool"] for t in tools_below}
            for _tool, _fid in per_tool_fidelity.items():
                if _tool in _below:
                    continue  # already flagged by the raw-pct gate
                if _fid["noise_signal_ratio"] > TARGET_NOISE_RATIO:
                    tools_below.append({
                        "tool": _tool,
                        "raw_pct": (per_tool.get(_tool) or {}).get("raw_pct", 0.0),
                        "fail_count": (per_tool.get(_tool) or {}).get("failed", 0),
                        "reason": "noise_above_target",
                        "noise_signal_ratio": _fid["noise_signal_ratio"],
                    })
                    log.info(
                        "R259.F: tool %s meets raw target but noise/signal=%.2f "
                        "> %.2f — ARTA's own defects dominate its failures; "
                        "loop stays open",
                        _tool, _fid["noise_signal_ratio"], TARGET_NOISE_RATIO,
                    )
        except Exception as _fid_exc:
            log.debug("R259.F: fidelity gate skipped: %s", _fid_exc)

    # 3. Partition each tool's FAIL rows by triage category + route.
    # R124.H — `sut_regression_signaled` is the canonical truthful key
    # (semantics: count of sut_regression rows detected + classified).
    # `ticket` is the back-compat alias retained for legacy dashboard
    # readers; both increment in lockstep at line ~190 below.
    actions = {
        "regen": 0,
        "ticket": 0,                       # back-compat alias
        "sut_regression_signaled": 0,      # R124.H — canonical key
        "replay": 0,
        "operator_review": 0,
        "ok": 0,
    }
    REGEN_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    for row in results:
        if not isinstance(row, dict):
            continue
        status = (row.get("status") or "").upper()
        if status in ("PASS",):
            actions["ok"] += 1
            continue
        tool = (row.get("automation_tool") or row.get("tool") or "unknown").lower()
        if tool not in {t["tool"] for t in tools_below}:
            continue   # tool already at target — don't queue more work
        md = row.get("metadata") or row.get("metadata_") or {}
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except (json.JSONDecodeError, TypeError):
                md = {}
        # R50.1 — prefer the defect_index lookup; fall back to metadata
        # for legacy paths or rows the defect classifier didn't reach.
        tid = row.get("test_id")
        idx_entry = defect_index.get(tid) if tid else None
        cat = (
            (idx_entry or {}).get("triage_category")
            or row.get("triage_category")
            or (md.get("triage_category") if isinstance(md, dict) else None)
        )
        # Merge defect-index signals into the metadata we pass to the
        # regen marker so R42.6 has the runtime failure context.
        if idx_entry:
            md = dict(md) if isinstance(md, dict) else {}
            md.setdefault("triage_signals", idx_entry.get("signals") or [])
            md.setdefault("status_code", idx_entry.get("status_code"))
            md.setdefault("error_message", idx_entry.get("sample_error") or md.get("error_message"))
        if cat == "test_gen_bug":
            _queue_regen_marker(row, md)
            actions["regen"] += 1
        elif cat == "sut_regression":
            # R124.H — renamed `ticket` → `sut_regression_signaled` so the
            # ledger key truthfully reflects DETECTION not Jira FILING.
            # Pre-R124.H the "ticket: 1951" count on the operator dashboard
            # implied 1951 Jira tickets were created. They weren't — R37.4
            # fires Jira create only when JIRA_URL/EMAIL/API_TOKEN are
            # configured. The actual semantic: 1951 sut_regression rows
            # were DETECTED + classified. Keep `ticket` alias for backward-
            # compat readers (legacy dashboards) but `sut_regression_signaled`
            # is the canonical truthful key.
            actions["sut_regression_signaled"] += 1
            actions["ticket"] += 1  # back-compat alias — deprecation tracked in R125
        elif status in ("BLOCKED",) or cat in ("cascade", "skip"):
            actions["replay"] += 1
        else:
            actions["operator_review"] += 1

    # 3. Persist a ledger row + log.
    ledger_path = _persist_ledger(run_id, project_id, per_tool, tools_below, actions)

    log.info(
        "R48.3: loop closed for run %s — tools_below_target=%d "
        "actions=%s ledger=%s",
        run_id, len(tools_below), actions,
        ledger_path.name if ledger_path else "(none)",
    )

    return {
        "run_id": run_id,
        "actions_queued": actions,
        "tools_below_target": tools_below,
        # R259.F — per-tool noise vs signal. A tool absent from
        # `tools_below_target` but carrying a high noise_signal_ratio here is
        # the operator's cue that its pass rate is measuring ARTA, not the SUT.
        "per_tool_fidelity": per_tool_fidelity,
        "target_noise_ratio": TARGET_NOISE_RATIO,
        "ledger_path": str(ledger_path) if ledger_path else None,
    }


def _per_tool_raw(results: list[dict]) -> dict[str, dict]:
    """Compute per-tool {passed, failed, blocked, skipped, total, raw_pct}.
    Same shape as quality_gate_agent._check_per_tool_raw_pass_rates."""
    per_tool: dict[str, dict] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        tool = (row.get("automation_tool") or row.get("tool") or "unknown").lower()
        slot = per_tool.setdefault(tool, {
            "passed": 0, "failed": 0, "blocked": 0, "skipped": 0,
            "total": 0, "raw_pct": 0.0,
        })
        slot["total"] += 1
        status = (row.get("status") or "").upper()
        if status == "PASS":
            slot["passed"] += 1
        elif status == "BLOCKED":
            slot["blocked"] += 1
        elif status in ("SKIP", "SKIPPED"):
            slot["skipped"] += 1
        else:
            slot["failed"] += 1
    for slot in per_tool.values():
        if slot["total"] > 0:
            slot["raw_pct"] = round(100.0 * slot["passed"] / slot["total"], 1)
    return per_tool


def _per_tool_fidelity(
    results: list[dict], defect_index: dict[str, dict],
) -> dict[str, dict]:
    """R259.F — per-tool NOISE vs SIGNAL, the companion to `_per_tool_raw`.

    `_per_tool_raw` asks "what fraction passed?". It cannot ask "are the
    failures even about the SUT?" — so a tool sitting at 95% raw with every
    remaining failure caused by ARTA's own fabricated ids looks finished.
    That is how the loop could declare victory while the mission (report SUT
    quality) was unmet.

    Triage already exists in this module but is used ONLY for routing
    (regen/ticket/replay). R259.F is the flip: the same triage now also SCORES.

    Returns per tool: {arta_attributed, sut_signal, unknown, noise_signal_ratio}.
    """
    from ..routers.execution import _R259_ARTA_CATEGORIES, _R259_SUT_CATEGORIES

    per_tool: dict[str, dict] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        status = (row.get("status") or "").upper()
        if status in ("PASS", "BLOCKED", "SKIP", "SKIPPED"):
            continue
        tool = (row.get("automation_tool") or row.get("tool") or "unknown").lower()
        slot = per_tool.setdefault(tool, {
            "arta_attributed": 0, "sut_signal": 0, "unknown": 0,
            "noise_signal_ratio": 0.0,
        })
        tid = row.get("test_id") or row.get("title")
        cat = (defect_index.get(tid) or {}).get("triage_category") or ""
        if cat in _R259_ARTA_CATEGORIES:
            slot["arta_attributed"] += 1
        elif cat in _R259_SUT_CATEGORIES:
            slot["sut_signal"] += 1
        else:
            slot["unknown"] += 1
    for slot in per_tool.values():
        slot["noise_signal_ratio"] = (
            round(slot["arta_attributed"] / slot["sut_signal"], 2)
            if slot["sut_signal"] else float(slot["arta_attributed"])
        )
    return per_tool


def _queue_regen_marker(row: dict, md: dict) -> None:
    """Write a regen marker file the R42.6 consumer drains every 5 min."""
    test_id = row.get("test_id") or row.get("title") or "unknown"
    payload = {
        "test_id": test_id,
        "triage_category": "test_gen_bug",
        "status_code": md.get("status_code") if isinstance(md, dict) else None,
        "signals": (md.get("triage_signals") if isinstance(md, dict) else None) or [],
        "sample_error": (
            row.get("error_message") or row.get("error")
            or (md.get("error_message") if isinstance(md, dict) else "")
            or ""
        ),
        "queued_by": "R48.3_post_run_coordinator",
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    marker = REGEN_QUEUE_DIR / f"{_safe(test_id)}.json"
    try:
        marker.write_text(json.dumps(payload, indent=2))
    except Exception as exc:
        log.debug("R48.3: marker write failed for %s: %s", test_id, exc)


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(s))[:200]


def queue_upstream_regen_marker(
    requirement_id: str,
    artifact_kind: str,
    hint: str,
    *,
    project_id: str | None = None,
    signals: list | None = None,
) -> Path | None:
    """B4 — queue an UPSTREAM-artifact regen marker (Gherkin / requirement).

    The R42.6 consumer routes `artifact_kind != "test_script"` to its
    upstream handler, which re-invokes the ATDD designer (gherkin) or routes
    to operator review (requirement) — the upstream-first fix, instead of
    repeatedly regenerating doomed scripts from a malformed Gherkin. The
    `hint` carries the upstream-quality violations the LLM should address.
    """
    REGEN_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_kind": artifact_kind,          # "gherkin" | "requirement"
        "requirement_id": requirement_id,
        "test_id": requirement_id,               # consumer stem/log compatibility
        "triage_category": "upstream_gen_quality",
        "hint": hint or "",
        "signals": signals or [],
        "project_id": project_id,
        "queued_by": "B4_upstream_gate",
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    marker = REGEN_QUEUE_DIR / f"UPSTREAM-{artifact_kind}-{_safe(requirement_id)}.json"
    try:
        marker.write_text(json.dumps(payload, indent=2))
        return marker
    except Exception as exc:
        log.debug("B4: upstream marker write failed for %s: %s", requirement_id, exc)
        return None


def _persist_ledger(
    run_id: str,
    project_id: str | None,
    per_tool: dict,
    tools_below: list[dict],
    actions: dict,
) -> Path | None:
    """Write a per-run ledger row so the dashboard can show
    `ARTA queued N corrections from this run`."""
    try:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        path = LEDGER_DIR / f"{_safe(run_id)}.json"
        path.write_text(json.dumps({
            "run_id": run_id,
            "project_id": project_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "per_tool_raw": per_tool,
            "tools_below_target": tools_below,
            "actions_queued": actions,
            "target_pct": TARGET_RAW_PCT,
        }, indent=2))
        return path
    except Exception as exc:
        log.debug("R48.3: ledger persist failed for %s: %s", run_id, exc)
        return None


def read_recent_ledger(limit: int = 10) -> list[dict]:
    """Helper for a dashboard endpoint — returns the N most recent
    ledger rows. Best-effort; missing dir → empty list."""
    if not LEDGER_DIR.is_dir():
        return []
    rows: list[dict] = []
    files = sorted(
        LEDGER_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    for f in files:
        try:
            rows.append(json.loads(f.read_text()))
        except Exception:
            continue
    return rows
