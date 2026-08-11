"""F8-2: Pluggable teacher models for the ARTA upskill pipeline.

Before this module, `upskill_pipeline.py` hard-coded
`gemini/gemini-3.1-pro-preview` as the only teacher. To experiment with
a different teacher (Claude, OpenAI, Mistral, etc.) you had to edit the
script — defeating the goal of reproducible runs.

This module exposes a small Protocol and two concrete implementations:

  - GeminiTeacher  — wraps `gemini/gemini-3.1-pro-preview` (default;
                     preserves the historical behaviour that produced
                     `arta-qwen-pro:latest`).
  - ClaudeTeacher  — wraps Anthropic Claude (default model
                     `claude-opus-4-7`); allows training a Claude-taught
                     `arta-qwen-pro` variant for comparison.

Both go through LiteLLM, so adding a new teacher is one subclass + one
factory entry. No fork of the pipeline needed.

Usage:
    from scripts.upskill.teachers import make_teacher
    teacher = make_teacher("claude")  # or "gemini"
    result = await teacher.critique_and_rewrite(student_output, req_context)
    # result = {"critique": str, "score": int, "improved_gherkin": str,
    #           "teacher_model": str}
"""
from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Literal

# F9-7: litellm import is deferred into _call() so unit tests + tooling that
# only need the factory/adapter shape don't pay the heavy import cost (and
# don't fail in venvs with an aiohttp/litellm version skew).

log = logging.getLogger("arta.upskill.teachers")


# Critique prompt is shared across teachers — moves the rubric out of the
# pipeline script so all teachers grade against the same yardstick.
CRITIQUE_PROMPT_TEMPLATE = """\
As a Principal Test Architect, evaluate the following Gherkin scenario \
drafted by a junior engineer.

Requirement Context:
{req_context}

Junior Draft:
{student_response}

Your task:
1. Identify any missing elements (concrete test data, strict thresholds,
   omitted failure handles).
2. Rewrite the scenario to be enterprise-grade, comprehensive, and
   perfectly formatted.

Provide your output as JSON:
{{
  "critique": "Brief 1-sentence evaluation",
  "score": 1,
  "improved_gherkin": "Feature: ...\\n  Scenario: ..."
}}
"""


# Corpus-mode critique — used when the student is producing TypeScript
# Playwright/Newman code (NOT Gherkin) for fine-tuning the model on
# K2/L13/L15 patterns. Without this template, the teacher graded TS code
# against a Gherkin rubric and returned Gherkin output (wrong shape for
# training data targeting code generation).
CORPUS_CRITIQUE_PROMPT_TEMPLATE = """\
As a Principal Test Architect, evaluate the following test SCRIPT \
(TypeScript / JSON / JavaScript — NOT Gherkin) drafted by a junior engineer.

The script previously failed an automated validator. The validator error \
and the original requirement context are below.

Validator + Requirement Context:
{req_context}

Junior Draft (raw script source):
{student_response}

Your task:
1. Identify why the validator rejected this script.
2. Rewrite the script as a CORRECT, RUNNABLE version that:
   - Satisfies the validator rule cited (K2 / L13 / L15 / F1 / A11 / PAGE_FIXTURE)
   - Preserves the original test intent (same coverage, same assertions,
     same endpoint targets)
   - Uses the exact same language/format as the original (TypeScript for
     Playwright, Postman JSON for Newman, etc.)

K2 specifically: every `await response.json()` MUST live inside a
try/catch that throws on parse failure (SUT may return HTML on
auth-redirect).

L13 specifically: for analytics tests, replace `toHaveText('<numeric>')`
with `toContainText('<numeric>')` OR `waitForResponse + jsonpath`.

L15 specifically: never hardcode prod URLs; use process.env.BASE_URL.

PAGE_FIXTURE specifically: every test/beforeEach/afterEach callback that
references `page.X` must destructure `{{ page }}` from its fixture argument.

Provide your output as a single JSON object:
{{
  "critique": "1-2 sentence diagnosis of the validator failure cause",
  "score": 1,
  "improved_gherkin": "<the COMPLETE corrected script source — TypeScript / JSON / JS — pasteable as a file>"
}}

The field is named `improved_gherkin` for back-compat with the upskill \
pipeline schema, but the value MUST be the corrected SCRIPT, not Gherkin. \
Output the raw code with no surrounding markdown fences. Score 1-10 where \
10 = perfect drop-in fix, 1 = no improvement.
"""


class TeacherAdapter(ABC):
    """Common shape for any teacher model used in the upskill pipeline."""

    name: str = "abstract"
    default_model: str = ""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or self.default_model

    @abstractmethod
    async def critique_and_rewrite(
        self,
        student_response: str,
        req_context: str,
        mode: Literal["gherkin", "corpus"] = "gherkin",
    ) -> dict:
        """Grade and rewrite the student's output.

        Args:
            student_response: the student LLM's draft to be graded.
            req_context:      the requirement context (and validator error
                              for corpus mode).
            mode:             "gherkin" for the original training pairs,
                              "corpus" for TS/JSON code training pairs
                              from `--seed-from-corpus`. Selects the
                              critique template + sets expectations on
                              `improved_gherkin` field shape.

        Returns a dict with keys:
          - critique:        short eval string
          - score:           1-10 integer (LLM-graded)
          - improved_gherkin: rewritten scenario text (mode='gherkin') OR
                              rewritten script source (mode='corpus' —
                              field name kept for back-compat)
          - teacher_model:   the LiteLLM model id used (for dataset provenance)
        """
        raise NotImplementedError

    @staticmethod
    def _select_template(mode: str) -> str:
        if mode == "corpus":
            return CORPUS_CRITIQUE_PROMPT_TEMPLATE
        return CRITIQUE_PROMPT_TEMPLATE

    # Common implementation — both Gemini and Claude flow through LiteLLM
    # with a JSON-object response format. Subclasses override only what
    # differs (env-var name, default model id).
    async def _call(self, prompt: str, *, temperature: float, json_mode: bool) -> str:
        # F9-7: lazy import — keeps unit tests + offline tooling from paying
        # litellm's heavy import (which can also blow up on aiohttp version skew).
        import litellm  # noqa: PLC0415
        kwargs: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = await litellm.acompletion(**kwargs)
        return response.choices[0].message.content


class GeminiTeacher(TeacherAdapter):
    """Default teacher — Gemini 3.x Pro (the model that produced arta-qwen-pro)."""

    name = "gemini"
    default_model = "gemini/gemini-3.1-pro-preview"

    def __init__(self, model: str | None = None) -> None:
        super().__init__(model)
        if not os.environ.get("GEMINI_API_KEY"):
            log.warning("GEMINI_API_KEY unset — GeminiTeacher calls will fail")

    async def critique_and_rewrite(
        self,
        student_response: str,
        req_context: str,
        mode: Literal["gherkin", "corpus"] = "gherkin",
    ) -> dict:
        prompt = self._select_template(mode).format(
            req_context=req_context, student_response=student_response
        )
        try:
            content = await self._call(prompt, temperature=1.0, json_mode=True)
            parsed = json.loads(content, strict=False)
        except Exception as exc:
            log.error("GeminiTeacher critique failed: %s", exc)
            return {"critique": f"ERROR: {exc}", "score": 0,
                    "improved_gherkin": "", "teacher_model": self.model}
        parsed["teacher_model"] = self.model
        return parsed


class ClaudeTeacher(TeacherAdapter):
    """Anthropic Claude teacher — opt-in via `--teacher claude` (or env)."""

    name = "claude"
    # claude-opus-4-7 is the strongest current Anthropic model — matches
    # Gemini 3.x Pro's tier so the comparison is fair. Override with
    # ARTA_UPSKILL_TEACHER_MODEL for cheaper experimentation.
    default_model = "claude-opus-4-7"

    def __init__(self, model: str | None = None) -> None:
        super().__init__(model)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log.warning("ANTHROPIC_API_KEY unset — ClaudeTeacher calls will fail")

    async def critique_and_rewrite(
        self,
        student_response: str,
        req_context: str,
        mode: Literal["gherkin", "corpus"] = "gherkin",
    ) -> dict:
        prompt = self._select_template(mode).format(
            req_context=req_context, student_response=student_response
        )
        # Claude doesn't natively honour response_format=json_object via
        # LiteLLM — request JSON in the prompt and parse defensively.
        prompt += "\n\nReturn ONLY the JSON object — no prose, no markdown fences."
        try:
            content = await self._call(prompt, temperature=0.7, json_mode=False)
            # Strip markdown fences if Claude wraps the JSON
            stripped = content.strip()
            if stripped.startswith("```"):
                stripped = stripped.split("```", 2)[1]
                if stripped.startswith("json"):
                    stripped = stripped[4:]
                stripped = stripped.strip()
            parsed = json.loads(stripped, strict=False)
        except Exception as exc:
            log.error("ClaudeTeacher critique failed: %s", exc)
            return {"critique": f"ERROR: {exc}", "score": 0,
                    "improved_gherkin": "", "teacher_model": self.model}
        parsed["teacher_model"] = self.model
        return parsed


class ClaudeCLITeacher(TeacherAdapter):
    """Claude Code CLI teacher — uses `claude --print` subprocess instead of
    the Anthropic HTTP API.

    Why: ARTA's host already has Claude Code CLI installed with valid OAuth
    tokens (managed by `~/.claude/.credentials.json`). This teacher reuses
    those tokens — no `ANTHROPIC_API_KEY` required, no per-call billing
    against the API budget. Same training quality as `ClaudeTeacher` since
    both ultimately reach the same model on Anthropic's infrastructure.

    Use when:
      - Local dev/iteration on the upskill pipeline
      - You have Claude Code CLI auth but no separate API key
      - You want to avoid double-counting tokens against API quota when
        the CLI's subscription already covers the work

    Selected via `--teacher claude-cli` or `ARTA_UPSKILL_TEACHER=claude-cli`.
    """

    name = "claude-cli"
    default_model = "claude-opus-4-7"

    def __init__(self, model: str | None = None) -> None:
        super().__init__(model)
        # Late import — claude_cli_client lives under src/agents which imports
        # heavy deps (anthropic SDK) we don't need to load eagerly here.
        self._client = None

    def _get_client(self):
        """Lazy-init the underlying ClaudeCLIClient."""
        if self._client is None:
            import sys as _sys
            from pathlib import Path as _Path
            # Make `src` importable — script context isn't always at repo root.
            _root = _Path(__file__).resolve().parents[2]
            if str(_root) not in _sys.path:
                _sys.path.insert(0, str(_root))
            from src.agents.claude_cli_client import ClaudeCLIClient
            cli_path = os.environ.get("CLAUDE_CLI_PATH", "claude")
            self._client = ClaudeCLIClient(cli_path=cli_path)
        return self._client

    async def _call(self, prompt: str, *, temperature: float, json_mode: bool) -> str:
        """Override: route through ClaudeCLIClient rather than LiteLLM.

        The CLI doesn't expose a `temperature` knob (it inherits Claude
        Code's defaults), and `response_format=json_object` isn't a CLI
        flag. We append a JSON-only directive in the prompt instead.
        """
        client = self._get_client()
        if json_mode:
            prompt = prompt + "\n\nReturn ONLY a valid JSON object — no prose, no markdown fences."
        # Use a generous max_tokens — corpus-mode TS specs run long.
        resp = await client.messages.create(
            model=self.model,
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
            timeout=600.0,
        )
        # ClaudeCLIClient returns a _CLIResponse with `.content[0].text`.
        return resp.content[0].text

    async def critique_and_rewrite(
        self,
        student_response: str,
        req_context: str,
        mode: Literal["gherkin", "corpus"] = "gherkin",
    ) -> dict:
        prompt = self._select_template(mode).format(
            req_context=req_context, student_response=student_response
        )
        try:
            content = await self._call(prompt, temperature=0.7, json_mode=True)
            stripped = content.strip()
            # CLI sometimes wraps in markdown fences — strip defensively.
            if stripped.startswith("```"):
                stripped = stripped.split("```", 2)[1]
                if stripped.startswith("json"):
                    stripped = stripped[4:]
                stripped = stripped.strip()
            parsed = json.loads(stripped, strict=False)
        except Exception as exc:
            log.error("ClaudeCLITeacher critique failed: %s", exc)
            return {"critique": f"ERROR: {exc}", "score": 0,
                    "improved_gherkin": "", "teacher_model": self.model}
        parsed["teacher_model"] = self.model
        return parsed


_TEACHERS: dict[str, type[TeacherAdapter]] = {
    "gemini": GeminiTeacher,
    "claude": ClaudeTeacher,
    "claude-cli": ClaudeCLITeacher,
}


def make_teacher(name: str | None = None, model: str | None = None) -> TeacherAdapter:
    """Resolve a teacher by name (defaults to Gemini for backward compat).

    Resolution order:
      1. explicit `name` arg
      2. ARTA_UPSKILL_TEACHER env var
      3. "gemini" (preserves the historical default)

    Model override order: explicit `model` arg → ARTA_UPSKILL_TEACHER_MODEL → class default.
    """
    chosen = (name or os.environ.get("ARTA_UPSKILL_TEACHER", "gemini")).lower()
    cls = _TEACHERS.get(chosen)
    if cls is None:
        raise ValueError(
            f"Unknown teacher '{chosen}'. Available: {sorted(_TEACHERS)}"
        )
    model = model or os.environ.get("ARTA_UPSKILL_TEACHER_MODEL")
    return cls(model=model)


def available_teachers() -> list[str]:
    return sorted(_TEACHERS.keys())
