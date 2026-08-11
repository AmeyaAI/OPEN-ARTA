"""Phase I7 — centralized accessors for `projects.discovery_settings` JSONB.

The schema defaults live in migration 009; this module is the canonical
read/write surface for application code. Putting the keys in one place
prevents the typical drift where 5 callsites duplicate the literal string
`"stage_2_5_enabled"` and a typo silently disables the flag.

All accessors take a plain dict (the JSONB value as Python sees it) and
return the typed value with a sensible default when the key is absent —
this matters for projects created before migration 009 ran, whose
`discovery_settings` may legitimately be `None` or `{}`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

# Per-project flag. Default FALSE for existing projects (opt-in via I7
# migration sweep + admin button). New projects flip this TRUE at create-time.
KEY_STAGE_2_5_ENABLED = "stage_2_5_enabled"

# Set TRUE by the migration sweep when a prior run had unresolved env vars.
# Surfaced in the admin "Discovery" panel so operators can opt-in to first run.
KEY_DISCOVERY_PENDING = "discovery_pending"

# Hard caps — Phase I2.
KEY_HAR_MAX_SIZE_BYTES = "har_max_size_bytes"
KEY_HAR_BODY_MAX_BYTES = "har_body_max_bytes"

# Phase I1 — operator-supplied regex patterns for SUT-specific secret formats.
KEY_REDACTION_EXTRA_PATTERNS = "redaction_extra_patterns"

# Phase I5 — DR-5 chain replay mode.
KEY_REPLAY_MODE = "replay_mode"
KEY_REPLAY_CRON = "replay_cron"

# Bookkeeping — populated after successful Stage 2.5 runs.
KEY_LAST_DISCOVERY_AT = "last_discovery_at"
KEY_LAST_DISCOVERY_RUN_ID = "last_discovery_run_id"
KEY_ENVVARS_HARVESTED_COUNT = "envvars_harvested_count"


_DEFAULTS: dict[str, Any] = {
    KEY_STAGE_2_5_ENABLED: False,
    KEY_DISCOVERY_PENDING: False,
    # R155.A — sync with `har_redactor.DEFAULT_MAX_FILE_BYTES`.
    # Pre-R155.A this default stayed at 50 MB and won over the har_redactor
    # constant via the sut_onboarding.py:239 precedence
    # (`ds.har_max_size_bytes(project_settings)` checked first). After
    # R150.A enabled `mode:'full'` HAR capture AND R154.A's click loop
    # generates more authenticated XHRs per route, typical HARs grew to
    # 100-200 MB. R155.A.1 (this iteration) bumped 200 MB → 300 MB after
    # Iter-11 evidence at 214 MB exceeded the 200 MB intermediate cap.
    # Per-body cap (64 KB) still bounds pathological large responses.
    KEY_HAR_MAX_SIZE_BYTES: 314_572_800,  # 300 MB (R155.A.1; was 200 MB R155.A; was 50 MB pre-R155.A)
    KEY_HAR_BODY_MAX_BYTES: 65_536,       # 64 KB
    KEY_REDACTION_EXTRA_PATTERNS: [],
    KEY_REPLAY_MODE: "read_only",
    KEY_REPLAY_CRON: "0 3 * * *",
    KEY_LAST_DISCOVERY_AT: None,
    KEY_LAST_DISCOVERY_RUN_ID: None,
    KEY_ENVVARS_HARVESTED_COUNT: 0,
}


def default_settings() -> dict[str, Any]:
    """Return a fresh copy of the default settings (safe for new-project init)."""
    return {**_DEFAULTS}


def default_settings_for_new_project() -> dict[str, Any]:
    """New projects get discovery enabled out of the box.

    Existing projects (default `stage_2_5_enabled=False` from migration 009)
    must opt in via the admin Discovery panel; we don't surprise them with a
    new pipeline stage on first read after deploy.
    """
    s = default_settings()
    s[KEY_STAGE_2_5_ENABLED] = True
    return s


def get(settings: dict[str, Any] | None, key: str) -> Any:
    """Read a setting with default fallback. Tolerates None / empty dict."""
    if not settings:
        return _DEFAULTS.get(key)
    return settings.get(key, _DEFAULTS.get(key))


def is_stage_2_5_enabled(settings: dict[str, Any] | None) -> bool:
    return bool(get(settings, KEY_STAGE_2_5_ENABLED))


def is_discovery_pending(settings: dict[str, Any] | None) -> bool:
    return bool(get(settings, KEY_DISCOVERY_PENDING))


def har_max_size_bytes(settings: dict[str, Any] | None) -> int:
    val = get(settings, KEY_HAR_MAX_SIZE_BYTES)
    try:
        return int(val) if val is not None else _DEFAULTS[KEY_HAR_MAX_SIZE_BYTES]
    except (TypeError, ValueError):
        return _DEFAULTS[KEY_HAR_MAX_SIZE_BYTES]


def har_body_max_bytes(settings: dict[str, Any] | None) -> int:
    val = get(settings, KEY_HAR_BODY_MAX_BYTES)
    try:
        return int(val) if val is not None else _DEFAULTS[KEY_HAR_BODY_MAX_BYTES]
    except (TypeError, ValueError):
        return _DEFAULTS[KEY_HAR_BODY_MAX_BYTES]


def redaction_extra_patterns(settings: dict[str, Any] | None) -> list[str]:
    val = get(settings, KEY_REDACTION_EXTRA_PATTERNS)
    return list(val) if isinstance(val, list) else []


def replay_mode(settings: dict[str, Any] | None) -> str:
    val = get(settings, KEY_REPLAY_MODE)
    return str(val) if val in {"read_only", "shadow_tenant", "idempotent_only"} else "read_only"


def stamp_last_discovery(
    settings: dict[str, Any] | None,
    *,
    run_id: str,
    envvars_harvested: int,
    when: datetime | None = None,
) -> dict[str, Any]:
    """Return a NEW dict with last-discovery bookkeeping updated.

    Caller persists the returned dict. We never mutate the input — the same
    immutability discipline as Phase A5 (avoids partial-write bugs if the
    persistence step raises).
    """
    out = dict(settings or {})
    out[KEY_LAST_DISCOVERY_AT] = (when or datetime.utcnow()).isoformat() + "Z"
    out[KEY_LAST_DISCOVERY_RUN_ID] = run_id
    out[KEY_ENVVARS_HARVESTED_COUNT] = int(envvars_harvested)
    out[KEY_DISCOVERY_PENDING] = False  # discovery just ran — clear the flag
    return out
