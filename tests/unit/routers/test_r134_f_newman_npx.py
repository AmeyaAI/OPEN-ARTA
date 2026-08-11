"""R134.F tests — Newman dispatch prefers `npx newman` for Docker compat.

Pre-R134.F: `shutil.which("newman")` checked first; this missed the
common Docker pattern where newman lives at /node_modules/.bin/newman
without that dir in PATH. Post-R134.F: `npx newman` resolves via
node_modules first, system PATH fallback. Mirrors PW's `npx playwright`.
"""
from __future__ import annotations

import inspect


def test_r134_f_dispatch_prefers_npx():
    """Regression guard — Newman dispatch resolver must prefer npx so
    Docker layouts (newman in node_modules but not in PATH) work."""
    from src.api.routers import execution
    src = inspect.getsource(execution)
    # The new resolver line: `shutil.which("npx") or shutil.which("newman")`
    assert 'shutil.which("npx") or shutil.which("newman")' in src, (
        "R134.F regression: newman resolver doesn't prefer npx — "
        "Docker layouts with newman in /node_modules/.bin will fail"
    )


def test_r134_f_arta_newman_bin_env_override_supported():
    """R134.F preserves operator escape hatch via ARTA_NEWMAN_BIN env var
    (CI pinning a specific newman version, alpine images with custom
    paths, etc.). Tests the override constant is referenced."""
    from src.api.routers import execution
    src = inspect.getsource(execution)
    assert 'ARTA_NEWMAN_BIN' in src, (
        "R134.F regression: ARTA_NEWMAN_BIN env override removed"
    )
