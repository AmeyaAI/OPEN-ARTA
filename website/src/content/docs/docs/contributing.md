---
title: Contributing
description: How to contribute to ARTA — ground rules, the development loop, the CLA, and good first contributions.
---

ARTA is developed in the open, and contributions are welcome. The canonical
guide is
[CONTRIBUTING.md](https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA/blob/main/CONTRIBUTING.md)
in the repository; this page is the short version.

## Ground rules

These are review policy, not aspirations:

- **The local loop stays free.** PRs that meter, gate, or cripple single-team
  functionality are rejected. The commercial boundary is documented in
  [ee/README.md](https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA/blob/main/ee/README.md).
- **Truthful reporting is the product.** Changes that make a report look
  better without making it more *true* — hiding failures, silent fallbacks,
  optimistic classification — are rejected.
- **No customer or SUT-specific defaults in core.** SUT-specific behavior
  belongs in project configuration; reference templates use neutral example
  hosts.

## Development loop

```bash
pip install -r requirements.txt
python -m pytest tests/unit -q              # unit suite — must stay green
cd frontend && npm ci && npx tsc --noEmit   # frontend type-check
```

Run the full stack locally with `docker compose up -d` (see
[Getting started](/OPEN-ARTA/docs/getting-started/)).

## CLA

A lightweight [CLA](https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA/blob/main/docs/CLA.md)
is enforced by a workflow: on your first PR you sign with a single PR
comment. Your copyright stays yours.

## Good first contributions

- New **requirement-source connectors** (the ingestion interface)
- New **grounding validators** (anti-hallucination checks) with unit tests
- Runtime support improvements (Playwright / Newman / k6 / ZAP / Axe / Pytest
  dispatch)
- Docs: SUT onboarding guides for common stacks

## PR checklist

- Unit tests for new behavior; suite green
- No deny-listed content: secrets, customer names, SUT-specific defaults
- Any new telemetry event/prop added to `src/telemetry/schema.py` **and**
  `docs/TELEMETRY.md` in the same PR (closed enums/buckets only)

Then open a PR — CI runs the unit suite, a frontend type-check, a login
smoke test, and content guards on every pull request.
