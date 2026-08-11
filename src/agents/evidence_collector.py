"""F8-6: BMAD-TEA Layer 6 — Evidence Collection.

Before this agent, evidence collection was a 4-line inline block in
`orchestrator._run_evidence_collection()` that hard-coded the `screenshot` /
`har_file` keys. That made the orchestrator's `self._agents` dict a 7-of-9
shape (vs the 9-layer BMAD spec) and there was no way to swap the collector
out for a project that uses different artifact types (Cypress videos, k6
summary JSONs, ZAP report HTML, etc.).

This agent makes Layer 6 a first-class, swappable participant — same dispatch
shape as every other agent in `orchestrator._agents`. It still does
exactly what the inline block did (best-effort path collection), so behaviour
is preserved; the win is consistency + extensibility.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("arta.evidence")


# Maps from execution_result keys to the evidence "kind" we tag the path with.
# Keep this in sync with `tests_helpers._EVIDENCE_TARGETS_BY_TOOL` so the
# generation-time stamp and the execution-time collection use the same
# vocabulary.
_RESULT_KEYS_TO_EVIDENCE_KIND: dict[str, str] = {
    "screenshot":   "screenshot",
    "video":        "video",
    "trace":        "trace",
    "har_file":     "har",
    "summary_json": "summary_json",
    "zap_report":   "zap_report_html",
    "pytest_report": "pytest_report_json",
    "stdout_log":   "stdout_log",
}


class EvidenceCollectorAgent:
    """Collect immutable execution artifacts for audit + replay.

    `collect()` walks each execution result, extracts the artifact paths it
    declares, and returns a flat list of `{kind, path, run_id?}` dicts. The
    orchestrator stamps these onto `ctx.evidence_paths`.
    """

    async def collect(self, execution_results: list[dict]) -> list[dict]:
        evidence: list[dict] = []
        for result in execution_results or []:
            # F9-4: defensive isinstance — a stray non-dict (None, string, etc.)
            # used to raise AttributeError on `.get()`, taking down the whole
            # evidence collection stage. Skip and log instead.
            if not isinstance(result, dict):
                log.warning("EvidenceCollectorAgent: skipping non-dict result %r", type(result).__name__)
                continue
            run_id = result.get("run_id") or result.get("test_id")
            test_id = result.get("test_id")
            for key, kind in _RESULT_KEYS_TO_EVIDENCE_KIND.items():
                value = result.get(key)
                if not value:
                    continue
                if isinstance(value, list):
                    for path in value:
                        if path:
                            evidence.append({"kind": kind, "path": str(path), "run_id": run_id, "test_id": test_id})
                else:
                    evidence.append({"kind": kind, "path": str(value), "run_id": run_id, "test_id": test_id})
        log.debug("EvidenceCollectorAgent gathered %d artifact(s) from %d result(s)",
                  len(evidence), len(execution_results or []))
        return evidence

    # ── Phase 3.3 — Manifest builder with sha256 + existence check ───────────
    # The reference Layer 6 spec calls for an immutable manifest mapping
    # artifact → test_id → run_id with content hashes for tamper-evidence.
    # `build_manifest` walks each evidence path, hashes it with sha256 (only
    # when readable), and partitions missing files into `evidence_orphans`
    # so callers can quarantine them rather than including unverifiable rows.

    async def build_manifest(
        self,
        run_id: str,
        execution_results: list[dict],
    ) -> dict[str, Any]:
        """Build the per-run evidence manifest.

        Returns:
            {
              "run_id": str,
              "evidence": [
                {"kind", "path", "test_id", "size_bytes", "sha256", "modified_at"}
              ],
              "evidence_orphans": [
                {"kind", "path", "test_id", "reason"}    # missing / unreadable
              ],
              "generated_at": ISO,
            }

        sha256 is computed in 8 KB chunks so a 1 GB trace.zip doesn't blow
        memory. Files that don't exist are quarantined to `evidence_orphans`
        rather than included with a fake hash; the gate reads only `evidence`.
        """
        import hashlib
        from datetime import datetime, timezone
        from pathlib import Path

        raw = await self.collect(execution_results)
        evidence: list[dict[str, Any]] = []
        orphans: list[dict[str, Any]] = []
        seen: set[str] = set()  # de-dup by absolute path

        for entry in raw:
            path_str = entry.get("path", "")
            if not path_str:
                orphans.append({**entry, "reason": "empty_path"})
                continue
            p = Path(path_str)
            try:
                resolved = str(p.resolve())
            except OSError:
                resolved = path_str
            if resolved in seen:
                continue
            seen.add(resolved)
            if not p.is_file():
                orphans.append({**entry, "reason": "missing_or_not_file"})
                continue
            try:
                stat = p.stat()
                size_bytes = stat.st_size
                mtime = stat.st_mtime
                hasher = hashlib.sha256()
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        hasher.update(chunk)
                evidence.append({
                    **entry,
                    "size_bytes": size_bytes,
                    "sha256": hasher.hexdigest(),
                    "modified_at": mtime,
                })
            except Exception as exc:
                log.warning("evidence manifest: hash failed for %s: %s", p, exc)
                orphans.append({**entry, "reason": f"hash_failed:{type(exc).__name__}"})

        manifest = {
            "run_id": run_id,
            "evidence": evidence,
            "evidence_orphans": orphans,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        log.info(
            "evidence manifest for %s: %d artifacts hashed, %d orphans quarantined",
            run_id, len(evidence), len(orphans),
        )
        return manifest
