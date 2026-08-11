# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
(Security tab → Report a vulnerability). Do not open public issues for security reports.
We aim to acknowledge within 72 hours.

## Scope notes for self-hosters

- Set `ARTA_API_KEY` — API auth intentionally no-ops in development when unset.
- The base `docker-compose.yml` is a development posture (published DB ports,
  ZAP API key disabled). Use `docker-compose.prod.yml` and close DB ports for
  any real deployment — see `docs/SELF_HOSTING_INFRASTRUCTURE.md`.
- `ARTA_DEMO_MODE=1` enables well-known demo credentials; never set it on a
  reachable host.
- SUT credentials pasted into ARTA are stored under `.arta/` and in Postgres —
  include both in your data-classification scope.

## Supported versions

Security fixes land on `main`. Pin releases for production use.
