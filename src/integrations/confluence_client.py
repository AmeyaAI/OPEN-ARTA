"""ARTA Confluence Integration — async REST API client for requirement ingestion."""
from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


class ConfluenceClient:
    """Async Confluence client using httpx for REST API v2.

    Config via env vars: CONFLUENCE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN.
    """

    def __init__(
        self,
        url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
    ):
        self.url = (url or os.environ.get("CONFLUENCE_URL", "")).rstrip("/")
        self.email = email or os.environ.get("CONFLUENCE_EMAIL", "")
        self.api_token = api_token or os.environ.get("CONFLUENCE_API_TOKEN", "")
        self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    async def connect(self) -> bool:
        """Initialise the httpx client. Returns True if connection succeeds."""
        if not self.url or not self.email or not self.api_token:
            logger.info("Confluence not configured (missing CONFLUENCE_URL/EMAIL/API_TOKEN)")
            return False
        try:
            import httpx
            self._client = httpx.AsyncClient(
                base_url=self.url,
                auth=(self.email, self.api_token),
                headers={"Accept": "application/json"},
                timeout=30.0,
            )
            # Verify connectivity
            resp = await self._client.get("/wiki/api/v2/spaces?limit=1")
            resp.raise_for_status()
            logger.info("Confluence connected to %s as %s", self.url, self.email)
            return True
        except Exception as exc:
            logger.warning("Confluence connection failed: %s", exc)
            self._client = None
            return False

    async def get_page(self, page_id: str) -> dict[str, Any]:
        """Fetch a single page by ID with its body content."""
        if not self.available:
            raise RuntimeError("Confluence client not connected")
        resp = await self._client.get(
            f"/wiki/api/v2/pages/{page_id}",
            params={"body-format": "storage"},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "id": data.get("id"),
            "title": data.get("title", ""),
            "body": self._strip_html(data.get("body", {}).get("storage", {}).get("value", "")),
            "url": f"{self.url}/wiki/spaces/{data.get('spaceId', '')}/pages/{data.get('id', '')}",
        }

    async def get_space_pages(self, space_key: str, limit: int = 25) -> list[dict]:
        """Fetch pages from a Confluence space."""
        if not self.available:
            raise RuntimeError("Confluence client not connected")
        resp = await self._client.get(
            "/wiki/api/v2/spaces",
            params={"keys": space_key, "limit": 1},
        )
        resp.raise_for_status()
        spaces = resp.json().get("results", [])
        if not spaces:
            return []
        space_id = spaces[0]["id"]

        resp = await self._client.get(
            "/wiki/api/v2/pages",
            params={"space-id": space_id, "limit": limit, "body-format": "storage"},
        )
        resp.raise_for_status()
        pages = resp.json().get("results", [])
        return [
            {
                "id": p.get("id"),
                "title": p.get("title", ""),
                "body": self._strip_html(p.get("body", {}).get("storage", {}).get("value", "")),
                "url": f"{self.url}/wiki/spaces/{space_key}/pages/{p.get('id', '')}",
            }
            for p in pages
        ]

    @staticmethod
    def _strip_html(html: str) -> str:
        """Strip HTML tags to extract plain text for requirement parsing."""
        text = re.sub(r"<br\s*/?>", "\n", html)
        text = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        return text.strip()

    # C6: Async context manager + explicit close for httpx client lifecycle.
    # Without this, the httpx pool could leak connections across repeated instantiations.
    async def close(self) -> None:
        """Cleanup the httpx client. Safe to call multiple times."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:
                logger.warning("ConfluenceClient close error: %s", exc)
            finally:
                self._client = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
