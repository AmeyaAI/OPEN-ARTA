"""ARTA GitHub Integration — async client for check runs and PR comments."""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class GitHubClient:
    """Async GitHub client using httpx for the REST API v3.

    Config via env vars: GITHUB_TOKEN, GITHUB_REPO (owner/repo format).
    """

    def __init__(
        self,
        token: str | None = None,
        repo: str | None = None,
    ):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.repo = repo or os.environ.get("GITHUB_REPO", "")  # owner/repo
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.token and self.repo)

    async def _ensure_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                base_url="https://api.github.com",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )

    async def create_check_run(
        self,
        head_sha: str,
        name: str = "ARTA Quality Gate",
        conclusion: str = "success",
        summary: str = "",
        details_url: str | None = None,
    ) -> dict[str, Any]:
        """Create a GitHub check run on a commit."""
        if not self.available:
            raise RuntimeError("GitHub client not configured")
        await self._ensure_client()
        body: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": f"ARTA: {conclusion.upper()}",
                "summary": summary,
            },
        }
        if details_url:
            body["details_url"] = details_url
        resp = await self._client.post(f"/repos/{self.repo}/check-runs", json=body)
        resp.raise_for_status()
        result = resp.json()
        logger.info("GitHub check run created: %s on %s", result.get("id"), head_sha[:8])
        return result

    async def update_check_run(
        self,
        check_run_id: int,
        conclusion: str,
        output: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Update an existing check run."""
        if not self.available:
            raise RuntimeError("GitHub client not configured")
        await self._ensure_client()
        body: dict[str, Any] = {
            "status": "completed",
            "conclusion": conclusion,
        }
        if output:
            body["output"] = output
        resp = await self._client.patch(
            f"/repos/{self.repo}/check-runs/{check_run_id}", json=body
        )
        resp.raise_for_status()
        return resp.json()

    async def create_pr_comment(
        self,
        pr_number: int,
        body: str,
    ) -> dict[str, Any]:
        """Post a comment on a pull request."""
        if not self.available:
            raise RuntimeError("GitHub client not configured")
        await self._ensure_client()
        resp = await self._client.post(
            f"/repos/{self.repo}/issues/{pr_number}/comments",
            json={"body": body},
        )
        resp.raise_for_status()
        return resp.json()

    @property
    def _headers(self) -> dict[str, str]:
        """Auth headers for standalone requests (outside the persistent client)."""
        h: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def fetch_issues(
        self,
        owner: str,
        repo: str,
        labels: str = "",
        state: str = "open",
        limit: int = 50,
    ) -> list[dict]:
        """Fetch GitHub issues from a repository."""
        import httpx

        url = f"https://api.github.com/repos/{owner}/{repo}/issues"
        params: dict[str, Any] = {
            "state": state,
            "per_page": min(limit, 100),
            "sort": "created",
            "direction": "desc",
        }
        if labels:
            params["labels"] = labels

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=self._headers, params=params)
            resp.raise_for_status()
            issues = resp.json()
            # Filter out pull requests (GitHub API returns PRs as issues)
            return [i for i in issues if "pull_request" not in i][:limit]

    async def fetch_readme(self, owner: str, repo: str) -> str:
        """Fetch README.md content from a repository."""
        import httpx

        url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                headers={**self._headers, "Accept": "application/vnd.github.raw+json"},
            )
            if resp.status_code == 200:
                return resp.text
            return ""

    async def fetch_directory_structure(
        self,
        owner: str,
        repo: str,
        path: str = "",
    ) -> list[dict]:
        """List files/directories in a repo path."""
        import httpx

        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=self._headers)
            if resp.status_code == 200:
                items = resp.json()
                if isinstance(items, list):
                    return [
                        {"name": i["name"], "type": i["type"], "path": i["path"]}
                        for i in items
                    ]
            return []

    async def close(self):
        """Cleanup the httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    # C6: Async context manager support so callers can do `async with GitHubClient() as gh:`
    # — guarantees the httpx pool is closed even on exception paths.
    async def __aenter__(self):
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
