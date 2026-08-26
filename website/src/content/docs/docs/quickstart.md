---
title: Your first test run
description: From an empty ARTA install to a truthful test report — create a project, import requirements, generate grounded tests, and run them.
---

This walkthrough assumes ARTA is [installed and running](/docs/getting-started/)
at http://localhost:38088. The flow below is the product's core loop and runs
entirely in the UI.

The **fastest first win is the API-test path from an OpenAPI spec** — it needs
no browser automation against your app and grounds generation in a contract
you already have.

## 1. Create a project

A project points ARTA at one system under test (SUT). Give it your SUT's
**OpenAPI spec URL or base URL**. From here ARTA can discover what actually
exists: endpoints, response shapes, and (for UI testing) the DOM.

## 2. Import requirements

Add the requirements you want verified — from Jira, an OpenAPI spec, or plain
documents. Each requirement gets **risk-scored** (probability × impact, 1–9)
so the highest-risk behavior is tested first, and decomposed into ATDD
Gherkin acceptance criteria.

## 3. Generate

Press **Generate**. For each requirement, ARTA produces test scripts for the
relevant runtimes — Playwright for UI flows, Newman collections for API
behavior — and runs every script through its **grounding validators**: any
selector, role, or endpoint that doesn't exist in what discovery found is
rejected, retried with hints, and honestly marked `BLOCKED` if it can't be
fixed. A blocked test is a visible outcome, not a silent gap.

## 4. Run

Press **Run**. ARTA dispatches the generated scripts across its execution
runtimes and collects results, retrying with self-healing hints where a
failure looks recoverable.

## 5. Read the report

The report is where ARTA differs from a raw test runner. Every failure is
classified:

| Classification | Meaning |
| --- | --- |
| `sut_regression` | Your system broke — this is the signal |
| `test_gen_bug` | ARTA generated a bad test — our bug, not yours |
| `grounding_blocked` | The test couldn't be grounded in discovered reality |

and every result is linked back through the traceability graph:
**requirement → acceptance criterion → test → execution → defect**. A red
report means something.

## Where the generated tests live

Generated artifacts are standard formats — Playwright specs, Postman
collections, k6 scripts — not a proprietary representation. They are yours to
inspect, edit, and version.

## Next

- [Core concepts](/docs/concepts/) — the ideas behind grounding and
  truthful reporting
- [Configuration](/docs/configuration/) — providers, Jira defect
  filing, telemetry
