-- Single-use, time-bounded invite tokens for the "Invite User" flow.
-- Admin issues an invite → row is created here referencing the new (inactive)
-- users row. When the invitee POSTs to /api/auth/accept-invite, the row is
-- marked accepted_at and the user becomes active.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, indexes likewise.
CREATE TABLE IF NOT EXISTS invite_tokens (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL UNIQUE,
    expires_at   TIMESTAMPTZ NOT NULL,
    invited_by   UUID REFERENCES users(id),
    project_id   UUID REFERENCES projects(id) ON DELETE SET NULL,
    project_role TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_invite_tokens_user ON invite_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_invite_tokens_expires ON invite_tokens(expires_at);
