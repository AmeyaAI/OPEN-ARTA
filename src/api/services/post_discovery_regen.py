"""R29.0 — Post-discovery test-gen regen pass (Stage 2.6).

Why this exists
---------------
Run-ad4913 (8% pass rate) traced 137 of 137 Playwright failures and 3 of
3 a11y compile-fails to a chicken-and-egg between test generation and
discovery harvest:

    Stage 2 (test gen)
      ↓ Playwright spec generated WITHOUT a populated DOM catalog
      ↓ LLM hallucinates `getByTestId('chart-area-fake-id')` etc.
    Stage 2.5 (discovery harvest)
      ↓ probe walks the SPA, captures REAL testids → dom_catalog.json
    Stage 3 (test execution)
      ↓ dispatches the SCRIPTS GENERATED IN STAGE 2 (still hallucinated)
      ↓ getByTestId timeouts → 137× FAIL

Newman/k6/ZAP escape this because they read env vars at dispatch time
via `--env-var` injection, but Playwright/axe specs bake testids in at
generation time — the only way for them to use real testids is to be
regenerated AFTER the catalog is populated.

R29.0 inserts a Stage 2.6 hook AFTER `_ensure_discovery_fresh` returns:
finds Playwright/axe spec files whose mtime is older than
`dom_catalog.json` (i.e. were generated pre-discovery), and re-invokes
`AutomationEngineerAgent.generate_single` with the same Gherkin + risk
profile but the now-populated catalog.

Cascade effect — once Playwright runs successfully:
  * SPA's authenticated flows complete → MORE API traffic in HAR
  * Subsequent harvest fills MORE env vars (compound effect)
  * Newman/k6 dispatch with the richer var set → higher pass rate too

Safety / no-op behavior
-----------------------
* Catalog has fewer than 10 testids → skip (insufficient signal; LLM
  would still hallucinate). No regen attempted; cascade-skip pattern
  may persist but no harm done.
* Project doesn't exist or no specs match → no-op.
* LLM call fails for a single spec → log + continue (other specs still
  regen).
* Original spec content always backed up as ``<spec>.pre-R29.bak`` so
  operators can revert.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]

# Same convention as `tools/regen_existing_tests.py` so operators can
# tell which sweep wrote a given backup. Lives next to the spec file.
_BACKUP_SUFFIX = ".pre-R29.bak"

# Catalog health gate. Empirically: fewer than 10 testids means the SPA
# either didn't load (auth expired) or the probe didn't navigate deep
# enough — regenerating against a thin catalog just shifts the
# hallucination from the LLM to the validator and burns LLM budget. The
# operator gets a clear log line and a hint to refresh auth + re-probe.
_MIN_TESTIDS_FOR_REGEN = 10

# Tools whose specs bake testids/selectors at generation time. Newman,
# k6, ZAP read env vars at dispatch — they don't need post-discovery
# regen. Pytest analytics is data-driven; same exemption.
_REGEN_TOOLS = ("playwright", "axe")


def _catalog_health(project_id: str) -> tuple[int, Path | None]:
    """Returns (testid_count, catalog_path). Catalog path is None when
    the file doesn't exist — caller should treat that as 'discovery
    didn't write anything; skip regen entirely.'"""
    if not project_id:
        return 0, None
    catalog_path = ROOT / ".arta" / "discovery" / project_id / "dom_catalog.json"
    if not catalog_path.is_file():
        return 0, None
    try:
        data = json.loads(catalog_path.read_text())
        return int(data.get("testid_count") or 0), catalog_path
    except Exception as exc:
        log.debug("R29.0: dom_catalog read failed for %s: %s", project_id, exc)
        return 0, catalog_path


def _find_pre_discovery_specs(catalog_path: Path) -> list[tuple[Path, str]]:
    """Returns list of (spec_path, tool) for Playwright + axe specs whose
    mtime is older than the dom_catalog.json mtime — i.e. were generated
    BEFORE the catalog was populated and are now using hallucinated
    testids.

    Spec naming convention: `*_a11y.spec.ts` is axe; everything else
    `*.spec.ts` in `src/automation/playwright/` is playwright.
    """
    pw_dir = ROOT / "src" / "automation" / "playwright"
    if not pw_dir.is_dir():
        return []
    catalog_mtime = catalog_path.stat().st_mtime
    out: list[tuple[Path, str]] = []
    for spec in sorted(pw_dir.glob("*.spec.ts")):
        # `.broken-*` files are quarantined; never regen those.
        if ".broken" in spec.name or spec.name.endswith(_BACKUP_SUFFIX):
            continue
        try:
            spec_mtime = spec.stat().st_mtime
        except OSError:
            continue
        if spec_mtime >= catalog_mtime:
            # Spec is newer than the catalog — already up to date.
            continue
        tool = "axe" if spec.name.endswith("_a11y.spec.ts") else "playwright"
        out.append((spec, tool))
    return out


def _load_generated_tests() -> list[dict]:
    p = ROOT / ".arta" / "generated_tests.json"
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        log.debug("R29.0: generated_tests.json load failed: %s", exc)
        return []


def _find_record_for_spec(
    records: list[dict], spec_path: Path, tool: str
) -> dict | None:
    """Map a spec file path back to its source record in
    generated_tests.json. Match by `script_path` first (exact);
    fall back to (requirement_id, tool) match derived from filename.

    Filename → req_id heuristic: `req_xy_001.spec.ts` → REQ-XY-001;
    `req_xy_007_a11y.spec.ts` → REQ-XY-007.
    """
    rel = spec_path.relative_to(ROOT).as_posix() if spec_path.is_absolute() else spec_path.as_posix()
    by_path = [r for r in records if r.get("script_path") == rel]
    if by_path:
        return by_path[0]

    stem = spec_path.stem.removesuffix(".spec")  # req_am_007_a11y
    parts = stem.split("_")
    if len(parts) >= 3 and parts[0] == "req":
        # parts[-1] may be 'a11y' for axe specs; strip it.
        if parts[-1] in ("a11y",) and len(parts) >= 4:
            req_id = "REQ-" + parts[1].upper() + "-" + parts[2]
        else:
            req_id = "REQ-" + parts[1].upper() + "-" + parts[2]
        matches = [
            r for r in records
            if r.get("requirement_id") == req_id
            and (r.get("tool") == tool or r.get("automation_tool") == tool)
        ]
        if matches:
            return matches[0]
    return None


def _reconstruct_risk(record: dict, project_id: str) -> dict:
    """Mirror of `tools/regen_existing_tests.py::reconstruct_risk` but
    inlined here so this module has no test-tools dependency. Backfills
    project_id when the record predates that field."""
    return {
        "id": record.get("id"),
        "test_id": record.get("id"),
        "requirement_id": record.get("requirement_id"),
        "ac_id": record.get("ac_id"),
        "title": record.get("title"),
        "priority": record.get("priority", "P2"),
        "risk_score": record.get("risk_score", 4.0),
        "test_type": record.get("test_type", "UI"),
        "test_types": [record.get("test_type", "UI")],
        "project_id": record.get("project_id") or project_id,
        "expected_outputs": record.get("expected_outputs") or {},
    }


async def _regen_spec_with_catalog(
    spec_path: Path,
    tool: str,
    record: dict,
    project_id: str,
    llm_client: Any,
) -> tuple[bool, str]:
    """Re-invoke AutomationEngineer for one stale spec. Returns
    (ok, message). Backs up the original to `<spec>.pre-R29.bak`
    so operators can revert.

    Tier 1 autofixes (K2 try/catch, L13 toContainText, R24b localhost)
    + R19c DOM-catalog injection + R19d testid validator all fire as
    part of the existing `generate_single` pipeline — this helper just
    drives them with the now-populated catalog.
    """
    gherkin = record.get("gherkin") or record.get("gherkin_scenario") or ""
    if not gherkin.strip():
        return False, "no gherkin in generated_tests.json record — cannot regen"

    from ...agents.automation_engineer import AutomationEngineerAgent

    agent = AutomationEngineerAgent(llm_client)
    risk = _reconstruct_risk(record, project_id)
    try:
        script = await agent.generate_single(gherkin, tool, risk)
    except Exception as exc:
        return False, f"regen failed: {type(exc).__name__}: {str(exc)[:200]}"

    fresh = getattr(script, "content", None) or ""
    if not fresh.strip():
        return False, "regen returned empty content"

    backup = spec_path.with_suffix(spec_path.suffix + _BACKUP_SUFFIX)
    if not backup.exists():
        try:
            backup.write_bytes(spec_path.read_bytes())
        except OSError as exc:
            return False, f"backup write failed ({backup.name}): {exc}"
    try:
        spec_path.write_text(fresh)
    except OSError as exc:
        return False, f"spec write failed: {exc}"
    return True, f"regenerated ({len(fresh)} chars; backup: {backup.name})"


async def post_discovery_regen_pass(
    project_id: str,
    run_id: str,
    llm_client: Any,
) -> dict:
    """Stage 2.6 entry point — call AFTER `_ensure_discovery_fresh`
    completes. Returns a stats dict the caller can stamp on the run
    record:

        {
          "fired": bool,                        # True iff any regen attempted
          "reason": str | None,                 # why it didn't fire
          "testid_count": int,                  # catalog health
          "stale_spec_count": int,
          "regenerated": int,
          "failed": int,
          "details": [{spec, ok, message}, ...]
        }

    Never raises — all failures captured in the result dict.
    """
    out: dict[str, Any] = {
        "fired": False,
        "reason": None,
        "testid_count": 0,
        "stale_spec_count": 0,
        "regenerated": 0,
        "failed": 0,
        "details": [],
    }
    if not project_id:
        out["reason"] = "no project_id"
        return out
    if llm_client is None:
        out["reason"] = "no llm_client — cannot drive regen"
        return out

    # R30.7-C — clear the per-project spec-drift accumulator at the start
    # of each post-discovery cycle. Any prior-run drift signals are stale
    # now that discovery has refreshed; without this, the gate's
    # `_check_spec_drift` carries stale WARN rows across runs in the same
    # gunicorn worker.
    try:
        from ...agents.automation_engineer import clear_spec_drift_for_project
        cleared = clear_spec_drift_for_project(project_id)
        if cleared:
            log.info(
                "R30.7-C: cleared %d stale spec-drift entries for project %s "
                "before regen pass", cleared, project_id,
            )
    except Exception as exc:
        log.debug("R30.7-C: spec-drift clear skipped: %s", exc)

    testid_count, catalog_path = _catalog_health(project_id)
    out["testid_count"] = testid_count
    if catalog_path is None:
        out["reason"] = "no dom_catalog.json — discovery didn't write one"
        return out
    if testid_count < _MIN_TESTIDS_FOR_REGEN:
        out["reason"] = (
            f"catalog has {testid_count} testids (need >={_MIN_TESTIDS_FOR_REGEN}); "
            f"regen would still hallucinate. Operator action: refresh auth, "
            f"re-trigger discovery."
        )
        log.info("R29.0: %s", out["reason"])
        return out

    stale_specs = _find_pre_discovery_specs(catalog_path)
    out["stale_spec_count"] = len(stale_specs)
    if not stale_specs:
        out["reason"] = "all Playwright/axe specs are newer than dom_catalog.json"
        return out

    records = _load_generated_tests()
    if not records:
        out["reason"] = "generated_tests.json missing — cannot reconstruct gherkin"
        return out

    log.info(
        "R29.0: regenerating %d Playwright/axe specs against fresh DOM catalog "
        "(%d testids) for run %s",
        len(stale_specs), testid_count, run_id,
    )
    out["fired"] = True

    for spec_path, tool in stale_specs:
        record = _find_record_for_spec(records, spec_path, tool)
        if record is None:
            msg = "no matching record in generated_tests.json"
            out["details"].append(
                {"spec": spec_path.name, "tool": tool, "ok": False, "message": msg}
            )
            out["failed"] += 1
            log.warning("R29.0: %s — %s", spec_path.name, msg)
            continue
        ok, msg = await _regen_spec_with_catalog(
            spec_path, tool, record, project_id, llm_client
        )
        out["details"].append(
            {"spec": spec_path.name, "tool": tool, "ok": ok, "message": msg}
        )
        if ok:
            out["regenerated"] += 1
            log.info("R29.0: %s — %s", spec_path.name, msg)
        else:
            out["failed"] += 1
            log.warning("R29.0: %s — %s", spec_path.name, msg)

    log.info(
        "R29.0: pass complete — regenerated=%d failed=%d (run %s)",
        out["regenerated"], out["failed"], run_id,
    )
    return out
