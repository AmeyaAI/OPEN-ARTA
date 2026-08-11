"""
ARTA AI Assistant Router — Streaming chat interface for developer commands.
Handles /generate-tests, /analyze-risk, /check-coverage, /run-atdd, etc.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import AsyncGenerator

import os

import anthropic
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

log = logging.getLogger("arta.api.assistant")


from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
# F4-1: API-key check is centralised in src/api/dependencies.py.
from ..dependencies import require_api_key as _require_api_key  # noqa: E402


SYSTEM_PROMPT = """\
You are ARTA — an AI Test Architect operating under the BMAD TEA \
(Test Engineering Architecture) methodology.

You act simultaneously as:
- Test Architect: design test strategy, risk analysis, coverage planning
- ATDD Designer: generate Gherkin BDD scenarios from requirements
- Automation Engineer: produce Playwright/Newman/k6 test code
- Quality Gate Authority: enforce evidence-based release decisions

Available commands:
  /generate-tests [requirement-id or description]
  /generate-edge-cases [feature or test description]
  /analyze-risk [requirement or module]
  /check-coverage [requirement-id or module]
  /run-atdd [feature name]
  /explain-failure [test-id]
  /check-gate [environment]
  /heal-tests [test file description]

TEA Principles you enforce:
1. Tests ALWAYS written before implementation (ATDD red-first)
2. Risk-based prioritization: P0 requires 100% coverage
3. All tests traceable to acceptance criteria
4. Quality gates are evidence-based — never subjective
5. Non-functional (perf/security) validated every release

When generating Gherkin:
- Use concrete data values, not placeholders
- Always include edge cases + security scenarios for P0/P1
- Link each scenario to its requirement and AC

When generating automation code:
- Playwright for UI (TypeScript)
- Newman/Postman for API
- k6 for performance
- Include assertions matching acceptance criteria thresholds

Respond conversationally but concisely. For code, use fenced blocks.
"""


class ChatMessage(BaseModel):
    role: str   # "user" | "assistant"
    content: str


class AssistantRequest(BaseModel):
    messages: list[ChatMessage]
    context: dict = {}   # Active requirement, risk profile, etc.


@router.post("/chat", dependencies=[Depends(_require_api_key)])
async def chat(req: AssistantRequest, request: Request):
    """Streaming AI assistant endpoint."""
    # R320 — resolve the client provider-robustly (mirror execute_command). For
    # claude_code/ollama providers `app.state.anthropic` may be unset; fall back
    # to `llm_client` and 503 cleanly instead of a 500 AttributeError.
    client = getattr(request.app.state, "anthropic", None) or \
             getattr(request.app.state, "llm_client", None)
    if client is None:
        raise HTTPException(503, "LLM client not initialised — check provider configuration")

    # Build context injection
    context_block = ""
    if req.context:
        context_block = f"\n\nCURRENT CONTEXT:\n{json.dumps(req.context, indent=2)}\n"

    messages = [
        {"role": m.role, "content": m.content}
        for m in req.messages
    ]

    return StreamingResponse(
        _stream_response(client, messages, context_block),
        media_type="text/event-stream",
    )


async def _stream_response(
    client: anthropic.AsyncAnthropic,
    messages: list[dict],
    context_block: str,
) -> AsyncGenerator[str, None]:
    import os as _os
    provider = (_os.environ.get("ARTA_LLM_PROVIDER", "") or "").lower()

    # G3.1 (H5): Prompt caching for Anthropic provider.
    # Static SYSTEM_PROMPT (ARTA identity + BMAD methodology) is reused across every
    # message → benefits from 5-min ephemeral cache (~50% input-token reduction).
    # Dynamic context_block changes per request → NOT cached.
    # For Ollama / Claude-CLI, plain-string system is preserved (they don't support blocks).
    if provider == "anthropic":
        system_blocks = [{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]
        if context_block:
            system_blocks.append({"type": "text", "text": context_block})
        system: object = system_blocks
    else:
        system = SYSTEM_PROMPT + context_block

    stream_kwargs = {
        "model": _os.environ.get("ARTA_LLM_MODEL", "claude-sonnet-4-6"),
        "max_tokens": 2048,
        "system": system,
        "messages": messages,
    }
    # Check for OAuth headers (set during app startup)
    _api_key = _os.environ.get("ANTHROPIC_API_KEY", "") or _os.environ.get("CLAUDE_CODE_API_KEY", "")
    if "sk-ant-oat" in _api_key:
        stream_kwargs["extra_headers"] = {
            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
        }

    async with client.messages.stream(**stream_kwargs) as stream:
        async for text in stream.text_stream:
            yield f"data: {json.dumps({'text': text})}\n\n"

    yield "data: [DONE]\n\n"


@router.post("/command", dependencies=[Depends(_require_api_key)])
async def execute_command(body: dict, request: Request):
    """
    Execute a slash command directly.
    Routes commands to real agents via the command dispatcher.
    Returns structured results for commands like /analyze-risk, /check-coverage.
    """
    command = body.get("command", "").strip()
    args = body.get("args", "")
    project_id = body.get("project_id")

    from ..services.command_dispatcher import dispatch

    # Surfaced by router test suite: previous direct attribute access
    # (`request.app.state.anthropic`) raised AttributeError when the LLM
    # client wasn't initialised (fresh container, ARTA_LLM_PROVIDER unset
    # in tests, etc.). Use getattr + 503 fallback so the endpoint reports a
    # clear service-unavailable instead of a 500.
    client = getattr(request.app.state, "anthropic", None) or \
             getattr(request.app.state, "llm_client", None)
    if client is None:
        from fastapi import HTTPException
        raise HTTPException(503, "LLM client not initialised — check provider configuration")
    return await dispatch(command, args, client, project_id=project_id)


# ── R320 — Test-Case Refinement Copilot ─────────────────────────────────────────
# A tester corrects an AI-generated test. ARTA CLASSIFIES the correction against
# its own accessible SUT knowledge (the keystone — fix at source, not a downstream
# hand-patch), persists a durable grounding fact when the knowledge is genuinely
# human, runs a surgical regen with the correction as the prompt hint, and records
# a traceable + revertible correction. draft mode classifies without writing;
# confirm mode writes + regens.

class RefineRequest(BaseModel):
    project_id: str | None = None
    test_id: str | None = None
    requirement_id: str | None = None
    ac_id: str | None = None
    tool: str | None = None                 # playwright|newman|k6|... (regen scope)
    kind: str = "endpoint"                  # endpoint|field_value|shape|intent
    method: str = "GET"
    path: str | None = None                 # endpoint the correction grounds onto
    field: str | None = None                # for field_value
    from_value: str | None = None
    to_value: str | None = None             # corrected endpoint path / field value / shape
    correction_text: str = ""
    corrected_by: str | None = None
    confirm: bool = False


@router.post("/refine", dependencies=[Depends(_require_api_key)])
async def refine_test(body: RefineRequest, request: Request):
    """R320 — draft (classify only) or confirm (write grounding + surgical regen +
    record) a tester correction of a generated test."""
    from ...agents.api_discovery import (
        classify_correction_provenance, write_human_correction)

    kind = (body.kind or "endpoint").strip()
    # The endpoint path the correction grounds onto: for an endpoint correction it
    # is the corrected path; for a field/shape correction it is the endpoint the
    # field/shape belongs to.
    ground_path = (body.to_value or body.path) if kind == "endpoint" else body.path

    verdict = {"verdict": "human_knowledge", "matched_source": None}
    if body.project_id and ground_path:
        # Kind-aware classify: for field/shape, arta_knew requires the FIELD/VALUE/
        # SHAPE to have been captured — not merely that the endpoint exists.
        verdict = classify_correction_provenance(
            body.project_id, body.method, ground_path,
            kind=kind, field=body.field, value=body.to_value)

    proposed = {"kind": kind, "method": body.method, "path": ground_path,
                "field": body.field, "to_value": body.to_value}

    if not body.confirm:
        # DRAFT — classify + propose the fact; NO writes (trust gate).
        if verdict["verdict"] == "arta_knew":
            explain = (f"ARTA already had this route (source: {verdict.get('matched_source')}) "
                       "— this is an ARTA gen/grounding DEFECT. Confirming will file it for the "
                       "ARTA backlog AND apply the fix to this test.")
        else:
            explain = ("ARTA could not know this from any accessible source (OpenAPI / discovered "
                       "/ SUT source). Confirming saves it as durable human grounding that improves "
                       "every future generation for this SUT.")
        return {"mode": "draft", "verdict": verdict["verdict"],
                "matched_source": verdict.get("matched_source"),
                "proposed_fact": proposed, "explain": explain}

    # CONFIRM — persist the grounding fact (when the kind produces one), surface an
    # ARTA defect when arta_knew, run a surgical regen, and record the correction.
    grounding_ref = None
    if (body.project_id and ground_path and kind in ("endpoint", "field_value", "shape")
            and os.environ.get("ARTA_R320_HUMAN_CORRECTION_DISABLE") != "1"):
        rvs = ({body.field: [body.to_value]}
               if kind == "field_value" and body.field and body.to_value else None)
        shape = body.to_value if kind == "shape" else None
        w = write_human_correction(
            body.project_id, method=body.method, path=ground_path,
            response_value_samples=rvs, response_body_shape=shape,
            corrected_by=body.corrected_by, rationale=body.correction_text)
        if w.get("written"):
            grounding_ref = w.get("key")

    if verdict["verdict"] == "arta_knew":
        log.warning(
            "R320 ARTA-KNEW correction: test=%s %s %s was already grounded (source=%s) but gen "
            "used a wrong value — upstream gen/grounding defect. Fix applied + recorded.",
            body.test_id, body.method, ground_path, verdict.get("matched_source"))

    # Regen — the human correction is prepended to the gen prompt (canonical
    # `feedback` path) + the new grounding fact is now in the store. Scoped to the
    # AC. NOTE: we deliberately do NOT pass tools=[tool]: force+ac_id CLEARS every
    # tool for the AC (tests.py), so a tool-filtered regen would delete the AC's
    # OTHER-tool tests without regenerating them (data loss). Regenerating the AC's
    # full tool set keeps clear-scope == regen-scope; the correction (grounding +
    # feedback) benefits every tool anyway.
    regen_result = None
    if body.requirement_id:
        try:
            from .tests import generate_tests, GenerateRequest
            regen_result = await generate_tests(GenerateRequest(
                requirement_id=body.requirement_id,
                ac_id=body.ac_id, feedback=body.correction_text or None, force=True,
            ), request)
        except Exception as exc:
            log.warning("R320: regen after correction failed: %s", exc)
            regen_result = {"status": "error", "message": str(exc)}

    correction_id = f"corr-{uuid.uuid4().hex[:8]}"
    defect_id = None
    try:
        from ..db_adapter import try_db
        async with try_db() as db:
            if db:
                from ...db.repository import TestCorrectionRepo
                await TestCorrectionRepo(db).create({
                    "correction_id": correction_id, "project_id": body.project_id,
                    "test_id": body.test_id, "requirement_id": body.requirement_id,
                    "ac_id": body.ac_id, "tool": body.tool, "kind": kind,
                    "field": body.field,
                    "correction_text": body.correction_text or "",
                    "from_value": body.from_value, "to_value": body.to_value,
                    "verdict": verdict["verdict"], "grounding_fact_ref": grounding_ref,
                    "verify_status": "not_run", "corrected_by": body.corrected_by,
                })
                # arta_knew → ARTA had the answer but gen ignored it. File a REAL
                # test_gen_bug on the defects backlog (fix ARTA at source, not just
                # a correction record). Deterministic id per endpoint → re-corrections
                # of the same route dedup instead of spamming the queue.
                if verdict["verdict"] == "arta_knew":
                    defect_id = await _file_arta_gen_defect(
                        db, body, ground_path, verdict.get("matched_source"))
    except Exception as exc:
        log.debug("R320: correction record skipped: %s", exc)

    # Traceability spine (best-effort, non-blocking): link the correction to its
    # Requirement→AC→TestCase so provenance is traceable in the report Trace panel.
    try:
        from ...graph.writer import upsert_correction, get_driver
        await upsert_correction(get_driver(), {
            "correction_id": correction_id, "kind": kind, "verdict": verdict["verdict"],
            "corrected_by": body.corrected_by, "grounding_fact_ref": grounding_ref,
            "correction_text": body.correction_text, "test_id": body.test_id,
            "requirement_id": body.requirement_id, "ac_id": body.ac_id,
        })
    except Exception as exc:
        log.debug("R320: correction graph linkage skipped: %s", exc)

    return {"mode": "confirmed", "correction_id": correction_id,
            "verdict": verdict["verdict"], "matched_source": verdict.get("matched_source"),
            "grounding_written": grounding_ref is not None,
            "grounding_fact_ref": grounding_ref, "arta_gen_defect_id": defect_id,
            "regen": regen_result}


async def _file_arta_gen_defect(db, body: "RefineRequest", ground_path: str | None,
                                matched_source: str | None) -> str | None:
    """R320 — file (idempotently) a test_gen_bug on the defects backlog for an
    `arta_knew` correction: ARTA HAD the answer (matched_source) but generation
    used a wrong value. Surfaces the gen/grounding defect at source. Best-effort."""
    import hashlib
    from ...db.repository import DefectRepo
    from ...db.models import DefectSeverity, RiskPriority, DefectStatusEnum
    from ...agents.defect_intel import _r258_skel
    # Dedup by the route SKELETON (id-normalized), not the exact path — a gen
    # defect on `/organizations/{id}` is ONE backlog item, however many concrete
    # ids get corrected.
    key = f"{body.method}:{_r258_skel(ground_path or '', aggressive=True)}"
    did = f"DEF-ARTA-GEN-{hashlib.sha1(key.encode()).hexdigest()[:8].upper()}"
    repo = DefectRepo(db)
    try:
        if await repo.get(did):        # dedup: same route already filed
            return did
    except Exception:
        pass
    await repo.create({
        "defect_id": did,
        "title": f"ARTA gen defect: {body.method} {ground_path} was grounded "
                 f"({matched_source}) but generation used a wrong value",
        "description": (f"A tester corrected {body.kind} on {body.method} {ground_path}. "
                        f"The correct value was already available to ARTA (source="
                        f"{matched_source}), so generation should have used it — an "
                        f"upstream gen/grounding defect, not a SUT issue.\n\n"
                        f"Correction: {body.correction_text or ''}"),
        "severity": DefectSeverity.medium,
        "priority": RiskPriority.P2,
        "status": DefectStatusEnum.open,
        "triage_category": "test_gen_bug",
        "triage_confidence": 0.9,
        "triage_signals": ["human_correction_arta_knew"],
        "root_cause_category": "test_gen_bug",
        "reporter": "ARTA-Refine",
    })
    return did


@router.get("/corrections", dependencies=[Depends(_require_api_key)])
async def list_corrections(project_id: str | None = None, limit: int = 100):
    """R320 — list tester corrections (provenance + verdict + grounding ref).
    Degrades to an empty list when the DB is unavailable (never 500s)."""
    from ..db_adapter import try_db
    try:
        async with try_db() as db:
            if db:
                from ...db.repository import TestCorrectionRepo, _to_dict
                rows = await TestCorrectionRepo(db).list(project_id=project_id, limit=limit)
                return {"corrections": [_to_dict(r) for r in rows], "total": len(rows)}
    except Exception as exc:
        log.debug("R320: list_corrections DB unavailable: %s", exc)
    return {"corrections": [], "total": 0}


@router.get("/tests/{test_id}/latest-result", dependencies=[Depends(_require_api_key)])
async def test_latest_result(test_id: str):
    """R320 — the corrected test's latest execution result (status + error +
    triage) so the RefineWorkspace shows the tester exactly what is failing.
    Degrades to nulls without a DB."""
    from ..db_adapter import try_db
    try:
        async with try_db() as db:
            if db:
                from sqlalchemy import text as _t
                row = (await db.execute(_t(
                    "SELECT er.status::text, er.error_message, er.metadata, tr.run_id, er.executed_at "
                    "FROM execution_results er JOIN test_runs tr ON tr.id = er.run_id "
                    "WHERE er.test_id = :tid ORDER BY er.executed_at DESC NULLS LAST LIMIT 1"),
                    {"tid": test_id})).first()
                if row:
                    md = row[2]
                    if isinstance(md, str):
                        try:
                            md = json.loads(md)
                        except Exception:
                            md = {}
                    if not isinstance(md, dict):
                        md = {}
                    return {"status": row[0], "error_message": row[1],
                            "triage_category": md.get("triage_category"),
                            "run_id": row[3],
                            "executed_at": str(row[4]) if row[4] else None}
    except Exception as exc:
        log.debug("R320: latest-result unavailable: %s", exc)
    return {"status": None, "error_message": None, "triage_category": None, "run_id": None}


@router.get("/corrections/analytics", dependencies=[Depends(_require_api_key)])
async def corrections_analytics(project_id: str | None = None):
    """R320 S2 — surface ARTA's SYSTEMATIC gaps from the correction history. The
    `arta_defect_rate` (share of corrections where ARTA already had the answer and
    gen ignored it) is the signal to fix ARTA's generation/discovery UPSTREAM; the
    top corrected endpoints point at exactly where. Degrades to zeros without a DB."""
    from collections import Counter
    from ..db_adapter import try_db
    try:
        async with try_db() as db:
            if db:
                from ...db.repository import TestCorrectionRepo
                rows = await TestCorrectionRepo(db).list(project_id=project_id, limit=1000)
                total = len(rows)
                arta = sum(1 for r in rows if r.verdict == "arta_knew")
                human = sum(1 for r in rows if r.verdict == "human_knowledge")
                top = Counter(r.grounding_fact_ref for r in rows if r.grounding_fact_ref).most_common(10)
                return {
                    "total": total, "arta_knew": arta, "human_knowledge": human,
                    "arta_defect_rate": round(arta / total, 3) if total else 0.0,
                    "by_kind": dict(Counter(r.kind for r in rows if r.kind)),
                    "by_tool": dict(Counter(r.tool for r in rows if r.tool)),
                    "by_verify_status": dict(Counter(r.verify_status or "not_run" for r in rows)),
                    "top_corrected_endpoints": [{"fact": f, "count": c} for f, c in top],
                }
    except Exception as exc:
        log.debug("R320: analytics DB unavailable: %s", exc)
    return {"total": 0, "arta_knew": 0, "human_knowledge": 0, "arta_defect_rate": 0.0,
            "by_kind": {}, "by_tool": {}, "by_verify_status": {}, "top_corrected_endpoints": []}


@router.post("/corrections/{correction_id}/verify", dependencies=[Depends(_require_api_key)])
async def verify_correction(correction_id: str, request: Request):
    """R320 S2 — close the loop with SUT evidence: run the corrected test (scoped
    to its requirement + tool, reusing execute-by-tool) so the operator can confirm
    the failure resolved. Sets verify_status=running + returns the run_id; the
    outcome lands in Run History (and reconciles via GET .../verify-status)."""
    from ..db_adapter import try_db
    try:
        async with try_db() as db:
            if db is None:
                raise HTTPException(503, "DB unavailable")
            from ...db.repository import TestCorrectionRepo
            repo = TestCorrectionRepo(db)
            obj = await repo.get(correction_id)
            if not obj:
                raise HTTPException(404, f"Correction {correction_id} not found")
            if not (obj.project_id and obj.requirement_id and obj.tool):
                raise HTTPException(400, "Correction lacks project_id/requirement_id/tool to scope a verify run")
            # Surgical: run JUST the corrected test (RunRequest.test_ids) instead of
            # the whole requirement+tool — the reconcile then reads that test's
            # result from this exact run. Falls back to the requirement scope only
            # when the correction has no test_id.
            from .execution import trigger_run, RunRequest
            try:
                _req = RunRequest(project_id=str(obj.project_id),
                                  tools=[obj.tool] if obj.tool else [],
                                  environment="staging")
                if obj.test_id:
                    _req.test_ids = [obj.test_id]
                else:
                    _req.requirement_ids = [obj.requirement_id]
                run = await trigger_run(_req, request)
            except Exception as exc:
                log.warning("R320: verify run failed to start: %s", exc)
                raise HTTPException(502, f"verify run failed to start: {exc}")
            run_id = (run.get("run_id") or run.get("id")) if isinstance(run, dict) else None
            obj.verify_status = "running"
            obj.verify_run_id = run_id           # scope reconcile to THIS run (not any prior)
            await db.flush()
            return {"correction_id": correction_id, "verify_status": "running", "run_id": run_id}
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("R320: verify DB error: %s", exc)
        raise HTTPException(503, "DB unavailable")


@router.get("/corrections/{correction_id}/verify-status", dependencies=[Depends(_require_api_key)])
async def reconcile_verify_status(correction_id: str):
    """R320 S2 — reconcile a verify run: read the corrected test's latest result
    and update verify_status (passed|failed|not_run). Evidence the correction is
    correct + aligned with the real SUT — and catches a wrong human fact."""
    from ..db_adapter import try_db
    try:
        async with try_db() as db:
            if db is None:
                raise HTTPException(503, "DB unavailable")
            from ...db.repository import TestCorrectionRepo
            repo = TestCorrectionRepo(db)
            obj = await repo.get(correction_id)
            if not obj:
                raise HTTPException(404, f"Correction {correction_id} not found")
            status = obj.verify_status or "not_run"
            # Scope to the SPECIFIC verify run (not the test's latest result across
            # all runs — that could report a stale PASS from an unrelated prior run).
            if obj.test_id and obj.verify_run_id:
                from sqlalchemy import text as _t
                row = (await db.execute(_t(
                    "SELECT er.status::text FROM execution_results er "
                    "JOIN test_runs tr ON tr.id = er.run_id "
                    "WHERE er.test_id = :tid AND tr.run_id = :vrid "
                    "ORDER BY er.executed_at DESC NULLS LAST LIMIT 1"),
                    {"tid": obj.test_id, "vrid": obj.verify_run_id})).first()
                if row:
                    st = str(row[0] or "").upper()
                    status = ("passed" if st == "PASS"
                              else "failed" if st in ("FAIL", "ERROR") else status)
                    await repo.set_verify_status(correction_id, status)
            return {"correction_id": correction_id, "verify_status": status}
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("R320: reconcile DB error: %s", exc)
        raise HTTPException(503, "DB unavailable")


@router.delete("/corrections/{correction_id}", dependencies=[Depends(_require_api_key)])
async def revert_correction(correction_id: str):
    """R320 — revert a correction: remove its human_correction grounding fact +
    delete the record. A wrong human fact never silently poisons grounding."""
    from ..db_adapter import try_db
    from ...agents.api_discovery import revert_human_correction
    async with try_db() as db:
        if db is None:
            raise HTTPException(503, "DB unavailable")
        from ...db.repository import TestCorrectionRepo
        repo = TestCorrectionRepo(db)
        obj = await repo.get(correction_id)
        if not obj:
            raise HTTPException(404, f"Correction {correction_id} not found")
        removed = 0
        if obj.grounding_fact_ref and ":" in obj.grounding_fact_ref and obj.project_id:
            _m, _, _pth = obj.grounding_fact_ref.partition(":")
            # Pass field/value so a value MERGED onto a real entry is un-merged too
            # (not just standalone human facts) — truthful revert.
            removed = revert_human_correction(
                str(obj.project_id), method=_m, path=_pth,
                field=obj.field, value=obj.to_value)
        await repo.delete(correction_id)
        return {"reverted": correction_id, "grounding_removed": removed}
