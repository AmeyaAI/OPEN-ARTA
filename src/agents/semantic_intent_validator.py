"""R127.E.2 — Semantic-vs-Gherkin intent validator.

Mission framing (Pillar 1b — high quality test scripts): a spec can be
structurally perfect (R102.A clean + R127.E.1 8-dim aggregate 1.0) and
STILL be semantically wrong. The live evidence post-R127.C: 13
test() blocks devolved into a click-cycle of catalog buttons (ANALYST →
VENDOR → SIGN IN → SIGN UP → ...) that has ZERO relation to the
Gherkin scenario ("Dataset Creation, File Indexing & Database
Connectors"). Such tests dispatch + always FAIL → counted as
`sut_regression` by R34.1 → operator's SUT-quality report shows
false-positive defects. ARTA's "execute flawlessly → report SUT
quality" mission requires that test bodies actually exercise the
Gherkin scenarios.

Two-tier design (per R127 plan + the "small Ollama on-prem
non-negotiable" directive):
  - Tier 1 (default): RULE-BASED keyword extraction. Zero LLM cost.
    Extracts content-bearing keywords from Gherkin, scans spec test()
    bodies for matches. Threshold = 20% overlap (configurable).
  - Tier 2 (opt-in): LLM-as-judge. Operator sets
    `project.llm_config.semantic_validator.mode = "llm_judge"` to add a
    secondary LLM-scored signal. Off by default.

This module exposes:
  - `extract_gherkin_keywords(text)` — stopword-filtered content tokens
  - `validate_intent_alignment(spec_content, gherkin_text)` — score dict
  - `score_gherkin_intent_alignment(...)` — convenience wrapper returning
    just the float alignment_score (used by R126.Q dimension)
"""
from __future__ import annotations

import os   # R279 — killswitch env var
import re
from typing import Iterable

# ── R127.E.2 keyword extraction ──────────────────────────────────────────────

# Stopwords: Gherkin syntax keywords, articles, pronouns, weak verbs,
# generic test-narrative nouns. These carry no test-intent signal and
# would otherwise dominate the overlap score with bland matches.
_GHERKIN_STOPWORDS: frozenset[str] = frozenset({
    # Gherkin syntax
    "given", "when", "then", "and", "but", "or", "scenario",
    "outline", "background", "feature", "examples", "example",
    # Articles + determiners
    "the", "a", "an", "this", "that", "these", "those", "any", "some",
    "each", "every", "all", "no", "none",
    # Auxiliary verbs + state
    "is", "are", "was", "were", "be", "been", "being", "has", "have",
    "had", "does", "did", "do", "doing", "done",
    # Modals
    "should", "must", "will", "would", "could", "shall", "may", "might",
    "can", "cannot", "ought",
    # Pronouns
    "i", "we", "they", "he", "she", "it", "you", "me", "us", "him",
    "her", "them", "myself", "yourself", "themselves", "itself",
    # Prepositions + connectors
    "to", "of", "in", "on", "at", "for", "with", "as", "by", "from",
    "into", "onto", "upon", "about", "above", "below", "before",
    "after", "during", "between", "among", "across", "through",
    "over", "under",
    # Generic test-narrative nouns (carry no SUT-specific signal)
    "user", "users", "system", "test", "tests", "case", "step", "page",
    "data", "value", "values", "result", "results", "status",
    # Numbers spelled out (low signal)
    "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten",
    # Common verbs that don't discriminate
    "see", "sees", "view", "views", "viewing", "click", "clicks",
    "clicking", "enter", "enters", "entering", "select", "selects",
    "selecting", "submit", "submits", "submitting", "navigate",
    "navigates", "navigating", "go", "goes", "going",
})


def extract_gherkin_keywords(
    gherkin_text: str,
    *,
    min_len: int = 4,
) -> set[str]:
    """R127.E.2 — return the content-bearing keywords from a Gherkin
    scenario.

    Algorithm:
      1. Tokenize on word boundaries (alphanumeric + underscore).
      2. Lowercase.
      3. Drop tokens shorter than `min_len` (default 4; "id"/"ok"/etc.
         carry low signal).
      4. Drop tokens in the Gherkin stopword set.

    Returns a set so downstream overlap math is deduplicated.
    """
    if not gherkin_text:
        return set()
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", gherkin_text.lower())
    return {
        t for t in tokens
        if len(t) >= min_len
        and t not in _GHERKIN_STOPWORDS
        and not _r279_is_id_fragment(t)
    }


# R279 — a GUID/hex fragment is not a CONTENT keyword.
#
# The tokenizer above splits on word boundaries, so a Gherkin carrying a real id
# (`73f0d2b8-5741-184c-9a9d-1a2b52ba96d1` — which R250's real-id seeding puts
# there ON PURPOSE) shatters into `f0d2b8`, `a2b52ba96d1`, … and every fragment
# became a "content keyword the spec must reference".
#
# That made ARTA contradict itself: R127.E.2 demanded the spec echo those
# fragments, while R250/R252 FORBID hardcoding ids in specs. The only way to
# satisfy the intent check was to violate the fabricated-id check. Live proof
# overlap vs a 20% threshold, the LAST blocker after R277/R278 had cleared the
# real ones, and unreachable by any legal spec.
#
# Precise by construction: hex-only AND contains a digit. `data`/`decade` are
# hex-only but digitless → kept. `acctguid`/`authorization` contain non-hex
# letters → kept. Killswitch ARTA_R279_ID_FRAGMENT_FILTER_DISABLE=1.
_R279_HEX_RE = re.compile(r"^[0-9a-f]+$")


def _r279_is_id_fragment(token: str) -> bool:
    """True for GUID/hash fragments — shrapnel from tokenizing a real id."""
    if os.environ.get("ARTA_R279_ID_FRAGMENT_FILTER_DISABLE") == "1":
        return False
    return bool(_R279_HEX_RE.match(token) and any(c.isdigit() for c in token))


# ── R127.E.2 spec-body keyword extraction ────────────────────────────────────

# Locate the head of each `test(...)` / `it(...)` block. The BODY is then
# extracted by BRACE-DEPTH COUNTING (see _iter_test_body_sources), not regex.
# We extract body tokens only (not import lines or describe headers — those are
# scaffolding, not test intent).
_TEST_HEAD_RE = re.compile(r"\b(?:test|it)\s*\(\s*['\"]")


def _iter_test_body_sources(spec_content: str) -> list[str]:
    """R290.A — return the full source of each test()/it() body via BRACE-DEPTH
    matching, correct for ARBITRARY nesting.

    Root cause this replaces: the prior `_TEST_BLOCK_BODY_RE` matched only ONE
    level of brace nesting (`[^}]*(?:\\{[^}]*\\}[^}]*)*`). A regex cannot match
    balanced braces to arbitrary depth, so for a modern generated spec whose
    assertions live inside nested if/for/try blocks (e.g. one spec's
    `if (resp.ok()) { ... expect(typeof body.currentState)... }`), the capture
    stopped at the first shallow `}` — the domain tokens (currentState, server,
    region, …) were INVISIBLE. That starved extract_spec_body_tokens (25 bodies →
    8 tokens, 0 domain terms) → a spurious 0% alignment → R102.C false-BLOCK of a
    demonstrably on-topic spec. Brace counting captures the WHOLE body regardless
    of depth, so the gate measures real intent (genuine drift still fails; correct
    specs stop being false-blocked — the threshold is untouched).

    Killswitch ARTA_R290_A_BRACEMATCH_DISABLE=1 reverts to the shallow regex.
    """
    if os.environ.get("ARTA_R290_A_BRACEMATCH_DISABLE") == "1":
        return [b if isinstance(b, str) else (b[0] if b else "")
                for b in re.compile(
                    r"\btest\s*\(\s*['\"][^'\"]*['\"]\s*,\s*async[^)]*\)\s*=>\s*"
                    r"\{([^}]*(?:\{[^}]*\}[^}]*)*)\}", re.DOTALL).findall(spec_content)]
    out: list[str] = []
    n = len(spec_content)
    for m in _TEST_HEAD_RE.finditer(spec_content):
        arrow = spec_content.find("=>", m.end())
        if arrow == -1:
            continue
        brace = spec_content.find("{", arrow)
        if brace == -1:
            continue
        # Brace-depth scan that SKIPS string/comment content — a `}` inside a
        # string ("has a } brace"), template URL, or comment is NOT a structural
        # close (the naive counter truncated the body there → dropped every token
        # after it → false-low alignment → false BLOCK). State machine over:
        #   ' " `  string/template literals (opaque; escapes honoured)
        #   //     line comment       /* */  block comment
        # Residual: a `{`/`}` inside a regex LITERAL (/\}/) is still counted —
        # rare in generated specs; documented, not silently masked.
        depth = 0
        i = brace
        state = None  # None | "'" | '"' | '`' | 'line' | 'block'
        captured = None
        while i < n:
            c = spec_content[i]
            nxt = spec_content[i + 1] if i + 1 < n else ""
            if state is None:
                if c in ("'", '"', "`"):
                    state = c
                elif c == "/" and nxt == "/":
                    state = "line"; i += 1
                elif c == "/" and nxt == "*":
                    state = "block"; i += 1
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        captured = spec_content[brace + 1:i]
                        break
            elif state in ("'", '"', "`"):
                if c == "\\":
                    i += 1            # skip escaped char
                elif c == state:
                    state = None
            elif state == "line":
                if c == "\n":
                    state = None
            elif state == "block":
                if c == "*" and nxt == "/":
                    state = None; i += 1
            i += 1
        if captured is not None:
            out.append(captured)
    return out


def extract_spec_body_tokens(spec_content: str) -> set[str]:
    """R127.E.2 — extract tokens from inside test() bodies only.

    String literals (quoted content) are the primary signal — they carry
    selector names, URL paths, expected texts. JS keywords, function
    names, and identifiers like `page`/`expect` are deliberately
    de-emphasised by relying on stopword filtering downstream.
    """
    if not spec_content:
        return set()
    bodies = _iter_test_body_sources(spec_content)
    if not bodies:
        # Fallback: scan whole spec when no test() blocks parsed (e.g.,
        # incomplete-Gherkin specs caught by R125.B before R127.E.2 even
        # runs — defensive)
        bodies = [spec_content]
    combined = " ".join(bodies)
    # Extract bare identifiers + string-literal contents
    tokens: set[str] = set()
    for m in re.finditer(r"[a-zA-Z_][a-zA-Z0-9_]*", combined.lower()):
        tokens.add(m.group(0))
    for m in re.finditer(r"['\"]([^'\"]+)['\"]", combined):
        for sub in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", m.group(1).lower()):
            tokens.add(sub)
    return tokens


# ── R127.E.2 alignment scoring ───────────────────────────────────────────────

def validate_intent_alignment(
    spec_content: str,
    gherkin_text: str,
    *,
    min_keyword_overlap: float = 0.20,
) -> dict:
    """R127.E.2 — score how well the spec test bodies reference the
    Gherkin scenario's content.

    Returns:
        {
          "alignment_score":   float [0.0, 1.0],
          "threshold":         float (the min_keyword_overlap argument),
          "passes_threshold":  bool,
          "gherkin_keywords":  set[str] (content tokens from Gherkin),
          "matched_keywords":  set[str] (gherkin ∩ spec_body),
          "missing_keywords":  set[str] (gherkin - spec_body),
        }

    Algorithm:
      1. Extract gherkin keywords (stopword-filtered).
      2. Extract spec body tokens (string literals + identifiers).
      3. alignment_score = |matched| / max(|gherkin|, 1)
      4. passes_threshold = alignment_score >= min_keyword_overlap

    Graceful edge cases:
      - Empty Gherkin → alignment_score=1.0 (no signal to measure
        against; not a violation).
      - Empty spec → alignment_score=0.0 if Gherkin has keywords.
    """
    gherkin_keywords = extract_gherkin_keywords(gherkin_text)
    if not gherkin_keywords:
        # No measurable signal → pass trivially. Pillar 4 truthfulness:
        # we report "no Gherkin keywords to validate" rather than
        # silently failing.
        return {
            "alignment_score":  1.0,
            "threshold":        min_keyword_overlap,
            "passes_threshold": True,
            "gherkin_keywords": set(),
            "matched_keywords": set(),
            "missing_keywords": set(),
        }
    spec_tokens = extract_spec_body_tokens(spec_content)
    matched = gherkin_keywords & spec_tokens
    missing = gherkin_keywords - spec_tokens
    alignment_score = len(matched) / max(len(gherkin_keywords), 1)
    # R291 — absolute-engagement floor. The recall ratio |matched|/|gherkin|
    # over-penalises a FOCUSED spec against a VERBOSE Gherkin: a legit UI spec
    # that references 19 distinct scenario keywords still scores 0.18 when the
    # keywords matched, blocked 2% under 0.20). A spec that references MANY
    # distinct Gherkin content keywords is demonstrably on-topic regardless of
    # the denominator — the "structurally-clean-but-semantically-wrong" class
    # NEAR-ZERO keywords, so this floor never rescues it. Pass when EITHER the
    # ratio clears the threshold OR the spec matches >= _R291_ABS_FLOOR distinct
    # keywords AND clears a low ratio guard (so a giant off-topic spec can't
    # coincidentally accumulate the floor). ONLY ADDS passes — never blocks a
    # spec the ratio already cleared. Env overrides: ARTA_R291_ABS_FLOOR,
    # ARTA_R291_FLOOR_MIN_OVERLAP; ARTA_R291_DISABLE=1 to revert.
    import os as _os_r291
    _passes = alignment_score >= min_keyword_overlap
    if not _passes and _os_r291.environ.get("ARTA_R291_DISABLE") != "1":
        try:
            _abs_floor = int(_os_r291.environ.get("ARTA_R291_ABS_FLOOR") or 12)
        except (ValueError, TypeError):
            _abs_floor = 12
        try:
            _floor_min = float(_os_r291.environ.get("ARTA_R291_FLOOR_MIN_OVERLAP") or 0.10)
        except (ValueError, TypeError):
            _floor_min = 0.10
        if len(matched) >= _abs_floor and alignment_score >= _floor_min:
            _passes = True
    return {
        "alignment_score":  round(alignment_score, 3),
        "threshold":        min_keyword_overlap,
        "passes_threshold": _passes,
        "gherkin_keywords": gherkin_keywords,
        "matched_keywords": matched,
        "missing_keywords": missing,
    }


def score_gherkin_intent_alignment(
    spec_content: str,
    gherkin_text: str,
    *,
    min_keyword_overlap: float = 0.20,
) -> float:
    """R127.E.2 — convenience wrapper for R126.Q dimension.

    Returns 1.0 when alignment ≥ threshold, 0.0 otherwise. Binary so
    aggregate-mean math composes cleanly with the other R126.Q dims.

    For projects where the operator wants a continuous score instead
    of binary, call `validate_intent_alignment()` directly + read
    `alignment_score`.
    """
    result = validate_intent_alignment(
        spec_content, gherkin_text,
        min_keyword_overlap=min_keyword_overlap,
    )
    return 1.0 if result["passes_threshold"] else 0.0
