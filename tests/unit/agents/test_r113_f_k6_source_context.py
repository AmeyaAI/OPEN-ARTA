"""R113.F — R104.B `_fetch_sut_source_context` parity for k6 gen.

Pre-R113.F: k6 LLM only saw R98.3 captured_endpoints, missing the SUT's
canonical backend route handlers from GitHub. Without source-truth, perf
tests could hallucinate paths → wasted perf signal. R113.F wires the same
helper PW + Newman use, with `include_fe_routes=False` since k6 only
exercises API endpoints.
"""
from __future__ import annotations

import re
from pathlib import Path


_AUTOMATION_ENGINEER_PY = (
    Path(__file__).resolve().parents[3] / "src" / "agents" / "automation_engineer.py"
)


def test_r113_f_k6_calls_fetch_sut_source_context():
    """Source check: `_generate_k6` invokes `_fetch_sut_source_context`."""
    content = _AUTOMATION_ENGINEER_PY.read_text()

    # Find the _generate_k6 method body
    k6_method_start = content.find("async def _generate_k6(")
    assert k6_method_start > 0, "_generate_k6 not found"

    # Find the end (next async def or class boundary) — search next ~10K chars
    k6_method_body = content[k6_method_start:k6_method_start + 10000]

    # R113.F should call self._fetch_sut_source_context with include_fe_routes=False
    pattern = re.compile(
        r"R113\.F.*?_fetch_sut_source_context\(\s*\n?"
        r".*?project=.*?,\s*\n?"
        r".*?gherkin_text=.*?,\s*\n?"
        r".*?max_chars=.*?,\s*\n?"
        r".*?include_fe_routes=False",
        re.DOTALL,
    )
    assert pattern.search(k6_method_body), (
        "R113.F: _generate_k6 missing _fetch_sut_source_context invocation "
        "with include_fe_routes=False"
    )


def test_r113_f_k6_log_line_present():
    """Source check: R113.F log line surfaces injection count."""
    content = _AUTOMATION_ENGINEER_PY.read_text()
    pattern = re.compile(
        r'R113\.F: injected SUT source context.*?into k6 prompt',
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R113.F: injection log format string missing"
    )
