"""F7-2 (M2 partial) — pure-utility helpers extracted from tests.py.

These functions have no FastAPI route decorators; they're imported by
tests.py + cross-router callers (execution.py imports `_get_project_req_ids`).
Moving them out shrinks tests.py and gives those imports a stable target.

All functions remain importable from `tests` via re-export so existing call
sites don't need to change.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from .tests_state import GENERATED_TESTS, _GENERATE_ALL_JOBS

log = logging.getLogger("arta.tests")

_MAX_TESTS_IN_MEMORY = int(os.environ.get("ARTA_MAX_TESTS_IN_MEMORY", "2000"))
_JOBS_FILE = Path(".arta/generate_all_jobs.json")


def _compute_prompt_version_hash() -> str:
    """K1: SHA-256 prefix of the concatenated prompt-template files.

    Changes whenever any prompt template is edited — lets debuggers correlate
    test regressions with prompt-template revisions.
    """
    try:
        repo = Path(__file__).resolve().parents[3]
        sources = [
            repo / "src" / "prompts" / "tea_prompts.py",
            repo / "src" / "agents" / "strategy_architect.py",
            repo / "src" / "agents" / "atdd_designer.py",
            repo / "src" / "agents" / "automation_engineer.py",
            repo / "src" / "agents" / "analytics_test_agent.py",
        ]
        h = hashlib.sha256()
        for s in sources:
            if s.exists():
                h.update(s.read_bytes())
        return h.hexdigest()[:16]
    except Exception as exc:
        log.warning("_compute_prompt_version_hash failed (%s: %s); returning empty",
                    type(exc).__name__, exc)
        return ""


def _looks_like_pytest(code: str) -> bool:
    """Gap-1.6: Quick check that `code` is pytest-shaped, not an LLM refusal / error."""
    if not code or len(code.strip()) < 50:
        return False
    lowered = code.lower()
    has_pytest = any(sig in lowered for sig in (
        "import pytest", "from pytest", "def test_", "@pytest.", "assert "
    ))
    refusal_markers = ("i cannot", "i can't", "i'm unable", "cannot generate", "rate limit", "unauthorized")
    is_refusal = any(m in lowered[:500] for m in refusal_markers)
    return has_pytest and not is_refusal


def _stamp_traceability(entries: list[dict], trace_id: str, model_version: str, prompt_version: str) -> None:
    """K1: Apply trace_id, model_version, prompt_version to every test entry.

    Idempotent — entries already stamped with the same trace_id are skipped.
    Called once per generation request after all entries are created.
    """
    for e in entries:
        if not isinstance(e, dict):
            continue
        e.setdefault("trace_id", trace_id)
        e.setdefault("model_version", model_version)
        e.setdefault("prompt_version", prompt_version)
        # I4 lite: red_phase_status starts as PENDING_VERIFICATION. Execution
        # router updates this based on the FIRST run outcome.
        e.setdefault("red_phase_status", "PENDING_VERIFICATION")


def _enforce_generated_tests_cap() -> None:
    """Evict oldest entries (by generated_at) when GENERATED_TESTS exceeds the cap."""
    if len(GENERATED_TESTS) <= _MAX_TESTS_IN_MEMORY:
        return
    overflow = len(GENERATED_TESTS) - _MAX_TESTS_IN_MEMORY
    GENERATED_TESTS.sort(key=lambda t: t.get("generated_at", "") or "")
    del GENERATED_TESTS[:overflow]
    log.info("GENERATED_TESTS cap (%d) exceeded — evicted %d oldest entries (now %d)",
             _MAX_TESTS_IN_MEMORY, overflow, len(GENERATED_TESTS))


def _save_tests_json(seed_test_ids: set[str]) -> None:
    """Persist all generated tests (beyond seed data) to a local JSON file as backup."""
    generated = [t for t in GENERATED_TESTS if t.get("id") not in seed_test_ids]
    if generated:
        p = Path(".arta/generated_tests.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(generated, indent=2, default=str))


def _load_tests_json() -> None:
    """Load previously generated tests from the JSON backup into GENERATED_TESTS."""
    p = Path(".arta/generated_tests.json")
    if not p.exists():
        return
    try:
        saved = json.loads(p.read_text())
        existing_ids = {t["id"] for t in GENERATED_TESTS}
        for t in saved:
            if t.get("id") not in existing_ids:
                GENERATED_TESTS.append(t)
    except Exception as exc:
        log.warning(
            "Failed to load %s (%s: %s) — starting with in-memory seed only",
            p, type(exc).__name__, exc,
        )


def _save_job_json(job: dict) -> None:
    """Persist a generate-all job result to disk (survives server restarts)."""
    try:
        _JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        jobs = _load_all_jobs_json()
        jobs = [j for j in jobs if j.get("job_id") != job["job_id"]]
        jobs.append(job)
        jobs = jobs[-20:]  # Keep last 20 jobs
        _JOBS_FILE.write_text(json.dumps(jobs, indent=2, default=str))
    except Exception as exc:
        log.warning(
            "Failed to persist job %s to %s (%s: %s) — in-memory state preserved",
            job.get("job_id", "?"), _JOBS_FILE, type(exc).__name__, exc,
        )


def _load_all_jobs_json() -> list[dict]:
    """Load all persisted jobs from disk."""
    if _JOBS_FILE.exists():
        try:
            return json.loads(_JOBS_FILE.read_text())
        except Exception:
            return []
    return []


_EVIDENCE_TARGETS_BY_TOOL: dict[str, list[str]] = {
    "playwright": ["screenshot", "video", "trace", "console_log"],
    "selenium":   ["screenshot", "page_source", "browser_log"],
    "cypress":    ["screenshot", "video"],
    "appium":     ["screenshot", "device_log"],
    "newman":     ["har", "request_log", "response_body"],
    "k6":         ["summary_json", "metrics_csv", "checks_log"],
    "zap":        ["zap_report_html", "zap_report_json", "alerts_json"],
    "pytest":     ["pytest_report_json", "stdout_log"],
}


def _evidence_targets_for_tool(tool: str) -> list[str]:
    """F8-1: Return the artifact types execution should collect for a given tool.

    The orchestrator's `_run_evidence_collection` reads execution_results
    looking for `screenshot` / `har_file` keys. Stamping the expected target
    list at generation time makes evidence collection a contract, not an
    implicit convention — runners can be audited against it.
    """
    return list(_EVIDENCE_TARGETS_BY_TOOL.get(tool, ["stdout_log"]))


def _compute_nfr_precheck(generated_tests: list[dict]) -> dict:
    """F8-1 (Layer 8 pre-check): warn-only NFR coverage signal at generation time.

    Mirrors `orchestrator._run_nfr_validation` but reports COVERAGE (which
    NFR dimensions have at least one test) rather than RESULTS (which need
    execution data). Output is informational — the gate decision still
    needs real execution results.
    """
    perf_tests = [t for t in generated_tests if t.get("test_type") == "Performance" or t.get("tool") == "k6"]
    sec_tests  = [t for t in generated_tests if t.get("test_type") == "Security"    or t.get("tool") == "zap"]
    a11y_tests = [
        t for t in generated_tests
        if t.get("test_type") == "Accessibility"
        or "_a11y." in (t.get("script_path") or "")
        or "a11y" in (t.get("title") or "").lower()
    ]
    warnings: list[str] = []
    if not perf_tests:
        warnings.append("no Performance test generated — NFR perf dimension will be UNCOVERED at gate time")
    if not sec_tests:
        warnings.append("no Security test generated — NFR security dimension will be UNCOVERED at gate time")
    if not a11y_tests:
        warnings.append("no Accessibility test generated — NFR a11y dimension will be UNCOVERED at gate time")
    return {
        "performance_covered": bool(perf_tests),
        "security_covered":    bool(sec_tests),
        "accessibility_covered": bool(a11y_tests),
        "performance_count":   len(perf_tests),
        "security_count":      len(sec_tests),
        "accessibility_count": len(a11y_tests),
        "warnings":            warnings,
    }


def _slugify_filename(s: str, max_len: int = 60) -> str:
    """F13-2: Make a string safe to use inside a Python module filename.

    The adversarial-test generation path interpolated raw LLM-emitted category
    strings (e.g. `"Large number edge cases (billions with precision)"`) into
    filenames, producing modules with spaces and parens that pytest could
    collect via path glob but Python could NOT import — silently dropping
    23 of 40 generated analytics tests.

    Rules:
      - lowercase
      - non-alphanumerics → underscore
      - collapse runs of underscores
      - strip leading/trailing underscores
      - truncate to `max_len` chars
      - empty input → 'unknown'
    """
    import re
    out = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    out = re.sub(r"_+", "_", out)
    return (out[:max_len].rstrip("_") or "unknown")


def _set_job_stage(req_id: str, stage: str) -> None:
    """F11-3: Update the per-stage progress on the active generate-all job
    that's currently processing `req_id`. The SSE stream picks this up on
    its next 2-second tick, so the UI shows live stage progress instead of
    sitting at the last completed-requirement count for ~100s.

    Forgiving — if there's no matching active job (single-req endpoint call,
    or job already completed) this is a no-op.

    `stage` should be one of: risk_scoring | atdd | automation | writing.
    """
    from datetime import datetime, timezone
    for job in _GENERATE_ALL_JOBS.values():
        if (job.get("status") in ("queued", "running")
                and job.get("current_requirement") == req_id):
            job["current_stage"] = stage
            job["current_stage_started_at"] = datetime.now(timezone.utc).isoformat()
            return


def _record_req_completion(job_id: str, elapsed_seconds: float) -> None:
    """F11-6: Track per-requirement completion time so the SSE payload can
    expose a rolling average + ETA estimate. Called once per requirement
    after the pipeline completes.
    """
    job = _GENERATE_ALL_JOBS.get(job_id)
    if not job:
        return
    completed = int(job.get("completed", 0))
    if completed <= 0:
        return
    prev_total = float(job.get("_per_req_total_seconds", 0.0))
    new_total = prev_total + float(elapsed_seconds)
    job["_per_req_total_seconds"] = new_total
    job["per_req_avg_seconds"] = round(new_total / completed, 1)
    remaining = max(0, int(job.get("total_requirements", 0)) - completed)
    job["eta_seconds"] = int(remaining * (new_total / completed))


def _normalize_generate_result(result) -> dict:
    """F10-2: Unwrap a fastapi Response → dict for the generate-all bg loop.

    `generate_tests()` returns either a plain dict OR (on the F8-A4
    automation-failure path) a fastapi.JSONResponse with status 202
    wrapping the failure body. The background loop reads fields uniformly,
    so without this normalisation `result.get("test_count", 0)` raises
    `'JSONResponse' object has no attribute 'get'` for every failed req,
    masking the real cause and reporting `0 tests / 21 errors` even when
    the underlying problem is a single bad config.
    """
    if isinstance(result, dict):
        return result
    if hasattr(result, "body") and hasattr(result, "status_code"):
        body = getattr(result, "body", None)
        if not body:
            return {}
        try:
            decoded = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
        except Exception as exc:
            log.warning("Could not decode generate_tests response body: %s", exc)
            return {"status": "failed", "message": "Could not decode generation response"}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _get_project_req_ids(project_id: str) -> set[str]:
    """Return the set of requirement IDs belonging to a project.

    Uses PROJECT_REQUIREMENTS (which contains all demo + custom projects)
    so we never need to hardcode project IDs or prefix checks.
    """
    from .requirements import PROJECT_REQUIREMENTS
    return {
        r.get("req_id") or r.get("id")
        for r in PROJECT_REQUIREMENTS.get(project_id, [])
        if r.get("req_id") or r.get("id")
    }
