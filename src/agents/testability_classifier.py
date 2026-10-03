"""Deterministic test-automatability classifier (no LLM).

A requirement is "automatable" when an executable UI/API test can meaningfully
verify it. Some requirements are genuinely NOT automatable — they are verified by
inspection, not by a running test:

  * CVE-triage / vulnerability-tracking tasks ("Exclude etcd CVE from scope",
    "Track idna advisory out-of-scope") — there is no behaviour to exercise.
  * Dependency / license version bumps ("Bump golang.org/x/net 0.17 -> 0.23",
    "Upgrade React to 19") — verified by the build/CI, not an ATDD test.
  * Pure spike / research / documentation / tracking tasks.

Forcing test-generation on these produces 100%-failing hollow specs that pollute
the quality signal (the "coverage for coverage's sake" anti-pattern). This gate
flags them so `generate-all` can SKIP them with a truthful disposition
(`skip_reason="not_automatable"` + `metadata.not_automatable` + `needs_attention`)
instead of burning LLM quota. It NEVER deletes a requirement — the flag is a
V&V-traceable "verified by inspection" record, surfaced for human confirmation.

Design principles:
  * DETERMINISTIC + conservative — fail-OPEN. Anything plausibly testable stays
    automatable. We only fire on strong, unambiguous non-feature signals so a real
    feature is never silently excluded.
  * No LLM, no network — pure regex over the requirement's title/summary/type.
  * Killswitch: env ``ARTA_TESTABILITY_GATE_DISABLE=1`` forces every requirement
    automatable (classifier becomes a no-op).
"""

from __future__ import annotations

import os
import re
from typing import Any

__all__ = ["classify_automatable", "NOT_AUTOMATABLE_REASONS"]

# Human-readable reason codes stamped onto the requirement / surfaced in the UI.
NOT_AUTOMATABLE_REASONS = {
    "cve_triage": "CVE / vulnerability triage task — verified by inspection, not an executable test",
    "dependency_bump": "Dependency / version bump — verified by build/CI, not an ATDD test",
    "doc_spike_tracking": "Documentation / spike / tracking task — no runtime behaviour to exercise",
}

# --- CVE-triage / vulnerability tracking -------------------------------------
# Strong signal: a triage verb near a CVE/vuln/finding/advisory noun, OR an
# explicit CVE identifier, OR an out-of-scope marker on a security finding.
_CVE_ID = re.compile(r"\bCVE-\d{4}-\d{3,}\b", re.IGNORECASE)
_CVE_TRIAGE = re.compile(
    r"\b(exclude|excluding|track|tracking|accept|accepting|acknowledge|document|"
    r"documenting|triage|triaging|ignore|ignoring|waive|waiving|suppress|suppressing|"
    r"dismiss|out[\s-]?of[\s-]?scope|not[\s-]?applicable)\b"
    r"[^.\n]{0,60}?"
    r"\b(cve|cves|vulnerabilit(?:y|ies)|finding|findings|advisor(?:y|ies)|"
    r"security\s+(?:issue|alert|scan))\b",
    re.IGNORECASE,
)

# --- Dependency / license version bumps --------------------------------------
# Require the update verb AND a concrete version/package signal so ordinary
# feature titles ("Update user profile page") are NOT caught.
_BUMP_VERB = re.compile(
    r"^\s*(?:chore\s*[:(-]?\s*)?"
    r"(bump|upgrade|upgrading|update|updating|downgrade|pin|pinning|migrate|migrating)\b",
    re.IGNORECASE,
)
_VERSION_SIGNAL = re.compile(
    r"("
    r"\bfrom\s+v?\d+\.\d+"                 # "from 0.17 to 0.23"
    r"|\bto\s+v?\d+\.\d+"                  # "to v19.2"
    r"|\b(?:version\s+)?v?\d+\.\d+(?:\.\d+)?(?:-[\w.]+)?\b"  # concrete version 1.2.5-r23
    r"|\b(go\.mod|go\.sum|package\.json|package-lock|requirements\.txt|pyproject|"
    r"Dockerfile|base[\s-]?image|docker\s+image|helm\s+chart|npm|pip|yarn|"
    r"pnpm|cargo|maven|gradle)\b"          # explicit dep-file / package-manager
    r")",
    # NOTE (deliberately NOT signals): a bare slash module-path, or the generic
    # words dependency/package/module/library/version/component/image/chart on
    # their own — those over-fire on UI migration tasks ("Migrate tables to
    # sanctioned component"), which ARE plausibly UI-testable. Conservative:
    # a dependency bump must carry a concrete version OR a dep-file/manager token.
    re.IGNORECASE,
)

# --- Documentation / spike / tracking ----------------------------------------
_DOC_SPIKE = re.compile(
    r"^\s*(?:chore\s*[:(-]?\s*)?"
    r"(spike|research|investigat(?:e|ion)|poc|proof[\s-]of[\s-]concept|"
    r"documentation|docs?\b|readme|tracking|track)\b",
    re.IGNORECASE,
)
_DOC_PHRASE = re.compile(
    r"\b(documentation\s+only|doc(?:s|umentation)?[\s-]only|no\s+code\s+change|"
    r"tracking\s+(?:issue|ticket|epic)|umbrella\s+(?:issue|epic)|"
    r"placeholder\s+(?:issue|ticket))\b",
    re.IGNORECASE,
)


def _text_of(requirement: dict[str, Any]) -> tuple[str, str]:
    """Return (title, blob). `title` drives the anchored `^` patterns; `blob`
    (title + description + jira summary) drives the unanchored ones."""
    title = (
        requirement.get("title")
        or requirement.get("summary")
        or requirement.get("name")
        or ""
    )
    parts = [title, requirement.get("description") or ""]
    meta = requirement.get("metadata")
    if isinstance(meta, dict):
        jira = meta.get("jira")
        if isinstance(jira, dict):
            parts.append(jira.get("summary") or "")
    return str(title), "\n".join(str(p) for p in parts if p)


def classify_automatable(requirement: dict[str, Any]) -> tuple[bool, str, str]:
    """Classify one requirement.

    Returns ``(automatable, reason_code, reason_text)``:
      * ``automatable`` — True if a running test can meaningfully verify it
        (the conservative default). False only on a strong non-feature signal.
      * ``reason_code`` — "" when automatable, else one of
        ``NOT_AUTOMATABLE_REASONS`` keys.
      * ``reason_text`` — "" when automatable, else the human-readable reason.
    """
    if os.environ.get("ARTA_TESTABILITY_GATE_DISABLE", "").lower() in ("1", "true", "yes"):
        return True, "", ""

    title, blob = _text_of(requirement)
    if not blob.strip():
        # No text to judge — fail OPEN (stays automatable).
        return True, "", ""

    # 1. CVE-triage — check first (most unambiguous non-feature class).
    if _CVE_TRIAGE.search(blob) or (
        _CVE_ID.search(blob)
        and re.search(r"\b(exclude|track|accept|ignore|waive|out[\s-]?of[\s-]?scope|"
                      r"document|triage|suppress|dismiss)\b", blob, re.IGNORECASE)
    ):
        return False, "cve_triage", NOT_AUTOMATABLE_REASONS["cve_triage"]

    # 2. Dependency / version bump — verb at start AND a version/package signal.
    if _BUMP_VERB.search(title) and _VERSION_SIGNAL.search(blob):
        return False, "dependency_bump", NOT_AUTOMATABLE_REASONS["dependency_bump"]

    # 3. Documentation / spike / tracking.
    if _DOC_SPIKE.search(title) or _DOC_PHRASE.search(blob):
        return False, "doc_spike_tracking", NOT_AUTOMATABLE_REASONS["doc_spike_tracking"]

    # Default: automatable (fail-open — anything plausibly testable stays IN).
    return True, "", ""
