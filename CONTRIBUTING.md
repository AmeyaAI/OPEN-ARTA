# Contributing to ARTA

Thanks for helping build the independent verification layer for AI-era software.

## Ground rules

- **The local loop stays free.** PRs that meter, gate, or cripple single-team
  functionality (generation, execution, validation, healing, reporting,
  single-project traceability) are rejected by policy. The commercial boundary
  is documented in [ee/README.md](ee/README.md).
- **Truthful reporting is the product.** Changes that make a report look
  better without making it more *true* (hiding failures, silent fallbacks,
  optimistic classification) are rejected.
- **No customer or SUT-specific defaults in core.** SUT-specific behavior
  belongs in project configuration (`env_block`, `discovery_settings`), never
  hard-coded. Reference templates must use neutral example hosts.

## Contributor License Agreement

We use a lightweight CLA (via cla-assistant, prompted automatically on your
first PR). It confirms you have the right to contribute your changes and
grants DPOD Labs the rights needed to keep licensing flexible. Your copyright
stays yours.

## Development

```bash
pip install -r requirements.txt
python -m pytest tests/unit -q          # unit suite — must stay green
cd frontend && npm ci && npx tsc --noEmit   # frontend type-check
```

Run the stack locally with `docker compose up -d` (see README quickstart).

## Good first contributions

- New **requirement-source** connectors (the ingestion interface)
- New **grounding validators** (anti-hallucination checks) with unit tests
- Runtime support improvements (Playwright/Newman/k6/ZAP/Axe/Pytest dispatch)
- Docs: SUT onboarding guides for common stacks

## PR checklist

- [ ] Unit tests for new behavior; suite green
- [ ] No deny-listed content: secrets, customer names, SUT-specific defaults
- [ ] Telemetry: any new event/prop must be added to `src/telemetry/schema.py`
      **and** `docs/TELEMETRY.md` in the same PR (closed enums/buckets only)
