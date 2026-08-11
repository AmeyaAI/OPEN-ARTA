"""R280 — no undefined names in src/ (the bug class `ast.parse` cannot see).

Origin: R279's killswitch called `os.environ` in a module that never imported
`os`. `ast.parse` accepted it happily -- syntax-checking is NOT import-checking,
and a NameError inside a function body only fires when that line RUNS.

Auditing the whole tree found **30** more of the same class already shipped, and
they were not cosmetic:

  * `quality_gate_agent.py` x11 -- EVERY one inside an `except Exception:`
    handler (`log.debug("...skipped: %s", exc)`). A soft SKIP became a hard
    NameError raised FROM the handler, taking the quality gate down.
  * `atdd_designer.py` x4 -- same shape: the error handler masked the original
    error by crashing.
  * `tests.py` `_captured` -- used ~1000 lines from where it is defined, inside
    a `try/except Exception: _bodies = {}`. The NameError was SWALLOWED, so
    `build_request_bodies` NEVER ran: chain-aware PW action bodies were always
    empty and nothing ever reported it. A silently dead feature.
  * `tests.py` `asyncio` -- bare `asyncio.CancelledError` in an except clause
    with only local `import asyncio as _asyncio` elsewhere: NameError raised
    exactly on the failure path.

The pattern that makes these invisible: they live on error paths, so the tests
that would catch them are the ones nobody writes.
"""
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RUFF = REPO / ".venv" / "bin" / "ruff"


def _ruff(*args) -> str:
    if not RUFF.is_file():
        pytest.skip("ruff not installed in this environment")
    return subprocess.run(
        [str(RUFF), "check", *args, "--output-format=concise", "--quiet"],
        cwd=REPO, capture_output=True, text=True, timeout=180,
    ).stdout


def test_src_has_no_undefined_names():
    """F821 == 0 across src/.

    Anything flagged here is a NameError waiting for its code path to run. If a
    hit is a deliberate `dir()`-style guard, annotate it `# noqa: F821` WITH the
    reason -- do not widen this test.
    """
    out = _ruff("src/", "--select", "F821")
    hits = [ln for ln in out.splitlines() if "F821" in ln]
    assert not hits, (
        "undefined name(s) — a NameError waiting to fire:\n  "
        + "\n  ".join(hits[:12])
    )


def test_the_r279_bug_shape_is_actually_caught(tmp_path):
    """Guard the guard: prove this test would have caught the original bug,
    rather than trusting that it does."""
    f = tmp_path / "probe.py"
    f.write_text("import re\n\n\ndef g(t):\n    return os.environ.get('X') and re.match('a', t)\n")
    out = _ruff(str(f), "--select", "F821")
    assert "F821" in out and "os" in out


def test_error_path_logger_is_defined_in_the_modules_that_lost_it():
    """The 15 `log` hits were ALL on error paths. A module that logs in an
    `except` handler must define `log` at import time, or the handler itself
    crashes."""
    for mod in ("src/agents/quality_gate_agent.py", "src/agents/atdd_designer.py"):
        src = (REPO / mod).read_text()
        if "log." in src:
            assert "import logging" in src, f"{mod}: uses log.* without importing logging"
            assert "log = logging.getLogger(" in src, f"{mod}: log.* with no module logger"
