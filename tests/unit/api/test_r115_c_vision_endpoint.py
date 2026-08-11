"""R115.C — /api/internal/vision-locate endpoint + telemetry.

Source-check approach (parallel to test_r115_g_mission_report.py): pin
the code path without needing live LLM. The endpoint's actual LLM call
is exercised in integration tests; the unit tests here confirm:
  1. Endpoint is registered + requires API key
  2. Telemetry endpoint exists + returns required counters
  3. Graceful-skip branch fires when LLM is unavailable / non-vision
  4. Bbox parsing path handles malformed LLM output gracefully
"""
from __future__ import annotations

import re
from pathlib import Path


_MAIN_PY = Path(__file__).resolve().parents[3] / "src" / "api" / "main.py"


def test_r115_c_vision_locate_endpoint_registered():
    """Source check: POST /api/internal/vision-locate exists with API-key dep."""
    content = _MAIN_PY.read_text()
    pattern = re.compile(
        r"@app\.post\(\"/api/internal/vision-locate\",\s*"
        r"dependencies=\[Depends\(_require_api_key\)\]\s*\)\s*"
        r"async def vision_locate",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R115.C.2: /api/internal/vision-locate endpoint missing OR not API-key gated"
    )


def test_r115_c_telemetry_endpoint_registered():
    """Source check: GET /api/internal/vision-locate-telemetry exposes counters."""
    content = _MAIN_PY.read_text()
    pattern = re.compile(
        r"@app\.get\(\"/api/internal/vision-locate-telemetry\".*?"
        r"async def vision_locate_telemetry",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R115.C.5: telemetry endpoint missing"
    )
    # Required counter fields surfaced on response
    tele_start = content.find("async def vision_locate_telemetry")
    window = content[tele_start:tele_start + 1500]
    for field in ("calls_total", "hits_total", "hit_rate", "latency_p95_ms"):
        assert f'"{field}"' in window, f"R115.C.5: telemetry missing field {field}"


def test_r115_c_graceful_skip_on_non_vision_llm():
    """Source check: endpoint returns null+source for non-vision providers (Ollama)."""
    content = _MAIN_PY.read_text()
    vl_start = content.find("async def vision_locate")
    window = content[vl_start:vl_start + 6000]
    # Provider gate: only anthropic / claude_code attempt vision call
    assert 'provider not in ("anthropic", "claude_code")' in window, (
        "R115.C.2: provider gate missing — Ollama / non-vision LLM should skip"
    )
    assert "no_vision_capable_llm" in window, (
        "R115.C.2: graceful-skip source marker missing"
    )


def test_r115_c_bbox_validation_strict():
    """Source check: bbox keys validated as numeric before returned."""
    content = _MAIN_PY.read_text()
    vl_start = content.find("async def vision_locate")
    window = content[vl_start:vl_start + 6000]
    # Validation: each of x/y/w/h must be int/float
    pattern = re.compile(
        r"isinstance\(_b\.get\(k\),\s*\(int,\s*float\)\)\s*for\s*k\s*in\s*"
        r"\(\"x\",\s*\"y\",\s*\"w\",\s*\"h\"\)",
        re.DOTALL,
    )
    assert pattern.search(window), (
        "R115.C.2: bbox numeric validation missing"
    )


def test_r115_c_cost_discipline_max_tokens_capped():
    """Source check: LLM call capped at 200 max_tokens (cost discipline)."""
    content = _MAIN_PY.read_text()
    vl_start = content.find("async def vision_locate")
    window = content[vl_start:vl_start + 6000]
    assert "max_tokens=200" in window, (
        "R115.C: max_tokens cap missing — cost discipline gate not enforced"
    )
