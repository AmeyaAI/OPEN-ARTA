"""R127.B — Requirement decomposition into per-scenario sub-requirements.

Why this module exists
======================

R126 live evidence showed 0/21 PW main specs landed when the Ollama
qwen-pro 32B model tried to emit TypeScript for dense requirements (18+
Gherkin scenarios per req). The model's instruction-following degrades when
input prompt + dense narrative + DOM catalog + captured-endpoints collectively
exceed ~50% of the 28K Ollama context budget.

R127.B adapts INPUT complexity to fit the model's reliable envelope — without
violating the "small Ollama on-prem non-negotiable" directive. The
decomposition is strictly PW-internal and Pytest-internal:

  • Parent requirement registry, traceability graph edges, and dispatch-side
    R102.A/C BLOCK identity all stay anchored on the parent requirement id.
  • Sub-requirements are gen-time-only routing tokens; only persist in
    `_gen_metrics.r127_b_part_id` for telemetry purposes.
  • Sub PW specs MERGE into the canonical req_am_NNN.spec.ts parent at write
    time (preserves R-PWProjectFilter regex + Fix FF quarantine logic).
  • The trigger gate is provider-aware: only fires for Ollama clients with
    scenario_count >= 6 (dense reqs).

Anti-fallback contract: when decomposition does NOT trigger (anthropic
provider OR scenario_count below threshold), the existing single-call gen
path runs unchanged — zero behavioral regression.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .sanitize import sanitize_req_id  # R134.H — sanitize_req_id SSoT

log = logging.getLogger(__name__)


# Tools that benefit from decomposition. Newman is already R118.F-chunked
# (handles dense reqs without exceeding qwen-pro's envelope); k6/axe/ZAP are
# template-driven and small. PW + Pytest are the LLM-heavy generators where
# R126 evidence showed the ceiling.
_R127_B_SUPPORTED_TOOLS: frozenset[str] = frozenset({"playwright", "pytest"})

# Decomposition triggers when scenario_count >= this OR estimated prompt
# typical dense reqs; the prompt-size threshold catches dense free-form
# Gherkin without explicit Scenario: blocks.
_R127_B_THRESHOLD_SCENARIO_COUNT: int = 6
_R127_B_THRESHOLD_PROMPT_TOKENS:  int = 12_000

# R136.A — lower thresholds for small-Ollama providers. qwen3:8b-class
# models hit R125.L's output-token ceiling on dense reqs even after
# R127.B decomposition at the default 6-scenario threshold. Lowering to
# 4 scenarios / 8K prompt-tokens decomposes earlier so each LLM call
# sees less context + emits less output → fits more reliably under the
# 23401-token hard ceiling. Larger Ollama models (qwen3:32b) keep the
# default thresholds; Anthropic/Claude paths are unaffected (R127.B is
# Ollama-only via `is_ollama_provider`).
_R136_A_SMALL_OLLAMA_THRESHOLD_SCENARIO_COUNT: int = 4
_R136_A_SMALL_OLLAMA_THRESHOLD_PROMPT_TOKENS:  int = 8_000

# Model-name substrings that identify "small Ollama" variants — those
# that empirically can't carry dense reqs without aggressive
# decomposition. Order doesn't matter (substring-match).
_R136_A_SMALL_OLLAMA_MODEL_PATTERNS: tuple[str, ...] = (
    "qwen3:8b",
    "qwen3:14b",
    "phi4:14b",
    "phi3:14b",
    "phi3:mini",
    "phi3:medium",
    "mistral:7b",
    "mistral:latest",
    "llama3:8b",
    "llama3.1:8b",
    "llama3.2:3b",
    "gemma:7b",
    "gemma2:9b",
)


def is_small_ollama_model(model_tag: str | None) -> bool:
    """R136.A — substring-match the model name against known small-Ollama
    variants. Used by `should_decompose` to lower its thresholds when the
    LLM client is backed by a small-Ollama model.

    Defensively case-insensitive. Returns False for empty/None tags so the
    default thresholds apply (backward compat for callers that don't pass
    the model tag).
    """
    if not model_tag:
        return False
    name_lower = str(model_tag).lower()
    return any(p in name_lower for p in _R136_A_SMALL_OLLAMA_MODEL_PATTERNS)

# Regex for splitting Gherkin on Scenario boundaries. Handles `Scenario:`
# and `Scenario Outline:`; tolerates trailing tags and indentation. Anchored
# at start of line so descriptive prose inside steps doesn't false-match.
_SCENARIO_HEADER_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<header>Scenario(?:\s+Outline)?:\s*.*)$",
    re.MULTILINE,
)
_BACKGROUND_HEADER_RE = re.compile(
    r"^[ \t]*Background:\s*.*$",
    re.MULTILINE,
)
_FEATURE_HEADER_RE = re.compile(
    r"^[ \t]*Feature:\s*.*$",
    re.MULTILINE,
)


@dataclass
class SubRequirement:
    """A single scenario carved from a parent requirement's Gherkin text.

    Carries only the gen-time-routing tokens (parent_id + part_id +
    scenario gherkin). The parent risk dict and dataset_recipe are
    inherited by reference — sub-reqs do NOT regenerate either.
    """
    parent_id:        str
    part_id:          str       # e.g. "SCN-01"
    part_index:       int       # 1-based for operator readability
    part_total:       int
    scenario_title:   str
    gherkin_text:     str       # shared Background + just this scenario


def is_ollama_provider(provider_tag: str | None) -> bool:
    """R127.B trigger gate — only Ollama clients hit the qwen-pro ceiling."""
    return bool(provider_tag and "ollama" in provider_tag.lower())


def count_scenarios(gherkin_text: str) -> int:
    """Count `Scenario:` / `Scenario Outline:` blocks in a Gherkin string."""
    if not gherkin_text:
        return 0
    return len(_SCENARIO_HEADER_RE.findall(gherkin_text))


def should_decompose(
    requirement: dict,
    gherkin_text: str,
    tool_name: str,
    provider_tag: str | None,
    *,
    scenario_threshold: int | None = None,
    prompt_token_threshold: int | None = None,
    model_tag: str | None = None,
) -> bool:
    """R127.B decision gate. Returns True only when ALL of:

    1. *tool_name* is in `_R127_B_SUPPORTED_TOOLS` (PW or Pytest)
    2. *provider_tag* indicates an Ollama client
    3. scenario_count >= *scenario_threshold* OR estimated prompt tokens
       >= *prompt_token_threshold*

    R136.A — when *model_tag* identifies a small-Ollama variant (qwen3:8b,
    phi4:14b, mistral:7b, etc.) and the caller did NOT pass explicit
    thresholds, decompose at the LOWER small-Ollama thresholds (4 scenarios
    / 8K tokens vs 6 / 12K). Larger Ollama models + missing model_tag both
    keep the original R127.B defaults (backward compat).

    Explicit *scenario_threshold* / *prompt_token_threshold* arguments
    always win over the R136.A model-driven defaults.
    """
    if tool_name not in _R127_B_SUPPORTED_TOOLS:
        return False
    if not is_ollama_provider(provider_tag):
        return False
    # R136.A — pick threshold defaults based on model tier when caller
    # didn't supply explicit overrides. Small-Ollama models get the
    # lower thresholds; other paths get the default R127.B values.
    if is_small_ollama_model(model_tag):
        _default_scn = _R136_A_SMALL_OLLAMA_THRESHOLD_SCENARIO_COUNT
        _default_tok = _R136_A_SMALL_OLLAMA_THRESHOLD_PROMPT_TOKENS
    else:
        _default_scn = _R127_B_THRESHOLD_SCENARIO_COUNT
        _default_tok = _R127_B_THRESHOLD_PROMPT_TOKENS
    scn_thr = scenario_threshold if scenario_threshold is not None else _default_scn
    tok_thr = prompt_token_threshold if prompt_token_threshold is not None else _default_tok
    scn_count = count_scenarios(gherkin_text)
    if scn_count >= scn_thr:
        return True
    # Fallback: dense free-form Gherkin lacking explicit Scenario: blocks
    estimated_tokens = len(gherkin_text or "") // 4
    return estimated_tokens >= tok_thr


def split_into_subs(
    requirement: dict,
    gherkin_text: str,
) -> list[SubRequirement]:
    """Split a Gherkin string into per-`Scenario:` sub-requirements.

    Each sub carries the shared `Background:` block (if present) plus its
    OWN single scenario — so the LLM sees one focused unit of work per
    sub-req. The Feature: heading is also preserved on each sub so the
    LLM has full context.

    Cold-fallback: when there are 0 or 1 scenarios, returns a single-element
    list containing the parent gherkin unchanged. The caller decides whether
    that means "no decomposition needed" (same as `should_decompose=False`)
    or whether to proceed with single-element gen.

    Returns: list of SubRequirement objects, one per scenario, in source
    order (preserves the operator's intended scenario sequence for merge).
    """
    parent_id = str(requirement.get("id") or requirement.get("requirement_id") or "")
    if not gherkin_text:
        return [SubRequirement(
            parent_id=parent_id, part_id="SCN-01", part_index=1, part_total=1,
            scenario_title="(empty)", gherkin_text="",
        )]

    headers = list(_SCENARIO_HEADER_RE.finditer(gherkin_text))
    if len(headers) < 2:
        # 0 or 1 scenario → no real decomposition possible.
        return [SubRequirement(
            parent_id=parent_id, part_id="SCN-01", part_index=1, part_total=1,
            scenario_title=(headers[0].group("header") if headers else "(no scenarios)"),
            gherkin_text=gherkin_text,
        )]

    # Extract Feature: line + Background: block — shared across all subs.
    feature_match = _FEATURE_HEADER_RE.search(gherkin_text)
    feature_line = feature_match.group(0) if feature_match else ""

    bg_match = _BACKGROUND_HEADER_RE.search(gherkin_text)
    background_block = ""
    if bg_match:
        # Background runs from its header to the start of the FIRST Scenario:
        bg_start = bg_match.start()
        first_scn_start = headers[0].start()
        if bg_start < first_scn_start:
            background_block = gherkin_text[bg_start:first_scn_start].rstrip()

    subs: list[SubRequirement] = []
    total = len(headers)
    for idx, hdr in enumerate(headers):
        scn_start = hdr.start()
        scn_end = headers[idx + 1].start() if idx + 1 < total else len(gherkin_text)
        scn_body = gherkin_text[scn_start:scn_end].rstrip()
        # Compose: Feature line (if present) + Background (if present) + this scenario
        parts = []
        if feature_line:
            parts.append(feature_line)
        if background_block:
            parts.append(background_block)
        parts.append(scn_body)
        composed = "\n\n".join(parts)
        title_match = re.match(
            r"\s*(Scenario(?:\s+Outline)?):\s*(.+)$",
            hdr.group("header").splitlines()[0],
        )
        title = title_match.group(2).strip() if title_match else hdr.group("header")
        subs.append(SubRequirement(
            parent_id=parent_id,
            part_id=f"SCN-{idx + 1:02d}",
            part_index=idx + 1,
            part_total=total,
            scenario_title=title,
            gherkin_text=composed,
        ))
    return subs


# ── Merge logic ──────────────────────────────────────────────────────────────

_R127_B_MARKER_PREFIX = "// @r127_b_merged"
_R127_B_FAIL_PLACEHOLDER_PREFIX = "// @r127_b_part_failed"


def is_r127_b_merged(spec_content: str) -> bool:
    """R127.B idempotency check — returns True when the spec already carries
    the R127.B merge marker header (so callers can skip re-decomposition)."""
    if not spec_content:
        return False
    head = spec_content[:512]
    return _R127_B_MARKER_PREFIX in head


# R127.D.2 — symbol-set-aware import dedup. Pre-R127.D.2 used pure string
# equality, so `import {test, expect, Page}` and `import {test, expect}`
# from the same module BOTH landed in the merged spec → TS2300 duplicate
# identifier compile error → Fix FF dry-run quarantine (req_am_013
# evidence: 2 distinct `@playwright/test` imports survived merge).
# Post-R127.D.2: per module, union the symbol sets and emit ONE
# destructured import line; pure-side-effect + bare-default imports keep
# string-equality dedup.
_R127_D2_IMPORT_RE_DESTR = re.compile(
    r"^\s*import\s+\{([^}]+)\}\s+from\s+['\"]([^'\"]+)['\"]\s*;\s*$"
)
_R127_D2_IMPORT_RE_SIMPLE = re.compile(
    r"^\s*import\s+(?:\*\s+as\s+\w+|\w+)\s+from\s+['\"]([^'\"]+)['\"]\s*;\s*$"
)
_R127_D2_IMPORT_RE_SIDE = re.compile(
    r"^\s*import\s+['\"]([^'\"]+)['\"]\s*;\s*$"
)


def _dedupe_imports(spec_contents: Iterable[str]) -> tuple[list[str], list[str]]:
    """Extract + dedupe `import` lines across sub-specs.

    R127.D.2: for `import {A,B,C} from 'mod'`-style destructured imports
    we group by module and emit ONE merged line carrying the UNION of
    symbols. This prevents the "duplicate `@playwright/test` import"
    class of TS2300 errors that previously survived plain-string dedup.

    Pure side-effect imports (`import './foo';`) and bare default-only
    imports (`import * as fs from 'fs';`) keep string-equality dedup.

    Returns (imports, bodies) where imports is the deduped list and
    bodies is the spec contents with their imports stripped.
    """
    destructured: dict[str, set[str]] = {}     # module → union of symbols
    simple_seen: set[str] = set()              # full normalized line
    side_effect_seen: set[str] = set()         # `import './foo';`
    bodies: list[str] = []
    import_re = re.compile(r"^\s*import\s+.+?;\s*$", re.MULTILINE)
    for content in spec_contents:
        if not content:
            bodies.append("")
            continue
        import_lines = import_re.findall(content)
        body = import_re.sub("", content).lstrip("\n")
        for ln in import_lines:
            normalized = ln.strip()
            m_destr = _R127_D2_IMPORT_RE_DESTR.match(normalized)
            m_simple = _R127_D2_IMPORT_RE_SIMPLE.match(normalized)
            m_side = _R127_D2_IMPORT_RE_SIDE.match(normalized)
            if m_destr:
                symbols_str, module = m_destr.group(1), m_destr.group(2)
                symbols = {
                    s.strip()
                    for s in symbols_str.split(",")
                    if s.strip()
                }
                destructured.setdefault(module, set()).update(symbols)
            elif m_side:
                side_effect_seen.add(normalized)
            elif m_simple:
                simple_seen.add(normalized)
            else:
                # Unrecognized shape — preserve verbatim under simple_seen
                # so we don't lose information.
                simple_seen.add(normalized)
        bodies.append(body)

    imports: list[str] = []
    # Per-module destructured imports become ONE line each carrying the
    # UNION of symbols (sorted for stable output).
    for module in sorted(destructured.keys()):
        sym_str = ", ".join(sorted(destructured[module]))
        imports.append(f"import {{ {sym_str} }} from '{module}';")
    imports.extend(sorted(simple_seen))
    imports.extend(sorted(side_effect_seen))
    return imports, bodies


# ── R127.D.6.C — top-level const/let/var dedup ──────────────────────────────
#
# resulting spec had 8 duplicate `const API_BASE_URL = ...;` declarations
# at top level, which TypeScript rejects with TS2451 (redeclare block-
# scoped variable). `_dedupe_imports` handles only `import` lines, not
# `const`/`let`/`var`. R127.D.6.C is the parallel helper for top-level
# declarations.
#
# LINE-START anchor (^ at column 0) is critical — block-scoped consts
# inside `test(...)` bodies (indented) must NOT be touched. Operator
# warning on value mismatch (no auto-rename fallback per the standing
# "avoid fallback methods" directive).

_R127_D6_C_DECL_RE = re.compile(
    r"^(const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([^;]+);\s*$",
    re.MULTILINE,
)


def _dedupe_top_level_consts(
    bodies: Iterable[str],
) -> tuple[list[str], list[str]]:
    """R127.D.6.C — dedupe top-level `const NAME = VALUE;` (also `let`/`var`)
    declarations across sub-scripts.

    LINE-START anchor (`^` at column 0) ensures only TOP-LEVEL declarations
    are touched — block-scoped consts inside `test(...)` bodies are
    indented and skipped.

    Behavior:
      - First-occurrence wins on (kind, name, value) match → emitted once.
      - On value MISMATCH for the same (kind, name): keep first + log
        WARN with both values truncated to 80 chars (operator-visible
        truthful signal; no auto-rename fallback).
      - Bodies returned with all matched lines stripped — the deduped
        declarations are emitted in the merged file's top-level prelude.

    Returns:
        (decls, bodies): `decls` is the deduped list of declaration
        lines (preserving insertion order of first appearances); `bodies`
        is the input bodies with matched top-level declaration lines
        stripped.
    """
    decls: list[str] = []
    # Track (kind, name) → (declaration_line_text, value) for dedup +
    # conflict detection.
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    bodies_out: list[str] = []
    for content in bodies:
        if not content:
            bodies_out.append("")
            continue
        matches = list(_R127_D6_C_DECL_RE.finditer(content))
        if not matches:
            bodies_out.append(content)
            continue
        # Process matches in order: record + dedup; strip lines from body.
        spans_to_remove: list[tuple[int, int]] = []
        for m in matches:
            kw, name, value = m.group(1), m.group(2), m.group(3).strip()
            key = (kw, name)
            line_text = m.group(0).strip()
            if key not in seen:
                seen[key] = (line_text, value)
                decls.append(line_text)
            else:
                _, first_value = seen[key]
                if first_value != value:
                    log.warning(
                        "R127.D.6.C: top-level `%s %s` declared with conflicting "
                        "values across subs; keeping first. first=%r ignored=%r",
                        kw, name, first_value[:80], value[:80],
                    )
                # else: same name + same value → silent dedup
            spans_to_remove.append((m.start(), m.end()))
        # Strip matched line spans from body (back-to-front to preserve
        # earlier indices).
        body_out_chars = list(content)
        for start, end in reversed(spans_to_remove):
            # Include trailing newline if present (clean removal)
            end_with_nl = end
            if end_with_nl < len(body_out_chars) and body_out_chars[end_with_nl] == "\n":
                end_with_nl += 1
            del body_out_chars[start:end_with_nl]
        bodies_out.append("".join(body_out_chars).lstrip("\n"))
    return decls, bodies_out


# ── R127.D.6.A KEYSTONE — post-merge paren+brace balance check ──────────────
#
# 44KB / 851-line spec had paren delta `(1114) vs )=1111 = +3` — each
# individual sub passed R127.D.5's per-sub ±1 tolerance, but small deltas
# summed at merge time. R127.D.5 runs PER SUB; R127.D.6.A runs POST MERGE.
#
# Tolerance: ±2 by default (matches R125.L permissiveness — string literals
# can legitimately carry a stray brace). The dominant evidence (+3) is
# beyond tolerance and gets caught. When stamped, R102.C dispatch BLOCK
# emits a truthful BLOCKED row with `metadata.violation_kinds`-driven
# breakdown via R111.E.


def _r127_d6_check_merged_balance(
    merged: str,
    *,
    paren_tolerance: int = 2,
    brace_tolerance: int = 2,
) -> list[dict]:
    """R127.D.6.A — post-merge paren+brace balance check.

    Returns [] when within tolerance; else a list of violation dicts ready
    for R102.A-style stamping via `grounding_stamp.stamp_pw_violations`.

    Two distinct kinds (both can fire concurrently):
      - `merged_paren_imbalance` — `|open( - close)|` > paren_tolerance
      - `merged_brace_imbalance` — `|open{ - close}|` > brace_tolerance
    """
    if not merged or not merged.strip():
        return []
    open_paren = merged.count("(")
    close_paren = merged.count(")")
    open_brace = merged.count("{")
    close_brace = merged.count("}")
    paren_delta = open_paren - close_paren
    brace_delta = open_brace - close_brace
    violations: list[dict] = []
    scenario_count = len(re.findall(r"^\s*test\(", merged, re.MULTILINE))
    common_location = (
        f"(paren_delta={paren_delta:+d}, brace_delta={brace_delta:+d}, "
        f"scenario_count={scenario_count}, len={len(merged)})"
    )
    if abs(paren_delta) > paren_tolerance:
        violations.append({
            "kind": "merged_paren_imbalance",
            "symbol": "<merged_spec>",
            "location": common_location,
            # R127.D.7.D — paren-balanced hint text. Pre-R127.D.7.D the
            # hint contained an intentional +1 paren imbalance in the
            # BROKEN code sample, which made the stamped file's measured
            # paren count exceed the stamp's reported delta by +1
            # (operator-confusing artifact: "stamp says +7, file says
            # +8"). R127.D.7.D rewrites the BROKEN example as descriptive
            # prose so the comment block itself adds zero paren/brace
            # bias. AFTER section kept (it's already paren-balanced) for
            # LLM clarity on retry.
            "hint": (
                "R127.D.6.A — merged Playwright spec has unbalanced parens. "
                f"Paren delta={paren_delta:+d} across {scenario_count} "
                "test() block(s). Each individual sub passed R125.L's "
                "per-sub +/-1 tolerance, but small deltas summed at merge "
                "(or R127.D.7.A/B layers were bypassed).\n\n"
                "BROKEN PATTERN (descriptive — see code in your spec): "
                "test() bodies that end mid-call expression where one "
                "opening paren lacks its matching closing paren before "
                "the closing semicolon. R125.L's retry-with-hint tolerates "
                "small per-sub imbalance; accumulation across N subs "
                "produces large merged-level delta.\n\n"
                "AFTER (CORRECT) — every test() self-balanced:\n"
                "  test('AC-03', async ({ page }) => {\n"
                "    await expect(page.getByRole('button', "
                "{ name: 'X' })).toBeVisible();\n"
                "  });\n\n"
                "Re-emit each sub keeping every `(` paired with `)` BEFORE "
                "the closing `});` of the test() block."
            ),
        })
    if abs(brace_delta) > brace_tolerance:
        violations.append({
            "kind": "merged_brace_imbalance",
            "symbol": "<merged_spec>",
            "location": common_location,
            "hint": (
                "R127.D.6.A — merged Playwright spec has unbalanced braces. "
                f"Brace delta={brace_delta:+d} across {scenario_count} "
                "test() block(s). Re-emit each sub keeping every `{` paired "
                "with `}`; check for un-closed object literals (e.g., "
                "`{ name: 'X', `) inside getByRole calls."
            ),
        })
    return violations


def _r127_d6_apply_balance_stamp(merged: str) -> str:
    """R127.D.6.A wire — call balance check + delegate stamp composition
    to R127.D.6.E's shared module. Defensive: any exception returns
    content unchanged (never block a merge on a defensive scan failure)."""
    try:
        viols = _r127_d6_check_merged_balance(merged)
        if not viols:
            return merged
        from .grounding_stamp import stamp_pw_violations
        return stamp_pw_violations(merged, viols)
    except Exception as exc:
        log.debug("R127.D.6.A: merged balance check skipped: %s", exc)
        return merged


def merge_pw_specs(
    parent_req_id: str,
    sub_scripts: list[Any],
    partial_failures: list[dict] | None = None,
    *,
    timestamp_iso: str | None = None,
) -> str:
    """Merge N sub-`test()` bodies into a single Playwright .spec.ts.

    Each sub-script's TypeScript body is preserved verbatim (the LLM's
    `test('AC-XX — ...', async ({page}) => { ... })` blocks). Imports are
    deduplicated across sub-scripts so the merged file has one copy of
    each `import {test, expect, ...} from '@playwright/test'`.

    Partial failures (per the R127.T contract) emit a `test.skip(...)`
    placeholder so the merged spec still parses cleanly + the operator
    sees a truthful BLOCKED row per failed sub at dispatch time.

    Returns: the merged spec content with the R127.B merge marker
    header. Idempotent — running merge_pw_specs twice with the same
    inputs produces the same output (modulo timestamp).
    """
    import datetime as _dt
    ts = timestamp_iso or _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    # Collect sub-script contents (each `sub_scripts[i]` may be an
    # AutomationScript object with .content, OR a dict with a partial-
    # failure marker per R127.T semantics).
    contents: list[str] = []
    fail_placeholders: list[str] = []
    # R127.D.7 telemetry: collect per-sub imbalance records for the
    # aggregated `subs_paren_imbalance` summary stamp emitted by
    # R127.D.7.C if any sub failed the safety-net check below.
    per_sub_imbalances: list[dict] = []
    for idx, sub in enumerate(sub_scripts):
        if isinstance(sub, dict) and sub.get("_r127_b_partial_failure"):
            scn = sub.get("scn_id") or "SCN-??"
            exc = (sub.get("exc") or "")[:200]
            fail_placeholders.append(
                f"{_R127_B_FAIL_PLACEHOLDER_PREFIX} {scn}: {exc!r}\n"
                f"test.skip('{scn} — LLM_FILL failed', async ({{ page }}) => {{}});\n"
            )
        else:
            content = getattr(sub, "content", None) or (sub if isinstance(sub, str) else "")
            # R127.D.7.B SAFETY NET — per-sub paren+brace balance check at
            # merge entry. R127.D.7.A's gen-time strict check + R127.C
            # escalation handles the LLM-emission case at the source; this
            # safety net catches the edge case where Claude Code escalation
            # ALSO produces ±1 imbalance (rare per live evidence). Skipping
            # the broken sub with a test.skip placeholder preserves the
            # 14/15 working subs in the merged output instead of
            # whole-spec quarantine.
            _r127_d7_b_paren_delta = content.count("(") - content.count(")")
            _r127_d7_b_brace_delta = content.count("{") - content.count("}")
            if (
                abs(_r127_d7_b_paren_delta) >= 1
                or abs(_r127_d7_b_brace_delta) >= 1
            ):
                _r127_d7_b_scn = (
                    getattr(sub, "test_id", None) or f"SUB-{idx:02d}"
                )
                log.warning(
                    "R127.D.7.B safety-net: sub %s has imbalance "
                    "paren=%+d brace=%+d (R127.D.7.A escalation must have "
                    "passed earlier OR sub bypassed gen retry); replacing "
                    "with test.skip placeholder",
                    _r127_d7_b_scn,
                    _r127_d7_b_paren_delta,
                    _r127_d7_b_brace_delta,
                )
                per_sub_imbalances.append({
                    "scn_id": _r127_d7_b_scn,
                    "paren_delta": _r127_d7_b_paren_delta,
                    "brace_delta": _r127_d7_b_brace_delta,
                })
                fail_placeholders.append(
                    f"// @r127_d7_b_per_sub_imbalance_safety_net "
                    f"{_r127_d7_b_scn}: paren="
                    f"{_r127_d7_b_paren_delta:+d}, brace="
                    f"{_r127_d7_b_brace_delta:+d}\n"
                    f"test.skip('{_r127_d7_b_scn} — per-sub paren/brace "
                    f"imbalance', async ({{ page }}) => {{}});\n"
                )
                continue
            contents.append(content)

    imports, bodies = _dedupe_imports(contents)
    # R127.D.6.C — dedupe top-level `const`/`let`/`var` declarations across
    # subs. Each sub may declare its own `const API_BASE_URL = ...;` at
    # top level; merge keeps duplicates → TS2451. R127.D.6.C lifts unique
    # declarations into the merged prelude + strips from bodies.
    top_consts, bodies = _dedupe_top_level_consts(bodies)

    # Compose merged output.
    total_subs = len(sub_scripts)
    success_count = len(contents)
    fail_count = len(fail_placeholders)
    header = (
        f"{_R127_B_MARKER_PREFIX} parent={parent_req_id} "
        f"parts={total_subs} succeeded={success_count} failed={fail_count} "
        f"at={ts}\n"
    )
    parts: list[str] = [header]
    if imports:
        parts.append("\n".join(imports))
        parts.append("")  # blank line after imports
    if top_consts:
        # R127.D.6.C: emit deduped top-level decls between imports + bodies
        parts.append("\n".join(top_consts))
        parts.append("")
    parts.extend(b.rstrip() + "\n" for b in bodies if b.strip())
    parts.extend(fail_placeholders)
    merged = "\n".join(parts).rstrip() + "\n"
    # R127.D.7.C — when R127.D.7.B safety-net skipped ≥1 sub, prepend an
    # aggregated `subs_paren_imbalance` summary stamp so the operator
    # dashboard surfaces "N of M subs rejected" truthfully. R127.D.6.F
    # maps the kind to `defect_subclass="per_sub_imbalance_skipped"` for
    # granular dashboard filtering. No-op when per_sub_imbalances=[]
    # (the GREEN-band outcome where R127.D.7.A's R127.C escalation
    # succeeded for all subs).
    if per_sub_imbalances:
        try:
            from .grounding_stamp import stamp_pw_violations
            _r127_d7_c_summary_viol = {
                "kind": "subs_paren_imbalance",
                "symbol": "<merged_spec>",
                "location": (
                    f"(skipped_subs={len(per_sub_imbalances)}, "
                    f"total_subs={total_subs})"
                ),
                "hint": (
                    f"R127.D.7.B safety-net skipped {len(per_sub_imbalances)} "
                    f"of {total_subs} subs at merge entry due to per-sub "
                    "paren/brace imbalance that survived R127.D.7.A's R127.C "
                    "escalation. Per-sub deltas: "
                    + ", ".join(
                        f"{v['scn_id']}: paren={v['paren_delta']:+d}/"
                        f"brace={v['brace_delta']:+d}"
                        for v in per_sub_imbalances[:5]
                    )
                    + (
                        "" if len(per_sub_imbalances) <= 5
                        else f" (+{len(per_sub_imbalances) - 5} more)"
                    )
                    + ". Operator: trigger per-sub regen for the skipped "
                    "sub(s); the other subs in this spec passed cleanly."
                ),
            }
            merged = stamp_pw_violations(merged, [_r127_d7_c_summary_viol])
        except Exception as _r127_d7_c_exc:
            log.debug(
                "R127.D.7.C: summary stamp skipped: %s", _r127_d7_c_exc,
            )
    # R127.D.6.A KEYSTONE — post-merge balance check + R102.A stamp via the
    # shared grounding_stamp module (R127.D.6.E). When the merged content
    # has cross-sub-accumulated paren/brace imbalance beyond ±2 tolerance,
    # prepend the R102.A-shape comment-header so R102.C dispatch BLOCKs at
    # dispatch time with a truthful operator CTA. Defensive: any failure
    # in the check returns content unchanged (never block a merge).
    merged = _r127_d6_apply_balance_stamp(merged)
    return merged


# ── R127.D.6.D — pytest AST-based merge validation ──────────────────────────
#
# Pytest analogue of R127.D.6.A. PW uses braces/parens; pytest uses
# indentation + Python syntax. `ast.parse()` is the canonical source of
# truth: catches IndentationError, EOFError (unclosed string/paren), and
# generic SyntaxError in one call. Cheaper + more reliable than
# re-implementing Python's grammar via regex.
#
# When the merged pytest module fails ast.parse(), R127.D.6.D stamps it
# with the same R102.A pattern (via R127.D.6.E's shared stamp module) so
# the R102.C-equivalent pytest dispatch reader BLOCKs cleanly with a
# truthful operator CTA.


def _r127_d6_d_validate_merged_pytest(merged: str) -> list[dict]:
    """R127.D.6.D — validate merged pytest module via `ast.parse()`.

    Returns [] when syntactically valid; else a list of violation dicts
    ready for R102.A-style stamping via
    `grounding_stamp.stamp_pytest_violations`.

    Kinds (ordered by specificity):
      - `pytest_syntax_error_indent` — IndentationError subclass
      - `pytest_syntax_error_eof`    — unclosed string/paren extends past EOF
      - `pytest_syntax_error_generic` — catch-all SyntaxError
    """
    import ast as _ast
    if not merged or not merged.strip():
        return []
    try:
        _ast.parse(merged)
        return []
    except IndentationError as exc:
        kind = "pytest_syntax_error_indent"
        return [{
            "kind": kind,
            "symbol": "<merged_pytest>",
            "location": f"line {exc.lineno or '?'}:{exc.offset or '?'}",
            "hint": (
                f"R127.D.6.D — merged pytest module fails ast.parse() with "
                f"IndentationError: {exc.msg}. One or more subs introduced "
                "inconsistent indentation (mix of tabs + spaces, or wrong "
                "level after a `def test_*` line).\n\n"
                "Re-emit each sub keeping CONSISTENT 4-space indentation "
                "(no tabs) and every `def test_*:` block self-contained."
            ),
        }]
    except SyntaxError as exc:
        msg = (exc.msg or "").lower()
        if "eof" in msg or "unexpected end" in msg or "unterminated" in msg:
            kind = "pytest_syntax_error_eof"
        else:
            kind = "pytest_syntax_error_generic"
        return [{
            "kind": kind,
            "symbol": "<merged_pytest>",
            "location": f"line {exc.lineno or '?'}:{exc.offset or '?'}",
            "hint": (
                f"R127.D.6.D — merged pytest module fails ast.parse(): "
                f"{exc.msg}. One or more subs has invalid Python syntax "
                "(unclosed string/paren, missing colon after `def`, "
                "malformed decorator).\n\n"
                "Re-emit each sub keeping every `def test_*` block "
                "self-balanced (matching parens/brackets, no trailing "
                "unclosed strings, valid decorator + colon syntax)."
            ),
        }]


def _r127_d6_d_apply_pytest_stamp(merged: str) -> str:
    """R127.D.6.D wire — call AST validate + delegate stamp composition
    to R127.D.6.E's shared module. Defensive: any failure returns content
    unchanged (truthful-degradation; never block a merge on a defensive
    scan failure)."""
    try:
        viols = _r127_d6_d_validate_merged_pytest(merged)
        if not viols:
            return merged
        from .grounding_stamp import stamp_pytest_violations
        return stamp_pytest_violations(merged, viols)
    except Exception as exc:
        log.debug("R127.D.6.D: merged pytest AST check skipped: %s", exc)
        return merged


def merge_pytest_specs(
    parent_req_id: str,
    sub_scripts: list[Any],
    partial_failures: list[dict] | None = None,
    *,
    timestamp_iso: str | None = None,
) -> str:
    """Merge N sub-pytest modules into a single parent module.

    Mirrors `merge_pw_specs` shape but uses Python comment syntax (`#`)
    for the merge marker + per-sub failure placeholders. Imports are
    deduplicated; bodies (test functions + fixtures) appear in source
    order.
    """
    import datetime as _dt
    ts = timestamp_iso or _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    contents: list[str] = []
    fail_placeholders: list[str] = []
    for sub in sub_scripts:
        if isinstance(sub, dict) and sub.get("_r127_b_partial_failure"):
            scn = sub.get("scn_id") or "SCN-??"
            exc = (sub.get("exc") or "")[:200]
            fn_name = f"test_{sanitize_req_id(scn)}_llm_fill_failed"
            fail_placeholders.append(
                f"# @r127_b_part_failed {scn}: {exc!r}\n"
                f"import pytest\n\n"
                f"@pytest.mark.skip(reason='R127.B LLM_FILL failed for {scn}: {exc[:80]}')\n"
                f"def {fn_name}():\n"
                f"    pass\n"
            )
        else:
            content = getattr(sub, "content", None) or (sub if isinstance(sub, str) else "")
            contents.append(content)

    # Dedupe Python imports (different syntax from TS imports).
    import_re_py = re.compile(
        r"^\s*(?:import\s+\S+(?:\s+as\s+\S+)?|from\s+\S+\s+import\s+.+?)\s*$",
        re.MULTILINE,
    )
    imports: list[str] = []
    seen: set[str] = set()
    bodies: list[str] = []
    for c in contents:
        if not c:
            bodies.append("")
            continue
        for m in import_re_py.findall(c):
            normalized = m.strip()
            if normalized not in seen:
                seen.add(normalized)
                imports.append(normalized)
        body = import_re_py.sub("", c).lstrip("\n")
        bodies.append(body)

    total_subs = len(sub_scripts)
    success_count = len(contents)
    fail_count = len(fail_placeholders)
    header = (
        f"# R127.B merged parent={parent_req_id} "
        f"parts={total_subs} succeeded={success_count} failed={fail_count} "
        f"at={ts}\n"
    )
    parts: list[str] = [header]
    if imports:
        parts.append("\n".join(imports))
        parts.append("")
    parts.extend(b.rstrip() + "\n" for b in bodies if b.strip())
    parts.extend(fail_placeholders)
    merged = "\n".join(parts).rstrip() + "\n"
    # R127.D.6.D — pytest AST validation + R102.A stamp via the shared
    # grounding_stamp module (R127.D.6.E). When the merged module fails
    # ast.parse() (indentation/EOF/generic syntax error), prepend a
    # Python-comment stamp so the pytest dispatch reader BLOCKs at
    # dispatch time with a truthful operator CTA. Defensive: never
    # blocks a merge on a defensive scan failure.
    merged = _r127_d6_d_apply_pytest_stamp(merged)
    return merged


# ── RequirementDecomposer (thin orchestrator) ────────────────────────────────


class RequirementDecomposer:
    """R127.B orchestrator — used by the per-req gen path in tests.py.

    Stateless across gen requests; methods can be called either as
    instance methods OR via the module-level functions above. The class
    exists mainly to make the gen-flow code in tests.py read naturally:

        decomposer = RequirementDecomposer(client, base_config=cfg, cache=cache)
        if decomposer.should_decompose(req, gherkin, "playwright", provider_tag):
            subs = decomposer.split_into_subs(req, gherkin)
            ...
            merged = decomposer.merge_pw_specs(req_id, sub_scripts, failures)
    """

    def __init__(
        self,
        client: Any,
        base_config: Any = None,
        tool_client_cache: dict | None = None,
    ):
        self._client = client
        self._base_config = base_config
        self._tool_client_cache = tool_client_cache if tool_client_cache is not None else {}

    @staticmethod
    def should_decompose(
        requirement: dict,
        gherkin_text: str,
        tool_name: str,
        provider_tag: str | None,
        *,
        model_tag: str | None = None,
    ) -> bool:
        return should_decompose(
            requirement, gherkin_text, tool_name, provider_tag,
            model_tag=model_tag,
        )

    @staticmethod
    def split_into_subs(
        requirement: dict,
        gherkin_text: str,
    ) -> list[SubRequirement]:
        return split_into_subs(requirement, gherkin_text)

    @staticmethod
    def merge_pw_specs(
        parent_req_id: str,
        sub_scripts: list[Any],
        partial_failures: list[dict] | None = None,
    ) -> str:
        return merge_pw_specs(parent_req_id, sub_scripts, partial_failures)

    @staticmethod
    def merge_pytest_specs(
        parent_req_id: str,
        sub_scripts: list[Any],
        partial_failures: list[dict] | None = None,
    ) -> str:
        return merge_pytest_specs(parent_req_id, sub_scripts, partial_failures)

    @staticmethod
    def is_already_merged(spec_content: str) -> bool:
        return is_r127_b_merged(spec_content)
