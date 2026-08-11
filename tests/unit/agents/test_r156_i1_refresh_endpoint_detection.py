"""R156.I.1 — Refresh-endpoint detection tests.

The helper `AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints`
scans FastAPI/Express handler files for auth-refresh endpoints + extracts
the canonical body + response field names used by the SUT. R156.I.2
feeds the result into TOKEN_CHAINS.refresh_flow; R156.J.1/J.2 inject
auto-refresh logic into generated Newman + PW scripts.

Mission contract (Pillar 2 — execute flawlessly): Iter 11 evidence shows
~110-140 min smoke wallclock vs ~60 min agent_token TTL → ~323 Newman
401 cluster fraction attributable to token expiry. R156.I.1 + R156.J
close this class by emitting test scripts that auto-refresh during
execution.
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent


# ── Detection happy path ───────────────────────────────────────────────


def test_r156_i1_detects_fastapi_refresh_endpoint():
    """FastAPI `@router.post('/api/auth/refresh')` → detected with method=POST,
    canonical path, and field names sourced from the Pydantic body class."""
    file_content = '''
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int


@router.post("/api/auth/refresh")
async def refresh(payload: RefreshRequest) -> RefreshResponse:
    return RefreshResponse(
        access_token="new",
        refresh_token="rotated",
        expires_in=3600,
    )
'''
    results = AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints(file_content)
    assert len(results) == 1
    r = results[0]
    assert r["endpoint_path"] == "/api/auth/refresh"
    assert r["method"] == "POST"
    assert r["request_body_field"] == "refresh_token"
    assert r["response_access_token_field"] == "access_token"
    assert r["response_refresh_token_field"] == "refresh_token"
    assert r["response_expires_in_field"] == "expires_in"
    assert r["source_evidence_line"] > 0


def test_r156_i1_detects_camelcase_field_names():
    """When the SUT uses camelCase (`refreshToken` / `accessToken`),
    detection captures the actual field names from the source."""
    file_content = '''
class RefreshRequestBody {
  refreshToken: string;
}

router.post("/token/refresh", async (req, res) => {
  res.json({
    accessToken: "new",
    refreshToken: "rotated",
    expiresIn: 3600,
  });
});
'''
    results = AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints(file_content)
    assert len(results) == 1
    r = results[0]
    assert r["endpoint_path"] == "/token/refresh"
    # The interface defines refreshToken — but Express handler signature
    # is arrow-function (no `def`), so body-schema heuristic returns None
    # and we fall back to canonical default. Response fields are detected
    # from the handler body's res.json({...}).
    assert r["response_access_token_field"] == "accessToken"
    assert r["response_refresh_token_field"] == "refreshToken"
    assert r["response_expires_in_field"] == "expiresIn"


def test_r156_i1_detects_multiple_refresh_endpoints_in_one_file():
    """A monorepo handler file with both `/auth/refresh` and
    `/token/refresh` endpoints — detect both, dedup by path."""
    file_content = '''
@router.post("/api/auth/refresh")
async def refresh_auth(payload: RefreshBody):
    return {"access_token": "x"}


@router.post("/api/v2/token/refresh")
async def refresh_token_v2(payload: RefreshV2Body):
    return {"access_token": "y", "refresh_token": "z"}
'''
    results = AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints(file_content)
    paths = {r["endpoint_path"] for r in results}
    assert "/api/auth/refresh" in paths
    assert "/api/v2/token/refresh" in paths


def test_r156_i1_no_rotation_when_only_access_token_returned():
    """SUT that returns ONLY access_token (no rotation) → response_refresh_token_field=None.
    Detector must not invent a rotation field that isn't in the source."""
    file_content = '''
@router.post("/api/auth/refresh")
async def refresh(payload: RefreshBody):
    new_access = mint_new_token()
    return {"access_token": new_access, "expires_in": 3600}
'''
    results = AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints(file_content)
    assert len(results) == 1
    assert results[0]["response_access_token_field"] == "access_token"
    assert results[0]["response_refresh_token_field"] is None
    assert results[0]["response_expires_in_field"] == "expires_in"


def test_r156_i1_no_expires_in_field():
    """SUT that doesn't surface `expires_in` (opaque tokens) →
    response_expires_in_field=None. Helper at runtime falls back to JWT
    `exp` claim decode."""
    file_content = '''
@router.post("/api/auth/refresh")
async def refresh(payload: RefreshBody):
    return {"access_token": "new", "refresh_token": "rotated"}
'''
    results = AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints(file_content)
    assert len(results) == 1
    assert results[0]["response_expires_in_field"] is None


# ── Negative cases ────────────────────────────────────────────────────


def test_r156_i1_returns_empty_when_no_refresh_endpoint():
    """File with no refresh-endpoint handler → []. Token-chain
    R156.I.2 then sets refresh_flow=None for this project."""
    file_content = '''
@router.get("/api/v1/users")
async def list_users():
    return []


@router.post("/api/v1/datasets")
async def create_dataset(payload: DatasetCreate):
    return {"id": "new"}
'''
    results = AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints(file_content)
    assert results == []


def test_r156_i1_returns_empty_on_empty_input():
    """Empty / whitespace-only input → []. Graceful, no exception."""
    assert AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints("") == []
    assert AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints("   \n\n  ") == []


def test_r156_i1_does_not_match_refresh_in_other_contexts():
    """A handler with `/refresh-cache` in path → MUST match (still
    contains `/refresh`). But `/api/refresh-status` should also match
    per current liberal regex. This test documents the false-positive
    risk — operator can override per-project if needed."""
    file_content = '''
@router.post("/api/refresh-status")
async def check_refresh_status():
    return {"ok": True}
'''
    results = AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints(file_content)
    # Helper IS conservative — only matches when the literal token
    # `auth/refresh`, `token/refresh`, or `/refresh` followed by
    # delimiter ([/?"']|$) appears. `/refresh-status` doesn't end
    # in a delimiter immediately after `refresh`, so should NOT match.
    assert results == []


# ── Source-derived field name vs canonical fallback ───────────────────


def test_r156_i1_falls_back_to_canonical_field_name():
    """When the Pydantic class is in a different file (not detectable
    via R156.A.1 helper), default to `refresh_token`. Future enhancement
    may follow imports cross-file."""
    file_content = '''
from .schemas import RefreshBody   # defined elsewhere

@router.post("/api/auth/refresh")
async def refresh(payload: RefreshBody):
    return {"access_token": "new"}
'''
    results = AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints(file_content)
    assert len(results) == 1
    # Pydantic class not in file → fall back to canonical "refresh_token"
    assert results[0]["request_body_field"] == "refresh_token"


def test_r156_i1_prefers_source_field_name_over_default():
    """Source uses `refreshKey` (non-canonical) → detection captures it
    over the canonical fallback default. Validates the Pydantic-snippet
    inspection step actually runs."""
    file_content = '''
from pydantic import BaseModel


class RefreshRequest(BaseModel):
    refreshKey: str = "..."


@router.post("/api/auth/refresh")
async def refresh(payload: RefreshRequest):
    return {"access_token": "new"}
'''
    results = AutomationEngineerAgent._r156_i_1_detect_refresh_endpoints(file_content)
    assert len(results) == 1
    assert results[0]["request_body_field"] == "refreshKey"
