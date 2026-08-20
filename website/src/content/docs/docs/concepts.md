---
title: Core concepts
description: Grounded generation, risk scoring, ATDD design, failure classification, traceability, and the non-mutation guarantee — the ideas ARTA is built on.
---

Six ideas explain most of ARTA's behavior. Everything else is machinery.

## Grounded generation

Every AI testing tool claims it doesn't hallucinate. ARTA is the one that
shows you the code that enforces it: generation is constrained by what
**discovery actually found** — the DOM catalog, captured endpoints, the
OpenAPI spec, and source code — and every generated test then passes through
[grounding validators](https://github.com/AmeyaAI/OPEN-ARTA/blob/main/src/agents/grounding_validator.py)
that reject selectors, roles, and endpoints that don't exist in your system.

Rejected tests are retried with corrective hints. If they still can't be
grounded, they are marked `BLOCKED` and reported as such — never silently
shipped, never quietly dropped.

## Risk-based prioritization

Requirements are scored **probability × impact** on a 1–3 scale each, giving
a 1–9 risk score. High-risk behavior gets designed and executed first, so a
partial run still covers what matters most.

## ATDD design

Before any script exists, each requirement is decomposed into Gherkin
acceptance criteria — a human-readable contract of what "verified" means.
Scripts are generated *from* those criteria, which is what keeps tests
answerable to requirements instead of to the whims of a code generator.

## Six runtimes, one report

ARTA generates **and executes** across six runtimes: Playwright (UI), Newman
(API), k6 (performance), OWASP ZAP (security), Axe (accessibility), and
Pytest (analytics). Cypress and Selenium scripts can be *generated*;
execution for them is not implemented yet. All results land in one report
behind one quality gate.

## Truthful failure classification

A raw test runner tells you *that* something failed. ARTA tells you *whose
fault it is*:

- **`sut_regression`** — your system broke. This is the signal.
- **`test_gen_bug`** — ARTA generated a bad test. Our bug, counted against us.
- **`grounding_blocked`** — the test couldn't be grounded and was blocked.

Changes that make a report look better without making it more *true* are
rejected by project policy. The same honesty extends to telemetry and to the
quality gate.

## Traceability

Requirement → acceptance criterion → test → execution → defect is stored as a
graph (Neo4j) you can query. "Which requirements are unverified?" and "what
does this failure trace back to?" are queries, not meetings.

## The non-mutation guarantee

Discovery and testing **never mutate your system by default**, enforced at
three independent layers: the network layer (non-read requests blocked during
discovery), generation time (destructive patterns rejected), and dispatch
time (destructive specs refused). Destructive testing requires explicit
opt-in markers *and* environment variables — both, deliberately.
