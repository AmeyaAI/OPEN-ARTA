"""ARTA Jira Integration — async wrapper around atlassian-python-api."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def adf_to_text(node: Any) -> str:
    """Flatten an Atlassian Document Format (ADF) body — or a plain string —
    into readable text. Jira Cloud returns `description`/`comment.body` as ADF
    (nested `{type, content, text}` nodes); older/string bodies pass through."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        # Render list items / paragraphs / headings with line breaks.
        parts = [adf_to_text(c) for c in (node.get("content") or [])]
        sep = "\n" if node.get("type") in ("paragraph", "heading", "listItem", "bulletList", "orderedList") else ""
        return sep.join(p for p in parts if p)
    if isinstance(node, list):
        return "\n".join(adf_to_text(c) for c in node if c)
    return ""


class JiraClient:
    """Async Jira client for ARTA defect filing and requirement ingestion.

    Uses `atlassian-python-api` (synchronous) wrapped with `asyncio.to_thread()`.
    Config via env vars: JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY.
    """

    def __init__(
        self,
        url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        project_key: str | None = None,
    ):
        self.url = url or os.environ.get("JIRA_URL", "")
        self.email = email or os.environ.get("JIRA_EMAIL", "")
        self.api_token = api_token or os.environ.get("JIRA_API_TOKEN", "")
        self.project_key = project_key or os.environ.get("JIRA_PROJECT_KEY", "PROJ")
        self._jira = None
        # R90.3 — surface unset-config in logs at instantiation time.
        # Pre-R90.3, missing env vars caused R37.4 auto-filing to
        # silently skip; 6 P0 sut_regression defects in run-1694af
        # never reached Jira and operators had NO signal. WARN once
        # per instantiation so the gap is visible in container logs.
        if not (self.url and self.email and self.api_token):
            _missing = [
                k for k, v in [
                    ("JIRA_URL", self.url),
                    ("JIRA_EMAIL", self.email),
                    ("JIRA_API_TOKEN", self.api_token),
                ] if not v
            ]
            logger.warning(
                "R90.3: Jira integration DISABLED — missing env: %s. "
                "R37.4 auto-filing of sut_regression defects will "
                "silently skip until configured. Set %s in arta-api "
                "container env to enable defect → Jira close-loop.",
                _missing, ", ".join(_missing),
            )

    @property
    def available(self) -> bool:
        return self._jira is not None

    async def connect(self) -> bool:
        """Initialise the Jira client. Returns True if connection succeeds."""
        if not self.url or not self.email or not self.api_token:
            logger.info("Jira not configured (missing JIRA_URL/JIRA_EMAIL/JIRA_API_TOKEN)")
            return False
        try:
            from atlassian import Jira
            self._jira = Jira(
                url=self.url,
                username=self.email,
                password=self.api_token,
                cloud=True,
            )
            # Verify connection
            await asyncio.to_thread(self._jira.myself)
            logger.info("Jira connected to %s as %s", self.url, self.email)
            return True
        except Exception as exc:
            logger.warning("Jira connection failed: %s", exc)
            self._jira = None
            return False

    # ── Issue operations ─────────────────────────────────────────────────

    async def create_issue(
        self,
        summary: str,
        description: str,
        issue_type: str = "Bug",
        priority: str = "High",
        labels: list[str] | None = None,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a Jira issue. Returns {key, id, self} dict."""
        if not self.available:
            raise RuntimeError("Jira client not connected")
        fields = {
            "project": {"key": project_key or self.project_key},
            "summary": summary,
            "description": description,
            "issuetype": {"name": issue_type},
            "priority": {"name": priority},
        }
        if labels:
            fields["labels"] = labels
        result = await asyncio.to_thread(self._jira.create_issue, fields=fields)
        return result

    async def get_issue(self, jira_key: str) -> dict[str, Any]:
        """Fetch a single Jira issue by key (e.g. PROJ-123)."""
        if not self.available:
            raise RuntimeError("Jira client not connected")
        return await asyncio.to_thread(self._jira.issue, jira_key)

    async def update_issue(self, jira_key: str, fields: dict[str, Any]) -> None:
        """Update fields on an existing Jira issue."""
        if not self.available:
            raise RuntimeError("Jira client not connected")
        await asyncio.to_thread(self._jira.update_issue_field, jira_key, fields)

    async def search_issues(self, jql: str, max_results: int = 50) -> list[dict]:
        """Execute a JQL search and return matching issues."""
        if not self.available:
            raise RuntimeError("Jira client not connected")
        results = await asyncio.to_thread(
            self._jira.jql, jql, limit=max_results
        )
        return results.get("issues", [])

    async def fetch_issue_enriched(
        self,
        key: str,
        *,
        with_links: bool = True,
        with_subtasks: bool = True,
        with_comments: bool = True,
        max_comments: int = 12,
    ) -> dict[str, Any]:
        """Fetch an issue AND enrich it from linked tickets + subtasks +
        comments into a single text blob suitable for AC synthesis.

        The base `search_issues`/`issue` return only the default fields; this
        traverses `issuelinks`, `subtasks`, and `comment.comments` (all ADF)
        so RequirementIntelAgent sees the FULL context an author would — not
        just the one-line summary. Returns
        {key, summary, priority, components, labels, enriched_text, raw}.
        """
        if not self.available:
            raise RuntimeError("Jira client not connected")
        issue = await asyncio.to_thread(self._jira.issue, key)
        f = (issue or {}).get("fields", {}) or {}
        summary = f.get("summary", "") or ""
        desc = adf_to_text(f.get("description"))
        parts: list[str] = [f"# {key}: {summary}", ""]
        if desc:
            parts += [desc, ""]
        if with_links:
            link_txt: list[str] = []
            for link in (f.get("issuelinks") or []):
                for side in ("inwardIssue", "outwardIssue"):
                    li = link.get(side)
                    if li:
                        rel = (link.get("type") or {}).get(
                            "inward" if side == "inwardIssue" else "outward") or "related"
                        link_txt.append(
                            f"- ({rel}) {li.get('key')}: "
                            f"{((li.get('fields') or {}).get('summary')) or ''}")
            if link_txt:
                parts += ["## Linked issues", *link_txt, ""]
        if with_subtasks:
            subs = [
                f"- {s.get('key')}: {((s.get('fields') or {}).get('summary')) or ''}"
                for s in (f.get("subtasks") or [])
            ]
            if subs:
                parts += ["## Subtasks", *subs, ""]
        if with_comments:
            comments: list[str] = []
            for c in ((f.get("comment") or {}).get("comments") or []):
                ct = adf_to_text(c.get("body")).strip()
                if ct:
                    comments.append(f"- {ct}")
            if comments:
                parts += ["## Comments (author context / defect notes)",
                          *comments[:max_comments], ""]
        return {
            "key": key,
            "summary": summary,
            "priority": (f.get("priority") or {}).get("name"),
            "components": [c.get("name") for c in (f.get("components") or [])],
            "labels": f.get("labels") or [],
            "issuetype": (f.get("issuetype") or {}).get("name"),
            "status": (f.get("status") or {}).get("name"),
            "enriched_text": "\n".join(parts).strip(),
            "raw": issue,
        }

    async def fetch_stories(
        self,
        sprint_id: str | None = None,
        epic_key: str | None = None,
    ) -> list[dict]:
        """Fetch user stories from a sprint or epic for requirement ingestion."""
        if not self.available:
            raise RuntimeError("Jira client not connected")

        jql_parts = [f'project = "{self.project_key}"', 'issuetype = Story']
        if sprint_id:
            jql_parts.append(f'sprint = {sprint_id}')
        if epic_key:
            jql_parts.append(f'"Epic Link" = {epic_key}')
        jql = " AND ".join(jql_parts)

        return await self.search_issues(jql)

    # ── Priority mapping ─────────────────────────────────────────────────

    @staticmethod
    def severity_to_jira_priority(severity: str) -> str:
        """Map ARTA severity (P0-P3) to Jira priority names."""
        return {
            "P0": "Highest",
            "P1": "High",
            "P2": "Medium",
            "P3": "Low",
        }.get(severity, "Medium")
