"""
ARTA Platform — Slash Command Dispatcher
Routes /generate-tests, /analyze-risk, etc. to real agent invocations.
Returns structured results that the assistant router can stream back.
"""
from __future__ import annotations

import logging
from uuid import uuid4

log = logging.getLogger("arta.commands")


def _to_plain_dict(obj):
    """Recursively convert dataclass instances to plain dicts. Lists/tuples
    keep their structure. Dicts pass through (their values are recursed).
    Everything else returned as-is.

    Required because shallow `vars(r)` leaves nested dataclass instances
    (e.g. `Requirement.acceptance_criteria` is a list of `AcceptanceCriterion`
    dataclass instances) untouched. Downstream agents call `.get()` on those
    nested objects and crash with AttributeError. Verified live in run-7c5ff8
    where every `/generate-edge-cases` invocation failed with
    `AttributeError: 'AcceptanceCriterion' object has no attribute 'get'`.
    """
    if hasattr(obj, '__dataclass_fields__'):
        return {k: _to_plain_dict(v) for k, v in vars(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_plain_dict(v) for k, v in obj.items()}
    return obj


async def dispatch(command: str, args: str, llm_client, *, project_id: str | None = None) -> dict:
    """Route a slash command to the appropriate agent(s).

    Args:
        command: The slash command (e.g. "/generate-tests")
        args: Free-text arguments (requirement ID, description, etc.)
        llm_client: The AsyncAnthropic-compatible LLM client from app.state

    Returns:
        Structured result dict with command output.
    """
    wf_id = f"wf-{uuid4().hex[:8]}"

    try:
        if command.startswith("/generate-tests"):
            return await _generate_tests(args, llm_client, wf_id, project_id=project_id)
        elif command.startswith("/analyze-risk"):
            return await _analyze_risk(args, llm_client, wf_id, project_id=project_id)
        elif command.startswith("/generate-edge-cases"):
            return await _generate_edge_cases(args, llm_client, wf_id, project_id=project_id)
        elif command.startswith("/explain-failure"):
            return await _explain_failure(args, llm_client, wf_id, project_id=project_id)
        elif command.startswith("/check-gate"):
            return await _check_gate(args, llm_client, wf_id, project_id=project_id)
        elif command.startswith("/heal-tests"):
            return await _heal_tests(args, llm_client, wf_id, project_id=project_id)
        elif command.startswith("/check-coverage"):
            return await _check_coverage(args, llm_client, wf_id, project_id=project_id)
        elif command.startswith("/run-atdd"):
            return await _run_atdd(args, llm_client, wf_id, project_id=project_id)
        else:
            return {
                "command": command,
                "status": "error",
                "message": f"Unknown command: {command}",
                "available": [
                    "/generate-tests", "/generate-edge-cases",
                    "/analyze-risk", "/check-coverage",
                    "/run-atdd", "/explain-failure",
                    "/check-gate", "/heal-tests",
                ],
            }
    except Exception as exc:
        log.exception("Command %s failed", command)
        return {
            "command": command,
            "workflow_id": wf_id,
            "status": "error",
            "message": str(exc),
        }


async def _resolve_project_for_stamping(project_id: str | None) -> dict | None:
    """Phase J2 helper — load the project record so requirement stamping
    can flow `is_api_only` + `discovery_settings` to Stage 2.5 without an
    extra DB round-trip per stage. Best-effort: returns None on failure;
    downstream code degrades to skipping discovery.
    """
    if not project_id:
        return None
    try:
        from ...db.db_adapter import try_db
        from ...db import models
        import sqlalchemy as sa
    except ImportError:
        return None
    async for session in try_db():   # type: ignore
        try:
            stmt = sa.select(models.Project).where(models.Project.id == project_id)
            result = await session.execute(stmt)
            project = result.scalar_one_or_none()
            if project is None:
                return None
            return {
                "id": str(project.id),
                "name": project.name,
                "is_api_only": bool(getattr(project, "is_api_only", False)),
                "discovery_settings": dict(getattr(project, "discovery_settings", None) or {}),
                "integrations": dict(getattr(project, "integrations", None) or {}),
            }
        except Exception as exc:
            log.debug("project lookup for J2 stamping failed: %s", exc)
            return None
    return None


def _stamp_project_on_requirements(requirements: list[dict], project: dict | None) -> list[dict]:
    """Phase J2 — set `req["project"] = project` on every requirement dict.
    No-op when project is None (pre-J2 callers / orphan requirements)."""
    if not project:
        return requirements
    for r in requirements:
        if isinstance(r, dict):
            r["project"] = project
    return requirements


def _lookup_seq_integrity_for_test(test_id: str) -> dict:
    """Phase J11 — find the most recent run that observed `test_id` and
    return its sequence_integrity dict so cascade-aware classification
    collapses the right way.

    Best-effort: returns `{}` when no recent run contains the test_id.
    Defect intel handles the empty case gracefully (all failures classified
    as root_causes).
    """
    if not test_id:
        return {}
    try:
        from ..routers.execution import _REAL_RUNS, _REAL_RESULTS
    except ImportError:
        return {}
    for run_id, results in (_REAL_RESULTS or {}).items():
        if not isinstance(results, list):
            continue
        if any(isinstance(r, dict) and r.get("test_id") == test_id for r in results):
            run = (_REAL_RUNS or {}).get(run_id) or {}
            seq = run.get("sequence_integrity")
            if isinstance(seq, dict):
                return seq
    return {}


# ── Command Handlers ─────────────────────────────────────────────────────────


async def _generate_tests(args: str, client, wf_id: str, *, project_id: str | None = None) -> dict:
    """Parse requirement text and generate Gherkin + automation scripts."""
    # If a project is selected, load its requirements directly
    from ..routers.requirements import BUGTRACKR_REQUIREMENTS, BUGTRACKR_PROJECT_ID, PROJECT_REQUIREMENTS

    project_reqs = []
    if project_id == BUGTRACKR_PROJECT_ID:
        project_reqs = BUGTRACKR_REQUIREMENTS
    elif project_id:
        # R320 S3b — an unknown project must NOT fall back to another project's
        # MOCK requirements (cross-project mock-as-real leak). Honest empty →
        # the real LLM/agent path below (or an empty, truthful result).
        project_reqs = PROJECT_REQUIREMENTS.get(project_id) or []

    if project_reqs:
        # R320 S3b — this console path LOADS the project's real requirements; it
        # does NOT run the full generation pipeline (that lives on Test
        # Architecture / POST /api/tests/generate — the single source of truth).
        # Label it truthfully instead of claiming "Generated tests".
        return {
            "command": "/generate-tests",
            "workflow_id": wf_id,
            "status": "completed",
            "message": (f"Loaded {len(project_reqs)} requirement(s) for this project. "
                        "Generate/regenerate the scripts on the Test Architecture page "
                        "(or POST /api/tests/generate) — this console does not run the "
                        "full pipeline."),
            "requirements": project_reqs,
        }

    # Fallback: parse text with LLM (original behavior)
    if client is None:
        return {
            "command": "/generate-tests",
            "workflow_id": wf_id,
            "status": "completed",
            "message": (
                "No LLM configured. To enable AI-powered test generation, "
                "configure an LLM provider in Settings → LLM Configuration "
                "(Anthropic Claude, Ollama, or Google Gemini). "
                "Alternatively, use the 'Generate Tests' button on the Test Architecture page "
                "which supports fallback template-based generation."
            ),
            "requirements": [],
            "scenarios": [],
        }

    from ...agents.requirement_intel import RequirementIntelAgent
    from ...agents.strategy_architect import StrategyArchitectAgent
    from ...agents.atdd_designer import ATDDDesignerAgent

    # Step 1: Parse requirements + Phase J2 stamp project on each so Stage 2.5
    # and the post-run chain pipeline can resolve discovery settings.
    project_record = await _resolve_project_for_stamping(project_id)
    req_agent = RequirementIntelAgent(client)
    requirements = await req_agent.parse_document(args, "user_story")
    # DEEP-convert dataclass objects (including nested AcceptanceCriterion)
    requirements = [_to_plain_dict(r) for r in requirements]
    requirements = _stamp_project_on_requirements(requirements, project_record)

    if not requirements:
        return {
            "command": "/generate-tests",
            "workflow_id": wf_id,
            "status": "completed",
            "message": "No requirements extracted from input",
            "requirements": [],
        }

    # Step 2: Risk scoring
    strategy_agent = StrategyArchitectAgent(client)
    risk_profiles_raw = await strategy_agent.score_risks(requirements)
    # Convert RiskProfile dataclasses to dicts
    risk_profiles = [
        vars(p) if hasattr(p, '__dataclass_fields__') else (p._to_dict() if hasattr(p, '_to_dict') else p)
        for p in (risk_profiles_raw if isinstance(risk_profiles_raw, list) else [risk_profiles_raw])
    ]

    # Step 3: Generate Gherkin — generate() expects (list[dict], list[dict])
    atdd_agent = ATDDDesignerAgent(client)
    try:
        scenarios_result = await atdd_agent.generate(requirements, risk_profiles)
        scenarios = scenarios_result if isinstance(scenarios_result, list) else [scenarios_result]
    except Exception as exc:
        log.warning("Gherkin generation failed: %s", exc)
        scenarios = [{"message": f"Generation failed: {exc}. Configure an LLM in Settings."}]

    return {
        "command": "/generate-tests",
        "workflow_id": wf_id,
        "status": "completed",
        "message": f"Generated tests for {len(requirements)} requirement(s)",
        "requirements": requirements,
        "risk_profiles": risk_profiles,
        "scenarios": scenarios,
    }


async def _analyze_risk(args: str, client, wf_id: str, *, project_id: str | None = None) -> dict:
    """Analyze risk for a requirement or module description."""
    from ..routers.requirements import BUGTRACKR_REQUIREMENTS, BUGTRACKR_PROJECT_ID, PROJECT_REQUIREMENTS

    project_reqs = []
    if project_id == BUGTRACKR_PROJECT_ID:
        project_reqs = BUGTRACKR_REQUIREMENTS
    elif project_id:
        # R320 S3b — an unknown project must NOT fall back to another project's
        # MOCK requirements (cross-project mock-as-real leak). Honest empty →
        # the real LLM/agent path below (or an empty, truthful result).
        project_reqs = PROJECT_REQUIREMENTS.get(project_id) or []

    if project_reqs:
        risk_profiles = [
            {
                "requirement_id": r.get("req_id", r.get("id")),
                "title": r.get("title"),
                "priority": r.get("priority"),
                "risk_score": r.get("risk_score"),
                "impact": r.get("impact"),
                "probability": r.get("probability"),
            }
            for r in project_reqs
        ]
        return {
            "command": "/analyze-risk",
            "workflow_id": wf_id,
            "status": "completed",
            "message": f"Risk analysis complete for {len(project_reqs)} requirement(s)",
            "risk_profiles": risk_profiles,
        }

    # Fallback: LLM-based parsing
    if client is None:
        return {
            "command": "/analyze-risk",
            "workflow_id": wf_id,
            "status": "completed",
            "message": "No LLM configured. Configure a provider in Settings → LLM Configuration.",
            "risk_profiles": [],
        }

    from ...agents.requirement_intel import RequirementIntelAgent
    from ...agents.strategy_architect import StrategyArchitectAgent

    project_record = await _resolve_project_for_stamping(project_id)
    req_agent = RequirementIntelAgent(client)
    requirements = await req_agent.parse_document(args, "user_story")
    requirements = [vars(r) if hasattr(r, '__dataclass_fields__') else r for r in requirements]
    requirements = _stamp_project_on_requirements(requirements, project_record)

    strategy_agent = StrategyArchitectAgent(client)
    risk_profiles = await strategy_agent.score_risks(requirements)

    return {
        "command": "/analyze-risk",
        "workflow_id": wf_id,
        "status": "completed",
        "message": f"Risk analysis complete for {len(requirements)} requirement(s)",
        "risk_profiles": [p._to_dict() if hasattr(p, '_to_dict') else p for p in risk_profiles],
    }


async def _generate_edge_cases(args: str, client, wf_id: str, *, project_id: str | None = None) -> dict:
    """Generate edge case scenarios for a feature description."""
    # Project-aware: generate edge cases from project requirements
    from ..routers.requirements import BUGTRACKR_REQUIREMENTS, BUGTRACKR_PROJECT_ID, PROJECT_REQUIREMENTS
    project_reqs = []
    if project_id == BUGTRACKR_PROJECT_ID:
        project_reqs = BUGTRACKR_REQUIREMENTS
    elif project_id:
        project_reqs = PROJECT_REQUIREMENTS.get(project_id, [])
    if project_reqs:
        edge_cases = []
        for r in project_reqs:
            title = r.get("title", "")
            edge_cases.extend([
                f"Empty/null input for {title}",
                f"Unauthorized access to {title} (no auth token)",
                f"Concurrent operations on {title} (race condition)",
                f"Maximum length input for {title} (10000+ chars)",
                f"Special characters in {title} fields (SQL injection, XSS)",
            ])
        return {
            "command": "/generate-edge-cases",
            "workflow_id": wf_id,
            "status": "completed",
            "message": f"Generated edge cases for {len(project_reqs)} requirement(s)",
            "edge_cases": edge_cases,
        }

    if client is None:
        return {
            "command": "/generate-edge-cases",
            "workflow_id": wf_id,
            "status": "completed",
            "message": "No LLM configured. Configure a provider in Settings → LLM Configuration.",
            "edge_cases": [],
        }

    from ...agents.requirement_intel import RequirementIntelAgent
    from ...agents.atdd_designer import ATDDDesignerAgent

    project_record = await _resolve_project_for_stamping(project_id)
    req_agent = RequirementIntelAgent(client)
    requirements = await req_agent.parse_document(args, "user_story")
    # DEEP-convert dataclass objects (including nested AcceptanceCriterion)
    requirements = [_to_plain_dict(r) for r in requirements]
    requirements = _stamp_project_on_requirements(requirements, project_record)

    atdd_agent = ATDDDesignerAgent(client)
    edge_cases = []
    for req in requirements:
        # Generate with edge-case focus by passing high-risk profile
        req_id = req.get("id", "") if isinstance(req, dict) else getattr(req, "id", "")
        mock_profile = {
            "requirement_id": req_id,
            "priority": "P0",
            "risk_score": 9.0,
            "impact": 3,
            "probability": 3,
            "risk_action": "AUTO_FAIL",
            "test_types": ["ui", "api", "security", "performance"],
            "coverage_target_pct": 100,
            "recommended_tools": ["playwright", "newman"],
            "rationale": "Edge case generation — treat as highest risk",
        }
        # generate() expects lists for both requirements and risk_profiles
        req_list = [req] if not isinstance(req, list) else req
        result = await atdd_agent.generate(req_list, [mock_profile])
        edge_cases.append(result)

    # If edge cases are empty (no LLM), provide useful fallback
    if not edge_cases or all(not ec.get("gherkin_scenarios") for ec in edge_cases):
        edge_cases = [{
            "gherkin_scenarios": [
                "Scenario: Reject empty required fields\n  Given I submit a form\n  When the title field is empty\n  Then I receive a 400 validation error\n  And the error message mentions 'title is required'",
                "Scenario: XSS injection in text fields\n  Given I create a resource\n  When I set the description to '<script>alert(\"xss\")</script>'\n  Then the HTML is escaped or stripped in the response",
                "Scenario: SQL injection attempt\n  Given I search for resources\n  When I use \"'; DROP TABLE bugs; --\" as search term\n  Then I receive a normal response (no server error)",
                "Scenario: Unauthorized access returns 401\n  Given I am not authenticated\n  When I access a protected endpoint\n  Then I receive a 401 or redirect to login",
                "Scenario: Invalid ID returns 404\n  Given a resource does not exist\n  When I request it with a random UUID\n  Then I receive a 404 Not Found response",
                "Scenario: Duplicate creation returns 409\n  Given a resource already exists\n  When I try to create it again with the same unique key\n  Then I receive a 409 Conflict error",
                "Scenario: Max length boundary\n  Given I create a resource\n  When the title is 10000 characters long\n  Then I receive a 400 error or the title is truncated",
            ],
            "acceptance_criteria": [],
            "test_data_fixtures": [],
            "note": "Template-based edge cases (no LLM). Configure LLM for AI-powered edge case discovery.",
        }]

    return {
        "command": "/generate-edge-cases",
        "workflow_id": wf_id,
        "status": "completed",
        "message": f"Generated edge cases for {len(requirements)} requirement(s)",
        "edge_cases": edge_cases,
    }


async def _explain_failure(args: str, client, wf_id: str, *, project_id: str | None = None) -> dict:
    """Analyze a test failure and provide root cause analysis."""
    if client is None:
        return {
            "command": "/explain-failure",
            "workflow_id": wf_id,
            "status": "completed",
            "message": "No LLM configured. Configure a provider in Settings → LLM Configuration to enable AI root cause analysis.",
            "defects": [],
        }

    from ...agents.defect_intel import DefectIntelAgent

    agent = DefectIntelAgent(client)

    # Create a mock failure for analysis
    failures = [{
        "test_id": args.strip() or "unknown",
        "title": f"Test failure: {args}",
        "status": "FAIL",
        "error_message": args,
        "tool": "unknown",
    }]
    # Phase J11 — when the test_id matches a recent run's known consumer,
    # pass `sequence_integrity` so cascade attribution + PCV detection
    # collapse the right way. /explain-failure has no run_id arg today, so
    # we look for the most recent run that contains this test_id.
    seq_integrity = _lookup_seq_integrity_for_test(args.strip())
    try:
        defects = await agent.analyze_failures(failures, sequence_integrity=seq_integrity)
    except TypeError:
        # Older agent signature without sequence_integrity kwarg.
        defects = await agent.analyze_failures(failures)

    return {
        "command": "/explain-failure",
        "workflow_id": wf_id,
        "status": "completed",
        "message": f"Root cause analysis for: {args}",
        "defects": defects,
    }


async def _check_gate(args: str, client, wf_id: str, *, project_id: str | None = None) -> dict:
    """Evaluate quality gate for an environment."""
    env = args.strip() or "staging"

    # R320 S3b — a BMAD TEA quality gate is EVIDENCE-BASED: it needs a real
    # execution run's coverage/pass-rate/defects. The console command has no run
    # context, so point to the canonical per-run gate rather than fabricate a
    # decision (this previously fed QualityGateAgent hardcoded 78%/91% coverage
    # and, on error, invented a whole CONCERNS decision).
    return {
        "command": "/check-gate",
        "workflow_id": wf_id,
        "status": "completed",
        "environment": env,
        "message": (
            "Quality gates are evidence-based and evaluated PER RUN. Trigger a run, "
            "then view the gate on the SUT Quality page or GET "
            "/api/gates/nfr?run_id=<run_id>. No gate decision is fabricated here."
        ),
    }


async def _heal_tests(args: str, client, wf_id: str, *, project_id: str | None = None) -> dict:
    """
    Trigger self-healing analysis for failing/fallback tests.

    Finds all tests using fallback templates (generation_source == "fallback"),
    diagnoses root causes via rule-based classification (zero LLM tokens for known
    errors), and retries LLM generation or queues proposals for review.
    """
    try:
        from ..routers.tests import GENERATED_TESTS
        from ...agents.self_healing import SelfHealingAgent

        # Find tests that used fallback template generation
        fallback_tests = [
            t for t in GENERATED_TESTS
            if t.get("generation_source") == "fallback"
        ]

        if not fallback_tests:
            # Also find tests that look like fallback stubs (old tests without tracking field)
            fallback_tests = [
                t for t in GENERATED_TESTS
                if (
                    not t.get("generation_source")
                    and t.get("script_content", "")
                    and (
                        "Stub smoke test" in t.get("script_content", "")
                        or "ARTA Auto-Generated" in t.get("script_content", "")
                        or "ARTA Stub" in t.get("script_content", "")
                    )
                )
                or not t.get("script_content")
                or t.get("generation_error")
            ]
            if not fallback_tests:
                return {
                    "command": "/heal-tests",
                    "workflow_id": wf_id,
                    "status": "completed",
                    "message": "All tests appear LLM-generated — no fallback stubs detected.",
                    "healed": 0,
                    "queued_for_approval": 0,
                }

        if client is None:
            # No LLM client — still queue informational proposals with rule-based analysis
            from ...agents.self_healing import _classify_generation_failure
            diagnoses: list[str] = []
            for t in fallback_tests[:5]:
                failure = t.get("generation_failure") or {}
                root_cause, _, hint = _classify_generation_failure(
                    failure.get("error_type", "Unknown"),
                    failure.get("error_msg", ""),
                )
                diagnoses.append(f"{t.get('requirement_id', '?')}/{t.get('tool', '?')}: {hint}")
            return {
                "command": "/heal-tests",
                "workflow_id": wf_id,
                "status": "completed",
                "message": (
                    f"Found {len(fallback_tests)} tests using fallback templates. "
                    f"LLM client unavailable for regeneration. Diagnoses:\n" +
                    "\n".join(diagnoses)
                ),
                "healed": 0,
                "queued_for_approval": 0,
                "diagnostics": diagnoses,
            }

        healer = SelfHealingAgent(client)
        result = await healer.heal_generation_failures(fallback_tests, project_id or "")

        msg_parts = [f"Found {len(fallback_tests)} tests using fallback templates."]
        if result["healed"] > 0:
            msg_parts.append(f"✓ {result['healed']} tests regenerated via LLM.")
        if result["queued"] > 0:
            msg_parts.append(f"⏳ {result['queued']} proposals queued in healing approval queue.")
        if result["failed"] > 0:
            msg_parts.append(f"⚠ {result['failed']} tests need manual configuration.")

        return {
            "command": "/heal-tests",
            "workflow_id": wf_id,
            "status": "completed",
            "message": " ".join(msg_parts),
            "healed": result["healed"],
            "queued_for_approval": result["queued"],
            "needs_manual_fix": result["failed"],
            "proposal_ids": result.get("proposals", []),
        }

    except Exception as exc:
        import logging as _logging
        _logging.getLogger("arta.commands").error("heal_tests failed: %s", exc, exc_info=True)
        return {
            "command": "/heal-tests",
            "workflow_id": wf_id,
            "status": "error",
            "message": f"Healing analysis failed: {exc}",
            "healed": 0,
            "queued_for_approval": 0,
        }


async def _check_coverage(args: str, client, wf_id: str, *, project_id: str | None = None) -> dict:
    """Check test coverage for a requirement or module."""
    from ..routers.requirements import BUGTRACKR_REQUIREMENTS, BUGTRACKR_PROJECT_ID, PROJECT_REQUIREMENTS

    project_reqs = []
    if project_id == BUGTRACKR_PROJECT_ID:
        project_reqs = BUGTRACKR_REQUIREMENTS
    elif project_id:
        project_reqs = PROJECT_REQUIREMENTS.get(project_id, [])

    if project_reqs:
        return {
            "command": "/check-coverage",
            "workflow_id": wf_id,
            "status": "completed",
            "message": f"Coverage analysis for {len(project_reqs)} requirements",
            "total_requirements": len(project_reqs),
            "covered": sum(1 for r in project_reqs if r.get("coverage_pct", 0) > 0),
            "uncovered": sum(1 for r in project_reqs if r.get("coverage_pct", 0) == 0),
            "coverage": {
                "total_requirements": len(project_reqs),
                "covered_requirements": sum(1 for r in project_reqs if r.get("coverage_pct", 0) > 0),
                "overall_coverage_pct": round(
                    sum(r.get("coverage_pct", 0) for r in project_reqs) / max(len(project_reqs), 1), 1
                ),
            },
        }

    # Fallback: real traceability coverage, or an HONEST empty — never fabricated.
    # R320 S3b — the previous mock (78%, a phantom "REQ-019 Refund Flow" gap)
    # presented invented numbers as this project's coverage.
    try:
        from ...agents.traceability_agent import TraceabilityAgent
        agent = TraceabilityAgent()
        if hasattr(agent, 'compute_coverage_report'):
            report = await agent.compute_coverage_report()
        elif hasattr(agent, 'compute_coverage'):
            report = await agent.compute_coverage()
        else:
            raise AttributeError("No coverage computation method available")
        return {
            "command": "/check-coverage", "workflow_id": wf_id, "status": "completed",
            "message": f"Coverage analysis for: {args or 'all requirements'}",
            "coverage": report,
        }
    except Exception as exc:
        log.warning("Coverage computation unavailable: %s", exc)
        return {
            "command": "/check-coverage", "workflow_id": wf_id, "status": "completed",
            "message": ("No coverage available for this project yet — import requirements "
                        "and run tests, then see the Traceability coverage matrix "
                        "(GET /api/traceability/coverage/matrix). No coverage is fabricated here."),
            "coverage": None,
        }


async def _run_atdd(args: str, client, wf_id: str, *, project_id: str | None = None) -> dict:
    """Run the full ATDD pipeline: generate tests → trigger execution."""
    result = await _generate_tests(args, client, wf_id, project_id=project_id)
    result["command"] = "/run-atdd"

    # Trigger real test execution directly (bypass HTTP auth)
    try:
        import asyncio
        from datetime import datetime, timezone
        from ..routers.execution import _REAL_RUNS, _real_execution, RunRequest

        run_id = f"run-{uuid4().hex[:6]}"
        build_id = f"build-{uuid4().hex[:4]}"

        # Determine best environment from project config (first configured env, or "local")
        run_env = "local"
        try:
            from ..routers.projects import _PROJECTS
            proj = _PROJECTS.get(project_id) if project_id else None
            if proj and proj.get("environments"):
                env_names = list(proj["environments"].keys())
                # Prefer local > staging > first available
                for pref in ["local", "staging"]:
                    if pref in env_names:
                        run_env = pref
                        break
                else:
                    if env_names:
                        run_env = env_names[0]
        except Exception:
            pass

        body = RunRequest(suite_type="full", environment=run_env, project_id=project_id)

        _REAL_RUNS[run_id] = {
            "id": run_id,
            "run_id": run_id,
            "build_id": build_id,
            "status": "running",
            "trigger": "atdd",
            "branch": "main",
            "environment": run_env,
            "suite_type": "full",
            "project_id": project_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "results": [],
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "total": 0,
            "coverage_pct": 0.0,
            "duration_s": 0,
            "gate_decision": None,
        }
        # Persist initial run to DB
        try:
            from ..routers.execution import _update_run_status_in_db
            from ..db_adapter import try_db
            async with try_db() as db:
                if db:
                    from sqlalchemy import text
                    import uuid as _uuid
                    await db.execute(text("""
                        INSERT INTO test_runs (run_id, build_id, environment, triggered_by, status, project_id)
                        VALUES (:run_id, :build_id, :env, 'atdd', CAST('running' AS run_status), CAST(:pid AS uuid))
                        ON CONFLICT (run_id) DO NOTHING
                    """), {"run_id": run_id, "build_id": build_id, "env": run_env, "pid": project_id})
                    await db.commit()
        except Exception:
            pass

        # C5: Supervise the background task so exceptions are logged rather than swallowed.
        from ...observability.task_supervisor import supervise
        supervise(
            asyncio.create_task(_real_execution(run_id, build_id, body)),
            f"atdd_execution:{run_id}",
        )

        result["execution"] = {"run_id": run_id, "status": "running"}
        result["message"] = (
            f"ATDD cycle complete: tests generated, execution started (run {run_id}). "
            f"Track progress in Run History or Test Explorer."
        )
    except Exception as exc:
        log.warning("ATDD execution trigger failed: %s", exc)
        result["message"] = f"Tests generated for: {args}. Execution trigger failed: {exc}"

    return result
