"""R213.K.13 — clamp arbitrary over-tight latency thresholds/checks UP to ARTA's
perf scoring gate (3000ms) so the spec's bar matches how ARTA scores. Only RAISES
(never tightens); leaves error-rate + lenient bars; keeps check names honest.
"""
from __future__ import annotations

from src.agents.chain_aware_k6 import normalize_k6_thresholds

SPEC = (
    "thresholds: { http_req_duration: ['p(95)<500', 'p(99)<750', 'p(95)<5000'], "
    "http_req_failed: ['rate<0.05'] },\n"
    "check(res, {'response time under 500ms': (r) => r.timings.duration < 500});\n"
    "check(res, {'lenient under 120000ms': (r) => r.timings.duration < 120000});\n"
)


def test_clamps_tight_latency_bars_to_gate():
    out, n = normalize_k6_thresholds(SPEC)
    assert "'p(95)<3000'" in out and "'p(99)<3000'" in out   # 500, 750 → 3000
    assert "r.timings.duration < 3000" in out                 # inline 500 → 3000
    assert n == 3


def test_leaves_lenient_and_error_rate_untouched():
    out, _ = normalize_k6_thresholds(SPEC)
    assert "'rate<0.05'" in out          # error-rate, not latency
    assert "p(95)<5000" in out            # already ≥ gate
    assert "< 120000" in out              # lenient, untouched


def test_keeps_check_names_honest():
    out, _ = normalize_k6_thresholds(SPEC)
    assert "under 3000ms" in out          # name updated to match the clamped assert
    assert "under 500ms" not in out


def test_configurable_floor():
    out, n = normalize_k6_thresholds("['p(95)<500']", floor_ms=2000)
    assert "p(95)<2000" in out and n == 1


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_K6_THRESHOLD_NORMALIZE_DISABLE", "1")
    assert normalize_k6_thresholds(SPEC) == (SPEC, 0)
