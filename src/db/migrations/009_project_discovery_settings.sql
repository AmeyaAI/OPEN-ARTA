-- Phase B5 + I7 — UI Discovery Run scaffolding on the projects table.
--
-- B5: `is_api_only` — when TRUE, Stage 2.5 (UI Discovery) is unconditionally
--      skipped and the existing OpenAPI + operator env-var path is used. Set
--      automatically during onboarding when `_live_probe` returns no
--      text/html base URL.
--
-- I7: `discovery_settings` — JSONB blob holding per-project feature flags +
--      runtime tunables for the discovery loop. Default is the safe shape:
--       {
--         "stage_2_5_enabled": false,        # opt-in for existing projects
--         "discovery_pending": false,         # set by migration sweep when
--                                             #   prior run had unresolved env vars
--         "har_max_size_bytes": 52428800,    # 50 MB hard cap (I2)
--         "har_body_max_bytes": 65536,       # 64 KB per-record body cap (I2)
--         "redaction_extra_patterns": [],    # SUT-specific secret regexes (I1)
--         "replay_mode": "read_only",        # I5 / DR-5 default
--         "replay_cron": "0 3 * * *",        # daily 03:00 default
--         "last_discovery_at": null,
--         "last_discovery_run_id": null,
--         "envvars_harvested_count": 0
--       }
--
-- For new projects, the application layer will populate this with the same
-- defaults but with `stage_2_5_enabled = true` so they get discovery from
-- day one.

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS is_api_only BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS discovery_settings JSONB NOT NULL DEFAULT '{
      "stage_2_5_enabled": false,
      "discovery_pending": false,
      "har_max_size_bytes": 52428800,
      "har_body_max_bytes": 65536,
      "redaction_extra_patterns": [],
      "replay_mode": "read_only",
      "replay_cron": "0 3 * * *",
      "last_discovery_at": null,
      "last_discovery_run_id": null,
      "envvars_harvested_count": 0
    }'::jsonb;

-- Index to find projects with pending discovery during the migration sweep.
CREATE INDEX IF NOT EXISTS idx_projects_discovery_pending
    ON projects ((discovery_settings->>'discovery_pending'))
    WHERE (discovery_settings->>'discovery_pending') = 'true';
