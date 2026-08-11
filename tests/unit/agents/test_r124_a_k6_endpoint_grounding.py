"""R124.A — k6 endpoint grounding via `validate_k6_grounded(captured_endpoints=...)`.

Live evidence: run-d52a8c k6 scripts hallucinated endpoint paths like
`/v1/seeds`, `/v1/parsers` when the SUT serves `/api/v1/datasets` etc.
R124.A extends `validate_k6_grounded` to flag these at gen time —
parallel to Newman's R98.3 + R118.F and PW's R101.E.
"""
from __future__ import annotations

from src.agents.grounding_validator import GroundingViolation, validate_k6_grounded


def _k6_script_with_url(url_path: str) -> str:
    return f"""import http from 'k6/http';
import {{ check }} from 'k6';
export default function () {{
  const resp = http.get(`${{__ENV.BASE_URL}}{url_path}`);
  check(resp, {{ 'status is 200': (r) => r.status === 200 }});
}}
"""


def test_r124_a_clean_url_passes():
    """k6 http.get to a captured endpoint → no violation."""
    captured = [{"path": "/api/v1/datasets", "method": "GET"}]
    src = _k6_script_with_url("/api/v1/datasets")
    out = validate_k6_grounded(
        src,
        env_vars={"BASE_URL": ""},
        captured_endpoints=captured,
    )
    unknown = [v for v in out if v.kind == "unknown_endpoint"]
    assert unknown == [], f"clean URL must not flag; got {unknown}"


def test_r124_a_hallucinated_path_flagged():
    """k6 http.get to a path NOT in captured → flagged as unknown_endpoint."""
    captured = [{"path": "/api/v1/datasets", "method": "GET"}]
    src = _k6_script_with_url("/v1/seeds")  # hallucinated
    out = validate_k6_grounded(
        src,
        env_vars={"BASE_URL": ""},
        captured_endpoints=captured,
    )
    unknown = [v for v in out if v.kind == "unknown_endpoint"]
    assert len(unknown) == 1, f"hallucinated path must flag; got {unknown}"
    assert unknown[0].tool == "k6"
    assert "/v1/seeds" in unknown[0].symbol


def test_r124_a_cold_start_skips_endpoint_check():
    """No captured_endpoints argument → endpoint grounding skipped silently."""
    src = _k6_script_with_url("/some/random/path")
    out = validate_k6_grounded(
        src,
        env_vars={"BASE_URL": ""},
        captured_endpoints=None,
    )
    unknown = [v for v in out if v.kind == "unknown_endpoint"]
    assert unknown == [], "cold-start must not flag without captured_endpoints"


def test_r124_a_env_var_check_unaffected():
    """R124.A adds endpoint check WITHOUT breaking the existing __ENV.X check."""
    src = """import http from 'k6/http';
export default function () {
  const r = http.get(`${__ENV.UNDECLARED_VAR}/api/x`);
}
"""
    out = validate_k6_grounded(
        src,
        env_vars={"BASE_URL": ""},
        captured_endpoints=[{"path": "/api/x"}],
    )
    unset = [v for v in out if v.kind == "unset_env_var"]
    assert len(unset) >= 1, "env_var check must still fire for UNDECLARED_VAR"


def test_r124_a_hint_contains_before_after():
    """Violation hint must include BEFORE/AFTER concrete-code blocks (R110.B idiom)."""
    captured = [{"path": "/api/v1/datasets"}, {"path": "/api/v1/projects"}]
    src = _k6_script_with_url("/phantom/endpoint")
    out = validate_k6_grounded(
        src,
        env_vars={"BASE_URL": ""},
        captured_endpoints=captured,
    )
    unknown = [v for v in out if v.kind == "unknown_endpoint"]
    assert len(unknown) == 1
    hint = unknown[0].hint
    assert "BEFORE" in hint and "AFTER" in hint, f"hint missing BEFORE/AFTER: {hint}"
    assert "/api/v1/datasets" in hint, "alternatives must include captured paths"
