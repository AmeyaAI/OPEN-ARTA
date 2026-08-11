"""R74.4 — single source of truth for R43 env-var substitution patterns.

Before R74.4, R43's pattern table lived in `src/api/routers/execution.py`
inside `_resolve_blocked_var_defaults`, AND a duplicate (with a
"must stay in sync" comment) lived in `src/agents/grounding_validator.py`
as `is_r43_substitutable_name()`. Drift was inevitable — when R71.3
added `version` patterns to the router, the validator's predicate
was updated manually; the next pattern addition could miss one site.

This module is the canonical predicate. Both router (R43) and
grounding validator (R72.2) import from here.

The predicate is intentionally pure (string in → bool out) so it can
be cheaply called per-var during pre-dispatch substitution AND per-
LLM-output during grounding.
"""
from __future__ import annotations

# Generic config-shape vars that ARTA's pre-dispatch substitution
# (R43) treats as having a deterministic safe default. The LLM is
# allowed to reference these in generated specs without being flagged
# as a grounding violation; at dispatch time `_resolve_blocked_var_defaults`
# produces a synthetic value (UUID for `*_id`, `"default"` for `*_type`,
# `"v1"` for `*_version`, etc.) so the test RUNS instead of BLOCKING.
GENERIC_CONFIG_NAMES = frozenset({"context", "scope", "tenant", "namespace"})


def is_r43_substitutable_name(var_name: str) -> bool:
    """Return True iff R43 can produce a synthetic value for `var_name`.

    Patterns covered (suffix-based unless noted):
      *_id, *_uuid, *id          — UUID-shaped synthetic
      *_name, *name              — `arta-synthetic-<name>`
      *_count, *count            — `"1"`
      *_path, *path              — `/arta-synthetic/path`
      *_type, *type              — `"default"`
      version, *_version, *version (R71.3) — `"v1"`
      context | scope | tenant | namespace — `"default"`
      *cookie* (any position; R48.1) — auto-fills from operator cookie
        paste at update_auth_state time

    Patterns explicitly NOT substitutable (returned False):
      *_token, auth_token, *_bearer, etc. — auth-only; never synthesise

    Implementation note: order matters in `_resolve_blocked_var_defaults`
    (first matching branch wins for value); ordering doesn't matter
    here since we only return bool.
    """
    if not isinstance(var_name, str) or not var_name:
        return False
    low = var_name.lower()
    if low.endswith("_id") or low.endswith("_uuid") or low.endswith("id"):
        return True
    if low.endswith("_name") or low.endswith("name"):
        return True
    if low.endswith("_count") or low.endswith("count"):
        return True
    if low.endswith("_path") or low.endswith("path"):
        return True
    if low.endswith("_type") or low.endswith("type"):
        return True
    if low in GENERIC_CONFIG_NAMES:
        return True
    # R71.3 — version patterns
    if low == "version" or low.endswith("_version") or low.endswith("version"):
        return True
    # R48.1 — cookie-shaped aliases auto-fill from operator paste
    if "cookie" in low:
        return True
    return False


def resolve_r43_synthetic_value(var_name: str) -> str | None:
    """R74.4 — return R43's synthetic value for `var_name`, or None if
    no pattern matches OR if the var is auth-only (auth_token etc.).

    This complements `is_r43_substitutable_name`: callers can use the
    predicate as a gate AND fetch the value in one place. Pre-R74.4 the
    pattern matching lived inside `_resolve_blocked_var_defaults` as
    interleaved `elif` branches that both checked patterns and produced
    values — making it easy for the standalone predicate to drift.

    Cookie patterns (R48.1) return None here because cookie auto-fill
    happens at PASTE time (in `update_auth_state`), not at dispatch.
    The predicate still returns True so the grounding validator
    doesn't flag `{{cookie_value}}` as a hallucination.

    Args:
        var_name: the env var name (case-insensitive matching).

    Returns:
        The synthetic value (e.g. UUID for `*_id`, `"v1"` for
        `*_version`) when R43 has a synthetic; None when the var is
        substitutable in principle but not at dispatch time (auth
        tokens, cookies), or not substitutable at all.
    """
    if not isinstance(var_name, str) or not var_name:
        return None
    low = var_name.lower()
    if low.endswith("_id") or low.endswith("_uuid") or low.endswith("id"):
        # Deterministic UUID derived from var name so multiple items in
        # the same run get the same value (tests can reference
        # baseline_id consistently across SETUP / VERIFY items).
        import hashlib as _hashlib
        digest = _hashlib.md5(low.encode("utf-8")).hexdigest()
        return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    if low.endswith("_name") or low.endswith("name"):
        return f"arta-synthetic-{low}"
    if low.endswith("_count") or low.endswith("count"):
        return "1"
    if low.endswith("_path") or low.endswith("path"):
        return "/arta-synthetic/path"
    if low.endswith("_type") or low.endswith("type"):
        return "default"
    if low in GENERIC_CONFIG_NAMES:
        return "default"
    if low == "version" or low.endswith("_version") or low.endswith("version"):
        return "v1"
    # R48.1 cookie aliases — predicate returns True but value is filled
    # at paste time, not at dispatch. Return None so callers know to
    # skip rather than substitute synthetically.
    if "cookie" in low:
        return None
    # Auth-only vars (auth_token, *_token, *_bearer) — predicate
    # returns False, but be defensive here in case future patterns shift.
    return None


# R145.A — single source of truth for placeholder-value tokens that
# indicate an env var was NOT supplied by the operator. Pre-R145.A
# these literals were inlined at three sites (execution.py:2339,
# execution.py:4661, execution.py:1269) plus a near-duplicate at
# execution.py:2233 — drift was inevitable. This frozenset is the
# canonical set; callers import + use directly. Adding a new
# placeholder shape lands in one place.
#
# Includes:
#   - REPLACE_ME / REPLACE-ME / REPLACEME — operator-OpenAPI placeholder
#   - __ARTA_UNSET_<var> — ARTA internal "not yet substituted" marker
#   - "" empty string
#   - "***" / "REDACTED" — masked secrets that leaked
#   - "TODO" — placeholder operators sometimes leave in env config
PLACEHOLDER_VALUES = frozenset({
    "REPLACE_ME", "REPLACE-ME", "REPLACEME",
    "***", "REDACTED", "TODO", "",
})


def is_placeholder_value(value: str | None) -> bool:
    """R145.A — return True iff `value` is one of the canonical
    placeholder tokens (or starts with the ARTA-internal unset prefix).

    Use this predicate everywhere a caller previously inlined
    `value in {"REPLACE_ME", "***", ...}` — eliminates drift across
    the R-SkipReplaceMe / R43 / R74.4 surfaces.
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    if value in PLACEHOLDER_VALUES:
        return True
    if value.startswith("__ARTA_UNSET_"):
        return True
    return False


# R145.A — placeholder regex for URL/path content matching. Used by
# the captured-endpoints sanitizer at execution.py:_r145_a_sanitize_*
# AND by the on-disk Newman sweep admin endpoint at
# main.py:sweep_replaceme. Matches the placeholder values as a path
# segment (bounded by '/' OR by start/end-of-string).
import re as _re_r145

_PLACEHOLDER_SEGMENT_RE = _re_r145.compile(
    r"(?:^|/)(REPLACE_ME|REPLACE-ME|REPLACEME|__ARTA_UNSET_[^/?#]+)(?:/|$|\?|#)"
)


def path_has_placeholder(path: str | None) -> bool:
    """R145.A — return True iff `path` contains a placeholder token as
    a path segment. Catches both literal REPLACE_ME and ARTA-internal
    __ARTA_UNSET_<var> unset markers.

    Used by R145.A.1 preflight sanitization + R145.A.2 admin sweep.
    """
    if not path or not isinstance(path, str):
        return False
    return bool(_PLACEHOLDER_SEGMENT_RE.search(path))


def find_placeholder_segments(path: str | None) -> list[tuple[int, str]]:
    """R145.A — return ordered list of (segment_index, placeholder_text)
    tuples for every placeholder-containing NON-EMPTY segment in `path`.
    Index is zero-based over '/'-split segments — the leading empty
    segment (from paths starting with '/') is INCLUDED in the count so
    that callers operating on `path.split('/')` can index directly.

    The empty string IS in `PLACEHOLDER_VALUES` (correct for env-var
    unset semantics: `account_id=""` means unset). But an empty
    path SEGMENT is just URL structure, not an unset placeholder —
    this function skips them.

    Used by the sanitization helper to compute positional substitution
    guesses (`/collection/REPLACE_ME` → guess `collection_id`).
    """
    if not path or not isinstance(path, str):
        return []
    # Strip query/fragment + split
    base = path.split("?", 1)[0].split("#", 1)[0]
    segs = base.split("/")
    out: list[tuple[int, str]] = []
    for i, seg in enumerate(segs):
        if not seg:
            continue   # skip empty segments (path-structure, not placeholder)
        if seg in PLACEHOLDER_VALUES:
            out.append((i, seg))
        elif seg.startswith("__ARTA_UNSET_"):
            out.append((i, seg))
    return out
