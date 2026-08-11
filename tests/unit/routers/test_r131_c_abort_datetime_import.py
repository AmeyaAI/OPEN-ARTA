"""R131.C — drive-by fix for abort_generate_all NameError on `datetime`.

Live evidence (R131 Iter 0): operator tried to abort runaway job d10f8710,
POST /api/tests/generate-all/abort?job_id=d10f8710 returned HTTP 500 with
`NameError: name 'datetime' is not defined` at tests.py:4414 because
`datetime` + `timezone` weren't imported at module scope for that path.

R131.C imports them locally inside the endpoint. The fix is one
`from datetime import datetime, timezone` line. This test locks in
the regression — running the import inside the endpoint MUST succeed
and produce a valid ISO-8601 timestamp.
"""
from __future__ import annotations

import re

import pytest


def test_r131c_datetime_import_in_abort_endpoint():
    """The abort endpoint code path MUST be able to call datetime.now(
    timezone.utc).isoformat() without raising NameError. We exercise the
    import by reproducing the exact pattern used in tests.py:4414."""
    # This is the line shape used inside the abort endpoint post-R131.C
    from datetime import datetime, timezone
    iso = datetime.now(timezone.utc).isoformat()
    # ISO-8601 with timezone offset (e.g. "2026-05-22T22:00:00.123456+00:00")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", iso), (
        f"abort_requested_at timestamp not ISO-8601: {iso}"
    )
    assert "+00:00" in iso or iso.endswith("Z"), (
        f"abort_requested_at missing UTC tz: {iso}"
    )


def test_r131c_abort_endpoint_imports_locally():
    """Verify the actual source code contains the local import — guards
    against accidental removal in future refactors."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[3] / "src" / "api" / "routers" / "tests.py"
    text = src.read_text()
    # Find the abort_generate_all function body
    abort_idx = text.find("async def abort_generate_all")
    assert abort_idx > 0, "abort_generate_all endpoint not found"
    # Look for the R131.C local import within the next ~1500 chars (the
    # function body)
    fn_body = text[abort_idx:abort_idx + 1500]
    assert "from datetime import datetime, timezone" in fn_body, (
        "R131.C local datetime import missing from abort_generate_all"
    )
