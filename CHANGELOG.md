# Changelog

All notable changes to OPEN-ARTA are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).
The `version` in `pyproject.toml` is the single source of truth and is bumped in the same commit as each release tag.

## [Unreleased]

## [0.1.0] - 2026-08-14

Initial public release of the ARTA open core.

### Added
- BMAD TEA ATDD pipeline: requirement extraction, risk scoring (Probability × Impact), Gherkin acceptance-test design, automation strategy, quality gates
- Test-script generation and execution across Playwright (UI), Newman (API), k6 (perf), OWASP ZAP (security), Axe (a11y), and Pytest (analytics)
- Grounding validators (endpoint/selector hallucination detection), self-healing regen queue, defect intelligence with truthful failure classification
- FastAPI backend + Next.js dashboard; docker-compose stack (Postgres, Neo4j, Redis, ChromaDB, ZAP)
- CI: unit tests, frontend type-check, DB-backed login smoke test, customer-data guards

### Fixed
- Fresh-install bootstrap: removed seed admin from `schema.sql` and fixed `ensure_bootstrap_admin()` passing an invalid `role` kwarg — a clean install now yields a working admin login (guarded by the `login-smoke` CI job)
