"""R42.6 — regen-queue consumer.

R37.1 + R38.4 queue `test_gen_regen` proposals → marker files at
`.arta/regen_queue/{test_id}.json`. Pre-R42.6 those markers were
written but never consumed → the self-healing pillar only "succeeded"
on paper.

This worker drains the queue:
  1. Scan `.arta/regen_queue/` every CYCLE_SECS.
  2. For each marker, look up the test in GENERATED_TESTS to find its
     requirement + tool + Gherkin context.
  3. Invoke `automation_engineer.generate_single(gherkin, tool, risk)`
     with the marker's failure-text as a Tier-2 hint prepended to the
     prompt (so the LLM avoids the same defect class).
  4. Write the regenerated content to the spec's existing path
     (overwrite). Update GENERATED_TESTS in-memory + best-effort DB.
  5. Move the marker to `.arta/regen_queue/applied/` for audit.

Idempotency: per-marker file lock via fcntl. Cap N regens per cycle
so the LLM isn't thrashed.

Failure modes (all best-effort):
  - LLM unavailable → marker stays, retry next cycle.
  - Test not found in GENERATED_TESTS → move to `applied/orphans/`.
  - Disk write failure → marker stays.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("arta.regen_consumer")

QUEUE_DIR = Path(".arta/regen_queue")
APPLIED_DIR = QUEUE_DIR / "applied"
ORPHAN_DIR = APPLIED_DIR / "orphans"
CYCLE_SECS = 300
MAX_PER_CYCLE = 20


async def _regen_one(marker_path: Path) -> bool:
    """Process a single marker. Returns True when consumed (success or
    permanent failure that warrants moving the marker out of the queue);
    False when transient failure → leave the marker for next cycle."""
    try:
        payload = json.loads(marker_path.read_text())
    except Exception as exc:
        log.warning("R42.6: marker %s unreadable, moving to orphans: %s", marker_path, exc)
        ORPHAN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(marker_path), str(ORPHAN_DIR / marker_path.name))
        except Exception:
            pass
        return True

    test_id = payload.get("test_id") or marker_path.stem
    sample_error = payload.get("sample_error") or ""
    signals = payload.get("signals") or []
    triage_category = payload.get("triage_category") or "test_gen_bug"

    # R130.E KEYSTONE — gate auto-heal on triage_category. Pre-R130.E the
    # consumer regen-flow fired for ALL markers regardless of category,
    # producing false-positive "heals" on operator_review (stale auth —
    # regen can't fix the bad cookie) and sut_regression (real SUT bug —
    # regen masks the truthful P0 signal). This corrupted the "1125 SUT
    # bugs detected" dashboard count + caused R37.4 Jira queue noise.
    # Post-R130.E: such markers are MOVED to applied/gated_no_regen/
    # with a truthful log line — operator must address via Refresh Auth
    # (operator_review) OR Jira/SUT-team escalation (sut_regression).
    _R130_E_GATED_TRIAGE_CATEGORIES = frozenset({"operator_review", "sut_regression"})
    if triage_category in _R130_E_GATED_TRIAGE_CATEGORIES:
        GATED_DIR = APPLIED_DIR / "gated_no_regen"
        GATED_DIR.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(marker_path), str(GATED_DIR / marker_path.name))
        except Exception as _r130_e_exc:
            log.warning(
                "R130.E: failed to move gated marker %s: %s",
                marker_path.name, _r130_e_exc,
            )
        log.info(
            "R130.E: marker %s gated (triage=%s) — auto-heal does not "
            "address this category; routed to gated_no_regen/. Operator "
            "must address via Refresh Auth (operator_review) OR Jira "
            "(sut_regression).",
            marker_path.name, triage_category,
        )
        return True  # marker consumed (moved); R42.6 cycle continues

    # ── R213 Fix — ADVERSARIAL/negative-test regen guard ─────────────────────
    # Adversarial test cases intentionally exercise INVALID inputs (hallucinated
    # endpoints, non-existent schema elements, malformed params) — they are
    # SUPPOSED to fail/grounding-violate. Queueing them for regen creates a
    # FUTILE LOOP: regen can never "fix" an intentionally-invalid input, so the
    # marker re-violates → re-queues → monopolizes the serialized claude_code CLI
    # Route these to applied/gated_adversarial/ with a truthful log instead of
    # regenerating. Killswitch ARTA_R213_ADVERSARIAL_REGEN_GUARD_DISABLE=1.
    import os as _os_adv
    if _os_adv.environ.get("ARTA_R213_ADVERSARIAL_REGEN_GUARD_DISABLE", "").lower() not in ("1", "true"):
        _adv_id = str(test_id or "").lower()
        _adv_sig = " ".join(str(s).lower() for s in (signals or []))
        if "adversarial" in _adv_id or "adversarial" in marker_path.stem.lower() or "adversarial" in _adv_sig:
            GATED_ADV_DIR = APPLIED_DIR / "gated_adversarial"
            GATED_ADV_DIR.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(marker_path), str(GATED_ADV_DIR / marker_path.name))
            except Exception as _adv_exc:
                log.warning("R213: failed to move adversarial marker %s: %s",
                            marker_path.name, _adv_exc)
            log.info(
                "R213: marker %s gated as ADVERSARIAL — adversarial/negative tests "
                "use invalid inputs by design; regen cannot ground them (futile "
                "loop that starves the CLI). Routed to gated_adversarial/.",
                marker_path.name)
            return True  # consumed (moved); no regen

    # ── B4: upstream-artifact regen routing ──────────────────────────────────
    # Markers can target an UPSTREAM artifact (Gherkin / requirement), not just
    # a test script. The upstream Gherkin-quality gate (routers/tests.py) queues
    # `artifact_kind="gherkin"` markers so a malformed/fallback Gherkin is
    # regenerated at the ATDD layer (the true root cause) instead of repeatedly
    # producing doomed scripts. `requirement` markers route to operator review —
    # ARTA never rewrites source-authored requirements.
    artifact_kind = (payload.get("artifact_kind") or "test_script").lower()
    if artifact_kind != "test_script":
        return await _regen_upstream_artifact(artifact_kind, payload, marker_path)

    try:
        from ..routers.tests_state import GENERATED_TESTS
    except Exception as exc:
        log.warning("R42.6: GENERATED_TESTS import failed: %s", exc)
        return False

    # R81.3 — tool-aware HARD filter when marker.signals contains a
    # recognised tool name. Pre-R81.3 the fuzzy matcher was tool-blind,
    # retries','k6']) would prefix-match `TC-AM-002-01-api` (Newman
    # entry, alpha-first) instead of `TC-AM-002-02-perf` (the k6 entry).
    # as Newman content overwriting an unrelated Newman file. Tool-aware
    # early-match eliminates the regression while preserving the existing
    # tool-blind cascade as fallback for markers without tool signals.
    _R81_3_TOOL_NAMES = {"k6", "playwright", "newman", "pytest", "zap", "axe", "selenium", "cypress"}
    _tool_hint = None
    for _s in (signals or []):
        if isinstance(_s, str) and _s.lower() in _R81_3_TOOL_NAMES:
            _tool_hint = _s.lower()
            break
    if _tool_hint is None and isinstance(payload.get("tool"), str):
        if payload["tool"].lower() in _R81_3_TOOL_NAMES:
            _tool_hint = payload["tool"].lower()

    test_entry: dict[str, Any] | None = None
    if _tool_hint:
        _candidates = [
            t for t in GENERATED_TESTS
            if (t.get("automation_tool") or t.get("tool") or "").lower() == _tool_hint
        ]
        if _candidates:
            # Replay the existing matcher cascade against the tool-filtered
            # pool: exact id → exact test_id → 3-seg prefix → REQ→TC →
            # API-prefix.
            test_entry = next((t for t in _candidates if t.get("id") == test_id), None)
            if test_entry is None:
                test_entry = next((t for t in _candidates if t.get("test_id") == test_id), None)
            if test_entry is None and isinstance(test_id, str) and "-" in test_id:
                _parts = test_id.split("-")
                if len(_parts) >= 3:
                    _pfx = "-".join(_parts[:3])
                    test_entry = next(
                        (t for t in _candidates
                         if (t.get("id") or "").startswith(_pfx)
                         or (t.get("test_id") or "").startswith(_pfx)),
                        None,
                    )
                    if test_entry is None and _pfx.upper().startswith("REQ-"):
                        _tcp = "TC-" + _pfx[4:]
                        test_entry = next(
                            (t for t in _candidates
                             if (t.get("id") or "").startswith(_tcp)
                             or (t.get("test_id") or "").startswith(_tcp)),
                            None,
                        )
            if test_entry is None and isinstance(test_id, str):
                import re as _re_81_3
                _m = _re_81_3.match(
                    r"^API-req_([a-z]{2,4})_(\d{2,4})_api-",
                    test_id, _re_81_3.IGNORECASE,
                )
                if _m:
                    _proj = _m.group(1).upper()
                    _num = _m.group(2)
                    _tcp2 = f"TC-{_proj}-{_num}-"
                    test_entry = next(
                        (t for t in _candidates
                         if (t.get("id") or "").startswith(_tcp2)
                         or (t.get("test_id") or "").startswith(_tcp2)),
                        None,
                    )
            if test_entry is not None:
                log.info(
                    "R81.3: tool-aware match for marker %s → %s (tool=%s, pool=%d)",
                    test_id,
                    test_entry.get("id") or test_entry.get("test_id"),
                    _tool_hint, len(_candidates),
                )

    # R50.2 — fuzzy test_id resolution. R48.3 emits markers keyed by
    # the test_id stamped at dispatch time (e.g., `TC-AM-012-AUTO024`)
    # but GENERATED_TESTS uses a different key for the SAME logical
    # test (e.g., `TC-AM-012` for the requirement-level test). Pre-R50.2
    # every marker landed in orphans/ → R42.6 healed nothing.
    # Resolution order:
    #   1. Exact match on `id`.
    #   2. Exact match on `test_id` field (some entries use it).
    #   3. Prefix match: marker `TC-AM-012-AUTO024` matches
    #      GENERATED_TESTS entry `TC-AM-012` when the marker's first
    #      3 hyphen-segments equal the entry's id.
    # R81.3 — when the tool-aware early match above found a hit,
    # test_entry is already set and the tool-blind cascade is skipped.
    if test_entry is None:
        test_entry = next(
            (t for t in GENERATED_TESTS if t.get("id") == test_id),
            None,
        )
    if test_entry is None:
        test_entry = next(
            (t for t in GENERATED_TESTS if t.get("test_id") == test_id),
            None,
        )
    if test_entry is None and test_id and "-" in test_id:
        # Prefix match — take the first 3 segments as the requirement-
        # level id and find any matching entry.
        parts = test_id.split("-")
        if len(parts) >= 3:
            prefix = "-".join(parts[:3])  # e.g. TC-AM-012
            test_entry = next(
                (t for t in GENERATED_TESTS
                 if (t.get("id") or "").startswith(prefix)
                 or (t.get("test_id") or "").startswith(prefix)),
                None,
            )
            if test_entry:
                log.info(
                    "R50.2: fuzzy-matched marker %s → existing test %s "
                    "(prefix=%s)",
                    test_id, test_entry.get("id") or test_entry.get("test_id"),
                    prefix,
                )

            # R57.10 — REQ→TC translation. Live audit found 98 orphans;
            # entry=`TC-AM-012-analytics-e2e`. The analytics test agent
            # writes markers with the requirement_id stem (`REQ-`) while
            # GENERATED_TESTS persists entries with `TC-` prefix. Pre-
            # `startswith("TC-AM-012")`. The translation step retries
            # with the prefix swapped.
            if test_entry is None and prefix.upper().startswith("REQ-"):
                tc_prefix = "TC-" + prefix[4:]
                test_entry = next(
                    (t for t in GENERATED_TESTS
                     if (t.get("id") or "").startswith(tc_prefix)
                     or (t.get("test_id") or "").startswith(tc_prefix)),
                    None,
                )
                if test_entry:
                    log.info(
                        "R57.10: REQ→TC-translated match for marker %s → %s "
                        "(tc_prefix=%s)",
                        test_id, test_entry.get("id") or test_entry.get("test_id"),
                        tc_prefix,
                    )

            # R57.10 — case-insensitive prefix match. Operator-typed
            # marker test_ids occasionally drift in case (`tc-am-012-x`
            # vs `TC-AM-012-X`). Cheap retry covers it.
            if test_entry is None:
                # Try both the original prefix and the REQ→TC-translated form, lower-cased
                candidate_prefixes_lower = [prefix.lower()]
                if prefix.upper().startswith("REQ-"):
                    candidate_prefixes_lower.append(("TC-" + prefix[4:]).lower())
                for pfx in candidate_prefixes_lower:
                    test_entry = next(
                        (t for t in GENERATED_TESTS
                         if (t.get("id") or "").lower().startswith(pfx)
                         or (t.get("test_id") or "").lower().startswith(pfx)),
                        None,
                    )
                    if test_entry:
                        log.info(
                            "R57.10: case-insensitive prefix match for marker %s → %s "
                            "(pfx=%s)",
                            test_id, test_entry.get("id") or test_entry.get("test_id"),
                            pfx,
                        )
                        break

            # R57.10 — substring last-resort with WARN. Cover the rare
            # remaining patterns (marker has separator drift,
            # requirement segment only). Weaker match → warn loudly so
            # operators can investigate any mis-targeting.
            if test_entry is None and len(parts) >= 2:
                # Take only the requirement segment (parts[:2] = e.g. REQ-AM or TC-AM)
                req_seg = "-".join(parts[:2]).lower()
                # Normalise REQ→TC for the substring search
                req_seg_normalized = req_seg.replace("req-", "tc-")
                # Prefer the more-specific 3-segment match before substring;
                # use 2-segment req_seg as a containment check on the entry id.
                test_entry = next(
                    (t for t in GENERATED_TESTS
                     if req_seg_normalized in (t.get("id") or "").lower()
                     or req_seg_normalized in (t.get("test_id") or "").lower()),
                    None,
                )
                if test_entry:
                    log.warning(
                        "R57.10: substring fallback match for marker %s → %s "
                        "(req_seg=%s) — weakest match; verify the regen targets "
                        "the intended test",
                        test_id, test_entry.get("id") or test_entry.get("test_id"),
                        req_seg_normalized,
                    )

    # R77.2 — Newman item-level marker pattern (R57.10 v3).
    # R42.6 emits markers per failed Newman ITEM (one per URL+method
    # within a collection). The test_id encodes
    # `API-req_am_NNN_api-METHOD_url-fragment` (e.g.
    # `API-req_am_001_api-POST _api_storage_au`). GENERATED_TESTS
    # entries are collection-level (`TC-AM-NNN-XX-api`). The earlier
    # fallback chain misses this case because:
    #   - Step 3 prefix=`API-req_am_001` doesn't start with `TC-`
    #   - Step 4 REQ→TC translation requires REQ- prefix
    #   - Step 6 substring on `api-req` doesn't appear in TC-AM entries
    # Live evidence: 155 orphans of this shape accumulated post-R57.10
    # — until R77.2 they had no resolution path.
    if test_entry is None and isinstance(test_id, str):
        import re as _re_77_2
        _m_api = _re_77_2.match(
            r"^API-req_(?:[a-z]{2,4})_(\d{2,4})_api-",
            test_id, _re_77_2.IGNORECASE,
        )
        if _m_api:
            _req_num = _m_api.group(1)
            # Match BOTH project family (AM/BT/...) and the digit run.
            # Re-extract from the marker to keep the project tag flexible.
            _m_full = _re_77_2.match(
                r"^API-req_([a-z]{2,4})_(\d{2,4})_api-",
                test_id, _re_77_2.IGNORECASE,
            )
            _proj_tag = _m_full.group(1).upper() if _m_full else "AM"
            _tc_prefix = f"TC-{_proj_tag}-{_req_num}-"
            test_entry = next(
                (t for t in GENERATED_TESTS
                 if (t.get("id") or "").startswith(_tc_prefix)
                 or (t.get("test_id") or "").startswith(_tc_prefix)),
                None,
            )
            if test_entry:
                log.info(
                    "R77.2: Newman item-marker matched %s → %s "
                    "(tc_prefix=%s)",
                    test_id,
                    test_entry.get("id") or test_entry.get("test_id"),
                    _tc_prefix,
                )

    # R124.M.2 — R57.10 v4 — extended orphan-resolver patterns.
    # Live evidence from .arta/regen_queue/applied/orphans (run-d52a8c):
    #   - "PERF-req_am_NNN_performance" / "PERF-req_am_NNN_chain_N"  (k6)
    #   - "Playwright_req_am_NNN.spec_timed_out"                      (PW)
    #   - "API-req_am_NNN_chain_adv.postman_collection-step_NN_..."   (Newman chain)
    # R77.2 only matched API-...-api- shapes; these 3 went straight to orphans.
    if test_entry is None and isinstance(test_id, str):
        import re as _re_r124_m
        # K6 PERF marker → TC-{PROJ}-{NNN}-*-perf entry
        _m_perf = _re_r124_m.match(
            r"^PERF-req_([a-z]{2,4})_(\d{2,4})_",
            test_id, _re_r124_m.IGNORECASE,
        )
        if _m_perf:
            _proj_tag = _m_perf.group(1).upper()
            _req_num = _m_perf.group(2)
            _tc_prefix = f"TC-{_proj_tag}-{_req_num}-"
            test_entry = next(
                (t for t in GENERATED_TESTS
                 if (t.get("id") or "").startswith(_tc_prefix)
                 and (t.get("tool") or "").lower() == "k6"),
                None,
            )
            if test_entry:
                log.info(
                    "R124.M.2: k6 PERF-marker matched %s → %s (prefix=%s)",
                    test_id, test_entry.get("id") or test_entry.get("test_id"),
                    _tc_prefix,
                )
        # Playwright spec-timeout marker
        if test_entry is None:
            _m_pw = _re_r124_m.match(
                r"^Playwright_req_([a-z]{2,4})_(\d{2,4})\.spec",
                test_id, _re_r124_m.IGNORECASE,
            )
            if _m_pw:
                _proj_tag = _m_pw.group(1).upper()
                _req_num = _m_pw.group(2)
                _tc_prefix = f"TC-{_proj_tag}-{_req_num}-"
                test_entry = next(
                    (t for t in GENERATED_TESTS
                     if (t.get("id") or "").startswith(_tc_prefix)
                     and (t.get("tool") or "").lower() == "playwright"),
                    None,
                )
                if test_entry:
                    log.info(
                        "R124.M.2: PW timeout-marker matched %s → %s",
                        test_id, test_entry.get("id") or test_entry.get("test_id"),
                    )
        # Newman chain-test marker: API-req_am_NNN_chain_*
        if test_entry is None:
            _m_chain = _re_r124_m.match(
                r"^API-req_([a-z]{2,4})_(\d{2,4})_chain",
                test_id, _re_r124_m.IGNORECASE,
            )
            if _m_chain:
                _proj_tag = _m_chain.group(1).upper()
                _req_num = _m_chain.group(2)
                _tc_prefix = f"TC-{_proj_tag}-{_req_num}-"
                test_entry = next(
                    (t for t in GENERATED_TESTS
                     if (t.get("id") or "").startswith(_tc_prefix)
                     and (t.get("tool") or "").lower() == "newman"),
                    None,
                )
                if test_entry:
                    log.info(
                        "R124.M.2: Newman chain-marker matched %s → %s",
                        test_id, test_entry.get("id") or test_entry.get("test_id"),
                    )

    if test_entry is None:
        log.warning(
            "R42.6: test %s not found in GENERATED_TESTS — marker -> orphans",
            test_id,
        )
        ORPHAN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(marker_path), str(ORPHAN_DIR / marker_path.name))
        except Exception:
            pass
        return True

    tool = (test_entry.get("tool") or "").lower()
    if tool not in ("playwright", "newman", "k6", "axe", "zap", "pytest"):
        log.info("R42.6: skip %s (unsupported tool=%s)", test_id, tool)
        return True

    spec_path = test_entry.get("script_path") or test_entry.get("spec_path")
    gherkin = test_entry.get("gherkin") or test_entry.get("gherkin_text") or ""
    requirement_id = test_entry.get("requirement_id")
    if not gherkin or not spec_path:
        log.warning(
            "R42.6: %s missing gherkin/spec_path — cannot regen",
            test_id,
        )
        return True

    try:
        from ...agents.automation_engineer import AutomationEngineerAgent
        from ..main import app as _app
    except Exception as exc:
        log.warning("R42.6: agent import failed: %s", exc)
        return False

    client = getattr(_app.state, "anthropic", None)
    if client is None:
        log.info("R42.6: LLM unavailable; deferring %s", test_id)
        return False

    agent = AutomationEngineerAgent(client)
    hint_block = _build_hint_block(triage_category, signals, sample_error)
    annotated_gherkin = (
        f"# REGEN HINT (do not include in script): {hint_block}\n\n{gherkin}"
        if hint_block else gherkin
    )

    risk = {
        "requirement_id": requirement_id or test_id,
        "priority": test_entry.get("priority") or "P2",
        "project_id": str(test_entry.get("project_id") or "") or None,
    }

    try:
        script = await agent.generate_single(annotated_gherkin, tool, risk)
    except Exception as exc:
        log.warning("R42.6: regen failed for %s (%s): %s", test_id, tool, exc)
        return False

    if not script or not getattr(script, "content", None):
        log.warning("R42.6: empty regen output for %s", test_id)
        return False

    spec_file = Path(spec_path)
    try:
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(script.content)
    except Exception as exc:
        log.warning("R42.6: write %s failed: %s", spec_path, exc)
        return False

    # R81.1.c — sync the regen'd content back into the GENERATED_TESTS
    # entry. Pre-R81.1.c the consumer wrote to disk but never updated
    # `script_content`, so frontend Code panels stayed empty AND the
    # generation_error flag stuck around forever. Verified live for
    # on disk because the consumer never touched the entry's content.
    test_entry["script_content"] = script.content
    if "generation_error" in test_entry:
        # The initial gen failed prefix-check (or some other validation),
        # but the regen produced valid content + disk-write succeeded.
        # Clear the error stamp so the UI stops showing the warning.
        log.info(
            "R81.1.c: clearing generation_error=%s on %s after successful regen",
            test_entry.get("generation_error"), test_id,
        )
        test_entry.pop("generation_error", None)
    # Best-effort: persist to disk so subsequent process restarts pick up
    # the recovered content. `_save_tests_json` is the canonical writer.
    try:
        from ..routers.tests_helpers import _save_tests_json
        _save_tests_json(seed_test_ids=set())
    except Exception as _save_exc:
        log.debug("R81.1.c: _save_tests_json best-effort skipped: %s", _save_exc)

    test_entry["regen_count"] = int(test_entry.get("regen_count", 0)) + 1
    test_entry["last_regen_at"] = datetime.now(timezone.utc).isoformat()
    test_entry["last_regen_signals"] = signals[:5] if isinstance(signals, list) else []

    APPLIED_DIR.mkdir(parents=True, exist_ok=True)
    try:
        applied_path = APPLIED_DIR / f"{marker_path.stem}-{int(datetime.now().timestamp())}.json"
        shutil.move(str(marker_path), str(applied_path))
    except Exception:
        try:
            marker_path.unlink()
        except Exception:
            pass

    log.info(
        "R42.6: regenerated %s (tool=%s, signals=%s) -> %s",
        test_id, tool, signals[:3], spec_path,
    )
    return True


def _b4_safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(s))[:200]


async def _regen_upstream_artifact(artifact_kind: str, payload: dict, marker_path: Path) -> bool:
    """B4 — handle an upstream-artifact regen marker.

    gherkin     → re-invoke the ATDD designer for the requirement with the
                  quality hint, re-validate, and on success persist the healed
                  Gherkin into GENERATED_TESTS + queue downstream script regen.
    requirement → ARTA does not rewrite source-authored requirements; route to
                  operator review (gated_no_regen) carrying the hint.
    """
    requirement_id = payload.get("requirement_id") or marker_path.stem
    hint = payload.get("hint") or ""
    GATED_DIR = APPLIED_DIR / "gated_no_regen"

    # requirement-level issues are for the operator — never auto-rewritten.
    if artifact_kind == "requirement":
        GATED_DIR.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(marker_path), str(GATED_DIR / marker_path.name))
        except Exception:
            pass
        log.info("B4: requirement %s routed to operator (gated_no_regen): %s",
                 requirement_id, (hint or "")[:160])
        return True

    if artifact_kind != "gherkin":
        ORPHAN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(marker_path), str(ORPHAN_DIR / marker_path.name))
        except Exception:
            pass
        log.warning("B4: unknown artifact_kind=%s for %s — orphaned", artifact_kind, requirement_id)
        return True

    # ── gherkin regen ────────────────────────────────────────────────────────
    try:
        from ...agents.atdd_designer import ATDDDesignerAgent
        from ..routers.tests import _find_requirement
        from ..routers.tests_state import GENERATED_TESTS
        from ...agents.upstream_quality import validate_gherkin_stage
        from ..main import app as _app
    except Exception as exc:
        log.warning("B4: gherkin-regen imports failed: %s", exc)
        return False

    requirement = _find_requirement(requirement_id)
    if not requirement:
        ORPHAN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(marker_path), str(ORPHAN_DIR / marker_path.name))
        except Exception:
            pass
        log.warning("B4: requirement %s not found — orphaned gherkin marker", requirement_id)
        return True

    client = getattr(_app.state, "anthropic", None)
    if client is None:
        log.info("B4: LLM unavailable; deferring gherkin regen for %s", requirement_id)
        return False  # transient — retry next cycle

    # Embed the quality hint so the LLM addresses the specific violations.
    req_for_gen = dict(requirement)
    if hint:
        req_for_gen["description"] = (
            f"{requirement.get('description', '')}\n\n"
            f"# UPSTREAM QUALITY HINT (address these before emitting Gherkin):\n{hint}"
        )
    risk = [{
        "requirement_id": requirement_id,
        "id": requirement.get("id"),
        "priority": requirement.get("priority") or "P2",
    }]
    try:
        agent = ATDDDesignerAgent(client)
        result = await agent.generate([req_for_gen], risk)
        new_gherkin = result.get("gherkin_scenarios", []) if isinstance(result, dict) else []
    except Exception as exc:
        log.warning("B4: ATDD regen failed for %s: %s", requirement_id, exc)
        return False  # transient — retry next cycle

    # Re-validate the regenerated Gherkin; still-blocked → operator.
    check = validate_gherkin_stage(new_gherkin, requirement, requirement.get("acceptance_criteria"))
    if check["should_block"] or not new_gherkin:
        GATED_DIR.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(marker_path), str(GATED_DIR / marker_path.name))
        except Exception:
            pass
        log.info("B4: gherkin regen for %s still blocked (%d errors) — routed to operator",
                 requirement_id, check.get("error_count", 0))
        return True

    # Persist healed Gherkin into all GENERATED_TESTS entries for this requirement.
    healed = "\n\n".join(g for g in new_gherkin if isinstance(g, str))
    n_updated = n_queued = 0
    for t in GENERATED_TESTS:
        if t.get("requirement_id") != requirement_id:
            continue
        t["gherkin"] = healed
        t["last_regen_at"] = datetime.now(timezone.utc).isoformat()
        n_updated += 1
        # Queue downstream script regen so the now-aligned scripts get rebuilt.
        if t.get("script_path"):
            try:
                _tid = t.get("id") or t.get("test_id")
                (QUEUE_DIR / f"{_b4_safe(_tid)}.json").write_text(json.dumps({
                    "test_id": _tid,
                    "triage_category": "test_gen_bug",
                    "tool": t.get("tool") or t.get("automation_tool"),
                    "signals": ["upstream_gherkin_healed", t.get("tool") or ""],
                    "sample_error": "Gherkin healed upstream (B4) — rebuild script from aligned scenario",
                    "queued_by": "B4_gherkin_heal",
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                }, indent=2))
                n_queued += 1
            except Exception:
                pass

    APPLIED_DIR.mkdir(parents=True, exist_ok=True)
    try:
        applied_path = APPLIED_DIR / f"{marker_path.stem}-{int(datetime.now().timestamp())}.json"
        shutil.move(str(marker_path), str(applied_path))
    except Exception:
        pass
    log.info("B4: healed Gherkin for %s — updated %d entries, queued %d script regens",
             requirement_id, n_updated, n_queued)
    return True


def _build_hint_block(triage_category: str, signals: list, sample_error: str) -> str:
    """Compose the Tier-2 hint string the LLM sees prepended to the
    Gherkin. Keeps it short — LLM context window is precious."""
    parts: list[str] = []
    if triage_category:
        parts.append(f"category={triage_category}")
    if signals:
        parts.append(f"signals={','.join(str(s) for s in signals[:3])}")
    if sample_error:
        excerpt = " ".join(sample_error.split())[:200]
        parts.append(f"prior_error={excerpt}")
    if not parts:
        return ""
    return (
        "Previous gen of this test failed runtime. Avoid the prior failure "
        "mode: " + "; ".join(parts) + ". Generate a corrected version."
    )


async def _drain_queue_once() -> int:
    """One pass over the queue. Returns N markers consumed."""
    if not QUEUE_DIR.is_dir():
        return 0
    # R213 — runtime pause. During a bulk regen the consumer competes for the
    # serialized claude_code CLI (R161 semaphore=1) and starves the foreground
    # gen. Pause it via env ARTA_REGEN_CONSUMER_PAUSE=1 OR a `.paused` sentinel
    # file in the queue dir (runtime-toggle, no restart needed). Default off.
    import os as _os_pause
    if (_os_pause.environ.get("ARTA_REGEN_CONSUMER_PAUSE", "").lower() in ("1", "true")
            or (QUEUE_DIR / ".paused").exists()):
        log.debug("R213: regen consumer paused (env/sentinel) — skipping drain")
        return 0
    markers = sorted(p for p in QUEUE_DIR.glob("*.json") if p.is_file())[:MAX_PER_CYCLE]
    if not markers:
        return 0
    consumed = 0
    for marker in markers:
        try:
            with marker.open("rb") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue   # another worker holds the lock
                if await _regen_one(marker):
                    consumed += 1
        except FileNotFoundError:
            continue
        except Exception as exc:
            log.warning("R42.6: marker %s caused unhandled error: %s", marker, exc)
    return consumed


_TASK: asyncio.Task | None = None


async def _consumer_loop() -> None:
    """Long-running loop. Restart-resilient — caller (api.main) ensures
    the task is recreated after restart."""
    log.info("R42.6: regen consumer loop started (cycle=%ds, cap=%d)",
             CYCLE_SECS, MAX_PER_CYCLE)
    while True:
        try:
            n = await _drain_queue_once()
            if n:
                log.info("R42.6: drained %d marker(s) this cycle", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("R42.6: cycle error: %s", exc)
        try:
            await asyncio.sleep(CYCLE_SECS)
        except asyncio.CancelledError:
            raise


def start_regen_consumer() -> None:
    """Spawn the consumer task. Idempotent — multiple calls are no-ops."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _TASK = loop.create_task(_consumer_loop())
    except Exception as exc:
        log.warning("R42.6: consumer start failed: %s", exc)


def is_alive() -> bool:
    """R55.11 — return True iff the consumer task exists and is not done.

    Pre-R55.11, `start_regen_consumer` could fail silently if
    `asyncio.get_event_loop()` raised (e.g., wrong thread context); the
    task stayed None and operators had no signal. Callers should invoke
    this immediately after `start_regen_consumer()` in the lifespan hook
    + via the /api/health/regen-consumer endpoint to surface the failure.
    """
    return _TASK is not None and not _TASK.done()


def queue_depth() -> dict[str, int]:
    """R55.11 — return current queue/applied/orphan counts.

    Operator-facing diagnostic used by /api/health/regen-consumer. Cheap
    O(N) directory scan; safe to invoke per health check (N is bounded
    by MAX_PER_CYCLE * runs-since-last-prune, typically < 1000).
    """
    out = {"queue_depth": 0, "applied": 0, "orphans": 0}
    try:
        if QUEUE_DIR.is_dir():
            out["queue_depth"] = sum(
                1 for p in QUEUE_DIR.iterdir()
                if p.is_file() and p.suffix == ".json"
            )
        if APPLIED_DIR.is_dir():
            out["applied"] = sum(
                1 for p in APPLIED_DIR.iterdir()
                if p.is_file() and p.suffix == ".json"
            )
        if ORPHAN_DIR.is_dir():
            out["orphans"] = sum(
                1 for p in ORPHAN_DIR.iterdir()
                if p.is_file() and p.suffix == ".json"
            )
    except Exception as exc:
        log.debug("R55.11: queue_depth scan failed: %s", exc)
    return out


async def trigger_drain_now() -> int:
    """On-demand drain. Returns N markers consumed. Used by tests + an
    optional admin endpoint."""
    return await _drain_queue_once()


def backfill_orphans(limit: int | None = None) -> dict:
    """R57.7 — one-time backfill: move every orphan marker back to the
    live queue so the R42.6 consumer re-attempts resolution with R57.10's
    improved fuzzy matcher (REQ→TC translation + case-insensitive +
    substring last-resort).

    The simplest strategy is to MOVE the orphan file back to QUEUE_DIR/
    and let the consumer's existing resolution logic handle it on its
    next cycle (or immediately via `trigger_drain_now`). After R57.10
    ships, ~80 of the 98 orphans will resolve cleanly; the rest will
    re-orphan with a clear log line for operator inspection.

    Args:
        limit: optional cap on how many orphans to move in one call.
               None = move all.

    Returns:
        {"requeued": int, "skipped": int, "still_orphan_before": int}
    """
    if not ORPHAN_DIR.is_dir():
        return {"requeued": 0, "skipped": 0, "still_orphan_before": 0}
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    orphans = sorted(p for p in ORPHAN_DIR.glob("*.json") if p.is_file())
    total = len(orphans)
    if limit is not None and limit > 0:
        orphans = orphans[:limit]
    requeued = 0
    skipped = 0
    for orphan in orphans:
        target = QUEUE_DIR / orphan.name
        if target.exists():
            # Live queue already has a marker for this test_id (a fresh
            # one was queued recently). Don't overwrite the live one.
            skipped += 1
            continue
        try:
            shutil.move(str(orphan), str(target))
            requeued += 1
        except Exception as exc:
            log.warning("R57.7: backfill move failed for %s: %s", orphan, exc)
            skipped += 1
    log.info(
        "R57.7: backfill moved %d orphan(s) back to live queue; %d skipped; "
        "%d remained in orphans/ (before this call)",
        requeued, skipped, total,
    )
    return {
        "requeued": requeued,
        "skipped": skipped,
        "still_orphan_before": total,
    }
