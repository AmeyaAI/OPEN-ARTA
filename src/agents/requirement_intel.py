"""
ARTA Requirement Intelligence Agent — TEA Layer 1: Test Strategy Architecture
Parses PRDs, user stories, API specs, and DB schemas into structured requirements
with acceptance criteria ready for ATDD test design.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..prompts.tea_prompts import REQUIREMENT_EXTRACTION, SINGLE_REQUIREMENT_EXTRACTION
from .retry_policy import LLM_RETRYABLE_EXC   # R134.C — single-source-of-truth retry tuple

log = logging.getLogger("arta.requirement_intel")


class SourceType(str, Enum):
    USER_STORY      = "user_story"
    PRD             = "prd"
    OPENAPI_SPEC    = "openapi_spec"
    DATABASE_SCHEMA = "database_schema"
    ARCHITECTURE    = "architecture_doc"
    JIRA_TICKET     = "jira_ticket"
    GITHUB_ISSUE    = "github_issue"


@dataclass
class AcceptanceCriterion:
    id: str
    statement: str
    given: str
    when: str
    then: str
    measurable_threshold: str = ""
    # Phase 1.2: structured triple {value, unit, comparator} extracted from
    # `then` / `measurable_threshold`. None when the AC is qualitative.
    # Downstream agents (DatasetRecipe, ATDD, Layer 4 generators) cite these
    # values verbatim instead of re-inferring from prose.
    measurable: dict | None = None
    test_types: list[str] = field(default_factory=list)
    covered: bool = False


@dataclass
class StructuredRequirement:
    id: str
    title: str
    description: str
    type: str                              # functional | non_functional | business_rule
    source_type: SourceType
    acceptance_criteria: list[AcceptanceCriterion]
    constraints: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    regulations: list[str] = field(default_factory=list)
    implicit_requirements: list[str] = field(default_factory=list)
    # Risk metadata (populated by StrategyArchitectAgent)
    priority: str = "P2"
    risk_score: float = 5.0
    historical_defect_rate_pct: float = 0.0
    days_since_last_change: int = 0
    external_dependencies: list[str] = field(default_factory=list)
    product_type: str = "SaaS web application"
    # Phase J2 — project record stamped by upstream loaders so Stage 2.5
    # (`_run_ui_discovery`) and the post-run chain pipeline (J1) can resolve
    # `is_api_only`, `discovery_settings`, etc. without re-fetching from DB.
    # Set by `RequirementIntelAgent.analyze` when project_id is provided.
    project: dict | None = None


class RequirementIntelAgent:
    """
    TEA Layer 1 — Requirement Intelligence Agent.

    Ingests requirements from any source and produces structured
    StructuredRequirement objects with acceptance criteria ready for
    the ATDD Designer Agent.

    Supports: Jira, GitHub Issues, Confluence PRD, OpenAPI specs,
    database schemas, raw text user stories.
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str = "claude-sonnet-4-6",
        fast_model: str = "claude-haiku-4-5-20251001",
    ):
        self._client = client
        self._model = model
        self._fast_model = fast_model
        # Thread-safe counter for auto-generating requirement IDs (itertools.count is atomic)
        self._req_counter: itertools.count = itertools.count(17)

    # ── Public API ────────────────────────────────────────────────────────────

    async def analyze(
        self,
        requirement_ids: list[str] | None = None,
        build_id: str | None = None,
        raw_documents: list[dict] | None = None,
        project: dict | None = None,
    ) -> list[dict]:
        """
        Entry point called by the Orchestrator.

        Args:
            requirement_ids: IDs to fetch from integrated trackers (Jira, GitHub)
            build_id: Current CI build — used to detect which PRs changed
            raw_documents: Inline documents [{"text": ..., "source_type": ...}]
            project: project record dict (Phase J2). When provided, every
                requirement gets `req["project"] = project` stamped so Stage
                2.5 (`_run_ui_discovery`) and the J1 post-run chain pipeline
                can resolve `is_api_only` / `discovery_settings` without a DB
                round-trip. Loaders that don't have the project record (older
                callers) pass None — downstream code degrades to skipping
                Stage 2.5 with `reason="no_project_record"`.

        Returns: list of requirement dicts ready for StrategyArchitectAgent
        """
        requirements: list[StructuredRequirement] = []

        # Process inline documents
        if raw_documents:
            import asyncio
            parsed = await asyncio.gather(
                *[
                    self.parse_document(doc["text"], doc.get("source_type", "user_story"))
                    for doc in raw_documents
                ],
                return_exceptions=True,
            )
            for result in parsed:
                if isinstance(result, list):
                    requirements.extend(result)

        # If no documents provided, return empty (in production: fetch from Jira/GitHub)
        if not requirements and requirement_ids:
            requirements = [self._stub_requirement(rid) for rid in requirement_ids]

        # Phase J2 — stamp project on every requirement so downstream stages
        # don't have to re-fetch.
        if project is not None:
            for req in requirements:
                req.project = project

        return [self._to_dict(r) for r in requirements]

    async def parse_document(
        self,
        text: str,
        source_type: str = "user_story",
    ) -> list[StructuredRequirement]:
        """
        Parse a raw document into structured requirements.
        Routes to specialized parsers based on source_type.
        """
        if source_type in SourceType.__members__.values():
            src = SourceType(source_type)
        else:
            log.warning(
                "parse_document.source_type_unknown source_type=%r — falling back to USER_STORY",
                source_type,
            )
            src = SourceType.USER_STORY

        if src == SourceType.OPENAPI_SPEC:
            reqs = await self._parse_openapi(text)
        elif src == SourceType.DATABASE_SCHEMA:
            reqs = await self._parse_db_schema(text)
        else:
            reqs = await self._parse_natural_language(text, src)

        # Fix MMM (Phase G): clarity-validate each requirement and stamp
        # warnings onto the StructuredRequirement.metadata. Operators
        # see warnings on the requirement card; ARTA still ships the
        # requirement (no hard rejection) so the user can override.
        #
        # Phase 1.1: When `no_acceptance_criteria` is among the warnings, the
        # requirement is QUARANTINED — persisted but flagged unfit for test
        # generation. Downstream layers honour this flag (tests.py guards at
        # generate_tests entry) so we never fabricate stub ACs and generate
        # tests for an AC the source never had.
        for req in reqs:
            warnings = check_requirement_clarity(req)
            meta = getattr(req, "metadata", None)
            if not isinstance(meta, dict):
                try:
                    req.metadata = {}
                    meta = req.metadata
                except Exception:
                    meta = None
            if isinstance(meta, dict):
                if warnings:
                    meta.setdefault("clarity_warnings", warnings)
                if "no_acceptance_criteria" in (warnings or []):
                    meta["unfit_for_test_generation"] = True
                    meta.setdefault(
                        "unfit_reason",
                        "no_acceptance_criteria — add at least one AC to enable test generation",
                    )
        return reqs


    async def parse_file(
        self,
        content: bytes,
        filename: str,
        source_type: str = "auto",
    ) -> list[StructuredRequirement]:
        """
        Parse a binary file into structured requirements.

        Dispatches to a format-specific extractor based on file extension,
        then passes the extracted text through the existing NL requirement parser.

        Supported formats:
          .docx  — python-docx paragraph extraction
          .xlsx  — openpyxl row/column extraction
          .pdf   — pdfplumber text extraction
          .md / .txt — direct NL parser
          .json / .yaml — schema-aware structured parse
          Gracefully degrades when optional libs are not installed.
        """
        from pathlib import Path
        ext = Path(filename).suffix.lower()

        extractors = {
            ".docx": self._extract_docx,
            ".xlsx": self._extract_xlsx,
            ".pdf":  self._extract_pdf,
            ".md":   self._extract_text,
            ".txt":  self._extract_text,
            ".json": self._extract_json_yaml,
            ".yaml": self._extract_json_yaml,
            ".yml":  self._extract_json_yaml,
        }

        extractor = extractors.get(ext, self._extract_text)
        raw_text = extractor(content, filename)

        if not raw_text.strip():
            return []

        # Determine source_type from filename if auto-detect
        if source_type == "auto":
            if ext in (".docx", ".pdf"):
                resolved_source = "prd"
            elif ext in (".xlsx",):
                resolved_source = "user_story"
            elif ext in (".json", ".yaml", ".yml"):
                resolved_source = "openapi_spec"
            else:
                resolved_source = "user_story"
        else:
            resolved_source = source_type

        return await self.parse_document(raw_text, resolved_source)

    def _extract_docx(self, content: bytes, filename: str) -> str:
        """Extract text from .docx using python-docx."""
        try:
            import io
            import docx  # python-docx
            doc = docx.Document(io.BytesIO(content))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n".join(paragraphs)
        except ImportError:
            return f"[python-docx not installed — raw bytes from {filename}]"
        except Exception as exc:
            return f"[Failed to parse {filename}: {exc}]"

    def _extract_xlsx(self, content: bytes, filename: str) -> str:
        """Extract rows from .xlsx using openpyxl; first row treated as headers."""
        try:
            import io
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return ""
            headers = [str(h or "") for h in rows[0]]
            lines: list[str] = []
            for row in rows[1:]:
                if any(cell is not None for cell in row):
                    row_dict = {headers[i]: str(row[i] or "") for i in range(min(len(headers), len(row)))}
                    lines.append("  ".join(f"{k}: {v}" for k, v in row_dict.items() if v))
            return "\n".join(lines)
        except ImportError:
            return f"[openpyxl not installed — raw bytes from {filename}]"
        except Exception as exc:
            return f"[Failed to parse {filename}: {exc}]"

    def _extract_pdf(self, content: bytes, filename: str) -> str:
        """Extract text from .pdf using pdfplumber."""
        try:
            import io
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
            return "\n".join(pages)
        except ImportError:
            return f"[pdfplumber not installed — raw bytes from {filename}]"
        except Exception as exc:
            return f"[Failed to parse {filename}: {exc}]"

    def _extract_text(self, content: bytes, filename: str) -> str:
        """Decode bytes as UTF-8 text (markdown, plain text)."""
        try:
            return content.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _extract_json_yaml(self, content: bytes, filename: str) -> str:
        """Serialize JSON/YAML content back to a string for LLM processing."""
        import json as json_mod
        text = content.decode("utf-8", errors="replace")
        # If valid JSON, pretty-print so LLM can parse it better
        try:
            data = json_mod.loads(text)
            return json_mod.dumps(data, indent=2)
        except json_mod.JSONDecodeError:
            pass
        # YAML fallback — return raw text (YAML is LLM-readable)
        return text

    async def extract_acceptance_criteria(
        self,
        requirement: dict,
    ) -> list[AcceptanceCriterion]:
        """
        Deepen an existing requirement by extracting more detailed AC.
        Useful for requirements that had vague initial criteria.
        """
        prompt = f"""\
Extract precise, testable acceptance criteria for this requirement.

REQUIREMENT:
Title: {requirement.get('title', '')}
Description: {requirement.get('description', '')}
Constraints: {', '.join(requirement.get('constraints', []))}

For each criterion, produce:
- A clear statement
- Given/When/Then structure
- Measurable threshold (if quantifiable)
- Required test types

Output JSON array of acceptance criteria. No prose.
"""
        message = await self._call_llm(
            model=self._model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        data = self._parse_json(message.content[0].text)
        if isinstance(data, list):
            return [
                ac for ac in (
                    _construct_ac_or_skip(item, i, req_id="<suggest>")
                    for i, item in enumerate(data, start=1)
                ) if ac is not None
            ]
        return []

    @staticmethod
    def compute_content_hash(
        title: str,
        description: str,
        acceptance_criteria: list[dict] | None = None,
    ) -> str:
        """
        Compute a SHA-256 content fingerprint for change detection.
        Normalises whitespace so trivial formatting changes don't trigger a diff.
        """
        parts = [
            " ".join(title.split()),
            " ".join(description.split()),
        ]
        if acceptance_criteria:
            for ac in sorted(acceptance_criteria, key=lambda a: a.get("id", "")):
                parts.append(" ".join(ac.get("statement", "").split()))
                parts.append(" ".join(ac.get("given", "").split()))
                parts.append(" ".join(ac.get("when", "").split()))
                parts.append(" ".join(ac.get("then", "").split()))
        combined = "\n".join(parts).strip().lower()
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    # ── Parsers ───────────────────────────────────────────────────────────────

    async def _parse_natural_language(
        self,
        text: str,
        source_type: SourceType,
    ) -> list[StructuredRequirement]:
        # Cap the document text — large Jira EPICS (their enriched_text bundles
        # every subtask + comment) can run to tens of KB. Feeding the whole
        # blob makes the claude_code CLI generate for >300s → CLI timeout → 0
        # ACs (observed on KCS epics: "Claude CLI timed out after 300s"). 8000
        # chars is plenty of context to extract requirement + ACs, and matches
        # the truncation this module already uses for spec/schema text.
        #
        # Prompt selection is the REAL fix for the timeout: a Jira ticket / user
        # story is a SINGLE requirement. The generic REQUIREMENT_EXTRACTION
        # prompt asks for "ALL requirements + implicit/industry-standard ones",
        # so on a rich epic the model emits a huge multi-requirement JSON (30K+
        # chars). Because the claude_code CLI ignores max_tokens, that unbounded
        # generation exceeds the 300s CLI timeout → 0 ACs. SINGLE_REQUIREMENT_
        # EXTRACTION bounds it to ONE requirement + 3-6 measurable ACs so
        # extraction stays fast and correct for ANY SUT's Jira import.
        if source_type in (SourceType.JIRA_TICKET, SourceType.USER_STORY):
            prompt = SINGLE_REQUIREMENT_EXTRACTION.format(document_text=text[:8000])
        else:
            prompt = REQUIREMENT_EXTRACTION.format(document_text=text[:8000])

        # R215.T (same rationale as strategy_architect / defect_intel): this is
        # a single-shot STRUCTURED-JSON extraction, but the prompt's "extract
        # the requirements" framing makes Claude Code attempt tool calls with
        # tools available → burns the --max-turns budget across `--continue`
        # turns → slow/timeout. `--tools none` lets it answer in one turn.
        # Guarded to the CLI transport — the SDK paths reject the kwarg.
        _cli_extra: dict = {}
        if type(getattr(self, "_client", None)).__name__ == "ClaudeCLIClient":
            _cli_extra["disable_tools"] = True

        message = await self._call_llm(
            model=self._model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            **_cli_extra,
        )

        raw = self._parse_json(message.content[0].text)
        if not isinstance(raw, list):
            raw = [raw]

        requirements = []
        for item in raw:
            req_id = item.get("id") or f"REQ-{next(self._req_counter):03d}"
            acs = [
                ac_obj for ac_obj in (
                    _construct_ac_or_skip(ac, i, req_id=req_id)
                    for i, ac in enumerate(item.get("acceptance_criteria", []), 1)
                ) if ac_obj is not None
            ]
            requirements.append(StructuredRequirement(
                id=req_id,
                title=item.get("title", "Untitled"),
                description=item.get("description", ""),
                type=item.get("type", "functional"),
                source_type=source_type,
                acceptance_criteria=acs,
                constraints=item.get("constraints", []),
                entities=item.get("entities", []),
                dependencies=item.get("dependencies", []),
                regulations=item.get("regulations", []),
                implicit_requirements=item.get("implicit_requirements", []),
                external_dependencies=item.get("dependencies", []),
            ))
        return requirements

    async def _parse_openapi(self, spec_text: str) -> list[StructuredRequirement]:
        """
        Convert OpenAPI spec operations into test requirements.
        Each endpoint operation → one requirement with AC per response code.
        """
        prompt = f"""\
You are a TEA Requirement Analyst. Convert this OpenAPI specification into
testable requirements. Each endpoint operation becomes a requirement.

API SPEC:
{spec_text[:8000]}  <!-- truncated for token limits -->

For each endpoint operation, generate a requirement with:
- One AC per documented response code (200, 400, 401, 404, 422, 500)
- AC for schema validation (required fields, types, formats)
- AC for authentication/authorization
- AC for rate limiting (if documented)
- AC for performance (if x-response-time extension present)

Output same JSON format as REQUIREMENT_EXTRACTION. No prose.
"""
        message = await self._call_llm(
            model=self._model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = self._parse_json(message.content[0].text)
        return self._build_from_raw(raw, SourceType.OPENAPI_SPEC)

    async def _parse_db_schema(self, schema_text: str) -> list[StructuredRequirement]:
        """
        Convert DB schema into data integrity test requirements.
        Constraints, FKs, unique indexes → requirements.
        """
        prompt = f"""\
You are a TEA Requirement Analyst. Derive testable data integrity requirements
from this database schema.

SCHEMA:
{schema_text[:6000]}

Generate requirements covering:
- Unique constraint enforcement
- NOT NULL constraint enforcement
- Foreign key referential integrity
- Check constraint validation
- Cascade delete/update behavior
- Index performance requirements

Output same JSON format as REQUIREMENT_EXTRACTION. No prose.
"""
        message = await self._call_llm(
            model=self._fast_model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = self._parse_json(message.content[0].text)
        return self._build_from_raw(raw, SourceType.DATABASE_SCHEMA)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @retry(
        # R134.C — extended via shared LLM_RETRYABLE_EXC tuple. Includes
        # anthropic.* SDK errors, RuntimeError (ClaudeCLI/Ollama subprocess
        # failures per F5-4), and httpx transient network errors (ConnectError,
        # ReadTimeout, RemoteProtocolError, etc.).
        retry=retry_if_exception_type(LLM_RETRYABLE_EXC),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _call_llm(self, **kwargs):
        """Wrapper around messages.create() with exponential-backoff retry + circuit breaker.

        F5-4: Routes through the same circuit breaker every other agent uses
        so a degraded LLM provider fails fast instead of looping for 90s.
        """
        from .circuit_breaker import get_breaker
        provider_name = getattr(self._client, "provider", None) or type(self._client).__name__
        breaker = get_breaker(str(provider_name))
        return await breaker.call(self._client.messages.create, **kwargs)

    def _build_from_raw(
        self,
        raw: Any,
        source_type: SourceType,
    ) -> list[StructuredRequirement]:
        if not isinstance(raw, list):
            raw = [raw] if isinstance(raw, dict) else []
        results = []
        for item in raw:
            req_id = item.get("id") or f"REQ-{next(self._req_counter):03d}"
            acs = [
                ac_obj for ac_obj in (
                    _construct_ac_or_skip(ac, i, req_id=req_id, default_test_types=["API"])
                    for i, ac in enumerate(item.get("acceptance_criteria", []), 1)
                ) if ac_obj is not None
            ]
            results.append(StructuredRequirement(
                id=req_id,
                title=item.get("title", "Untitled"),
                description=item.get("description", ""),
                type=item.get("type", "functional"),
                source_type=source_type,
                acceptance_criteria=acs,
                constraints=item.get("constraints", []),
                entities=item.get("entities", []),
            ))
        return results

    def _parse_json(self, text: str) -> Any:
        """R134.D — delegates to shared SSoT extractor.

        NOTE: this method (and its surrounding block of "private" methods
        starting at line 419) is parsed inside the module-level
        `check_requirement_clarity` function due to a historical indentation
        accident — the live RequirementIntelAgent class ends at line 213.
        The fix-up here keeps the dead-code contract consistent with the
        SSoT extractor in case a future refactor revives this block."""
        from .json_extract import extract_json_from_llm_output
        return extract_json_from_llm_output(text, default=[])

    def _stub_requirement(self, req_id: str) -> StructuredRequirement:
        """Fallback stub when fetching from Jira/GitHub is not configured."""
        return StructuredRequirement(
            id=req_id,
            title=f"Requirement {req_id}",
            description="Fetched from external tracker (stub — configure Jira/GitHub integration)",
            type="functional",
            source_type=SourceType.JIRA_TICKET,
            acceptance_criteria=[
                AcceptanceCriterion(
                    id=f"{req_id}-AC-001",
                    statement="System behaves as documented",
                    given="the system is operational",
                    when="the feature is exercised",
                    then="it behaves according to specification",
                )
            ],
        )

    def _to_dict(self, req: StructuredRequirement) -> dict:
        return {
            "id": req.id,
            "title": req.title,
            "description": req.description,
            "type": req.type,
            "source_type": req.source_type.value,
            "acceptance_criteria": [
                {
                    "id": ac.id,
                    "statement": ac.statement,
                    "given": ac.given,
                    "when": ac.when,
                    "then": ac.then,
                    "measurable_threshold": ac.measurable_threshold,
                    "measurable": ac.measurable,  # Phase 1.2 — structured triple, may be None
                    "test_types": ac.test_types,
                    "covered": ac.covered,
                }
                for ac in req.acceptance_criteria
            ],
            "constraints": req.constraints,
            "entities": req.entities,
            "dependencies": req.dependencies,
            "regulations": req.regulations,
            "implicit_requirements": req.implicit_requirements,
            "priority": req.priority,
            "risk_score": req.risk_score,
            "historical_defect_rate_pct": req.historical_defect_rate_pct,
            "days_since_last_change": req.days_since_last_change,
            "external_dependencies": req.external_dependencies,
            "product_type": req.product_type,
            # Phase J2 — round-trip the project record through the dict form
            # so downstream agents (atdd_designer, automation_engineer,
            # discovery_executor) can read it without an extra DB call.
            "project": req.project,
        }



# ── R178: module-level requirement helpers + regex constants, MOVED out of
# the class body. They had been inserted mid-class (between parse_document and
# parse_file), de-nesting ~12 methods (parse_file, _parse_natural_language,
# extract_acceptance_criteria, _parse_openapi/_db_schema, _call_llm, ...) as
# unreachable nested defs inside check_requirement_clarity → parse_document()
# raised AttributeError('_parse_natural_language') for ALL natural-language
# requirement extraction. Called as module functions, so module scope is correct. ──
# Phase 1.2: Deterministic regex extractor for structured measurable thresholds.
# Pulls a `{value, unit, comparator}` triple from prose like:
#   "response within 500ms"        → {500, "ms", "<="}
#   "no more than 200ms"           → {200, "ms", "<="}
#   "at least 99.9% uptime"        → {99.9, "%", ">="}
#   "magnitude_pct of 12.5"        → {12.5, "%", "~"}    (≈ tolerance, recipe-flagged)
#   "metric == 'sales'"            → {"sales", null, "=="}
# Returns None when no triple is recognisable (qualitative AC).
import re as _re_pm

_UNIT_ALT = r"(?:ms|milliseconds?|s|seconds?|m|minutes?|h|hours?|%|percent|requests?/s|rps|qps|MB|GB|kb|KB)"
_MEASURABLE_PATTERNS: list[tuple[Any, str]] = [
    # comparator + numeric + unit (unit is optional but greedy when present;
    # trailing context just needs to be non-digit so we don't consume into
    # another number).
    (_re_pm.compile(rf"\b(?:within|at most|at-most|no more than|less than|under|max(?:imum)?|below|<=)\s*(\d+(?:\.\d+)?)\s*({_UNIT_ALT})?(?=\W|$)", _re_pm.IGNORECASE), "<="),
    (_re_pm.compile(rf"\b(?:at least|>=|min(?:imum)?|over|above|more than)\s*(\d+(?:\.\d+)?)\s*({_UNIT_ALT})?(?=\W|$)", _re_pm.IGNORECASE), ">="),
    (_re_pm.compile(rf"\b(?:within|of|approximately|approx|~|around)\s*(\d+(?:\.\d+)?)\s*({_UNIT_ALT})?(?=\W|$)", _re_pm.IGNORECASE), "~"),
    # bare numeric + unit (lower priority — only when none of the comparator forms hit)
    (_re_pm.compile(rf"\b(\d+(?:\.\d+)?)\s*({_UNIT_ALT})(?=\W|$)", _re_pm.IGNORECASE), "<="),
]
_UNIT_NORMALISE = {
    "milliseconds": "ms", "millisecond": "ms",
    "seconds": "s", "second": "s",
    "minutes": "m", "minute": "m",
    "hours": "h", "hour": "h",
    "percent": "%",
    "requests/s": "rps", "request/s": "rps", "qps": "rps",
}


def _extract_measurable_triple(*texts: str) -> dict | None:
    """Best-effort regex extraction. Returns None when no numeric/qualitative
    threshold is found. The recipe agent (Phase 1.6) gets this triple as
    structured input and can verify the LLM's expected_outputs against it.
    """
    blob = " ".join(t for t in texts if t)
    if not blob:
        return None
    for pat, comp in _MEASURABLE_PATTERNS:
        m = pat.search(blob)
        if not m:
            continue
        try:
            value = float(m.group(1))
            if value.is_integer():
                value = int(value)
        except (ValueError, IndexError):
            continue
        unit = (m.group(2) or "").lower() if m.lastindex and m.lastindex >= 2 else ""
        unit = _UNIT_NORMALISE.get(unit, unit) or None
        return {"value": value, "unit": unit, "comparator": comp}

    # Qualitative equality: `metric == "sales"`, `direction = "up"`
    eq = _re_pm.search(r"""\b([a-z_][a-z0-9_]*)\s*(?:==|=|should\s+be)\s*['"]?([a-z][a-z_0-9 ]{0,40})['"]?""",
                      blob, _re_pm.IGNORECASE)
    if eq:
        prop = eq.group(1).strip().lower()
        val = eq.group(2).strip().strip('"').strip("'")
        # Skip prose like "should be fast" — heuristic: drop matches where
        # the value is a vague modifier or contains spaces > 2 words.
        if val and val not in {"fast", "quick", "good", "nice"} and len(val.split()) <= 2:
            return {"value": val, "unit": None, "comparator": "==", "property": prop}
    return None


_AC_REQUIRED_FIELDS = ("statement", "given", "when", "then")


def _construct_ac_or_skip(
    ac_dict: dict,
    idx: int,
    *,
    req_id: str = "?",
    default_id_prefix: str = "AC",
    default_test_types: list[str] | None = None,
) -> AcceptanceCriterion | None:
    """Phase A1: validate required AC fields before construction.

    Why: an AC missing `statement`/`given`/`when`/`then` produces a `None`
    measurable triple and a Gherkin scaffold the LLM has to invent — exactly
    the upstream-divergence problem Phase 1 was built to fix.

    How: log + skip malformed ACs. If all ACs in a requirement are skipped,
    downstream `check_requirement_clarity` flags `no_acceptance_criteria`
    (existing path) and the requirement is marked `unfit_for_test_generation`.
    """
    missing = [f for f in _AC_REQUIRED_FIELDS if not (ac_dict.get(f) or "").strip()]
    if missing:
        log.warning(
            "ac.malformed req=%s ac_idx=%d missing=%s id=%s",
            req_id, idx, missing, ac_dict.get("id"),
        )
        return None
    return AcceptanceCriterion(
        id=ac_dict.get("id", f"{default_id_prefix}-{idx:03d}"),
        statement=ac_dict["statement"],
        given=ac_dict["given"],
        when=ac_dict["when"],
        then=ac_dict["then"],
        measurable_threshold=ac_dict.get("measurable_threshold", ""),
        measurable=_extract_measurable_triple(
            ac_dict["then"],
            ac_dict.get("measurable_threshold", ""),
            ac_dict["statement"],
        ),
        test_types=ac_dict.get("test_types", default_test_types or ["UI"]),
    )


def check_requirement_clarity(req) -> list[str]:
    """Fix MMM: deterministic clarity check for requirements.

    A requirement is *testable* when it includes:
    1. A measurable outcome (number, percentage, threshold, time)
    2. A stated input/trigger (when/given/upon)
    3. At least one boundary or SLA marker

    Returns a list of warning strings (empty when clear).
    """
    import re as _re
    warnings: list[str] = []
    title = (getattr(req, "title", "") or "").strip()
    desc = (getattr(req, "description", "") or "").strip()
    full = f"{title}\n{desc}".lower()
    if not full:
        warnings.append("requirement_empty")
        return warnings
    # 1. Measurable outcome — look for numbers, percentages, thresholds
    has_measurable = bool(_re.search(r"\b\d+(\.\d+)?\s*(%|ms|s|sec|second|minute|hour|mb|gb|count|item|user|request)\b", full))
    if not has_measurable:
        warnings.append("no_measurable_outcome")
    # 2. Trigger / input — when/given/upon/on/after
    has_trigger = bool(_re.search(r"\b(when|given|upon|on|after|once|if)\b", full))
    if not has_trigger:
        warnings.append("no_explicit_trigger")
    # 3. SLA / boundary — within/at-most/less-than/no-more-than
    has_boundary = bool(_re.search(r"\b(within|at most|at-most|less than|no more than|maximum|max|min)\b", full))
    if not has_boundary and not has_measurable:
        warnings.append("no_boundary_or_sla")
    # 4. Anti-patterns — vague modifiers
    if _re.search(r"\b(should be fast|should be quick|works well|user-friendly|good|nice|seamless)\b", full):
        warnings.append("vague_quality_modifier")
    # 5. Acceptance criteria presence
    acs = getattr(req, "acceptance_criteria", None) or []
    if not acs:
        warnings.append("no_acceptance_criteria")
    return warnings
