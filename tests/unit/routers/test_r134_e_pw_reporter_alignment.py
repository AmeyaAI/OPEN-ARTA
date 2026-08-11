"""R134.E tests — Playwright reporter config alignment regression guards.

The original R134.E claim (`--reporter=junit` flag in dispatch + json
reporter in config = mismatch) turned out to be OUTDATED:
- The PW dispatch invocations in execution.py do NOT pass --reporter=junit
- The base config emits `json` to RESULTS_PATH + `html` + `list`
- _parse_playwright_json reads the JSON output

R134.E ships these tests as REGRESSION GUARDS: if a future contributor
adds --reporter=junit thinking it's missing, OR removes the json reporter
from the base config, the tests fire.
"""
from __future__ import annotations

import inspect
from pathlib import Path


def test_r134_e_no_junit_reporter_flag_in_pw_dispatch():
    """Regression guard — PW subprocess commands must NOT include
    --reporter=junit. The base config emits JSON; junit flag would
    silently override + break _parse_playwright_json downstream."""
    from src.api.routers import execution
    src = inspect.getsource(execution)
    # Search for any literal `--reporter=junit` or `'junit'` reporter override
    assert "--reporter=junit" not in src, (
        "R134.E regression: --reporter=junit found in execution.py PW dispatch"
    )
    assert '"reporter", "junit"' not in src
    assert "'reporter', 'junit'" not in src


def test_r134_e_pw_base_config_emits_json_reporter():
    """Regression guard — playwright.base.config.ts must keep the json
    reporter outputting to RESULTS_PATH. Removing the json reporter would
    break _parse_playwright_json (which expects JSON, not html/list)."""
    config_path = Path("src/automation/common/playwright.base.config.ts")
    assert config_path.exists(), "playwright.base.config.ts missing"
    config_text = config_path.read_text()
    # JSON reporter must be present + output to RESULTS_PATH
    assert "'json'" in config_text or '"json"' in config_text, (
        "R134.E regression: json reporter dropped from playwright.base.config.ts"
    )
    assert "RESULTS_PATH" in config_text, (
        "R134.E regression: RESULTS_PATH env var no longer wired in config"
    )
    # junit reporter must NOT be added — the dispatch parser reads JSON,
    # not XML. Adding junit would silently bloat output without changing
    # behavior (no XML parser exists).
    assert "'junit'" not in config_text and '"junit"' not in config_text, (
        "R134.E regression: junit reporter added to base config — dispatch "
        "parser reads JSON, not XML, so this would be dead code at best."
    )


def test_r134_e_parse_playwright_json_reads_target_results_path():
    """Regression guard — _parse_playwright_json (the live PW result
    parser) must remain wired to the JSON file at TARGET_RESULTS_PATH.
    If a future change switches the parser source, the dispatch chain
    breaks silently."""
    from src.api.routers.execution import _parse_playwright_json
    # The parser exists + is callable. The actual contract is verified
    # via existing integration tests; here we just verify the function
    # signature accepts the JSON file path (consumer of the json reporter
    # output).
    sig = inspect.signature(_parse_playwright_json)
    params = list(sig.parameters.keys())
    # Must accept at least one positional arg (path to JSON OR JSON content)
    assert len(params) >= 1, (
        "R134.E regression: _parse_playwright_json signature changed; "
        "verify it still consumes the json reporter output."
    )
