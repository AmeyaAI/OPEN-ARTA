-- Phase 1.2 — Structured measurable threshold on each acceptance criterion.
--
-- Today the threshold is buried in `then_` prose ("response within 500ms"). The
-- DatasetRecipe agent and Layer 4 script generators need it as a structured
-- triple so they can cite it verbatim instead of having the LLM re-infer it.
--
-- Shape: `{"value": <num|str>, "unit": <str|null>, "comparator": "<="|">="|"=="|"~"|null}`
-- e.g. {"value": 500, "unit": "ms", "comparator": "<="}
--      {"value": 12.5, "unit": "%", "comparator": "~"}
--      {"value": "sales", "unit": null, "comparator": "=="}
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, additive only.
ALTER TABLE acceptance_criteria
    ADD COLUMN IF NOT EXISTS measurable JSONB;

-- Partial index — most ACs won't have a measurable triple (qualitative); only
-- index the rows where it's set so downstream agents can quickly find them.
CREATE INDEX IF NOT EXISTS idx_ac_measurable_present
    ON acceptance_criteria((measurable IS NOT NULL))
    WHERE measurable IS NOT NULL;
