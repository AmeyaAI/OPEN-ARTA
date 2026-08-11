"""R127.D.6.E — Single-source-of-truth for R102.A grounding stamps.

Both PW (TS comment-header) and pytest (Python comment-header) callers
delegate stamp composition + idempotency-guard logic here. Block kind
strings live as module constants so any future enum update is one-line.

Pre-R127.D.6.E: `_r102_a_stamp_violations` lived only at
automation_engineer.py:6568 (PW path). R127.D.6.A's post-merge balance
check + R127.D.6.D's pytest AST-validation each needed the same stamp
mechanism, threatening duplication. R127.D.6.E pulls the shape into one
module so all 3 callers consume the same canonical implementation:

  1. `src/agents/automation_engineer.py:6568` — `_r102_a_stamp_violations`
      method body delegates to `stamp_pw_violations` here.
  2. `src/agents/requirement_decomposer.py` — R127.D.6.A's
     `_r127_d6_apply_balance_stamp` calls `stamp_pw_violations` directly.
  3. `src/agents/requirement_decomposer.py` — R127.D.6.D's
     pytest AST-validation wire calls `stamp_pytest_violations` directly.

Zero external deps; imports only `json` + standard typing. No circular
import risk: this module imports nothing from automation_engineer or
requirement_decomposer.
"""
from __future__ import annotations

import json
from typing import Any

# Canonical block-kind constants referenced by:
#   - R102.C dispatch reader at src/api/routers/execution.py:3336-3456
#   - R118.G defect classifier at src/agents/defect_intel.py:755-758
#   - R111.E violation_kinds aggregator at execution.py:3366-3368
PW_BLOCK_KIND = "playwright_grounding_violation"
PYTEST_BLOCK_KIND = "pytest_grounding_violation"


def _serialize_violation(v: Any) -> str | None:
    """Convert a GroundingViolation OR dict into the canonical JSON shape
    the R102.C reader at execution.py:3353 consumes.

    Accepts both shapes so callers can pass either GroundingViolation
    dataclass instances OR plain dicts (R127.D.6.A and R127.D.6.D pass
    plain dicts; automation_engineer passes dataclass instances).
    """
    try:
        if isinstance(v, dict):
            kind = v.get("kind", "?")
            symbol = v.get("symbol") or ""
            hint = v.get("hint") or ""
        else:
            kind = getattr(v, "kind", "?")
            symbol = getattr(v, "symbol", None) or ""
            hint = getattr(v, "hint", None) or ""
        return json.dumps({
            "kind": kind,
            "symbol": str(symbol)[:120],
            "hint": str(hint)[:240],
        })
    except Exception:
        return None


def stamp_pw_violations(content: str, violations: list[Any]) -> str:
    """R102.A-shape stamp for Playwright TypeScript specs.

    Prepends a TS comment-header block listing the grounding violations
    that R57.1's retry loop exhausted on. Dispatch-time R102.C reads
    this header and emits BLOCKED rows instead of dispatching the
    broken spec.

    Idempotent: if `_dispatch_block_kind: playwright_grounding_violation`
    is already present in the first 2KB of content, returns unchanged.

    Args:
        content: PW spec source (post-LLM, post-validators).
        violations: list of GroundingViolation objects OR plain dicts
            with `kind`, `symbol`, `hint` keys.

    Returns:
        content with comment-header prepended, OR content unchanged if
        no violations OR header already present.
    """
    if not violations:
        return content
    if f"_dispatch_block_kind: {PW_BLOCK_KIND}" in content[:2000]:
        return content
    lines = [
        "// ── ARTA _grounding_violations stamp (R102.A) ──",
        f"// _dispatch_block_kind: {PW_BLOCK_KIND}",
        "// _grounding_violations:",
    ]
    for v in violations[:10]:
        serialized = _serialize_violation(v)
        if serialized:
            lines.append(f"//   {serialized}")
    lines.append("// ────────────────────────────────────────────────")
    lines.append("")
    return "\n".join(lines) + content


def stamp_pytest_violations(content: str, violations: list[Any]) -> str:
    """R127.D.6.D-shape stamp for pytest Python modules.

    Same idempotency + JSON-shape contract as `stamp_pw_violations` but
    uses Python comment syntax (`#`) so the stamp is itself a syntactically
    valid Python comment block — does NOT prevent later `ast.parse()`
    attempts on the file (the stamp lines are ignored by the parser).

    Idempotent: if `_dispatch_block_kind: pytest_grounding_violation` is
    already present in the first 2KB of content, returns unchanged.

    Args:
        content: pytest module source (post-merge).
        violations: list of GroundingViolation objects OR plain dicts.

    Returns:
        content with Python-comment-header prepended, OR content unchanged
        if no violations OR header already present.
    """
    if not violations:
        return content
    if f"_dispatch_block_kind: {PYTEST_BLOCK_KIND}" in content[:2000]:
        return content
    lines = [
        "# ── ARTA _grounding_violations stamp (R127.D.6.D) ──",
        f"# _dispatch_block_kind: {PYTEST_BLOCK_KIND}",
        "# _grounding_violations:",
    ]
    for v in violations[:10]:
        serialized = _serialize_violation(v)
        if serialized:
            lines.append(f"#   {serialized}")
    lines.append("# ────────────────────────────────────────────────")
    lines.append("")
    return "\n".join(lines) + content
