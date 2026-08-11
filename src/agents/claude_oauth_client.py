"""
Claude Code OAuth Client — Bearer token authentication for Claude Code tokens.

Claude Code tokens (sk-ant-oat01-*) require `Authorization: Bearer` headers,
not the `x-api-key` header used by the standard Anthropic SDK.

This client wraps httpx to make direct API calls with the correct auth headers,
matching the pattern used by OpenClaw.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("arta.claude_oauth")


class ClaudeOAuthClient:
    """Anthropic Messages API client using OAuth Bearer auth (for Claude Code tokens).

    Provides a `.messages.create()` interface compatible with the Anthropic SDK
    so it can be used as a drop-in replacement for `AsyncAnthropic`.
    """

    API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, token: str):
        self.token = token
        self.messages = self  # Support client.messages.create() pattern

    async def create(self, *, model: str, max_tokens: int, messages: list,
                     system: str | None = None, **kwargs: Any) -> _OAuthResponse:
        """Call the Anthropic Messages API with Bearer auth."""
        import httpx

        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system

        headers = {
            "Authorization": f"Bearer {self.token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.API_URL,
                headers=headers,
                json=body,
                timeout=120.0,
            )
            if resp.status_code != 200:
                error_text = resp.text
                log.error("Claude OAuth API error %d: %s", resp.status_code, error_text[:500])
                raise Exception(f"Claude API error {resp.status_code}: {error_text[:200]}")

            data = resp.json()
            return _OAuthResponse(data)


class _OAuthResponse:
    """Mimics the Anthropic SDK response object structure."""

    def __init__(self, data: dict):
        self.id = data.get("id", "")
        self.type = data.get("type", "message")
        self.role = data.get("role", "assistant")
        self.content = [_ContentBlock(b) for b in data.get("content", [])]
        self.model = data.get("model", "")
        self.stop_reason = data.get("stop_reason", "")
        self.usage = data.get("usage", {})


class _ContentBlock:
    """Mimics an Anthropic SDK content block."""

    def __init__(self, block: dict):
        self.type = block.get("type", "text")
        self.text = block.get("text", "")
