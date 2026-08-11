"""Unified run report (summary.html) + artifacts directory index.

Covers the readability/robustness fixes for Newman-heavy runs:
  - no dead "Open full Playwright HTML report" link when Playwright didn't run,
  - the link IS present when a real index.html exists,
  - BLOCKED is a first-class status (chip + filter), not folded into FAIL,
  - error text is HTML-escaped (no injection into the report),
  - the `-artifacts` dir gets a browsable index.html listing.
"""
from __future__ import annotations

from src.api.routers import execution as ex


def _results():
    return [
        {"tool": "newman", "status": "FAIL", "test_id": "t1", "title": "A",
         "error_message": "boom <script>alert(1)</script>", "duration_ms": 5},
        {"tool": "newman", "status": "BLOCKED", "test_id": "t2", "title": "B",
         "error_message": "R168: POST blocked — read-only contract", "duration_ms": 0},
        {"tool": "newman", "status": "PASS", "test_id": "t3", "title": "C", "duration_ms": 3},
    ]


def test_newman_only_report_has_no_playwright_link(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    html = ex._render_unified_report(
        "run-t1", {"gate_decision": "FAIL", "environment": "staging"}, _results())
    assert "Open full Playwright" not in html          # no index.html on disk → no dead link
    assert 'class="toolbar"' in html                   # filter/search toolbar present
    assert 'data-status="FAIL"' in html                # rows are filterable
    assert 'data-f="BLOCKED"' in html                  # blocked filter chip present
    assert '<span class="chip blocked">BLOCKED</span>' in html
    # error text HTML-escaped, not injected
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)" not in html


def test_playwright_native_report_embedded_as_iframe_tab(tmp_path, monkeypatch):
    """When a real Playwright report exists, it's embedded as its own
    'Playwright (native)' tab via a lazy iframe (src set on first open), with an
    'Open in full page' escape hatch — separate from the summary/table tab."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    rdir = tmp_path / "run-t2-report"
    rdir.mkdir()
    # real PW report is > 1KB (the redirect stub is ~150B and must be excluded)
    (rdir / "index.html").write_text("<html>" + "x" * 2000 + "</html>", encoding="utf-8")
    html = ex._render_unified_report(
        "run-t2", {"gate_decision": "PASS", "environment": "staging"},
        [{"tool": "playwright", "status": "PASS", "test_id": "p", "title": "P", "duration_ms": 1}])
    assert 'data-tool="playwright-native"' in html          # dedicated tab
    assert 'Open in full page' in html                      # escape hatch
    # the iframe is lazy (data-src, no src) and lives in the native PANEL
    native = html.split('<section class="panel" data-tool="playwright-native"', 1)[1].split('</section>', 1)[0]
    assert 'class="pw-frame" data-src="index.html"' in native
    assert ' src=' not in native.split('<iframe')[1].split('>')[0]   # not yet loaded
    # the summary/table tab still exists separately
    assert 'data-tool="playwright"' in html


def test_axe_per_rule_detail(tmp_path, monkeypatch):
    """The axe tab shows per-RULE detail (impact, WCAG criteria, affected elements,
    fix link) — not just aggregate counts."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    results = [
        {"tool": "axe", "status": "FAIL", "test_id": "axe-agg", "duration_ms": 0,
         "title": "Accessibility scan (3 spec, 2/1/0 crit/mod/minor)",
         "metadata": {"a11y_violations_critical": 2, "a11y_violations_moderate": 1,
                      "a11y_violations_minor": 0, "a11y_scanned": True}},
        {"tool": "axe", "status": "FAIL", "test_id": "axe-rule-color-contrast",
         "title": "[Accessibility] color-contrast: Elements must meet contrast ratio",
         "duration_ms": 0,
         "error_message": ("Critical impact · wcag2aa, wcag143 · 12 element(s) affected · "
                           "e.g. .btn-primary · Fix: https://dequeuniversity.com/rules/axe/color-contrast"),
         "metadata": {"a11y_impact": "critical", "a11y_rule": "color-contrast",
                      "a11y_wcag": "wcag2aa, wcag143", "a11y_nodes": 12,
                      "a11y_help_url": "https://dequeuniversity.com/rules/axe/color-contrast",
                      "a11y_detail_row": True}},
    ]
    html = ex._render_unified_report(
        "run-axe", {"gate_decision": "FAIL", "environment": "staging"}, results)
    # per-rule row: red impact chip + affected-element count + a docs link
    assert '<span class="sig s5">critical</span>' in html
    assert "12×" in html
    assert "docs ↗" in html
    assert "dequeuniversity.com/rules/axe/color-contrast" in html
    # WCAG criteria + affected elements are in the detail column
    assert "wcag2aa, wcag143" in html and "12 element(s) affected" in html
    # the aggregate row still shows crit/mod/min counts
    assert "2 crit" in html


def test_zap_scan_scope_in_context(tmp_path, monkeypatch):
    """The ZAP tab shows WHAT was tested (URLs, requests, auth, risk breakdown) so a
    'no alerts' result is trustworthy — not an ambiguous shrug."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    scope = {
        "zap_target": "https://sut.example", "zap_authenticated": True,
        "zap_urls_scanned": 342, "zap_requests": 5120,
        "zap_alert_counts": {"High": 0, "Medium": 0, "Low": 3, "Informational": 8},
    }
    html = ex._render_unified_report(
        "run-zs", {"gate_decision": "PASS", "environment": "staging"},
        [{"tool": "zap", "status": "PASS", "test_id": "ZAP-clean", "duration_ms": 700000,
          "title": "[Security] OWASP ZAP Scan — no alerts found",
          "error_message": "Scanned 342 URL(s) · 5120 request(s) · authenticated — no Medium+ alerts.",
          "metadata": scope}])
    # scan scope rollup
    assert "342 URLs" in html and "5120 requests" in html and "authenticated" in html
    assert "Low×3" in html and "Informational×8" in html
    # the per-row detail (what was scanned) is visible too
    assert "Scanned 342 URL(s)" in html


def test_zap_native_report_embedded_when_present(tmp_path, monkeypatch):
    """The native-report mechanism is generic: a ZAP run WITH a zap-report.html on
    disk gets a 'ZAP (native)' iframe tab, exactly like Playwright."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    rdir = tmp_path / "run-z1-report"
    rdir.mkdir()
    (rdir / "zap-report.html").write_text("<html>" + "z" * 500 + "</html>", encoding="utf-8")
    html = ex._render_unified_report(
        "run-z1", {"gate_decision": "FAIL", "environment": "staging"},
        [{"tool": "zap", "status": "FAIL", "test_id": "z", "title": "[Security] XSS",
          "error_message": "Medium risk", "duration_ms": 100, "metadata": {"risk": "Medium"}}])
    assert 'data-tool="zap-native"' in html
    native = html.split('<section class="panel" data-tool="zap-native"', 1)[1].split('</section>', 1)[0]
    assert 'class="pw-frame" data-src="zap-report.html"' in native


def test_zap_native_tab_absent_without_report(tmp_path, monkeypatch):
    """A ZAP run with NO zap-report.html on disk shows only the summary tab — no
    native tab is faked."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    (tmp_path / "run-z2-report").mkdir()
    html = ex._render_unified_report(
        "run-z2", {"gate_decision": "FAIL", "environment": "staging"},
        [{"tool": "zap", "status": "FAIL", "test_id": "z", "title": "[Security] XSS",
          "error_message": "Medium risk", "duration_ms": 100, "metadata": {"risk": "Medium"}}])
    assert 'data-tool="zap-native"' not in html
    assert 'data-tool="zap"' in html   # summary tab still present


def test_save_zap_html_report(tmp_path, monkeypatch):
    """_save_zap_html_report pulls the daemon's HTML report and writes it; a
    non-200 / tiny body is ignored; the helper never raises."""
    import asyncio
    from unittest.mock import AsyncMock
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)

    class _Resp:
        def __init__(self, status, content):
            self.status_code = status
            self.content = content

    ok_client = AsyncMock()
    ok_client.get = AsyncMock(return_value=_Resp(200, b"<html>" + b"x" * 500 + b"</html>"))
    assert asyncio.run(ex._save_zap_html_report(ok_client, "run-z3")) is True
    assert (tmp_path / "run-z3-report" / "zap-report.html").exists()

    # non-200 → not saved, no raise
    bad_client = AsyncMock()
    bad_client.get = AsyncMock(return_value=_Resp(403, b"denied"))
    assert asyncio.run(ex._save_zap_html_report(bad_client, "run-z4")) is False
    assert not (tmp_path / "run-z4-report").exists()

    # transport error → swallowed, returns False
    boom_client = AsyncMock()
    boom_client.get = AsyncMock(side_effect=RuntimeError("connection reset"))
    assert asyncio.run(ex._save_zap_html_report(boom_client, "run-z5")) is False


def test_no_native_tab_for_tiny_stub_index(tmp_path, monkeypatch):
    """A Newman-only run with only a redirect-stub index.html gets NO native tab
    (the stub is not a real Playwright report). Note: the strings 'pw-frame' and
    'playwright-native' appear in the static CSS/JS regardless, so assert on the
    actual tab/panel markup."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    rdir = tmp_path / "run-t2z-report"
    rdir.mkdir()
    (rdir / "index.html").write_text(
        "<meta http-equiv=refresh content='0;url=summary.html'>", encoding="utf-8")
    html = ex._render_unified_report(
        "run-t2z", {"gate_decision": "FAIL", "environment": "staging"},
        [{"tool": "newman", "status": "FAIL", "test_id": "n", "title": "[API] x",
          "error_message": "500", "duration_ms": 5, "metadata": {"status_code": 500}}])
    assert 'data-tool="playwright-native"' not in html      # no native tab/panel
    assert 'class="pw-frame"' not in html                   # no embedded iframe element


def test_report_is_tabbed_per_tool(tmp_path, monkeypatch):
    """Multi-tool run renders a tab + panel per tool, with a server-chosen default
    (the most-failing tool) and per-tool status counts on the tab."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    results = [
        {"tool": "newman", "status": "FAIL", "test_id": "n1", "title": "[API] x",
         "error_message": "500", "duration_ms": 5, "metadata": {"status_code": 500}},
        {"tool": "newman", "status": "FAIL", "test_id": "n2", "title": "[API] y",
         "error_message": "500", "duration_ms": 5, "metadata": {"status_code": 500}},
        {"tool": "k6", "status": "PASS", "test_id": "k1", "title": "[Performance] a",
         "duration_ms": 10, "metadata": {"perf": {"p95_ms": 100, "threshold_pass": True}}},
    ]
    html = ex._render_unified_report("run-tab", {"gate_decision": "FAIL", "environment": "staging"}, results)
    assert 'class="tabbar"' in html
    assert 'data-tool="newman"' in html and 'data-tool="k6"' in html
    assert "showTab(" in html
    # default tab = the most-failing tool (newman: 2 fails vs k6: 0) — active +
    # its panel visible server-side; the non-default (k6) panel is hidden.
    assert 'window.__defaultTab="newman"' in html
    assert '<section class="panel" data-tool="newman">' in html          # default: visible
    assert '<section class="panel" data-tool="k6" hidden>' in html       # non-default: hidden
    assert 'class="tab hasfail active" data-tool="newman"' in html       # default tab active


def test_newman_only_no_playwright_link_even_with_stub_index(tmp_path, monkeypatch):
    """Regression: a redirect-stub index.html exists for Newman-only runs (so the
    canonical URL resolves), but the Playwright link must NOT appear — it's gated
    on Playwright actually running, not on index.html existing."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    rdir = tmp_path / "run-t2b-report"
    rdir.mkdir()
    (rdir / "index.html").write_text("<meta http-equiv=refresh content='0;url=summary.html'>", encoding="utf-8")
    html = ex._render_unified_report(
        "run-t2b", {"gate_decision": "FAIL", "environment": "staging"},
        [{"tool": "newman", "status": "FAIL", "test_id": "n", "title": "[API] x",
          "error_message": "500", "duration_ms": 5, "metadata": {"status_code": 500}}])
    assert "Open full Playwright" not in html


def test_browse_artifacts_links_to_index_file_not_dir(tmp_path, monkeypatch):
    """The 'Browse raw artifacts' link must target the listing FILE, not the
    directory — a directory URL 307-redirects to the internal host via the proxy."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    adir = tmp_path / "run-t2c-artifacts"
    adir.mkdir()
    (adir / "resp.txt").write_text("x", encoding="utf-8")
    html = ex._render_unified_report(
        "run-t2c", {"gate_decision": "FAIL", "environment": "staging"},
        [{"tool": "newman", "status": "PASS", "test_id": "n", "title": "[API] x", "duration_ms": 5}])
    assert '../run-t2c-artifacts/index.html' in html          # file, not the bare dir
    assert 'href="../run-t2c-artifacts/"' not in html          # never the directory URL


def test_report_urls_prefers_summary_and_file_artifacts(tmp_path, monkeypatch):
    """_report_urls: report_url → summary.html (not the stub index.html);
    artifacts_url → the listing FILE; both are concrete files, never a dir."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    rdir = tmp_path / "run-t2d-report"; rdir.mkdir()
    (rdir / "summary.html").write_text("<html>report</html>", encoding="utf-8")
    (rdir / "index.html").write_text("<html>stub</html>", encoding="utf-8")
    adir = tmp_path / "run-t2d-artifacts"; adir.mkdir()
    (adir / "resp.txt").write_text("x", encoding="utf-8")
    report_url, artifacts_url = ex._report_urls("run-t2d")
    assert report_url == "/artifacts/run-t2d-report/summary.html"
    assert artifacts_url == "/artifacts/run-t2d-artifacts/index.html"
    assert (adir / "index.html").exists()   # self-healed the listing


def test_report_pass_rate_excludes_blocked_and_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    # 1 pass, 1 fail, 1 blocked → executed=2 → 50% (blocked not in denominator)
    html = ex._render_unified_report(
        "run-t3", {"gate_decision": "FAIL", "environment": "staging"}, _results())
    assert "50.0%" in html


def test_per_tool_sections_and_signals(tmp_path, monkeypatch):
    """A multi-tool run renders a section per tool, each with its own type-specific
    Signal column + context rollup."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    results = [
        # Newman: HTTP status code signal + context rollup
        {"tool": "newman", "status": "FAIL", "test_id": "n1", "title": "[API] POST /x",
         "error_message": "expected 200", "duration_ms": 50, "metadata": {"status_code": 500, "method": "POST"}},
        {"tool": "newman", "status": "PASS", "test_id": "n2", "title": "[API] GET /y",
         "duration_ms": 20, "metadata": {"status_code": 200, "method": "GET"}},
        # Playwright: failure class + trace link
        {"tool": "playwright", "status": "FAIL", "test_id": "p1", "title": "E2E login",
         "error_message": "locator timeout", "duration_ms": 900,
         "metadata": {"failure_class": "Assertion Mismatch", "trace_url": "/artifacts/trace.zip"}},
        # axe: WCAG violation counts
        {"tool": "axe", "status": "FAIL", "test_id": "a1", "title": "a11y scan", "duration_ms": 0,
         "metadata": {"a11y_violations_critical": 3, "a11y_violations_moderate": 5, "a11y_violations_minor": 2}},
        # k6 + zap present too
        {"tool": "k6", "status": "FAIL", "test_id": "k1", "title": "perf", "duration_ms": 24,
         "metadata": {"blocked_reason": "sut_5xx"}},
        {"tool": "zap", "status": "FAIL", "test_id": "z1", "title": "scan", "duration_ms": 100,
         "metadata": {"risk": "High"}},
    ]
    html = ex._render_unified_report("run-multi", {"gate_decision": "FAIL", "environment": "staging"}, results)
    # one section per tool
    for lbl in ("API (Newman)", "E2E UI (Playwright)", "Accessibility (axe)",
                "Performance (k6)", "Security (ZAP)"):
        assert lbl in html
    # type-specific signals
    assert ">POST </span>500" in html or "500</span>" in html   # newman HTTP code
    assert "Assertion Mismatch" in html and "trace" in html      # playwright class + trace
    assert "3 crit" in html and "5 mod" in html                  # axe impact
    assert "High" in html                                        # zap risk
    # per-tool context rollups
    assert "HTTP:" in html                 # newman status rollup
    assert "WCAG violations:" in html      # axe rollup
    assert "3 critical" in html
    # per-tool column headers differ
    assert ">Failure class<" in html and ">Impact<" in html and ">Risk<" in html


def test_k6_perf_signal_and_rollup(tmp_path, monkeypatch):
    """k6 section shows real perf numbers with each dimension colored by its OWN
    threshold (so a fast-latency-but-failed-checks row reads truthfully), plus a
    p95 range / native-throughput rollup — not just blocked/health context."""
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    results = [
        # Latency FINE (312ms) but checks FAILED (0%) — the truthful-verdict case.
        {"tool": "k6", "status": "FAIL", "test_id": "PERF-a", "title": "[Performance] a",
         "error_message": "checks failed (401)", "duration_ms": 5000, "metadata": {"perf": {
             "p95_ms": 312.0, "check_pass_pct": 0.0, "error_rate_pct": 100.0,
             "total_requests": 225, "throughput_rps": 45.0, "vus": 5,
             "p95_threshold_ms": 3000, "check_threshold_pct": 90, "error_threshold_pct": 1.0,
             "threshold_pass": False}}},
        # Latency BREACHED but checks fine.
        {"tool": "k6", "status": "FAIL", "test_id": "PERF-b", "title": "[Performance] b",
         "error_message": "p95 breach", "duration_ms": 8000, "metadata": {"perf": {
             "p95_ms": 4200.0, "check_pass_pct": 100.0, "total_requests": 100,
             "throughput_rps": 12.0, "p95_threshold_ms": 3000, "check_threshold_pct": 90,
             "threshold_pass": False}}},
    ]
    html = ex._render_unified_report("run-k6", {"gate_decision": "FAIL", "environment": "staging"}, results)
    # p95 312ms is GREEN (within latency threshold) even though the row FAILED…
    assert '<span class="sig s2">p95 312ms</span>' in html
    # …because checks are RED — the failing dimension is now obvious, no misleading ✗
    assert '<span class="sig s5">0% chk</span>' in html
    assert "✓" not in html and "✗" not in html   # dropped the confusing single verdict mark
    # p95 4200ms is RED (latency breach) while its checks are green
    assert '<span class="sig s5">p95 4200ms</span>' in html
    assert '<span class="sig s2">100% chk</span>' in html
    # error rate surfaced only when it exceeds threshold
    assert "100% err" in html
    # rollup: p95 range + total requests + NATIVE req/s range + threshold pass count
    assert "<b>p95:</b> 312–4200ms" in html
    assert "325</b> requests" in html
    assert "12–45 req/s" in html
    assert "0/2 scripts passed all thresholds" in html


def test_write_artifacts_index_lists_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    adir = tmp_path / "run-t4-artifacts"
    adir.mkdir()
    (adir / "resp.txt").write_text("hello", encoding="utf-8")
    (adir / "data.json").write_text("{}", encoding="utf-8")
    assert ex._write_artifacts_index("run-t4") is True
    idx = (adir / "index.html").read_text(encoding="utf-8")
    assert "Raw artifacts" in idx
    assert "resp.txt" in idx and "data.json" in idx
    # the index itself is not listed as an entry
    assert idx.count("index.html") <= 1  # only (possibly) in the back-link, not a row


def test_write_artifacts_index_missing_dir_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "ARTIFACTS_DIR", tmp_path)
    assert ex._write_artifacts_index("run-does-not-exist") is False
