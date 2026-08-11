# ARTA Enterprise Edition (`ee/`)

This directory is reserved for ARTA's commercial edition. It is **not** part of
the Apache-2.0 open core: anything under `ee/` is source-available under a
commercial license only (none of it is present in this repository today).

## Where the line is

**Open core (this repository, Apache-2.0, free forever):** the entire
single-team verification loop — requirement ingestion, risk scoring, ATDD
Gherkin design, grounded test generation for all six runtimes (Playwright,
Newman, k6, ZAP, Axe, Pytest), execution and truthful mission reports,
grounding validators, self-healing, single-project traceability, local-LLM
support, per-project RBAC and invites, CI templates. If a change would meter
or cripple any of that locally, it will not be merged.

**Commercial (`ee/` + ARTA Cloud):** org-level capabilities —

- Organization/team entity and hosted control plane
- Cross-run and cross-project analytics; fleet management
- SAML / SCIM / enterprise IdP integration
- Real, tamper-evident audit trails and compliance evidence exports
- Jira-at-scale and test-management-suite sync
- ARTA Intelligence (fine-tuned generation models + training pipeline)
- Managed LLM inference (usage credits)

The seam in code is [`src/api/entitlements.py`](../src/api/entitlements.py):
the core ships an allow-everything no-op; commercial editions install their
own implementation. Contributions that move local-loop features behind the
seam are rejected by policy.

Questions: open a GitHub Discussion.
