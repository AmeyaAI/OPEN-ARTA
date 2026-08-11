"""F7-5: Tests for the centralised require_api_key dependency.

The Bearer-bypass at dependencies.py:115 (closed by F6-1) was the most
critical security finding in the audit. This test pins the fix so a future
refactor can't silently re-introduce it.
"""
from __future__ import annotations

import os

import pytest
from fastapi import HTTPException


pytestmark = pytest.mark.asyncio


async def test_dev_mode_no_api_key_allows_all():
    """When ARTA_API_KEY is empty, require_api_key returns without raising."""
    os.environ.pop("ARTA_API_KEY", None)
    from src.api.dependencies import require_api_key
    # Should not raise
    await require_api_key(x_api_key=None, authorization=None, api_key=None, token=None)


async def test_valid_api_key_passes():
    os.environ["ARTA_API_KEY"] = "valid-key-001"
    try:
        from src.api.dependencies import require_api_key
        await require_api_key(x_api_key="valid-key-001", authorization=None, api_key=None, token=None)
    finally:
        os.environ.pop("ARTA_API_KEY", None)


async def test_invalid_api_key_raises_401():
    os.environ["ARTA_API_KEY"] = "valid-key-002"
    try:
        from src.api.dependencies import require_api_key
        with pytest.raises(HTTPException) as exc:
            await require_api_key(x_api_key="wrong", authorization=None, api_key=None, token=None)
        assert exc.value.status_code == 401
    finally:
        os.environ.pop("ARTA_API_KEY", None)


async def test_bearer_x_does_not_bypass_auth():
    """F6-1 regression pin: `Authorization: Bearer x` MUST NOT pass auth.

    Pre-F6-1 code returned successfully on any header starting with "Bearer ",
    which was a complete auth bypass. If this test fails, the bypass is back.
    """
    os.environ["ARTA_API_KEY"] = "valid-key-003"
    try:
        from src.api.dependencies import require_api_key
        with pytest.raises(HTTPException) as exc:
            await require_api_key(x_api_key=None, authorization="Bearer x", api_key=None, token=None)
        assert exc.value.status_code == 401, \
            "F6-1 regression: Bearer-anything bypass is back"
    finally:
        os.environ.pop("ARTA_API_KEY", None)


async def test_bearer_with_correct_api_key_passes():
    """Bearer-as-API-key path: token == ARTA_API_KEY → success."""
    os.environ["ARTA_API_KEY"] = "valid-key-004"
    try:
        from src.api.dependencies import require_api_key
        await require_api_key(x_api_key=None, authorization="Bearer valid-key-004", api_key=None, token=None)
    finally:
        os.environ.pop("ARTA_API_KEY", None)


async def test_bearer_with_wrong_token_fails():
    os.environ["ARTA_API_KEY"] = "valid-key-005"
    try:
        from src.api.dependencies import require_api_key
        with pytest.raises(HTTPException):
            await require_api_key(x_api_key=None, authorization="Bearer wrong-token", api_key=None, token=None)
    finally:
        os.environ.pop("ARTA_API_KEY", None)
