"""
ARTA Platform — User & Auth Pydantic models
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, EmailStr, Field


ProjectRoleLiteral = Literal["admin", "test_architect", "qa_lead", "tester", "developer", "viewer"]


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str
    password: str          # plaintext — hashed before storage
    is_admin: bool = False


class UserUpdate(BaseModel):
    full_name: str | None = None
    avatar_url: str | None = None


class UserPublic(BaseModel):
    id: str
    email: str
    full_name: str
    avatar_url: str | None = None
    is_admin: bool
    is_active: bool
    created_at: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int        # seconds
    user: UserPublic


class RefreshRequest(BaseModel):
    refresh_token: str


class ProjectRoleAssignment(BaseModel):
    user_id: str
    role: ProjectRoleLiteral


class InviteRequest(BaseModel):
    email: EmailStr
    full_name: str
    is_admin: bool = False
    project_id: str | None = None
    project_role: ProjectRoleLiteral | None = None


class InviteResponse(BaseModel):
    invite_id: str
    user_id: str
    email: str
    expires_at: str
    invite_url: str
    email_sent: bool
    # Surfaced so the admin can copy/paste into their mail client when SMTP
    # is not configured. Same template the SMTP path would have sent.
    email_subject: str
    email_body: str


class AcceptInviteRequest(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=128)
