"""Phase J5 — auto-apply Neo4j Phase C schema fragment on app startup.

The canonical `src/graph/schema.cypher` is permission-locked (uid 7474,
mode 700) so it can't be merged into a single file by the dev process
that runs migrations. The Phase C constraints + indexes live in
`src/graph/schema_phase_c.cypher` instead.

This module reads that file and runs each `CREATE CONSTRAINT IF NOT
EXISTS` / `CREATE INDEX IF NOT EXISTS` statement via the async Neo4j
driver. Idempotent + non-blocking on failure (Phase 5.3 stub-mode
pattern).

Comments and blank lines are skipped. Statements are separated by `;`
at end of line — same convention cypher-shell uses.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("arta.graph.schema_loader")


_SCHEMA_PHASE_C_PATH = Path(__file__).parent / "schema_phase_c.cypher"
_SCHEMA_ARCH_PATH = Path(__file__).parent / "schema_arch_discovery.cypher"


def _split_statements(text: str) -> list[str]:
    """Split a Cypher file into individual statements.

    Strategy: strip line-comments (`//...`), accumulate non-comment lines,
    then split on `;`. Empty/whitespace-only statements filtered out.
    """
    cleaned: list[str] = []
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].rstrip()
        if line.strip():
            cleaned.append(line)
    joined = "\n".join(cleaned)
    return [s.strip() for s in joined.split(";") if s.strip()]


async def _apply_schema(neo4j_driver: Any, schema_path: Path, label: str) -> dict:
    """Apply idempotent DDL (CREATE CONSTRAINT/INDEX) from a cypher file.

    Returns `{"applied", "skipped", "errors", "path"}`. Always returns —
    never raises. No-op (stub mode) when the driver is None.
    """
    out: dict[str, Any] = {
        "applied": 0,
        "skipped": 0,
        "errors": [],
        "path": str(schema_path),
    }
    if neo4j_driver is None:
        out["skipped"] = -1
        out["errors"].append("neo4j driver is None — stub mode")
        return out

    if not schema_path.is_file():
        out["errors"].append(f"schema file not found: {schema_path}")
        return out

    try:
        text = schema_path.read_text(encoding="utf-8")
    except OSError as exc:
        out["errors"].append(f"read failed: {exc}")
        return out

    statements = _split_statements(text)
    log.info("%s schema: %d statements to apply", label, len(statements))

    try:
        async with neo4j_driver.session() as session:
            for stmt in statements:
                # Only run idempotent DDL — anything that's not a
                # CREATE CONSTRAINT/INDEX is a comment-driven artifact
                # (e.g. reference queries) that operators run manually.
                if not stmt.upper().startswith(("CREATE CONSTRAINT", "CREATE INDEX")):
                    out["skipped"] += 1
                    continue
                try:
                    await session.run(stmt)
                    out["applied"] += 1
                except Exception as exc:
                    msg = f"{stmt[:80]}…: {exc}"
                    out["errors"].append(msg)
                    log.warning("%s schema stmt failed: %s", label, msg)
    except Exception as exc:
        out["errors"].append(f"session error: {exc}")
        log.warning("%s schema apply: session error: %s", label, exc)

    log.info(
        "%s schema apply: applied=%d skipped=%d errors=%d",
        label, out["applied"], out["skipped"], len(out["errors"]),
    )
    return out


async def apply_phase_c_schema(neo4j_driver: Any) -> dict:
    """Apply the Phase C (CallChain/Endpoint/EnvVar) schema. No-op when driver None."""
    return await _apply_schema(neo4j_driver, _SCHEMA_PHASE_C_PATH, "Phase C")


async def apply_arch_discovery_schema(neo4j_driver: Any) -> dict:
    """Phase AD — apply Architecture Discovery schema (Service/Token/Scenario)."""
    return await _apply_schema(neo4j_driver, _SCHEMA_ARCH_PATH, "Phase AD")


__all__ = ["apply_phase_c_schema", "apply_arch_discovery_schema"]
