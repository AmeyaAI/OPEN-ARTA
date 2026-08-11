"""
Ollama Direct Client — bypasses LiteLLM for reliable Ollama API calls.

Implements an Anthropic SDK-compatible interface so agents written for
`AsyncAnthropic` work unchanged. Talks to Ollama's /api/chat endpoint via httpx.

Behaviour the agents depend on:
  - `client.messages.create(model=..., max_tokens=..., messages=[...], **kwargs)`
  - Returns an object with `.content[0].text`
  - May be called with `timeout=` (seconds) per-call

Provider-specific behaviour:
  - Maps Anthropic-style model names (claude-haiku/sonnet/opus) to Ollama
    models via env vars ARTA_FAST_MODEL / ARTA_PRIMARY_MODEL / ARTA_DEEP_MODEL.
  - Strips Qwen3 <think>...</think> blocks if they leak through despite think=False.
  - Auto-enables Ollama JSON mode when the prompt asks for JSON-only output.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger("arta.ollama")


# ── Model routing — map Claude-style names to configured Ollama models ──────
# The agents pass model="claude-sonnet-4-6" or "claude-haiku-4-5-20251001".
# We resolve those to the user's local Ollama models per task tier.
def _claude_to_ollama(requested_model: str | None, default_model: str) -> str:
    if not requested_model:
        return default_model
    lowered = requested_model.lower()
    if "haiku" in lowered:
        return os.environ.get("ARTA_FAST_MODEL", "qwen3:8b")
    if "opus" in lowered:
        return os.environ.get("ARTA_DEEP_MODEL", "qwen3:32b")
    if "sonnet" in lowered:
        return os.environ.get("ARTA_PRIMARY_MODEL", default_model)
    # If it's already an Ollama model name (qwen, llama, etc.), pass through
    return requested_model


# Prompt fragments that signal we want strict JSON output → enable Ollama JSON mode.
_JSON_HINT_PATTERNS = (
    "output only valid json",
    "output only the json",
    "output only json",
    "return only valid json",
    "respond with json",
    "respond only with json",
    "json object below",
)


def _wants_json_mode(messages: list) -> bool:
    """Return True if the most-recent user/system message asks for JSON-only output."""
    for m in reversed(messages):
        content = (m.get("content") or "").lower()
        if any(p in content for p in _JSON_HINT_PATTERNS):
            return True
    return False


# Strip Qwen3 thinking-mode tokens that may leak into content despite think=False.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", flags=re.DOTALL)


class OllamaDirectClient:
    """Direct Ollama API client with Anthropic SDK-compatible interface.

    Usage:
        client = OllamaDirectClient("http://localhost:11434", "arta-qwen-pro:latest")
        response = await client.messages.create(
            model="claude-sonnet-4-6",   # routed to ARTA_PRIMARY_MODEL
            max_tokens=8000,
            messages=[{"role": "user", "content": "Hello"}],
            timeout=120.0,
        )
        print(response.content[0].text)
    """

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.messages = self  # Support client.messages.create() pattern

    async def create(self, *, model: str | None = None, max_tokens: int = 4096,
                     messages: list, system: str | None = None,
                     **kwargs: Any) -> "_OllamaResponse":
        """Call Ollama /api/chat and return Anthropic-compatible response.

        Honours kwargs:
          - timeout (float, seconds): per-request HTTP timeout (default 120s)
          - extra_headers (dict): ignored (Anthropic-only)
          - temperature, top_p, top_k: passed through to Ollama options
        """
        import httpx

        # Build message list with optional system prompt
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        for m in messages:
            msgs.append({
                "role": m.get("role", "user"),
                "content": m.get("content", ""),
            })

        # C3: Resolve Claude model names to configured Ollama models per task tier.
        resolved_model = _claude_to_ollama(model, self.model)

        # Per-call options (C2: trust caller's max_tokens, no 8192 cap)
        options: dict = {"num_predict": int(max_tokens)}
        if "temperature" in kwargs:
            options["temperature"] = kwargs["temperature"]
        if "top_p" in kwargs:
            options["top_p"] = kwargs["top_p"]
        if "top_k" in kwargs:
            options["top_k"] = kwargs["top_k"]

        body: dict = {
            "model": resolved_model,
            "messages": msgs,
            "stream": False,
            "think": False,  # disable Qwen3 thinking for stable structured output
            "options": options,
        }

        # C5: Auto-enable JSON mode when the prompt asks for JSON-only output.
        if _wants_json_mode(msgs):
            body["format"] = "json"

        # C1: Honour per-call timeout from kwargs (was hardcoded 90s).
        # Default 120s — generous enough for small models, tight enough to surface hangs.
        timeout = float(kwargs.get("timeout", 120.0))

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/api/chat",
                    json=body,
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                log.warning(
                    "Ollama call timed out after %.1fs (model=%s, max_tokens=%d). "
                    "Increase timeout or use a smaller model.",
                    timeout, resolved_model, max_tokens,
                )
                raise RuntimeError(f"Ollama timeout after {timeout:.1f}s ({resolved_model})") from exc
            except httpx.RequestError as exc:
                log.warning("Ollama request error to %s: %s", self.base_url, exc)
                raise RuntimeError(f"Ollama request error: {exc}") from exc

            if resp.status_code != 200:
                error_text = resp.text[:300]
                log.error("Ollama API error %d: %s", resp.status_code, error_text)
                raise RuntimeError(f"Ollama error {resp.status_code}: {error_text}")

            data = resp.json()
            return _OllamaResponse(data, resolved_model)


class _OllamaResponse:
    """Mimics Anthropic SDK Message response."""

    def __init__(self, data: dict, model: str = ""):
        text = data.get("message", {}).get("content", "")
        # C4: Strip Qwen3 <think>...</think> blocks that may leak through.
        if "<think>" in text:
            stripped = _THINK_BLOCK_RE.sub("", text).strip()
            if stripped:
                text = stripped
        self.content = [_ContentBlock(text)]
        self.model = data.get("model", model)
        # R79.1 KEYSTONE — map Ollama's `done_reason` to Anthropic-compatible
        # stop_reason. Pre-R79.1 the heuristic used only `done=True/False`
        # which set stop_reason="end_turn" even when output was truncated
        # at num_predict — the growing-budget retry wrapper never fired for
        # tools whose error path doesn't pre-upcast to _TokenLimitHit
        # (k6/axe/zap/playwright stayed stuck at their starting budget).
        _done_reason = (data.get("done_reason") or "").lower()
        if _done_reason == "length":
            self.stop_reason = "max_tokens"
        elif _done_reason == "stop":
            self.stop_reason = "end_turn"
        elif _done_reason in ("load", "unload"):
            self.stop_reason = "model_load_issue"
        elif _done_reason:
            self.stop_reason = _done_reason   # forward unknown reasons verbatim
        else:
            # Legacy fallback: pre-2024 Ollama servers lacked done_reason.
            self.stop_reason = "end_turn" if data.get("done") else "max_tokens"
        self.usage = {
            "input_tokens": data.get("prompt_eval_count", 0),
            "output_tokens": data.get("eval_count", 0),
        }


class _ContentBlock:
    """Mimics Anthropic SDK TextBlock."""

    def __init__(self, text: str):
        self.type = "text"
        self.text = text
