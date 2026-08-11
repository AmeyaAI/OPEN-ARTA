"""Phase I1 + I2 — HAR redaction pipeline + size caps.

Why this is a blocker before B persists anything: HAR captures hold
authorization headers, session cookies, JWTs, and PII in request/response
bodies. Persisting raw HAR to disk or to Neo4j would create a regulated-data
surface ARTA does not have today. Every artifact downstream of `_ingest_har`
must come from a redacted HAR; the unredacted form is held in-memory only
for the duration of one parse and then discarded.

Design notes:

- Redaction is **shape-preserving**. We replace `Bearer eyJ…` with
  `<<REDACTED_TOKEN>>` rather than removing the header entirely so chain
  extraction (Phase C) can still see "this request had an Authorization
  header" without leaking the value.

- Redaction is **idempotent**. Running the redactor twice is a no-op — we
  never substitute redacted-output back as a candidate match.

- Operator-extensible. Per-project `redaction_extra_patterns` from
  `discovery_settings` (I7) is folded into the pattern list so SUT-specific
  secrets (e.g., a custom `X-Acme-Tenant-Token`) can be scrubbed without a
  code change.

- Audit metric. `redaction_hits` counter is exposed for Phase I6's
  Prometheus surface — operators can see WHAT is being scrubbed even though
  the scrubbed values themselves never leave the redactor.

Phase I2 — robustness caps:

- File-level size cap (default 50 MB) enforced by `load_har_with_caps`.
  Larger files raise `HARTooLarge` so the orchestrator can switch the next
  capture to `mode=minimal, content=omit` and retry.

- Per-record body cap (default 64 KB). Bodies above the cap are stored as
  `{"truncated": true, "bytes": n, "shape_prefix": ...}` — the shape is
  still inferrable from the surviving prefix, but the redactor never sees
  the full payload (which both saves memory AND limits the scope of any
  redaction-pattern miss).
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

log = logging.getLogger("arta.har_redactor")


# ── I2 caps ─────────────────────────────────────────────────────────────────

# R150.A.2 — bumped DEFAULT_MAX_FILE_BYTES 50MB → 150MB to absorb HAR
# size growth from the R150.A discovery probe `mode: 'full'` change.
# bodies embedded expect 60-90MB. R155.A bumped further 150MB → 200MB
# after R154.A's click loop (5 clicks/route × ~15-25 routes) drove
# Per-body cap (DEFAULT_MAX_BODY_BYTES = 64KB) preserves shape-
# inferability while bounding pathological large responses. Operators
# can override via `project.discovery_settings.har_max_size_bytes` per
# discovery_settings.py:89 (which now defaults to the SAME 200MB value
# — single source of truth between the two constants).
DEFAULT_MAX_FILE_BYTES = 314_572_800  # 300 MB (R155.A.1; was 200 MB R155.A; was 150 MB R150.A.2; was 50 MB pre-R150.A.2)
DEFAULT_MAX_BODY_BYTES = 65_536       # 64 KB
_TRUNCATED_MARKER = "<<TRUNCATED>>"


class HARTooLarge(RuntimeError):
    """Raised by `load_har_with_caps` when the file exceeds the size cap.

    Caller should handle by switching the next Playwright HAR capture to
    `mode=minimal, content=omit` and retrying once. Phase I8 owns the retry
    decision.
    """

    def __init__(self, path: Path, size_bytes: int, cap_bytes: int):
        super().__init__(
            f"HAR file {path} is {size_bytes} bytes — exceeds cap {cap_bytes}. "
            f"Reduce capture mode (omit content) and retry, or raise the cap "
            f"in project.discovery_settings.har_max_size_bytes."
        )
        self.path = path
        self.size_bytes = size_bytes
        self.cap_bytes = cap_bytes


def _truncate_body_if_oversized(body: str | None, max_bytes: int) -> tuple[str | None, bool, int]:
    """Phase I2 per-record body cap.

    Returns (text_or_marker, was_truncated, original_bytes). Original bytes is
    needed by the chain-extractor's shape inference even when the body is
    truncated — the prefix is still informative.
    """
    if body is None or not isinstance(body, str):
        return body, False, 0
    n = len(body.encode("utf-8", errors="replace"))
    if n <= max_bytes:
        return body, False, n
    # Keep a prefix that's safely under the cap but big enough to infer shape
    # for typical JSON envelopes (status, top-level keys, first array item).
    keep = max(0, min(max_bytes // 2, 4096))
    prefix = body[:keep]
    return f"{prefix}\n{_TRUNCATED_MARKER}", True, n


def load_har_with_caps(
    path: Path | str,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> dict[str, Any]:
    """Read a HAR from disk, enforcing the I2 size caps.

    File-size check happens BEFORE we read into memory, so a 1GB HAR cannot
    OOM the worker. Per-record body truncation happens after parse, so the
    caller still gets the redactable structure.

    The returned dict is NOT YET REDACTED — caller must pass through
    `redact_har()`. Splitting the two passes keeps each function single-purpose
    and lets tests exercise them independently.
    """
    p = Path(path)
    size = p.stat().st_size
    if size > max_file_bytes:
        raise HARTooLarge(p, size, max_file_bytes)

    raw = p.read_text(encoding="utf-8", errors="replace")
    try:
        har = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("har_redactor.load_failed_json path=%s error=%s", p, exc)
        raise

    # Per-record body cap
    if not isinstance(har, dict):
        return har
    out = dict(har)
    out_log = dict(out.get("log") or {})
    entries = []
    truncations = 0
    for entry in (out_log.get("entries") or []):
        if not isinstance(entry, dict):
            entries.append(entry)
            continue
        new_entry = dict(entry)

        request = dict(entry.get("request") or {})
        post_data = request.get("postData")
        if isinstance(post_data, dict) and "text" in post_data:
            new_text, was_trunc, orig_bytes = _truncate_body_if_oversized(
                post_data.get("text"), max_body_bytes,
            )
            if was_trunc:
                truncations += 1
                pd = dict(post_data)
                pd["text"] = new_text
                pd["_arta_truncated"] = True
                pd["_arta_original_bytes"] = orig_bytes
                request["postData"] = pd
        new_entry["request"] = request

        response = dict(entry.get("response") or {})
        content = response.get("content")
        if isinstance(content, dict) and "text" in content:
            new_text, was_trunc, orig_bytes = _truncate_body_if_oversized(
                content.get("text"), max_body_bytes,
            )
            if was_trunc:
                truncations += 1
                c = dict(content)
                c["text"] = new_text
                c["_arta_truncated"] = True
                c["_arta_original_bytes"] = orig_bytes
                response["content"] = c
        new_entry["response"] = response

        entries.append(new_entry)
    out_log["entries"] = entries
    out["log"] = out_log
    if truncations:
        log.info(
            "har_redactor.body_truncations path=%s count=%d cap=%d",
            p, truncations, max_body_bytes,
        )
    return out


# ── Regex patterns ────────────────────────────────────────────────────────────
# Order matters slightly: more-specific patterns run first so a generic catch
# doesn't shadow an audit-worthy specific match in the metrics counter.

_BEARER_PAT = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]{8,}")
_JWT_PAT = re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{4,}(?:\.[A-Za-z0-9_\-]+)?\b")
_BASIC_AUTH_PAT = re.compile(r"(?i)basic\s+[A-Za-z0-9+/=]{12,}")
_API_KEY_PAT = re.compile(r"\b(?:sk|pk|api)[_-][A-Za-z0-9]{16,}\b")
_AWS_KEY_PAT = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GENERIC_TOKEN_PAT = re.compile(r"\b[A-Fa-f0-9]{32,}\b")  # long hex blobs (csrf, session ids)

# PII shape-preserving placeholders
_EMAIL_PAT = re.compile(r"\b([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
_SSN_PAT = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CC_PAT = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
_PHONE_PAT = re.compile(r"\b\+?\d{1,3}[ \-]?\(?\d{3}\)?[ \-]?\d{3}[ \-]?\d{4}\b")

# Header names whose VALUE is treated as a secret regardless of shape.
# Lowercased for case-insensitive match.
_SENSITIVE_HEADER_NAMES = {
    "authorization", "cookie", "set-cookie", "proxy-authorization",
    "x-api-key", "x-auth-token", "x-csrf-token", "x-session-token",
    "x-access-token", "x-refresh-token",
}
# Pattern for "any header whose name suggests it carries a secret" — used
# in addition to the explicit list above (catches `X-Acme-Auth`, `X-Foo-Token`).
_SENSITIVE_HEADER_NAME_PAT = re.compile(r"(?i)(?:^|[_-])(?:token|secret|auth|key|password|cred)(?:[_-]|$)")


_TOKEN_REPLACEMENT = "<<REDACTED_TOKEN>>"
_EMAIL_REPLACEMENT = r"\1@<<REDACTED_DOMAIN>>"
_SSN_REPLACEMENT = "<<REDACTED_SSN>>"
_CC_REPLACEMENT = "<<REDACTED_CC>>"
_PHONE_REPLACEMENT = "<<REDACTED_PHONE>>"
_HEADER_VALUE_REPLACEMENT = "<<REDACTED_HEADER_VALUE>>"


# Redaction sentinels — never re-match these so passes are idempotent.
_SENTINELS = (
    "<<REDACTED_TOKEN>>", "<<REDACTED_DOMAIN>>", "<<REDACTED_SSN>>",
    "<<REDACTED_CC>>", "<<REDACTED_PHONE>>", "<<REDACTED_HEADER_VALUE>>",
)


def _is_sentinel(s: str) -> bool:
    return any(sent in s for sent in _SENTINELS)


def _redact_text(text: str, hits: Counter, extra_patterns: list[re.Pattern[str]] | None = None) -> str:
    """Apply value-level redaction to a free-form string.

    Stamps the `hits` counter so the caller can emit `arta_har_redaction_hits_total`.
    """
    if not text or not isinstance(text, str):
        return text

    def _sub_tracked(pat: re.Pattern[str], repl: str, key: str, s: str) -> str:
        # `n` count gets returned by re.subn — gives us metrics in one pass.
        new, n = pat.subn(repl, s)
        if n:
            hits[key] += n
        return new

    text = _sub_tracked(_JWT_PAT, _TOKEN_REPLACEMENT, "jwt", text)
    text = _sub_tracked(_BEARER_PAT, _TOKEN_REPLACEMENT, "bearer", text)
    text = _sub_tracked(_BASIC_AUTH_PAT, _TOKEN_REPLACEMENT, "basic_auth", text)
    text = _sub_tracked(_API_KEY_PAT, _TOKEN_REPLACEMENT, "api_key", text)
    text = _sub_tracked(_AWS_KEY_PAT, _TOKEN_REPLACEMENT, "aws_key", text)
    text = _sub_tracked(_EMAIL_PAT, _EMAIL_REPLACEMENT, "email", text)
    text = _sub_tracked(_SSN_PAT, _SSN_REPLACEMENT, "ssn", text)
    text = _sub_tracked(_CC_PAT, _CC_REPLACEMENT, "credit_card", text)
    text = _sub_tracked(_PHONE_PAT, _PHONE_REPLACEMENT, "phone", text)
    # Generic-hex catches CSRF / session-id-shaped values. Run last so it
    # doesn't eat structured tokens above.
    text = _sub_tracked(_GENERIC_TOKEN_PAT, _TOKEN_REPLACEMENT, "hex_blob", text)

    if extra_patterns:
        for i, pat in enumerate(extra_patterns):
            text = _sub_tracked(pat, _TOKEN_REPLACEMENT, f"extra_{i}", text)

    return text


def _redact_headers(
    headers: list[dict[str, Any]],
    hits: Counter,
    extra_patterns: list[re.Pattern[str]] | None,
) -> list[dict[str, Any]]:
    """HAR header lists look like [{'name': 'Authorization', 'value': '...'}, ...].

    Sensitive-named headers are wholesale-replaced with a sentinel.
    Other header values get text-level scrubbing (a `User-Agent` header still
    runs through the regex pass so any leaked bearer tokens get caught).
    """
    out = []
    for h in headers or []:
        if not isinstance(h, dict):
            out.append(h)
            continue
        name = str(h.get("name", ""))
        lname = name.lower()
        is_sensitive = (
            lname in _SENSITIVE_HEADER_NAMES
            or bool(_SENSITIVE_HEADER_NAME_PAT.search(lname))
        )
        new_h = dict(h)
        val = str(h.get("value", "")) if h.get("value") is not None else ""
        if is_sensitive and val and not _is_sentinel(val):
            new_h["value"] = _HEADER_VALUE_REPLACEMENT
            hits[f"header:{lname}"] += 1
        elif val:
            new_h["value"] = _redact_text(val, hits, extra_patterns)
        out.append(new_h)
    return out


def _redact_cookies(
    cookies: list[dict[str, Any]],
    hits: Counter,
) -> list[dict[str, Any]]:
    """HAR cookie lists. Every cookie value is treated as a secret."""
    out = []
    for c in cookies or []:
        if not isinstance(c, dict):
            out.append(c)
            continue
        new_c = dict(c)
        if c.get("value") and not _is_sentinel(str(c.get("value"))):
            new_c["value"] = _HEADER_VALUE_REPLACEMENT
            hits["cookie"] += 1
        out.append(new_c)
    return out


def _redact_query_string(
    qs: list[dict[str, Any]],
    hits: Counter,
    extra_patterns: list[re.Pattern[str]] | None,
) -> list[dict[str, Any]]:
    """HAR queryString lists: [{'name': 'token', 'value': '...'}, ...]."""
    out = []
    for q in qs or []:
        if not isinstance(q, dict):
            out.append(q)
            continue
        new_q = dict(q)
        name = str(q.get("name", "")).lower()
        val = str(q.get("value", "")) if q.get("value") is not None else ""
        if (name in {"token", "secret", "key", "auth", "password", "code", "access_token"}
                or _SENSITIVE_HEADER_NAME_PAT.search(name)) and val and not _is_sentinel(val):
            new_q["value"] = _HEADER_VALUE_REPLACEMENT
            hits[f"query:{name}"] += 1
        elif val:
            new_q["value"] = _redact_text(val, hits, extra_patterns)
        out.append(new_q)
    return out


def _redact_post_data(
    post_data: dict[str, Any] | None,
    hits: Counter,
    extra_patterns: list[re.Pattern[str]] | None,
) -> dict[str, Any] | None:
    if not post_data or not isinstance(post_data, dict):
        return post_data
    out = dict(post_data)
    if "text" in out and isinstance(out["text"], str):
        out["text"] = _redact_text(out["text"], hits, extra_patterns)
    if "params" in out and isinstance(out["params"], list):
        out["params"] = _redact_query_string(out["params"], hits, extra_patterns)
    return out


def _redact_content(content: dict[str, Any] | None, hits: Counter,
                    extra_patterns: list[re.Pattern[str]] | None) -> dict[str, Any] | None:
    if not content or not isinstance(content, dict):
        return content
    out = dict(content)
    if "text" in out and isinstance(out["text"], str):
        out["text"] = _redact_text(out["text"], hits, extra_patterns)
    return out


def _redact_url(url: str, hits: Counter, extra_patterns: list[re.Pattern[str]] | None) -> str:
    """URLs may carry tokens in query string. Strip those + any embedded creds."""
    if not url:
        return url
    # Embedded basic-auth credentials: https://user:pass@host/...
    new, n = re.subn(r"(://)([^/@\s]+)@", r"\1<<REDACTED_USERPASS>>@", url)
    if n:
        hits["url_embedded_creds"] += n
    return _redact_text(new, hits, extra_patterns)


def redact_har(
    har: dict[str, Any],
    *,
    extra_patterns: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Walk a parsed HAR dict and return a redacted copy + per-pattern hit counts.

    Args:
        har: parsed HAR (the dict you'd get from `json.loads(har_path.read_text())`).
        extra_patterns: per-project regexes (from `discovery_settings.redaction_extra_patterns`).

    Returns:
        (redacted_har, hits_by_category) — hits maps short pattern names to
        match counts. The category names are stable so I6 dashboards can
        track them as labels.

    Idempotent: redact_har(redact_har(har)[0])[0] == redact_har(har)[0]
    (modulo dict-copy depth) and the second call returns hits = {} for all
    patterns.
    """
    hits: Counter = Counter()
    compiled_extra: list[re.Pattern[str]] = []
    for p in extra_patterns or []:
        try:
            compiled_extra.append(re.compile(p))
        except re.error as exc:
            log.warning("har_redactor.extra_pattern_invalid pattern=%r error=%s — ignoring", p, exc)

    if not isinstance(har, dict):
        log.warning("har_redactor.input_not_dict type=%s — returning unchanged", type(har).__name__)
        return har, dict(hits)

    out = dict(har)
    out_log = dict(out.get("log") or {})
    entries = []
    for entry in (out_log.get("entries") or []):
        if not isinstance(entry, dict):
            entries.append(entry)
            continue
        new_entry = dict(entry)

        request = dict(entry.get("request") or {})
        if "url" in request:
            request["url"] = _redact_url(request["url"], hits, compiled_extra)
        request["headers"] = _redact_headers(request.get("headers") or [], hits, compiled_extra)
        request["cookies"] = _redact_cookies(request.get("cookies") or [], hits)
        request["queryString"] = _redact_query_string(request.get("queryString") or [], hits, compiled_extra)
        request["postData"] = _redact_post_data(request.get("postData"), hits, compiled_extra)
        new_entry["request"] = request

        response = dict(entry.get("response") or {})
        response["headers"] = _redact_headers(response.get("headers") or [], hits, compiled_extra)
        response["cookies"] = _redact_cookies(response.get("cookies") or [], hits)
        response["content"] = _redact_content(response.get("content"), hits, compiled_extra)
        new_entry["response"] = response

        entries.append(new_entry)
    out_log["entries"] = entries
    out["log"] = out_log

    log.info("har_redactor.complete entries=%d hits=%s", len(entries), dict(hits))
    return out, dict(hits)


__all__ = [
    "redact_har",
    "load_har_with_caps",
    "HARTooLarge",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_BODY_BYTES",
]
