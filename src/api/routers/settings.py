"""ARTA Settings API — runtime LLM provider switching.

Endpoints:
  GET  /api/settings/llm           Resolved provider/model + available providers and models
  PUT  /api/settings/llm           Switch provider in-place (no container restart)
  POST /api/settings/llm/test      Ping the provider with a small prompt; return latency

Provider-state contract:
  app.state.anthropic     — the active LLM client (Anthropic | OllamaDirectClient | ClaudeCLIClient)
  app.state.llm_provider  — string label: "ollama" | "anthropic" | "claude_code"
"""
from __future__ import annotations

import logging
import os
import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel


# F4-1: API-key check is centralised in src/api/dependencies.py.
from ..dependencies import require_api_key as _require_api_key  # noqa: E402


log = logging.getLogger("arta.settings")
router = APIRouter(prefix="/settings", tags=["settings"])


class LLMSettingsBody(BaseModel):
    provider: Literal["ollama", "anthropic", "claude_code"]
    model: str | None = None
    base_url: str | None = None
    # api_key only honoured for provider="anthropic"
    api_key: str | None = None


@router.get("/llm", dependencies=[Depends(_require_api_key)])
async def get_llm_settings(request: Request):
    """Return current resolved provider, model, and available models per provider."""
    state = request.app.state
    provider = getattr(state, "llm_provider", None)

    # Available Ollama models (live query)
    ollama_models: list[str] = []
    ollama_base = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    try:
        import httpx
        resp = httpx.get(f"{ollama_base}/api/tags", timeout=3)
        if resp.status_code == 200:
            ollama_models = [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception as exc:
        log.debug("Could not list Ollama models: %s", exc)

    # CLI binary status
    cli_path = os.environ.get("CLAUDE_CLI_PATH", "claude")
    cli_available = False
    cli_version = None
    try:
        import subprocess
        result = subprocess.run([cli_path, "--version"], capture_output=True, timeout=3, text=True)
        cli_available = result.returncode == 0
        cli_version = result.stdout.strip() if cli_available else None
    except Exception:
        pass

    return {
        "current": {
            "provider": provider,
            "model": os.environ.get("ARTA_LLM_MODEL"),
            "base_url": ollama_base if provider == "ollama" else None,
        },
        "available": {
            "ollama": {
                "reachable": bool(ollama_models),
                "base_url": ollama_base,
                "models": ollama_models,
            },
            "anthropic": {
                "reachable": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "models": [
                    "claude-haiku-4-5-20251001",
                    "claude-sonnet-4-6",
                    "claude-opus-4-7",
                ],
            },
            "claude_code": {
                "reachable": cli_available,
                "cli_path": cli_path,
                "version": cli_version,
            },
        },
    }


@router.put("/llm", dependencies=[Depends(_require_api_key)])
async def update_llm_settings(body: LLMSettingsBody, request: Request):
    """Switch LLM provider in-place. Existing in-flight requests use the snapshot
    they captured at request-start; new requests use the swapped client.
    """
    # Push body into env so _try_init_llm reads the new values
    os.environ["ARTA_LLM_PROVIDER"] = body.provider
    if body.model:
        os.environ["ARTA_LLM_MODEL"] = body.model
    if body.base_url:
        os.environ["OLLAMA_BASE_URL"] = body.base_url
    if body.api_key and body.provider == "anthropic":
        os.environ["ANTHROPIC_API_KEY"] = body.api_key

    # Recreate the client
    from ..main import _try_init_llm
    new_client = _try_init_llm(body.provider)
    if new_client is None:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to initialize {body.provider} — see container logs for details",
        )

    # I3: Set both attributes so legacy `app.state.anthropic` callers still work.
    request.app.state.llm_client = new_client
    request.app.state.anthropic = new_client
    request.app.state.llm_provider = body.provider

    log.info("Settings: switched LLM provider to %s (model=%s)",
             body.provider, body.model or os.environ.get("ARTA_LLM_MODEL"))

    return {
        "status": "applied",
        "provider": body.provider,
        "model": os.environ.get("ARTA_LLM_MODEL"),
        "message": "New requests will use the updated provider. In-flight requests are unaffected.",
    }


@router.post("/llm/test", dependencies=[Depends(_require_api_key)])
async def test_llm_connection(request: Request):
    """Send a tiny prompt to the current provider; return latency + sample response."""
    client = getattr(request.app.state, "anthropic", None)
    if client is None:
        raise HTTPException(status_code=503, detail="No LLM client configured")
    started = time.monotonic()
    try:
        resp = await client.messages.create(
            model=os.environ.get("ARTA_LLM_MODEL", "arta-qwen-pro:latest"),
            max_tokens=50,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            timeout=30.0,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        text = resp.content[0].text if resp.content else ""
        return {
            "status": "ok",
            "provider": getattr(request.app.state, "llm_provider", "?"),
            "latency_ms": elapsed_ms,
            "sample_response": text[:200],
        }
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "status": "error",
            "provider": getattr(request.app.state, "llm_provider", "?"),
            "latency_ms": elapsed_ms,
            "error": f"{type(exc).__name__}: {exc}",
        }
