"""R134.H — Single-source-of-truth filename / id sanitizer.

Pre-R134.H: 32+ call sites used `req_id.lower().replace("-", "_")`. This
preserved characters like `@ * ; \\ /` which create filesystem + shell
hazards when the resulting filename is subprocess-passed (e.g., `npx
playwright test src/automation/playwright/<filename>`).

Post-R134.H: one `sanitize_req_id(req_id)` helper using a strict
per-character regex. Any non-[a-z0-9_] character becomes `_`. Idempotent;
preserves trailing/leading underscores; safe for filesystem + subprocess.
"""
from __future__ import annotations

import re


_R134_H_SANITIZE_RE = re.compile(r"[^a-z0-9_]")


def sanitize_req_id(req_id: str | None) -> str:
    """R134.H — convert a req_id to a filesystem-safe slug.

    Algorithm:
      1. None / empty → ""
      2. Lowercase + replace `-` with `_` (preserves the historical
         `REQ-XY-001` → `req_xy_001` contract)
      3. Strip every remaining character that is not [a-z0-9_] (turns
         them into `_`)

    Idempotent: re-running on already-sanitized output is a no-op.
    """
    if not req_id:
        return ""
    return _R134_H_SANITIZE_RE.sub("_", str(req_id).lower().replace("-", "_"))
