"""R134.D — Single-source-of-truth JSON extractor for LLM output.

Pre-R134.D: 4+ agent files (strategy_architect, defect_intel, dataset_recipe,
self_healing, plus the dead-code _parse_json in requirement_intel) each had
their own copy of the JSON-from-LLM-output extraction logic. Ollama outputs
(especially qwen-pro 32B) wrap JSON in ```json ... ``` markdown fences much
more often than Claude → the greedy `{.*}` / `[.*]` regex could grab from
the first `{` in surrounding prose to the last `}` in conclusion text →
invalid JSON → silent fallback to {} or [].

Post-R134.D: one canonical `extract_json_from_llm_output()` shared across
all agents. Fence-strip happens BEFORE the greedy regex, so Ollama outputs
survive the round-trip. Any future tweak (new model output convention,
additional pre-cleaning) lands in one place.
"""
from __future__ import annotations

import json
import re
from typing import Any


# R134.D — markdown-fence stripper. Matches ```json ... ``` AND bare
# ``` ... ``` blocks. Non-greedy quantifier so multi-block outputs
# take the FIRST block (Ollama convention: canonical answer first,
# explanation blocks after).
_R134_D_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```", re.MULTILINE)


# Reasoning-block stripper. Qwen / DeepSeek / o1 emit `<think>...</think>`
# blocks that may themselves contain bracket characters — strip first so the
# greedy regex doesn't capture from inside the thinking block.
_THINK_BLOCK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_BARE_THINK_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)


def extract_json_from_llm_output(text: str, default: Any = None) -> Any:
    """R134.D — robustly extract JSON from LLM output.

    Pre-cleaning order:
      1. Strip `<think>...</think>` reasoning blocks (qwen/deepseek/o1)
      2. Strip ```json ... ``` markdown fences (Ollama, especially qwen-pro)
      3. Direct json.loads attempt
      4. Greedy `{.*}` then `[.*]` regex fallback

    Returns `default` (or empty dict if not specified) when extraction
    fails. Callers that need raise-on-failure can wrap this in their own
    handler — the bare extractor is fail-soft per existing agent contract.
    """
    if default is None:
        default = {}
    if not text:
        return default
    text = str(text).strip()
    # 1. Strip reasoning blocks
    text = _THINK_BLOCK_RE.sub("", text)
    text = _BARE_THINK_TAG_RE.sub("", text)
    text = text.strip()
    # 2. R134.D — strip markdown fences BEFORE greedy regex. Use the FIRST
    # fence block (LLMs sometimes emit explanation prose after the JSON,
    # which may contain its own ``` examples).
    _fence_match = _R134_D_FENCE_RE.search(text)
    if _fence_match:
        text = _fence_match.group(1).strip()
    # 3. Direct parse (model returned clean JSON)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 4. Fallback: greedy {...} or [...] block
    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                continue
    return default
