"""Phase J7 — tests for the HAR redactor (Phase I1 + I2).

Acceptance: every secret pattern lands as a sentinel; redaction is
idempotent; size caps fire correctly; body truncation preserves a
shape-inferable prefix.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.agents.har_redactor import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_FILE_BYTES,
    HARTooLarge,
    load_har_with_caps,
    redact_har,
)


def _make_har(headers=None, cookies=None, post_text=None, response_text=None,
              set_cookies=None, url="https://api.example.com/v1/datasets"):
    return {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": url,
                        "headers": headers or [],
                        "cookies": cookies or [],
                        "queryString": [],
                        "postData": ({"mimeType": "application/json", "text": post_text}
                                     if post_text else None),
                    },
                    "response": {
                        "headers": [{"name": "Set-Cookie", "value": v} for v in (set_cookies or [])],
                        "cookies": [{"name": "x", "value": v} for v in (set_cookies or [])],
                        "content": ({"mimeType": "application/json", "text": response_text}
                                    if response_text else {}),
                    },
                }
            ]
        }
    }


def test_authorization_header_redacted():
    har = _make_har(headers=[{"name": "Authorization", "value": "Bearer eyJabc.def.ghi"}])
    redacted, hits = redact_har(har)
    auth = redacted["log"]["entries"][0]["request"]["headers"][0]
    assert auth["value"] == "<<REDACTED_HEADER_VALUE>>"
    assert hits.get("header:authorization", 0) >= 1


def test_jwt_in_response_body_redacted():
    har = _make_har(response_text='{"token":"eyJabcdef.eyJxyz12.signature"}')
    redacted, hits = redact_har(har)
    text = redacted["log"]["entries"][0]["response"]["content"]["text"]
    assert "eyJabcdef" not in text
    assert "<<REDACTED_TOKEN>>" in text


def test_email_pii_redacted_in_post_body():
    har = _make_har(post_text='{"email":"alice@example.com"}')
    redacted, hits = redact_har(har)
    text = redacted["log"]["entries"][0]["request"]["postData"]["text"]
    assert "alice" in text   # local part preserved
    assert "@example.com" not in text
    assert "<<REDACTED_DOMAIN>>" in text
    assert hits.get("email", 0) >= 1


def test_ssn_redacted():
    har = _make_har(post_text='{"ssn":"123-45-6789"}')
    redacted, hits = redact_har(har)
    text = redacted["log"]["entries"][0]["request"]["postData"]["text"]
    assert "123-45-6789" not in text
    assert "<<REDACTED_SSN>>" in text


def test_api_key_redacted():
    har = _make_har(headers=[{"name": "X-API-Key", "value": "sk_live_abcdef1234567890"}])
    redacted, _ = redact_har(har)
    val = redacted["log"]["entries"][0]["request"]["headers"][0]["value"]
    assert val == "<<REDACTED_HEADER_VALUE>>"


def test_set_cookie_redacted():
    har = _make_har(set_cookies=["session_aaa_bbb_ccc_ddd_eee"])
    redacted, _ = redact_har(har)
    set_cookie_val = redacted["log"]["entries"][0]["response"]["cookies"][0]["value"]
    assert set_cookie_val == "<<REDACTED_HEADER_VALUE>>"


def test_idempotent():
    har = _make_har(
        headers=[{"name": "Authorization", "value": "Bearer eyJabc.eyJdef.sig"}],
        post_text='{"email":"u@example.com","ssn":"111-22-3333"}',
    )
    once, _ = redact_har(har)
    twice, hits2 = redact_har(once)
    # No new hits on the second pass.
    assert sum(hits2.values()) == 0
    # Result is structurally identical.
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)


def test_extra_patterns():
    har = _make_har(headers=[{"name": "X-Custom-X", "value": "ACMEAUTH_zzz_yyy"}])
    redacted, hits = redact_har(har, extra_patterns=[r"ACMEAUTH_\w+"])
    val = redacted["log"]["entries"][0]["request"]["headers"][0]["value"]
    assert "ACMEAUTH_" not in val


def test_har_too_large_raises():
    har = _make_har(post_text="x" * 10)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".har", delete=False) as f:
        json.dump(har, f)
        path = Path(f.name)
    try:
        with pytest.raises(HARTooLarge):
            load_har_with_caps(path, max_file_bytes=10)
    finally:
        path.unlink(missing_ok=True)


def test_body_truncation_preserves_prefix():
    big_body = "x" * 200_000
    har = _make_har(post_text=big_body)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".har", delete=False) as f:
        json.dump(har, f)
        path = Path(f.name)
    try:
        loaded = load_har_with_caps(path, max_body_bytes=1024)
        post = loaded["log"]["entries"][0]["request"]["postData"]
        assert post.get("_arta_truncated") is True
        assert post["_arta_original_bytes"] == 200_000
        assert "<<TRUNCATED>>" in post["text"]
        assert len(post["text"].encode("utf-8")) < 5000
    finally:
        path.unlink(missing_ok=True)


def test_none_input_passthrough():
    out, hits = redact_har(None)   # type: ignore[arg-type]
    assert out is None
    assert hits == {}


def test_empty_har_normalised():
    out, hits = redact_har({})
    assert out["log"]["entries"] == []
    assert hits == {}
