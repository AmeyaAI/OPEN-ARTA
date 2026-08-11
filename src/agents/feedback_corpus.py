"""G4.2 / I10 minimal — Per-project feedback corpus.

Records normalized failure signatures from test execution results so subsequent
regeneration attempts can inject "avoid these patterns" advisory hints into
the LLM prompt. Closes the architecture-level learning loop.

Storage: per-project JSON file at .arta/feedback_corpus_{project_id}.json
API:
    FeedbackCorpus.record(project_id, test_id, tool, error_message)
    FeedbackCorpus.top_signatures(project_id, tool=None, limit=5) -> list[str]
    FeedbackCorpus.format_advisory_block(project_id, tool=None) -> str
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any

log = logging.getLogger("arta.feedback_corpus")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORPUS_DIR = _REPO_ROOT / ".arta"
_CORPUS_DIR.mkdir(parents=True, exist_ok=True)


def _corpus_path(project_id: str) -> Path:
    safe = (project_id or "default").replace("/", "_")[:80]
    return _CORPUS_DIR / f"feedback_corpus_{safe}.json"


# ── Signature normalization ───────────────────────────────────────────────────

# Strip volatile identifiers so "Selector .btn-abc not found" and "Selector .btn-xyz not found"
# collapse to the same signature.
_NORM_RULES = [
    (re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'), '<uuid>'),
    (re.compile(r'\b0x[0-9a-f]+\b', re.I), '<hex>'),
    (re.compile(r'\b\d{4,}\b'), '<n>'),
    (re.compile(r'"[^"]{20,}"'), '"<str>"'),
    (re.compile(r"'[^']{20,}'"), "'<str>'"),
    (re.compile(r'\b\d+\.\d+\b'), '<float>'),
    (re.compile(r'\b[A-Z][A-Z0-9_]{3,}\b'), '<CONST>'),
    (re.compile(r'(at\s+)[A-Za-z0-9_\.]+(:\d+)?\b'), r'\1<frame>'),  # stack frames
]


def normalize_signature(error_message: str, max_len: int = 200) -> str:
    """Normalize an error message into a dedup-friendly signature."""
    if not error_message:
        return ""
    sig = str(error_message)[:max_len * 3]
    for pattern, replacement in _NORM_RULES:
        sig = pattern.sub(replacement, sig)
    sig = re.sub(r'\s+', ' ', sig).strip()
    return sig[:max_len]


# ── Storage ──────────────────────────────────────────────────────────────────


class _FeedbackCorpus:
    """Thread-safe feedback corpus (file-backed, in-memory cache)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}  # project_id → corpus dict

    def _load(self, project_id: str) -> dict:
        if project_id in self._cache:
            return self._cache[project_id]
        path = _corpus_path(project_id)
        if path.exists():
            try:
                self._cache[project_id] = json.loads(path.read_text())
            except Exception as exc:
                log.debug("feedback_corpus: load failed for %s: %s", project_id, exc)
                self._cache[project_id] = {"by_tool": {}}
        else:
            self._cache[project_id] = {"by_tool": {}}
        return self._cache[project_id]

    def _save(self, project_id: str) -> None:
        try:
            path = _corpus_path(project_id)
            path.write_text(json.dumps(self._cache[project_id], indent=2))
        except Exception as exc:
            log.debug("feedback_corpus: save failed for %s: %s", project_id, exc)

    def record(
        self,
        project_id: str,
        test_id: str,
        tool: str,
        error_message: str,
    ) -> None:
        """Record a test failure signature. Truncates per-tool list to 500 entries."""
        if not error_message or not project_id:
            return
        sig = normalize_signature(error_message)
        if not sig:
            return
        with self._lock:
            corpus = self._load(project_id)
            by_tool = corpus.setdefault("by_tool", {})
            entries = by_tool.setdefault(tool or "unknown", [])
            entries.append({"sig": sig, "test_id": test_id})
            # Cap per-tool entry list
            if len(entries) > 500:
                del entries[:-500]
            self._save(project_id)

    def top_signatures(
        self,
        project_id: str,
        tool: str | None = None,
        limit: int = 5,
    ) -> list[tuple[str, int]]:
        """Return top-N most frequent signatures as (signature, count) tuples."""
        if not project_id:
            return []
        with self._lock:
            corpus = self._load(project_id)
            by_tool = corpus.get("by_tool", {})
            candidates: list[str] = []
            if tool:
                candidates = [e["sig"] for e in by_tool.get(tool, []) if e.get("sig")]
            else:
                for entries in by_tool.values():
                    candidates.extend(e["sig"] for e in entries if e.get("sig"))
            counter = Counter(candidates)
            return counter.most_common(limit)

    def format_advisory_block(
        self,
        project_id: str,
        tool: str | None = None,
        limit: int = 5,
    ) -> str:
        """Return a prompt-injectable advisory block. Empty string when no history."""
        top = self.top_signatures(project_id, tool=tool, limit=limit)
        if not top:
            return ""
        lines = [
            "RECENT FAILURES IN THIS PROJECT (advisory — avoid these patterns if applicable):"
        ]
        for sig, count in top:
            lines.append(f"  - ({count}×) {sig}")
        lines.append("")
        return "\n".join(lines)


# Singleton
feedback_corpus = _FeedbackCorpus()
