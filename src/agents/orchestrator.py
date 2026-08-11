"""
ARTA Orchestrator Agent
Master coordinator implementing the TEA (Test Engineering Architecture) workflow.
Manages the multi-agent pipeline from requirement intake to quality gate.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger("arta.orchestrator")


# ──────────────────────────────────────────────────────────────────────────────
# TEA Workflow Stages (maps directly to TEA methodology layers)
# ──────────────────────────────────────────────────────────────────────────────

class TEAStage(str, Enum):
    REQUIREMENT_ANALYSIS  = "requirement_analysis"
    RISK_SCORING          = "risk_scoring"
    ACCEPTANCE_TEST_DESIGN = "acceptance_test_design"
    # Phase B1 — UI Discovery Run. Sits between ATDD generation and
    # automation generation: runs the freshly-generated Playwright spec
    # with HAR capture, mines the HAR for path-param env-var values +
    # captured-endpoint shapes, and persists the harvest. Phase D's
    # Newman/k6/Playwright generators then consume the harvested values.
    UI_DISCOVERY          = "ui_discovery"
    AUTOMATION_GENERATION = "automation_generation"
    TEST_EXECUTION        = "test_execution"
    EVIDENCE_COLLECTION   = "evidence_collection"
    TRACEABILITY_UPDATE   = "traceability_update"
    NFR_VALIDATION        = "nfr_validation"
    QUALITY_GATE          = "quality_gate"


class WorkflowStatus(str, Enum):
    PENDING          = "pending"
    RUNNING          = "running"
    PENDING_REVIEW   = "pending_review"   # Awaiting human review of generated tests
    COMPLETED        = "completed"
    FAILED           = "failed"
    BLOCKED          = "blocked"          # Quality gate blocked release


@dataclass
class WorkflowContext:
    """Shared context passed between TEA agents."""
    workflow_id: UUID = field(default_factory=uuid4)
    trigger: str = "manual"               # push | pr | scheduled | /command
    project_type: str = "web"             # web | mobile | api | microservices | analytics
    requirement_ids: list[str] = field(default_factory=list)
    build_id: str | None = None
    branch: str | None = None
    stage: TEAStage = TEAStage.REQUIREMENT_ANALYSIS
    status: WorkflowStatus = WorkflowStatus.PENDING

    # Accumulated results through the pipeline
    requirements: list[dict] = field(default_factory=list)
    risk_profiles: list[dict] = field(default_factory=list)
    acceptance_criteria: list[dict] = field(default_factory=list)
    gherkin_scenarios: list[str] = field(default_factory=list)
    automation_scripts: dict[str, str] = field(default_factory=dict)
    execution_results: list[dict] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    defects: list[dict] = field(default_factory=list)
    coverage_report: dict = field(default_factory=dict)
    gate_decision: dict = field(default_factory=dict)

    # Phase B1 — populated by Stage 2.5 UI Discovery (Phase B). Empty when
    # discovery skipped (api_only project, feature flag off, or no UI surface).
    discovery_result: dict = field(default_factory=dict)

    # Review gate control
    auto_approve: bool = False         # If True, skip human review gate
    review_approved: bool = False      # Set to True when human approves generated tests

    # Events for real-time streaming
    events: list[dict] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

class ARTAOrchestrator:
    """
    Master TEA workflow orchestrator.

    Executes the full TEA pipeline:
      1. Requirement Intelligence
      2. Risk Scoring (TEA Layer 2)
      3. Acceptance Test Design (TEA ATDD — tests fail first)
      4. Automation Generation
      5. Execution
      6. Evidence Collection
      7. Traceability Update
      8. NFR Validation
      9. Quality Gate Decision
    """

    def __init__(
        self,
        req_agent: Any,
        strategy_agent: Any,
        atdd_agent: Any,
        automation_agent: Any,
        execution_agent: Any,
        defect_agent: Any,
        traceability_agent: Any,
        quality_gate_agent: Any,
        event_bus: Any,
        evidence_agent: Any = None,
    ) -> None:
        # F8-6: lazy default for backward compat — callers that don't pass
        # one get the in-tree EvidenceCollectorAgent.
        if evidence_agent is None:
            from .evidence_collector import EvidenceCollectorAgent
            evidence_agent = EvidenceCollectorAgent()
        self._agents = {
            TEAStage.REQUIREMENT_ANALYSIS:   req_agent,
            TEAStage.RISK_SCORING:           strategy_agent,
            TEAStage.ACCEPTANCE_TEST_DESIGN: atdd_agent,
            TEAStage.AUTOMATION_GENERATION:  automation_agent,
            TEAStage.TEST_EXECUTION:         execution_agent,
            # F8-6: register Evidence Collection as a first-class agent so
            # all 9 BMAD-TEA layers dispatch through the same _agents dict.
            TEAStage.EVIDENCE_COLLECTION:    evidence_agent,
            TEAStage.TRACEABILITY_UPDATE:    traceability_agent,
            TEAStage.QUALITY_GATE:           quality_gate_agent,
        }
        self._defect_agent = defect_agent
        self._bus = event_bus
        self._analytics_agent = None   # Set via set_analytics_agent() for analytics projects
        self._healing_agent = None     # Phase G2/G3 — set via set_healing_agent()

    def set_analytics_agent(self, agent: Any) -> None:
        """Register the AnalyticsTestAgent for analytics project type routing."""
        self._analytics_agent = agent

    def set_healing_agent(self, agent: Any) -> None:
        """Phase G2 + G3 — register the SelfHealingAgent so the orchestrator
        can auto-queue `chain_reverify` and `provider_contract_heal`
        proposals after defect classification. Optional: when missing, the
        cascade-aware defect output still lands but no heal proposals fire.
        """
        self._healing_agent = agent

    async def run(self, ctx: WorkflowContext) -> WorkflowContext:
        """Execute the full TEA pipeline."""
        # F8-4: Bind ctx.workflow_id as the trace_id for this asyncio context
        # so every agent's log lines (atdd/automation/quality_gate/...) carry
        # the same trace via the TraceIdFilter — no per-agent kwarg plumbing
        # needed because contextvars propagate across `await` boundaries.
        try:
            from ..observability.log_context import set_trace_id
            set_trace_id(str(ctx.workflow_id))
        except Exception:  # observability is best-effort
            pass
        ctx.status = WorkflowStatus.RUNNING
        await self._emit(ctx, "workflow_started", {"workflow_id": str(ctx.workflow_id)})

        pipeline = [
            (TEAStage.REQUIREMENT_ANALYSIS,   self._run_requirement_analysis),
            (TEAStage.RISK_SCORING,           self._run_risk_scoring),
            (TEAStage.ACCEPTANCE_TEST_DESIGN, self._run_atdd_design),
            # Phase B1 — Stage 2.5. Run AFTER Playwright specs exist (ATDD)
            # and BEFORE Newman/k6/ZAP generation so they can consume
            # harvested env vars + captured endpoint shapes.
            (TEAStage.UI_DISCOVERY,           self._run_ui_discovery),
            (TEAStage.AUTOMATION_GENERATION,  self._run_automation_gen),
            (TEAStage.TEST_EXECUTION,         self._run_execution),
            (TEAStage.EVIDENCE_COLLECTION,    self._run_evidence_collection),
            (TEAStage.TRACEABILITY_UPDATE,    self._run_traceability),
            (TEAStage.NFR_VALIDATION,         self._run_nfr_validation),
            (TEAStage.QUALITY_GATE,           self._run_quality_gate),
        ]

        # Per-stage timeouts (seconds) — override via stage-specific config if needed
        _STAGE_TIMEOUTS: dict[TEAStage, int] = {
            TEAStage.REQUIREMENT_ANALYSIS:   120,
            TEAStage.RISK_SCORING:           90,
            TEAStage.ACCEPTANCE_TEST_DESIGN: 180,
            TEAStage.UI_DISCOVERY:           600,   # Phase B1 — full Playwright run with HAR
            TEAStage.AUTOMATION_GENERATION:  300,
            TEAStage.TEST_EXECUTION:         600,
            TEAStage.EVIDENCE_COLLECTION:    60,
            TEAStage.TRACEABILITY_UPDATE:    120,
            TEAStage.NFR_VALIDATION:         30,
            TEAStage.QUALITY_GATE:           60,
        }

        for stage, handler in pipeline:
            ctx.stage = stage
            timeout = _STAGE_TIMEOUTS.get(stage, 120)
            await self._emit(ctx, "stage_started", {"stage": stage.value, "timeout_s": timeout})
            try:
                async with asyncio.timeout(timeout):
                    ctx = await handler(ctx)
                await self._emit(ctx, "stage_completed", {"stage": stage.value})
            except TimeoutError:
                ctx.status = WorkflowStatus.FAILED
                msg = f"Stage timed out after {timeout}s"
                await self._emit(ctx, "stage_failed", {"stage": stage.value, "error": msg})
                logger.error("TEA pipeline timed out at %s (%ds)", stage.value, timeout)
                raise RuntimeError(msg) from None
            except Exception as exc:
                ctx.status = WorkflowStatus.FAILED
                await self._emit(ctx, "stage_failed", {"stage": stage.value, "error": str(exc)})
                logger.error("TEA pipeline failed at %s: %s", stage.value, exc, exc_info=True)
                raise

            # Short-circuit: if gate blocks, stop pipeline
            if ctx.status == WorkflowStatus.BLOCKED:
                await self._emit(ctx, "workflow_blocked", ctx.gate_decision)
                return ctx

        ctx.status = WorkflowStatus.COMPLETED
        await self._emit(ctx, "workflow_completed", {
            "pass_rate": ctx.coverage_report.get("pass_rate"),
            "coverage": ctx.coverage_report.get("coverage_pct"),
            "gate": ctx.gate_decision.get("decision"),
        })
        return ctx

    # ── Stage Handlers ──────────────────────────────────────────────────────

    async def _run_requirement_analysis(self, ctx: WorkflowContext) -> WorkflowContext:
        agent = self._agents[TEAStage.REQUIREMENT_ANALYSIS]
        ctx.requirements = await agent.analyze(ctx.requirement_ids, ctx.build_id)
        return ctx

    async def _run_risk_scoring(self, ctx: WorkflowContext) -> WorkflowContext:
        agent = self._agents[TEAStage.RISK_SCORING]
        ctx.risk_profiles = await agent.score_risks(ctx.requirements)
        # F5-2: Persist strategy artifact for audit/replay (best-effort, non-blocking).
        try:
            if hasattr(agent, "persist_strategy"):
                agent.persist_strategy(
                    ctx.risk_profiles,
                    project_id=getattr(ctx, "project_id", None),
                    trace_id=str(getattr(ctx, "workflow_id", "")) or None,
                )
        except Exception as exc:
            logger.warning("strategy persistence (orchestrator) failed: %s", exc)
        return ctx

    async def _run_atdd_design(self, ctx: WorkflowContext) -> WorkflowContext:
        """
        TEA ATDD Phase: generate FAILING tests first.
        Tests must exist before implementation — this enforces red-green cycle.

        Routes analytics projects to AnalyticsTestAgent for layer-by-layer
        test generation with frozen fixtures and adversarial inputs.
        """
        if ctx.project_type == "analytics" and self._analytics_agent is not None:
            # Use specialized analytics test agent
            result = await self._analytics_agent.generate(ctx.requirements, ctx.risk_profiles)
            ctx.acceptance_criteria = result["acceptance_criteria"]
            ctx.gherkin_scenarios = result["gherkin_scenarios"]
            # Store analytics-specific metadata in events
            await self._emit(ctx, "analytics_tests_generated", {
                "suites": result.get("analytics_suites", []),
            })
        else:
            agent = self._agents[TEAStage.ACCEPTANCE_TEST_DESIGN]
            result = await agent.generate(ctx.requirements, ctx.risk_profiles)
            ctx.acceptance_criteria = result["acceptance_criteria"]
            ctx.gherkin_scenarios = result["gherkin_scenarios"]
        return ctx

    async def _run_ui_discovery(self, ctx: WorkflowContext) -> WorkflowContext:
        """Phase B1 — Stage 2.5. Run the freshly-generated Playwright spec
        with HAR capture; mine the HAR for env-var values + endpoint shapes.

        This stage is OPT-IN per project (Phase I7 feature flag) and can be
        skipped for several legitimate reasons (no UI surface, recent
        discovery is fresh, api_only project). Each skip path emits a
        structured event so the admin "Discovery" panel can show why the
        stage didn't fire.

        On HAR launch failure (Phase I8 fallback), the orchestrator does NOT
        block the pipeline — it logs `discovery.failed` and continues with
        empty harvest. Phase F's gate flags this with `discovery_degraded`.
        """
        try:
            project = ctx.requirements[0].get("project") if ctx.requirements else None
        except (IndexError, AttributeError, KeyError):
            project = None

        # Project-level gates first — cheap, no Playwright invocation.
        try:
            from . import discovery_settings as ds
        except ImportError as exc:
            await self._emit(ctx, "ui_discovery.skipped", {"reason": "settings_module_missing", "detail": str(exc)})
            return ctx

        settings = (project or {}).get("discovery_settings") if isinstance(project, dict) else None
        is_api_only = bool((project or {}).get("is_api_only")) if isinstance(project, dict) else False

        if is_api_only:
            await self._emit(ctx, "ui_discovery.skipped", {"reason": "api_only_project"})
            return ctx

        if not ds.is_stage_2_5_enabled(settings):
            await self._emit(ctx, "ui_discovery.skipped", {"reason": "feature_flag_off"})
            return ctx

        # Surface check: any Playwright specs to run?
        playwright_specs = [
            f for f in (ctx.automation_scripts or {}).keys()
            if f.endswith(".spec.ts") or f.endswith(".spec.js")
        ]
        # ATDD may also have stamped specs onto gherkin_scenarios — be lenient.
        if not playwright_specs and not ctx.gherkin_scenarios:
            await self._emit(ctx, "ui_discovery.skipped", {"reason": "no_ui_surface"})
            return ctx

        # Delegate the heavy lifting to a dedicated agent if registered;
        # otherwise emit a pending-implementation event. Wiring the runtime
        # to actually spawn `npx playwright --project=discovery` and mine
        # HAR happens in B2/B4 + the discovery_executor module — this hook
        # is the sole pipeline insertion point.
        try:
            from . import discovery_executor   # type: ignore
        except ImportError:
            await self._emit(ctx, "ui_discovery.skipped", {
                "reason": "executor_not_implemented",
                "note": "Stage hook fires; discovery_executor module not present yet.",
            })
            return ctx

        try:
            harvest = await discovery_executor.execute(ctx, project or {})
            ctx.discovery_result = harvest or {}
            await self._emit(ctx, "ui_discovery.completed", {
                "envvars_harvested": len(ctx.discovery_result.get("envvar_values") or {}),
                "endpoints_captured": len(ctx.discovery_result.get("endpoints") or []),
                "multi_value_warnings": len(ctx.discovery_result.get("multi_value_warnings") or []),
            })
        except Exception as exc:   # Phase I8 — degraded fallback
            ctx.discovery_result = {"degraded": True, "degraded_reason": str(exc)}
            await self._emit(ctx, "ui_discovery.failed", {"error": str(exc)})
            logger.warning("UI Discovery failed for workflow %s: %s — continuing degraded",
                           ctx.workflow_id, exc)
        return ctx

    async def _run_automation_gen(self, ctx: WorkflowContext) -> WorkflowContext:
        # ── Review Gate ──────────────────────────────────────────────
        # ATDD requires stakeholder collaboration on acceptance tests.
        # Unless auto_approve is set, pause here for human review of
        # generated Gherkin before proceeding to script generation.
        if not ctx.auto_approve and not ctx.review_approved:
            ctx.status = WorkflowStatus.PENDING_REVIEW
            await self._emit(ctx, "review_gate_pending", {
                "stage": "automation_generation",
                "gherkin_count": len(ctx.gherkin_scenarios),
                "message": "Generated tests await human review. Approve, edit, or reject before proceeding.",
            })
            # Pipeline pauses here — resume_after_review() continues execution
            return ctx

        agent = self._agents[TEAStage.AUTOMATION_GENERATION]
        ctx.automation_scripts = await agent.generate(
            ctx.gherkin_scenarios,
            ctx.risk_profiles,
        )
        return ctx

    async def resume_after_review(self, ctx: WorkflowContext) -> WorkflowContext:
        """Resume the TEA pipeline after human review approval.

        Call this after the user approves generated tests via the review UI.
        The pipeline continues from AUTOMATION_GENERATION through QUALITY_GATE.
        """
        # F8-4: Re-bind trace_id on resume — the resume call may run in a
        # fresh asyncio context (different request handler) so the contextvar
        # set in run() doesn't carry over.
        try:
            from ..observability.log_context import set_trace_id
            set_trace_id(str(ctx.workflow_id))
        except Exception:
            pass
        ctx.review_approved = True
        ctx.status = WorkflowStatus.RUNNING
        await self._emit(ctx, "review_gate_approved", {
            "stage": "automation_generation",
            "message": "Human review approved. Resuming pipeline.",
        })

        # Continue from automation generation through the remaining stages
        remaining_pipeline = [
            (TEAStage.AUTOMATION_GENERATION,  self._run_automation_gen),
            (TEAStage.TEST_EXECUTION,         self._run_execution),
            (TEAStage.EVIDENCE_COLLECTION,    self._run_evidence_collection),
            (TEAStage.TRACEABILITY_UPDATE,    self._run_traceability),
            (TEAStage.NFR_VALIDATION,         self._run_nfr_validation),
            (TEAStage.QUALITY_GATE,           self._run_quality_gate),
        ]

        _STAGE_TIMEOUTS = {
            TEAStage.AUTOMATION_GENERATION: 300,
            TEAStage.TEST_EXECUTION: 600,
            TEAStage.EVIDENCE_COLLECTION: 60,
            TEAStage.TRACEABILITY_UPDATE: 120,
            TEAStage.NFR_VALIDATION: 30,
            TEAStage.QUALITY_GATE: 60,
        }

        for stage, handler in remaining_pipeline:
            ctx.stage = stage
            timeout = _STAGE_TIMEOUTS.get(stage, 120)
            await self._emit(ctx, "stage_started", {"stage": stage.value, "timeout_s": timeout})
            try:
                async with asyncio.timeout(timeout):
                    ctx = await handler(ctx)
                await self._emit(ctx, "stage_completed", {"stage": stage.value})
            except TimeoutError:
                ctx.status = WorkflowStatus.FAILED
                msg = f"Stage timed out after {timeout}s"
                await self._emit(ctx, "stage_failed", {"stage": stage.value, "error": msg})
                raise RuntimeError(msg) from None
            except Exception as exc:
                ctx.status = WorkflowStatus.FAILED
                await self._emit(ctx, "stage_failed", {"stage": stage.value, "error": str(exc)})
                raise

            if ctx.status == WorkflowStatus.BLOCKED:
                await self._emit(ctx, "workflow_blocked", ctx.gate_decision)
                return ctx

        ctx.status = WorkflowStatus.COMPLETED
        await self._emit(ctx, "workflow_completed", {
            "pass_rate": ctx.coverage_report.get("pass_rate"),
            "coverage": ctx.coverage_report.get("coverage_pct"),
            "gate": ctx.gate_decision.get("decision"),
        })
        return ctx

    async def _run_execution(self, ctx: WorkflowContext) -> WorkflowContext:
        agent = self._agents[TEAStage.TEST_EXECUTION]
        ctx.execution_results = await agent.run(ctx.automation_scripts, ctx.build_id)

        # Fix V: Trigger defect analysis for real failures only — exclude
        # pre-flight rows and environment-class failures. SUT unreachability
        # is not a defect; running RCA on it wastes LLM tokens and risks
        # false-positive Jira/Linear tickets.
        #
        # Phase G1 ordering note: cascade-aware classification needs
        # `coverage_report["sequence_integrity"]` which is computed by
        # `_run_traceability` (stage 7). At THIS stage (5) the report is
        # empty, so we run a flat first-pass classification here, then
        # re-classify in `_run_traceability` once seq_integrity exists.
        # The re-classification path collapses any naive root-cause
        # defects into cascades — operators see the right view either way.
        failures = [
            r for r in ctx.execution_results
            if r.get("status") == "FAIL"
               and r.get("automation_tool") != "preflight"
               and r.get("failure_class") != "environment"
        ]
        ctx._failures_for_classification = failures   # type: ignore[attr-defined]
        if failures and not getattr(self, "_defer_defect_intel_to_traceability", True):
            # Legacy / opt-out path: classify here without sequence_integrity.
            try:
                ctx.defects = await self._defect_agent.analyze_failures(
                    failures, sequence_integrity={},
                )
            except TypeError:
                ctx.defects = await self._defect_agent.analyze_failures(failures)

        return ctx

    async def _queue_chain_heals_from_defects(self, ctx: WorkflowContext) -> None:
        """Phase G2 + G3 auto-queue logic.

        For each defect classified as `cascade_failure` or `provider_contract_violation`
        by Phase G1, queue the corresponding PENDING_APPROVAL via the
        SelfHealingAgent (when registered). Best-effort: missing healing
        agent is silently skipped.
        """
        healing = getattr(self, "_healing_agent", None)
        if healing is None:
            return

        # Group cascades by root_cause for chain_reverify proposals.
        cascade_by_root: dict[str, list[str]] = {}
        cascade_via_var: dict[str, str] = {}
        pcvs: list[dict] = []
        for d in (ctx.defects or []):
            if not isinstance(d, dict):
                continue
            cls = d.get("defect_class")
            if cls == "cascade_failure":
                root = d.get("cascade_of")
                if root:
                    cascade_by_root.setdefault(root, []).append(d.get("test_id", ""))
                    cascade_via_var[root] = d.get("via_var") or cascade_via_var.get(root, "")
            elif cls == "provider_contract_violation":
                pcvs.append(d)

        for root_id, affected in cascade_by_root.items():
            try:
                proposal_id = await healing.propose_chain_reverify(
                    root_cause_test_id=root_id,
                    affected_tests=affected,
                    via_var=cascade_via_var.get(root_id),
                )
                ctx.events.append({
                    "type": "chain_reverify_proposed",
                    "proposal_id": proposal_id,
                    "root_cause": root_id,
                    "affected_count": len(affected),
                })
            except Exception as exc:
                logger.warning("chain_reverify proposal failed for %s: %s", root_id, exc)

        for pcv in pcvs:
            try:
                proposal_id = await healing.propose_provider_contract_heal(
                    test_id=pcv.get("test_id", ""),
                    var_name=pcv.get("var_name", ""),
                    old_jsonpath=pcv.get("expected_jsonpath", "$.id"),
                    new_jsonpath=None,   # operator picks from shape
                    provider_response_shape=pcv.get("provider_response_shape") or {},
                )
                ctx.events.append({
                    "type": "provider_contract_heal_proposed",
                    "proposal_id": proposal_id,
                    "test_id": pcv.get("test_id"),
                    "var_name": pcv.get("var_name"),
                })
            except Exception as exc:
                logger.warning("provider_contract_heal proposal failed: %s", exc)

    async def _run_evidence_collection(self, ctx: WorkflowContext) -> WorkflowContext:
        """F8-6: Dispatch through EvidenceCollectorAgent so the 9-layer BMAD-TEA
        dispatch table stays consistent. Falls back to the previous inline
        screenshot+har scan if the agent is missing (defensive).
        """
        agent = self._agents.get(TEAStage.EVIDENCE_COLLECTION)
        if agent is not None and hasattr(agent, "collect"):
            evidence = await agent.collect(ctx.execution_results)
            # Preserve the legacy `evidence_paths` shape (flat list of strings)
            # for downstream consumers (gates, reports). Agents that need the
            # richer per-artifact metadata can read `evidence_artifacts`.
            ctx.evidence_paths = [e["path"] for e in evidence if e.get("path")]
            try:
                # Stash the structured form on ctx without breaking the dataclass
                # field set — gate/report agents look at this when present.
                ctx.events.append({"type": "evidence_collected", "count": len(evidence)})
            except Exception:
                pass
            return ctx
        # Fallback (shouldn't happen — __init__ supplies a default agent)
        ctx.evidence_paths = [
            r.get("screenshot") for r in ctx.execution_results if r.get("screenshot")
        ] + [
            r.get("har_file") for r in ctx.execution_results if r.get("har_file")
        ]
        return ctx

    async def _run_traceability(self, ctx: WorkflowContext) -> WorkflowContext:
        agent = self._agents[TEAStage.TRACEABILITY_UPDATE]
        # Phase C4: pass chains harvested in Stage 2.5 so the agent ingests
        # CallChain → Endpoint → EnvVar nodes/edges before deriving DEPENDS_ON.
        chains = (ctx.discovery_result or {}).get("chains") or []
        # Resolve project_id from the first requirement (set by upstream stages)
        project_id = None
        try:
            project = ctx.requirements[0].get("project") if ctx.requirements else None
            if isinstance(project, dict):
                project_id = str(project.get("id") or "")
        except (IndexError, AttributeError, KeyError):
            pass

        ctx.coverage_report = await agent.update_graph(
            requirements=ctx.requirements,
            acceptance_criteria=ctx.acceptance_criteria,
            execution_results=ctx.execution_results,
            defects=ctx.defects,
            chains=chains,
            project_id=project_id,
        )

        # Phase F1 — produce sequence-integrity report for the gate. Pulls
        # step-level data from execution.py's _REAL_STEPS via the public
        # `get_steps()` accessor when a build_id is set (otherwise stub-mode
        # derivation from chains alone). Best-effort: gate degrades gracefully.
        try:
            steps: list[dict] = []
            if ctx.build_id:
                try:
                    from ..api.routers.execution import get_steps   # type: ignore
                    steps = get_steps(ctx.build_id)
                except (ImportError, AttributeError):
                    steps = []
            seq_integrity = await agent.compute_sequence_integrity(
                ctx.execution_results,
                project_id=project_id or "unknown",
                chains=chains,
                steps=steps,
            )
            ctx.coverage_report["sequence_integrity"] = seq_integrity
        except Exception as exc:
            logger.warning("traceability: sequence_integrity compute failed: %s", exc)
            ctx.coverage_report["sequence_integrity"] = {
                "cascade_failures": [],
                "provider_contract_violations": [],
                "unresolved_chain_starts": [],
                "param_provenance": {},
                "degraded": True,
                "degraded_reason": f"compute_failed:{type(exc).__name__}",
            }

        # Phase G1 — cascade-aware defect classification. Runs HERE (after
        # sequence_integrity is populated) rather than in _run_execution
        # because cascade attribution requires the graph. _run_execution
        # stashed the failure list on ctx._failures_for_classification.
        failures = getattr(ctx, "_failures_for_classification", []) or []
        if failures:
            seq_integrity_for_defects = ctx.coverage_report.get("sequence_integrity") or {}
            try:
                ctx.defects = await self._defect_agent.analyze_failures(
                    failures, sequence_integrity=seq_integrity_for_defects,
                )
            except TypeError:
                # Backwards-compat: defect agent without seq_integrity arg.
                ctx.defects = await self._defect_agent.analyze_failures(failures)

            # Phase G2+G3 — auto-queue chain_reverify + provider_contract_heal
            # proposals derived from cascade/PCV defect classes.
            await self._queue_chain_heals_from_defects(ctx)
        return ctx

    async def _run_nfr_validation(self, ctx: WorkflowContext) -> WorkflowContext:
        """Non-Functional Requirements validation (perf/security/a11y)."""
        nfr_results = [
            r for r in ctx.execution_results
            if r.get("test_type") in ("Performance", "Security", "Accessibility")
        ]
        ctx.coverage_report["nfr"] = {
            "performance_p95_ms": self._extract_p95(nfr_results),
            "security_findings": self._count_security_findings(nfr_results),
            "a11y_violations": self._count_a11y_violations(nfr_results),
        }
        return ctx

    async def _run_quality_gate(self, ctx: WorkflowContext) -> WorkflowContext:
        """Fix HH: iterate-until-green loop. When the gate decides FAIL and
        actionable heals are available, run the self-healing agent and
        re-execute failing tests, then re-decide. Bounded by
        ARTA_MAX_GATE_ITERATIONS (default 2) so a runaway run never loops
        forever. Each iteration re-runs ONLY the failing test_ids — not
        the full suite — so the cost is proportional to the failure count.

        Bound conditions for terminating the loop:
        1. decision ∈ {PASS, WAIVED} → done
        2. iteration count reached cap → exit BLOCKED
        3. no actionable heals (e.g. only environment failures) → exit
        """
        import os as _os
        max_iter = int(_os.environ.get("ARTA_MAX_GATE_ITERATIONS", "2"))
        agent = self._agents[TEAStage.QUALITY_GATE]
        for _iter in range(max_iter + 1):
            ctx.gate_decision = await agent.decide(
                coverage_report=ctx.coverage_report,
                risk_profiles=ctx.risk_profiles,
                defects=ctx.defects,
            )
            decision = ctx.gate_decision.get("decision")
            if decision in ("PASS", "WAIVED", "CONCERNS"):
                break
            if _iter >= max_iter:
                break
            # Find actionable failures: must have a test_id, must not be
            # an environment-class failure (those are SUT-side and won't
            # heal via test-side rewrites).
            failed = [
                r for r in (ctx.execution_results or [])
                if r.get("status") == "FAIL"
                   and r.get("failure_class") not in ("environment", "preflight")
                   and r.get("automation_tool") != "preflight"
                   and r.get("test_id")
            ]
            if not failed:
                logger.info("HH: no actionable heals (iter=%d); exiting loop", _iter)
                break
            logger.info("HH iter=%d: %d failures actionable; running self-healer + retry",
                        _iter, len(failed))
            try:
                healer = self._self_healing_agent if hasattr(self, "_self_healing_agent") else None
                if healer is not None:
                    await healer.heal_execution_failures(failed, ctx.project_id or "")
                # Re-run the failing test_ids only. The execution agent
                # supports `retry_only` if defined; otherwise just re-run
                # the full set (still bounded by max_iter).
                exec_agent = self._agents[TEAStage.TEST_EXECUTION]
                if hasattr(exec_agent, "run") and "retry_only" in exec_agent.run.__code__.co_varnames:
                    new_results = await exec_agent.run(
                        ctx.automation_scripts, ctx.build_id,
                        retry_only=[r["test_id"] for r in failed],
                    )
                else:
                    # Fallback: don't re-run full suite — too expensive.
                    # Just exit; the gate decision stays the same.
                    logger.info("HH: execution agent has no retry_only; cannot iterate")
                    break
                # Splice new results over the old failed ones (keep PASSes intact)
                old_passing = [r for r in (ctx.execution_results or [])
                               if r.get("status") == "PASS" or r.get("test_id") not in {x["test_id"] for x in failed}]
                ctx.execution_results = old_passing + new_results
            except Exception as _hh_exc:
                logger.warning("HH iteration %d failed: %s", _iter, _hh_exc)
                break
        if ctx.gate_decision.get("decision") in ("FAIL", "BLOCK"):
            ctx.status = WorkflowStatus.BLOCKED
        signals = ctx.gate_decision.get("violation_signals") or {}
        if signals:
            ctx.coverage_report.setdefault("_gate_violation_signals", {}).update(signals)
        return ctx

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _emit(self, ctx: WorkflowContext, event_type: str, payload: Any):
        event = {"workflow_id": str(ctx.workflow_id), "type": event_type, **payload}
        ctx.events.append(event)
        if self._bus is not None:
            try:
                await self._bus.publish("arta.workflow", event)
            except Exception as bus_err:
                # Non-fatal: log but don't fail the pipeline if the event bus is unavailable
                logger.warning("Event bus publish failed (non-fatal): %s", bus_err)
        logger.info("[%s] %s → %s", ctx.workflow_id, event_type, payload)

    def _extract_p95(self, results: list[dict]) -> float | None:
        durations = [r.get("p95_ms") for r in results if r.get("p95_ms")]
        return max(durations) if durations else None

    def _count_security_findings(self, results: list[dict]) -> int:
        return sum(len(r.get("security_findings", [])) for r in results)

    def _count_a11y_violations(self, results: list[dict]) -> int:
        return sum(r.get("a11y_violations", 0) for r in results)
