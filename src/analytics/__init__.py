"""R71.2 — analytics module shim for in-process pytest fixtures.

Generated pytest specs + analytics_helpers.py reference `src.analytics`
(e.g. `from src.analytics import cache as _cache` at line 281 of
analytics_helpers.py). The actual analytics implementation lives in the
SUT, not in ARTA. This shim provides a minimal in-process replacement so
ARTA's pytest fixtures can run without depending on the SUT's runtime.

Re-exports the `cache` submodule. Other submodules (`db`, etc.) are
expected to be patched via mock_db_returning / mock_llm_returning at
test time and don't need shims here.
"""
from . import cache  # noqa: F401

__all__ = ["cache"]
